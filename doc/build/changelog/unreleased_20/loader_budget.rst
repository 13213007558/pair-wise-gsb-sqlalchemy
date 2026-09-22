.. change::
    :tags: feature, orm

    Added configurable batch chunking and an eager-load graph visit budget
    for ORM relationship loaders.  The :func:`_orm.selectinload` loader
    option now accepts a :paramref:`_orm.selectinload.chunk_size` parameter,
    and the ``orm_selectinload_chunk_size`` execution option may be used to
    size SELECT IN batches for a whole query execution; the configured size
    is a budget of bound parameters per batch, so batches against composite
    primary keys are scaled down to remain within database parameter
    limits.  Additionally, the new ``orm_eager_load_budget`` execution
    option accepts a :class:`_orm.LoaderBudget` (or a dictionary of limits)
    that tracks and optionally caps the number of SQL batches, bound
    parameters, and object identity visits performed while loading an
    object graph, raising :class:`_orm.exc.LoaderBudgetExceeded` when a
    limit would be exceeded.
