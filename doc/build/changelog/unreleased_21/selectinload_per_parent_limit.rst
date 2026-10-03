.. change::
    :tags: feature, orm

    Added the :paramref:`_orm.selectinload.per_parent_limit` parameter to
    :func:`.selectinload`, which eagerly loads at most the given number of
    related objects for each parent object.  The feature applies to
    one-to-many and many-to-many collection relationships, supports
    composite parent primary keys, :func:`.with_loader_criteria` and nested
    eager loaders, and uses a deterministic ordering (the relationship's
    ``order_by`` followed by the related primary key) so that results are
    repeatable across batches.  Per-parent limiting is implemented with the
    ``ROW_NUMBER()`` window function and raises
    :class:`_exc.InvalidRequestError` on backends that lack window function
    support.  Partially loaded collections are populated as committed state,
    are not marked as user-modified, and do not cause unloaded related rows
    to be deleted on flush.
