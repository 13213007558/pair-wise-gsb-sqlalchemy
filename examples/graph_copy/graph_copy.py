"""Copy a graph of ORM-mapped objects into a new graph of transient
objects, preserving relationship topology without reusing source object
identity.

Typical usage::

    copier = GraphCopier()
    new_root = copier.copy(old_root)
    session.add(new_root)   # normal ORM flush wires up the new FKs
    session.commit()

Design notes:

* ``copy.deepcopy`` is deliberately *not* used.  New instances are
  created with ``cls.__new__(cls)`` and only mapped attribute values
  are assigned, so no ORM-internal state (``_sa_instance_state``,
  attribute history, session affiliation) is ever shared between the
  source graph and the copy.

* A memo map keyed by ORM identity key guarantees that a source entity
  reachable through several paths is copied exactly once, and makes
  cyclic/self-referential relationships terminate while preserving the
  topology of the graph.

* Database-generated values are not carried over: autoincrement,
  ``Identity`` and ``Sequence`` primary keys, ``server_default`` and
  ``Computed`` columns, and the mapper's ``version_id_col`` are left
  unset so fresh values are produced on flush.

* Foreign-key columns that are covered by a relationship are never
  copied directly; the relationship is pointed at the new object and
  the ORM synchronizes the FK columns during the normal flush.

* Relationships listed in ``exclude`` are handled according to
  ``on_excluded``: ``"ignore"`` leaves them unset on the copy, while
  ``"keep"`` makes the copy reference the *original* target objects.
  With ``"keep"`` the kept targets must be persistent in the Session
  where the copies are flushed - use the same Session the sources were
  loaded from, or ``Session.merge()`` the kept objects into the target
  Session first, otherwise the flush fails because the objects belong
  to a different (or no) Session.

* Unloaded or expired attributes never trigger SQL by default.  All
  missing attribute paths are collected and reported in a single
  ``UnloadedAttributeError`` so the caller can load the graph
  explicitly (``selectinload``, ``Session.refresh``, attribute access)
  and retry.  Pass ``allow_unloaded=True`` to opt into lazy loading
  during the copy.

"""

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import Integer
from sqlalchemy import Sequence

__all__ = ["GraphCopier", "UnloadedAttributeError", "copy_graph"]


class UnloadedAttributeError(Exception):
    """Raised when the copy encounters attributes that are not loaded.

    Carries the sorted tuple of dotted attribute paths in ``missing``,
    e.g. ``("Document.lines", "Document.title")``.  Load those
    attributes explicitly and retry the copy.
    """

    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__(
            "Cannot copy object graph; %d attribute(s) are not loaded "
            "and reading them would emit SQL. Load them explicitly "
            "(e.g. selectinload()/refresh()) and retry: %s"
            % (len(self.missing), ", ".join(self.missing))
        )


class GraphCopier:
    """Copies graphs of ORM-mapped objects.

    :param exclude: iterable of ``(mapped class or mapper, "attrname")``
        tuples naming relationships that must not be traversed.
    :param on_excluded: policy for excluded relationships, ``"ignore"``
        (leave unset on the copy, the default) or ``"keep"`` (reference
        the original target objects; see the module docstring for the
        Session constraint this implies).
    :param allow_unloaded: if True, unloaded/expired attributes are
        read normally, which may emit SQL.  The default False collects
        every missing path and raises :class:`UnloadedAttributeError`
        without emitting any SQL.
    """

    def __init__(self, *, exclude=(), on_excluded="ignore",
                 allow_unloaded=False):
        if on_excluded not in ("ignore", "keep"):
            raise ValueError(
                "on_excluded must be 'ignore' or 'keep', got %r"
                % (on_excluded,)
            )
        self.on_excluded = on_excluded
        self.allow_unloaded = allow_unloaded
        self._exclude = set()
        for cls_or_mapper, key in exclude:
            inspected = sa_inspect(cls_or_mapper)
            cls = getattr(inspected, "class_", None) or cls_or_mapper
            self._exclude.add((cls, key))

    def copy(self, roots):
        """Copy ``roots`` (a single mapped object or an iterable of them)
        and return the new transient object(s).

        The returned objects are not attached to any Session; add them
        to a Session and flush to persist them.
        """
        self._memo = {}
        self._missing = set()
        if isinstance(roots, (list, tuple, set, frozenset)):
            many = True
            root_list = list(roots)
        else:
            many = False
            root_list = [roots]
        result = [
            self._clone(root, type(root).__name__) for root in root_list
        ]
        if self._missing:
            raise UnloadedAttributeError(sorted(self._missing))
        return result if many else result[0]

    # -- internals -----------------------------------------------------

    def _clone(self, obj, path):
        state = sa_inspect(obj)
        mapper = state.mapper
        cls = mapper.class_

        # persistent/pending objects have an identity key; fall back to
        # object identity for transient sources.
        memo_key = state.key if state.key is not None else (cls, id(obj))
        if memo_key in self._memo:
            return self._memo[memo_key]

        # a fresh instance with a fresh InstanceState: __init__ is not
        # called and deepcopy is never used, so no ORM-internal state is
        # carried over.  The instance is registered in the memo *before*
        # recursing, which is what makes cyclic and self-referential
        # graphs terminate.
        copy = mapper.class_manager.new_instance()
        self._memo[memo_key] = copy

        self._copy_columns(obj, copy, state, mapper, path)
        self._copy_relationships(obj, copy, state, mapper, path)
        return copy

    def _copy_columns(self, obj, copy, state, mapper, path):
        rel_columns = set()
        for rel in mapper.relationships:
            rel_columns.update(rel.local_columns)

        for attr in mapper.column_attrs:
            key = attr.key
            if not self._check_loaded(state, key, path):
                continue
            columns = attr.columns
            if any(
                self._is_generated(mapper, column) or column in rel_columns
                for column in columns
            ):
                continue
            # plain scalar value; assigned by reference, never deep-copied
            setattr(copy, key, getattr(obj, key))

    def _copy_relationships(self, obj, copy, state, mapper, path):
        for rel in mapper.relationships:
            key = rel.key
            if rel.viewonly:
                # viewonly relationships are derived; never copied
                continue
            if not self._check_loaded(state, key, path):
                continue
            value = getattr(obj, key)
            if self._is_excluded(mapper, key):
                if self.on_excluded == "keep":
                    # reference the original target(s); a fresh container
                    # is used so the source collection is never shared
                    setattr(
                        copy,
                        key,
                        self._map_collection(value, lambda item: item)
                        if rel.uselist
                        else value,
                    )
                # "ignore": leave unset (None / empty collection)
                continue
            if rel.uselist:
                setattr(
                    copy,
                    key,
                    self._map_collection(
                        value,
                        lambda item: self._clone(item, "%s.%s" % (path, key)),
                    ),
                )
            else:
                setattr(
                    copy,
                    key,
                    None
                    if value is None
                    else self._clone(value, "%s.%s" % (path, key)),
                )

    def _check_loaded(self, state, key, path):
        if self.allow_unloaded or key not in state.unloaded:
            return True
        self._missing.add("%s.%s" % (path, key))
        return False

    def _is_excluded(self, mapper, key):
        return any(
            (base, key) in self._exclude for base in mapper.class_.__mro__
        )

    @staticmethod
    def _map_collection(collection, func):
        """Apply ``func`` to the elements of a loaded collection,
        preserving the container type (list / set / dict-keyed)."""
        if hasattr(collection, "items"):
            return {key: func(value) for key, value in collection.items()}
        if isinstance(collection, (set, frozenset)):
            return {func(value) for value in collection}
        return [func(value) for value in collection]

    @staticmethod
    def _is_generated(mapper, column):
        """True for columns whose value is produced by the database or
        by the ORM versioning machinery, and therefore must not be
        copied."""
        if mapper.version_id_col is not None and (
            column is mapper.version_id_col
        ):
            return True
        if column.server_default is not None:
            return True
        if column.computed is not None or column.identity is not None:
            return True
        if column.primary_key:
            if isinstance(column.default, Sequence):
                return True
            autoincrement = column.autoincrement
            if autoincrement is True:
                return True
            if autoincrement in ("auto", None):
                # the ORM autoincrements single-column integer PKs only
                pk_columns = column.table.primary_key.columns
                return len(pk_columns) == 1 and (
                    column.type._type_affinity is Integer
                )
        return False


def copy_graph(roots, **kw):
    """Convenience one-shot wrapper around :class:`GraphCopier`."""
    return GraphCopier(**kw).copy(roots)
