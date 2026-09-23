.. change::
    :tags: bug, asyncio, pool

    Fixed a series of issues where cancellation of an asyncio task at
    various points of a connection's lifecycle could leave the connection
    pool in an inconsistent state, causing subsequent checkouts to time
    out.  The pool ledger is now closed at every cancellation point:

    * When a task is cancelled while the pool is resetting a connection
      for return (rollback-on-return), the connection record is now
      invalidated, closed and checked back into the pool before the
      ``asyncio.CancelledError`` propagates, instead of remaining
      checked out until garbage collection.

    * When a task is cancelled while the driver-level connection is being
      established, including while connect event handlers such as dialect
      initialization are running, the half-initialized DBAPI connection is
      now closed instead of being leaked.

    * :meth:`_engine.Connection.close` now disassociates the connection
      from the pool connection and marks itself closed even if the
      operation is interrupted by ``asyncio.CancelledError``.

    * Connection record checkin and invalidation now guarantee the record
      is returned to the pool exactly once, even if finalizers, event
      handlers, or connection termination are interrupted by an exit
      exception such as ``asyncio.CancelledError``.
