"""
Copy a whole graph of ORM-mapped objects into a new, transient graph,
preserving relationship topology without reusing source object identity.

This is the typical "configuration release" problem: a released
configuration consists of many interrelated rows, and producing a new
release requires duplicating the entire object graph while keeping its
internal structure intact.

Features of the copier in ``graph_copy.py``:

* a source entity reachable through several paths is copied exactly
  once (identity is tracked with a memo map), and cyclic relationships
  - including self-references - are preserved topologically without
  infinite recursion.

* database-generated values are never carried over: autoincrement /
  ``Identity`` / ``Sequence`` / ``server_default`` primary keys,
  ``server_default`` and ``Computed`` columns, and the mapper's
  ``version_id_col`` are left unset so the database and the ORM
  generate fresh values on flush.

* foreign keys towards copied entities are re-established by assigning
  the *relationships* to the new objects; the ORM synchronizes the FK
  columns during the normal flush.

* relationships that should not be traversed can be excluded with an
  explicit policy: ``ignore`` (leave unset) or ``keep`` (reference the
  original target object).  ``keep`` requires the kept objects to be
  persistent in the Session where the copies are flushed - typically
  the same Session the sources were loaded from, or a Session into
  which they have been merged.

* unloaded or expired attributes never trigger SQL by default.  The
  copy collects every missing attribute path and raises
  ``UnloadedAttributeError`` so the caller can load the graph
  explicitly (e.g. with ``selectinload``) and retry.

* ``copy.deepcopy`` is never used; new instances are created with
  ``cls.__new__`` and only mapped attribute values are assigned, so no
  ORM-internal state (``_sa_instance_state``) is shared.

Composite primary keys, joined-table inheritance, scalar and
collection relationships (lists, sets, dict-keyed collections) are
supported.

Usage is illustrated via a unit test module ``test_graph_copy.py``,
which can be run directly with pytest against an in-memory SQLite
database::

    $ python -m pytest examples/graph_copy/test_graph_copy.py

.. autosource::

"""
