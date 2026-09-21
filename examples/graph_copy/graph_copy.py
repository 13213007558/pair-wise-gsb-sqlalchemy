"""Deep-copy a graph of ORM-mapped objects into a new, transient graph.

This module implements "configuration release" style copying: a whole
object graph (e.g. a Release with its sections, items and tags) is
cloned into brand new transient instances so it can be persisted again
with fresh database-generated identities, while the source objects are
left completely untouched.

Design rules
------------
* **One copy per source entity.**  Copies are memoized on the ORM
  identity key of each source object, so the same row reachable through
  several paths (shared nodes, diamond shapes) produces exactly one new
  object.  This also works when the same row is reachable through
  objects loaded in different sessions.

* **Cycles are preserved, not recursed into.**  The memo entry is
  installed *before* attributes are copied, so cyclic and
  self-referential relationships terminate and the new graph keeps the
  same topology as the source.

* **Generated values are not copied.**  Database-generated primary key
  columns (autoincrement / client-side 'default' / 'server_default'),
  the mapper's 'version_id_col' and any column with a 'server_default'
  are left unset on the copies so the database regenerates them on
  INSERT.  Natural (composite) primary key components *are* copied.

* **Foreign keys follow relationships.**  Plain FK columns are copied
  as-is, except many-to-one FK columns which are left unset and driven
  by the relationship assignment instead: for traversed relationships
  a normal ORM flush re-points the FK at the new row, for KEEP
  relationships the FK ends up pointing at the original row, and for
  IGNORE it stays NULL.

* **No deepcopy of ORM internals.**  New instances are created with
  'ClassManager.new_instance()' (no '__init__' call, no copied instance
  state) and populated attribute by attribute.  'copy.deepcopy' is only
  ever applied to plain scalar column values (e.g. JSON payloads),
  never to mapped objects or instrumented state.

* **Unloaded attributes never trigger SQL.**  Before anything is
  copied, the graph is walked using 'InstanceState.unloaded' only
  (expired attributes count as unloaded).  If any attribute that the
  copy would need is not loaded, 'UnloadedAttributeError' is raised
  listing every missing path, so the caller can load them explicitly
  and retry.  Pass 'on_unloaded="skip"' to leave such attributes unset
  on the copies instead.

Relationship policies
---------------------
By default every relationship is **traversed** (related objects are
copied).  The 'policies' argument overrides this per relationship;
keys may be '"ClassName.attribute"' strings, '(MappedClass,
"attribute")' tuples, or the class-bound 'InstrumentedAttribute'
itself:

* TRAVERSE - copy the related object(s) (default).
* IGNORE   - leave the relationship empty on the copy.  The
  attribute does not need to be loaded on the source.
* KEEP     - point the copy at the *original* related object(s)
  instead of copying them.

.. note:: **Session constraint of the KEEP policy**

   A copy made with KEEP references the original objects.  Adding
   the new graph to a Session cascades (save-update) to those
   originals, so they must be attachable to that session: either add
   the new graph to the *same* session the originals live in, or
   expunge/detach the originals first.  Adding it to a *different*
   session while the originals are still attached elsewhere raises
   'sqlalchemy.exc.InvalidRequestError' ("already attached to
   session ...").

Supported mapping features: composite primary keys, joined-table
inheritance (the concrete subclass of each source object is
re-created), scalar and collection relationships including
many-to-many, and deferred/expired attribute detection.
"""

from __future__ import annotations

import copy as _copy

from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy.orm import ColumnProperty
from sqlalchemy.orm import configure_mappers
from sqlalchemy.orm import RelationshipProperty

__all__ = [
    "TRAVERSE",
    "KEEP",
    "IGNORE",
    "copy_graph",
    "GraphCopyError",
    "UnloadedAttributeError",
]

#: copy the related object(s) (default policy)
TRAVERSE = "traverse"
#: point the copy at the original related object(s)
KEEP = "keep"
#: leave the relationship empty on the copy
IGNORE = "ignore"

_POLICIES = (TRAVERSE, KEEP, IGNORE)


class GraphCopyError(Exception):
    """Base class for graph copy errors."""


class UnloadedAttributeError(GraphCopyError):
    """Raised when the copy would need attributes that are not loaded.

    'paths' holds every missing attribute path, e.g.
    "Release.sections -> Section.items".  No SQL is emitted while
    collecting these paths; load the attributes explicitly and retry.
    """

    def __init__(self, paths):
        self.paths = tuple(sorted(set(paths)))
        super().__init__(
            "cannot copy graph; these attributes are not loaded "
            "(load them explicitly, or use on_unloaded='skip'): %s"
            % ", ".join(self.paths)
        )


def copy_graph(roots, *, policies=None, on_unloaded="fail"):
    """Copy one or more mapped object graphs into new transient objects.

    :param roots: a single mapped instance or an iterable of instances.
    :param policies: optional mapping of relationship locator to
     TRAVERSE / KEEP / IGNORE.
    :param on_unloaded: "fail" (default) raises
     UnloadedAttributeError listing all missing paths;
     "skip" leaves unloaded attributes unset on the copies.
    :return: the new transient root object(s), mirroring the shape of
     'roots'.  Add them to a Session and flush to persist.
    """
    if on_unloaded not in ("fail", "skip"):
        raise ValueError("on_unloaded must be 'fail' or 'skip'")
    configure_mappers()

    single = not isinstance(roots, (list, tuple, set, frozenset))
    root_list = [roots] if single else list(roots)
    policy_map = _normalize_policies(policies)

    missing = []
    skipped = set()
    for root in root_list:
        _check_loaded(
            root, (), policy_map, set(), missing, skipped, on_unloaded
        )
    if missing:
        raise UnloadedAttributeError(missing)

    memo = {}
    copies = [
        _copy_entity(root, memo, policy_map, skipped) for root in root_list
    ]
    return copies[0] if single else copies


def _normalize_policies(policies):
    normalized = {}
    for key, policy in (policies or {}).items():
        if policy not in _POLICIES:
            raise ValueError(
                "unknown relationship policy %r; expected one of %s"
                % (policy, ", ".join(_POLICIES))
            )
        if hasattr(key, "class_") and hasattr(key, "key"):
            key = (key.class_, key.key)
        normalized[key] = policy
    return normalized


def _policy_for(policy_map, mapper, key):
    for cls in mapper.class_.__mro__:
        if (cls, key) in policy_map:
            return policy_map[(cls, key)]
        named = "%s.%s" % (cls.__name__, key)
        if named in policy_map:
            return policy_map[named]
    return TRAVERSE


def _is_generated_column(mapper, column):
    """True for columns whose value the database/ORM must regenerate."""
    if mapper.version_id_col is not None and column.shares_lineage(
        mapper.version_id_col
    ):
        return True
    if column.server_default is not None:
        return True
    if column.primary_key:
        if column.default is not None or column.server_default is not None:
            return True
        if column.autoincrement is True:
            return True
        if (
            column.autoincrement == "auto"
            and len(mapper.primary_key) == 1
            and isinstance(column.type, Integer)
        ):
            return True
    return False


def _column_is_copied(mapper, prop):
    return not all(_is_generated_column(mapper, col) for col in prop.columns)


def _relationship_driven_columns(mapper):
    """Columns whose values are driven by relationship assignments.

    These are the local FK columns of many-to-one relationships; the
    relationship assignment (new object, original object or None)
    determines their value, so the source value must not be copied.
    """
    driven = set()
    for prop in mapper.relationships:
        if prop.viewonly or prop.secondary is not None:
            continue
        if prop.direction.name != "MANYTOONE":
            continue
        driven.update(prop.local_columns)
    return driven


def _check_loaded(
    obj, path, policy_map, visited, missing, skipped, on_unloaded
):
    """Walk the graph without emitting SQL; record unloaded attributes."""
    state = inspect(obj)
    if state is None:
        raise GraphCopyError("object %r is not ORM-mapped" % (obj,))
    if id(obj) in visited:
        return
    visited.add(id(obj))

    mapper = state.mapper
    unloaded = state.unloaded
    fk_driven = _relationship_driven_columns(mapper)

    for prop in mapper.attrs:
        key = prop.key
        locator = "%s.%s" % (mapper.class_.__name__, key)
        is_rel = isinstance(prop, RelationshipProperty)
        policy = _policy_for(policy_map, mapper, key) if is_rel else None
        if is_rel and policy == IGNORE:
            continue
        if isinstance(prop, ColumnProperty) and not _column_is_copied(
            mapper, prop
        ):
            continue
        if (
            isinstance(prop, ColumnProperty)
            and prop.columns
            and all(col in fk_driven for col in prop.columns)
        ):
            continue
        if key in unloaded:
            where = " -> ".join(path + (locator,))
            if on_unloaded == "skip":
                skipped.add((id(obj), key))
            else:
                missing.append(where)
            continue
        if is_rel and policy == TRAVERSE:
            value = getattr(obj, key)
            if prop.uselist:
                for child in value:
                    _check_loaded(
                        child,
                        path + (locator,),
                        policy_map,
                        visited,
                        missing,
                        skipped,
                        on_unloaded,
                    )
            elif value is not None:
                _check_loaded(
                    value,
                    path + (locator,),
                    policy_map,
                    visited,
                    missing,
                    skipped,
                    on_unloaded,
                )


def _copy_entity(obj, memo, policy_map, skipped):
    state = inspect(obj)
    memo_key = state.key if state.key is not None else ("_transient", id(obj))
    if memo_key in memo:
        return memo[memo_key]

    mapper = state.mapper
    # create a new instance without running __init__ and without
    # copying any ORM-internal state
    new_obj = mapper.class_manager.new_instance()
    # register *before* copying attributes so cycles terminate
    memo[memo_key] = new_obj

    fk_driven = _relationship_driven_columns(mapper)
    for prop in mapper.column_attrs:
        if not _column_is_copied(mapper, prop):
            continue
        if prop.columns and all(col in fk_driven for col in prop.columns):
            continue
        if (id(obj), prop.key) in skipped:
            continue
        # plain scalar value; deepcopy only affects mutable payloads
        # such as JSON columns, never ORM state
        setattr(new_obj, prop.key, _copy.deepcopy(getattr(obj, prop.key)))

    for prop in mapper.relationships:
        if prop.viewonly:
            continue
        policy = _policy_for(policy_map, mapper, prop.key)
        if policy == IGNORE:
            setattr(new_obj, prop.key, [] if prop.uselist else None)
            continue
        if (id(obj), prop.key) in skipped:
            continue
        value = getattr(obj, prop.key)
        if policy == KEEP:
            setattr(new_obj, prop.key, list(value) if prop.uselist else value)
        elif prop.uselist:
            setattr(
                new_obj,
                prop.key,
                [_copy_entity(v, memo, policy_map, skipped) for v in value],
            )
        else:
            setattr(
                new_obj,
                prop.key,
                None
                if value is None
                else _copy_entity(value, memo, policy_map, skipped),
            )
    return new_obj
