"""Multi-tenant ``schema_translate_map`` isolation tests.

These tests simulate a service where every tenant shares one
:class:`_engine.Engine` and one compiled statement cache, and each
request selects its own data space through ``schema_translate_map``.
Several in-memory databases are ATTACHed to a single SQLite connection
and hold same-named tables loaded with distinct marker values, so any
namespace bleed is visible both in the rows returned and in the final
SQL received by the DBAPI.
"""

import collections

from unittest.mock import Mock
from unittest.mock import patch

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import exc
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import testing
from sqlalchemy.testing import eq_
from sqlalchemy.testing import expect_raises_message
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import in_

TenantEnv = collections.namedtuple(
    "TenantEnv", ["engine", "items", "odd", "dbapi_sql"]
)


class TenantSchemaTranslateTest(fixtures.TestBase):
    __only_on__ = "sqlite"

    @testing.fixture
    def tenant_env(self):
        engine = create_engine("sqlite://")

        @event.listens_for(engine, "connect")
        def _attach_tenant_spaces(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("ATTACH DATABASE ':memory:' AS tenant_a")
            cursor.execute("ATTACH DATABASE ':memory:' AS tenant_b")
            cursor.execute("ATTACH DATABASE ':memory:' AS \"tenant a\"")
            cursor.close()

        with engine.begin() as conn:
            for space, marker in (
                ("main", "MAIN"),
                ("tenant_a", "A"),
                ("tenant_b", "B"),
                ('"tenant a"', "QUOTED"),
            ):
                conn.exec_driver_sql(
                    f"CREATE TABLE {space}.items ("
                    "id INTEGER PRIMARY KEY, marker VARCHAR(30))"
                )
                conn.exec_driver_sql(
                    f"INSERT INTO {space}.items VALUES (1, '{marker}')"
                )
            conn.exec_driver_sql(
                'CREATE TABLE "tenant a"."odd table" ('
                "id INTEGER PRIMARY KEY, marker VARCHAR(30))"
            )
            conn.exec_driver_sql(
                'INSERT INTO "tenant a"."odd table" '
                "VALUES (1, 'QUOTED-ODD')"
            )

        dbapi_sql = []

        @event.listens_for(engine, "before_cursor_execute")
        def _record_dbapi_sql(
            conn, cursor, statement, parameters, context, executemany
        ):
            dbapi_sql.append(statement)

        metadata = MetaData()
        items = Table(
            "items",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("marker", String(30)),
        )
        odd = Table(
            "odd table",
            MetaData(),
            Column("id", Integer, primary_key=True),
            Column("marker", String(30)),
        )

        try:
            yield TenantEnv(engine, items, odd, dbapi_sql)
        finally:
            engine.dispose()

    def test_connection_level_a_b_a_roundtrip(self, tenant_env):
        """Same statements run A->B->A with connection-level options and
        a shared compiled cache: the cached structure is reused while
        the SQL reaching the DBAPI always names the current tenant."""

        engine, items, odd, dbapi_sql = tenant_env
        cache = {}

        stmt = select(items.c.marker).where(items.c.id == 1)
        ins = items.insert()
        ia = items.alias("ia")
        aliased_stmt = select(ia.c.marker).where(ia.c.id == 1)
        stmt_id2 = select(items.c.marker).where(items.c.id == 2)

        with engine.connect() as conn:
            conn.execution_options(compiled_cache=cache)
            with patch.object(
                stmt, "_compiler", Mock(side_effect=stmt._compiler)
            ) as compile_stmt, patch.object(
                ins, "_compiler", Mock(side_effect=ins._compiler)
            ) as compile_ins, patch.object(
                aliased_stmt,
                "_compiler",
                Mock(side_effect=aliased_stmt._compiler),
            ) as compile_aliased:
                # tenant A
                conn.execution_options(
                    schema_translate_map={None: "tenant_a"}
                )
                eq_(conn.scalar(stmt), "A")
                in_("FROM tenant_a.items", dbapi_sql[-1])
                conn.execute(ins, {"id": 2, "marker": "A2"})
                in_("INSERT INTO tenant_a.items", dbapi_sql[-1])
                eq_(conn.scalar(aliased_stmt), "A")
                in_("FROM tenant_a.items AS ia", dbapi_sql[-1])

                # tenant B
                conn.execution_options(
                    schema_translate_map={None: "tenant_b"}
                )
                eq_(conn.scalar(stmt), "B")
                in_("FROM tenant_b.items", dbapi_sql[-1])
                conn.execute(ins, {"id": 2, "marker": "B2"})
                in_("INSERT INTO tenant_b.items", dbapi_sql[-1])
                eq_(conn.scalar(aliased_stmt), "B")
                in_("FROM tenant_b.items AS ia", dbapi_sql[-1])

                # back to tenant A: B's writes must not be visible
                conn.execution_options(
                    schema_translate_map={None: "tenant_a"}
                )
                eq_(conn.scalar(stmt), "A")
                in_("FROM tenant_a.items", dbapi_sql[-1])
                eq_(conn.scalar(stmt_id2), "A2")
                in_("FROM tenant_a.items", dbapi_sql[-1])

            # each statement was compiled exactly once; tenants B and
            # the second A run were served from the shared cache
            eq_(compile_stmt.call_count, 1)
            eq_(compile_ins.call_count, 1)
            eq_(compile_aliased.call_count, 1)

        # one cached structure per distinct statement shape, shared by
        # all tenants: stmt and stmt_id2 differ only in a bound
        # literal and collapse into one entry, plus ins and
        # aliased_stmt
        eq_(len(cache), 3)

    def test_statement_level_a_b_a_roundtrip(self, tenant_env):
        """Per-execute ``execution_options`` select the tenant space for
        each individual statement on a shared connection."""

        engine, items, odd, dbapi_sql = tenant_env
        cache = {}

        stmt = select(items.c.marker).where(items.c.id == 1)
        ins = items.insert()

        opt_a = {
            "compiled_cache": cache,
            "schema_translate_map": {None: "tenant_a"},
        }
        opt_b = {
            "compiled_cache": cache,
            "schema_translate_map": {None: "tenant_b"},
        }

        with engine.connect() as conn:
            with patch.object(
                stmt, "_compiler", Mock(side_effect=stmt._compiler)
            ) as compile_stmt, patch.object(
                ins, "_compiler", Mock(side_effect=ins._compiler)
            ) as compile_ins:
                eq_(conn.scalar(stmt, execution_options=opt_a), "A")
                in_("FROM tenant_a.items", dbapi_sql[-1])
                conn.execute(
                    ins, {"id": 2, "marker": "A2"}, execution_options=opt_a
                )
                in_("INSERT INTO tenant_a.items", dbapi_sql[-1])

                eq_(conn.scalar(stmt, execution_options=opt_b), "B")
                in_("FROM tenant_b.items", dbapi_sql[-1])
                conn.execute(
                    ins, {"id": 2, "marker": "B2"}, execution_options=opt_b
                )
                in_("INSERT INTO tenant_b.items", dbapi_sql[-1])

                eq_(conn.scalar(stmt, execution_options=opt_a), "A")
                in_("FROM tenant_a.items", dbapi_sql[-1])

            eq_(compile_stmt.call_count, 1)
            eq_(compile_ins.call_count, 1)

        eq_(len(cache), 2)

    def test_none_value_maps_to_default_schema(self, tenant_env):
        """``None`` as a map value selects the connection's default
        schema (``main`` for SQLite); switching back and forth stays
        within a single shared cache entry."""

        engine, items, odd, dbapi_sql = tenant_env
        cache = {}

        stmt = select(items.c.marker).where(items.c.id == 1)

        with engine.connect() as conn:
            conn.execution_options(compiled_cache=cache)
            with patch.object(
                stmt, "_compiler", Mock(side_effect=stmt._compiler)
            ) as compile_stmt:
                conn.execution_options(
                    schema_translate_map={None: "tenant_a"}
                )
                eq_(conn.scalar(stmt), "A")
                in_("FROM tenant_a.items", dbapi_sql[-1])

                conn.execution_options(schema_translate_map={None: None})
                eq_(conn.scalar(stmt), "MAIN")
                in_("FROM main.items", dbapi_sql[-1])

                conn.execution_options(
                    schema_translate_map={None: "tenant_a"}
                )
                eq_(conn.scalar(stmt), "A")
                in_("FROM tenant_a.items", dbapi_sql[-1])

            # {None: None} and {None: "tenant_a"} share one cache
            # entry: the placeholder structure is identical, only the
            # execution-time rendering differs
            eq_(compile_stmt.call_count, 1)

        eq_(len(cache), 1)

    def test_quoted_schema_and_table_identifiers(self, tenant_env):
        """Identifiers that require quoting are translated and quoted
        correctly in the SQL that reaches the DBAPI."""

        engine, items, odd, dbapi_sql = tenant_env
        cache = {}

        stmt = select(items.c.marker).where(items.c.id == 1)
        odd_stmt = select(odd.c.marker).where(odd.c.id == 1)

        with engine.connect() as conn:
            conn.execution_options(
                compiled_cache=cache,
                schema_translate_map={None: "tenant a"},
            )
            eq_(conn.scalar(stmt), "QUOTED")
            in_('FROM "tenant a".items', dbapi_sql[-1])
            eq_(conn.scalar(odd_stmt), "QUOTED-ODD")
            in_('FROM "tenant a"."odd table"', dbapi_sql[-1])

    def test_failed_execution_does_not_poison_cache(self, tenant_env):
        """A bad map or a failed execution must not corrupt the shared
        cache for the next request."""

        engine, items, odd, dbapi_sql = tenant_env
        cache = {}

        stmt = select(items.c.marker).where(items.c.id == 1)

        with engine.connect() as conn:
            conn.execution_options(
                compiled_cache=cache,
                schema_translate_map={None: "tenant_a"},
            )
            eq_(conn.scalar(stmt), "A")
            eq_(len(cache), 1)

            # map that renders a schema which does not exist: the
            # DBAPI raises and no rows come back
            conn.execution_options(
                schema_translate_map={None: "no_such_tenant"}
            )
            with expect_raises_message(
                exc.OperationalError, "no_such_tenant"
            ):
                conn.scalar(stmt)

            # map whose keys are inconsistent with the cached
            # statement: placeholder rendering fails before anything
            # is sent to the database
            conn.execution_options(
                schema_translate_map={"tenant_a": "tenant_b"}
            )
            with expect_raises_message(
                exc.StatementError, "previously had `None` present"
            ):
                conn.scalar(stmt)

            # the next well-formed request reuses the cached structure
            # untouched and lands in its own space
            conn.execution_options(
                schema_translate_map={None: "tenant_b"}
            )
            with patch.object(
                stmt, "_compiler", Mock(side_effect=stmt._compiler)
            ) as compile_stmt:
                eq_(conn.scalar(stmt), "B")
            eq_(compile_stmt.call_count, 0)
            in_("FROM tenant_b.items", dbapi_sql[-1])
            eq_(len(cache), 1)

    def test_caller_supplied_map_dict_is_used_live(self, tenant_env):
        """Evidence for how the caller's dict participates in
        execution: it is consulted at execution time rather than
        copied, and the rendering step records its internal ``_none``
        placeholder translation inside the caller's own dict."""

        engine, items, odd, dbapi_sql = tenant_env
        cache = {}

        tenant_map = {None: "tenant_a"}
        stmt = select(items.c.marker).where(items.c.id == 1)

        with engine.connect() as conn:
            conn.execution_options(
                compiled_cache=cache, schema_translate_map=tenant_map
            )
            eq_(conn.scalar(stmt), "A")
            in_("FROM tenant_a.items", dbapi_sql[-1])

            # the execution wrote its "_none" placeholder translation
            # into the caller's dict
            eq_(tenant_map, {None: "tenant_a", "_none": "tenant_a"})

            # mutating the caller's dict between executions switches
            # the space for the next one, with no new
            # execution_options() call
            tenant_map[None] = "tenant_b"
            eq_(conn.scalar(stmt), "B")
            in_("FROM tenant_b.items", dbapi_sql[-1])
            eq_(tenant_map["_none"], "tenant_b")

    def test_stashed_rendered_sql_leaks_across_spaces(self, tenant_env):
        """The final rendered SQL string is tenant-specific: if an
        application stashes one request's rendered statement and
        replays it under the next request, it silently reads the first
        tenant's space.  Only the unrendered statement structure is
        safe to share."""

        engine, items, odd, dbapi_sql = tenant_env

        stmt = select(items.c.marker).where(items.c.id == 1)

        with engine.connect() as conn:
            conn.execution_options(
                schema_translate_map={None: "tenant_a"}
            )
            eq_(conn.scalar(stmt), "A")
            rendered_for_a = dbapi_sql[-1]
            in_("FROM tenant_a.items", rendered_for_a)

            conn.execution_options(
                schema_translate_map={None: "tenant_b"}
            )

            # control: re-executing the statement itself under tenant
            # B renders B's namespace, the legitimate form of reuse
            eq_(conn.scalar(stmt), "B")
            in_("FROM tenant_b.items", dbapi_sql[-1])

            # hazard caught: replaying A's rendered SQL under B's
            # request reaches back into A's space
            eq_(conn.exec_driver_sql(rendered_for_a, (1,)).scalar(), "A")
            in_("FROM tenant_a.items", dbapi_sql[-1])
