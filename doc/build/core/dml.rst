Insert, Updates, Deletes
========================

INSERT, UPDATE and DELETE statements build on a hierarchy starting
with :class:`.UpdateBase`.   The :class:`_expression.Insert` and :class:`_expression.Update`
constructs build on the intermediary :class:`.ValuesBase`.

.. currentmodule:: sqlalchemy.sql.expression

.. _dml_foundational_consructors:

DML Foundational Constructors
--------------------------------------

Top level "INSERT", "UPDATE", "DELETE" constructors.

.. autofunction:: delete

.. autofunction:: insert

.. autofunction:: update


DML ``RETURNING`` Capabilities
------------------------------

``INSERT``, ``UPDATE`` and ``DELETE`` may render a SQL ``RETURNING`` clause
either explicitly through :meth:`_expression.Insert.returning`,
:meth:`_expression.Update.returning` or
:meth:`_expression.Delete.returning`, or implicitly through
:meth:`_expression.UpdateBase.return_defaults` and the ORM flush process.
The same result-column metadata and cursor lifecycle rules apply to all of
these paths.

.. list-table::
   :widths: 16 18 22 22 22
   :header-rows: 1

   * - Entry point
     - Columns consumed
     - ``rowcount``
     - Cursor lifecycle
     - Unsupported dialect
   * - Core, sync, single row
     - ``Result.keys()`` and ``Row`` identities come from the compiled
       ``RETURNING`` column list, including computed columns and
       ``server_default`` values. Values changed by database triggers are
       returned only when the database itself exposes them in RETURNING;
       otherwise use ``implicit_returning=False`` and a follow-up SELECT.
     - Explicit ``RETURNING`` returns the DBAPI value only when
       ``preserve_rowcount=True`` or the dialect sets
       ``supports_sane_rowcount_returning=True``; otherwise it is ``-1``.
       ``return_defaults()`` may calculate UPDATE/DELETE counts from returned
       rows.
     - Exhaustion performs a soft close and further fetches return empty
       rows; ``Result.close()`` and transaction rollback perform a hard close
       and further fetches raise :class:`.ResourceClosedError`. Transaction
       commit leaves an explicitly retained result object usable until the
       connection is returned.
     - Compile or execute raises a dialect-specific :class:`.CompileError`
       or :class:`.InvalidRequestError`; unsupported SQL is never silently
       rendered or executed.
   * - Core, sync, ``executemany``
     - INSERT uses insertmanyvalues when available and preserves result
       ordering for requested primary keys; all requested and implicit columns
       are available positionally, by name and by SQL expression identity.
       Composite primary keys retain one value per primary-key column.
     - The value is DBAPI-defined unless ``preserve_rowcount=True``; that
       option reports the number of batched input rows for insertmanyvalues.
       RETURNING still returns ``-1`` on dialects that mark it unreliable.
     - Each batch is buffered before it is returned. Full consumption, close,
       exception or rollback releases the cursor for every batch.
     - INSERT is rejected unless ``insert_executemany_returning`` is enabled;
       UPDATE/DELETE are rejected unless their corresponding
       ``*_executemany_returning`` flags are enabled. ORM flush may instead
       fall back to single-row statements when it needs generated values.
   * - Core, async, non-streaming
     - Columns are buffered and exposed by :class:`_asyncio.AsyncResult` with
       the same identities as the sync result.
     - Same ``rowcount`` rules as sync execution.
     - Async scalar helpers release the adapted cursor before returning;
       fetch errors, ``aclose()`` and rollback hard-close it.
     - Same compile/execute rejection as sync execution.
   * - Core or ORM, ``stream`` / server-side cursor
     - ``RETURNING`` rows are exposed incrementally; each fetched row keeps
       the same positional, named and expression identities.
     - Do not rely on ``rowcount`` until all rows have been consumed; dialects
       that do not support RETURNING rowcount report ``-1``.
     - Partial consumption must be closed explicitly or by the streaming
       context manager. ``AsyncResult`` closes it on context exit, exception
       or rollback.
     - Streaming is rejected for dialects without server-side cursors;
       unsupported ``RETURNING`` is independently rejected before execution.
   * - Scalar and frozen results
     - ``scalar()`` and ``scalar_one*()`` consume exactly the first column;
       ``Result.freeze()`` eagerly copies all remaining rows and releases the
       DBAPI cursor. Extra columns remain in the frozen metadata but are not
       returned as scalars.
     - Scalar methods do not establish a meaningful ``rowcount`` beyond the
       underlying result's rule.
     - Scalar methods hard-close after selecting the value; frozen results no
       longer hold a cursor.
     - The statement is rejected before creating a scalar result.
   * - ORM flush
     - Only columns selected for entity/default population are mapped onto the
       entity. Additional supplemental or explicit ``RETURNING`` columns stay
       in the separate result metadata and are never treated as unmapped entity
       attributes.
     - Optimistic version and row-existence checks use counted returned rows
       only for implicit ``return_defaults()``; explicit DML follows the Core
       rules.
     - Generated values are consumed during flush, after which the internal
       result is closed. Failure or rollback closes any partially consumed
       result.
     - With ``implicit_returning=True`` the flush uses RETURNING when the
       operation is supported; with ``implicit_returning=False`` it falls back
       to lastrowid, prefetch/postfetch or per-row SELECT. Explicit ORM DML
       ``RETURNING`` is rejected if unavailable.

DML Class Documentation Constructors
--------------------------------------

Class documentation for the constructors listed at
:ref:`dml_foundational_consructors`.

.. autoclass:: Delete
   :members:

   .. automethod:: Delete.where

   .. automethod:: Delete.with_dialect_options

   .. automethod:: Delete.returning

.. autoclass:: Insert
   :members:

   .. automethod:: Insert.with_dialect_options

   .. automethod:: Insert.values

   .. automethod:: Insert.returning

.. autoclass:: Update
   :members:

   .. automethod:: Update.returning

   .. automethod:: Update.where

   .. automethod:: Update.with_dialect_options

   .. automethod:: Update.values

.. autoclass:: sqlalchemy.sql.expression.UpdateBase
   :members:

.. autoclass:: sqlalchemy.sql.expression.ValuesBase
   :members:
