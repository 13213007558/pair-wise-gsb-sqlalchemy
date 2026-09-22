.. change::
    :tags: usecase, orm

    Added configurable SELECT IN batch splitting for
    :func:`_orm.selectinload` eager loading, via the new
    :paramref:`_orm.selectinload.chunk_size` parameter and the
    ``orm_selectin_chunk_size`` execution option, as well as a new
    :class:`_orm.GraphBudget` execution option (``orm_graph_budget``)
    that bounds the number of post-load SELECT statements, cumulative bound
    parameters, and distinct object identities materialized while loading
    an eagerly loaded object graph.  Batch splitting preserves ordering,
    parent/child association and identity-map deduplication; cyclic and
    otherwise repeated relationship paths are deduplicated and never count
    the same identity twice.  Exceeding a configured budget raises the new
    :class:`_orm.GraphBudgetExceeded` before the next statement is emitted
    or the next entity is materialized.  The feature inter-operates with
    :func:`_orm.joinedload`, :func:`_orm.contains_eager`, composite primary
    keys, aliased relationships, :func:`_orm.raiseload` and
    :class:`_asyncio.AsyncSession`.
