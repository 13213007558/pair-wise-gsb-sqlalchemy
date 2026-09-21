"""Integration tests for SQLite generated columns and RETURNING.

SQLite added generated columns (GENERATED ALWAYS AS) in 3.31 and
INSERT/UPDATE/DELETE ... RETURNING in 3.35.  These tests exercise the
combination of both features across DDL compilation, reflection,
MetaData.create_all() round trips (Alembic style), Core DML, ORM
flush/refresh, and the fallback behavior for SQLite versions that do not
support RETURNING.
"""

from sqlalchemy import Column
from sqlalchemy import Computed
from sqlalchemy import create_engine
from sqlalchemy import exc
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import testing
from sqlalchemy.dialects.sqlite import base as sqlite
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable
from sqlalchemy.testing import AssertsCompiledSQL
from sqlalchemy.testing import eq_
from sqlalchemy.testing import expect_raises_message
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_
from sqlalchemy.testing import mock
from sqlalchemy.testing.assertsql import assert_engine
from sqlalchemy.testing.assertsql import CompiledSQL
from sqlalchemy.testing.fixtures import fixture_session


def _dialect_for_version(version_info):
    """SQLite dialect instance as it would be initialized against a
    connection reporting the given SQLite library version."""
    return sqlite.dialect(
        dbapi=mock.Mock(
            version_info=(2, 6, 0),
            sqlite_version_info=version_info,
            paramstyle="qmark",
        )
    )


class ReturningCapabilityDetectionTest(fixtures.TestBase):
    """The dialect must advertise RETURNING support only for SQLite
    3.35+, so that implicit RETURNING (ORM eager defaults,
    insertmanyvalues, etc.) degrades gracefully on older libraries."""

    __only_on__ = "sqlite"

    @testing.combinations(
        ((3, 30, 0), False),
        ((3, 31, 0), False),
        ((3, 34, 1), False),
        ((3, 35, 0), True),
        ((3, 40, 1), True),
    )
    def test_returning_capability_detection(self, version, expected):
        dialect = _dialect_for_version(version)
        is_(dialect.insert_returning, expected)
        is_(dialect.update_returning, expected)
        is_(dialect.delete_returning, expected)

    def test_real_dialect_matches_library_version(self):
        import sqlite3 as sqlite3_dbapi

        expect = sqlite3_dbapi.sqlite_version_info >= (3, 35)
        is_(testing.db.dialect.insert_returning, expect)
        is_(testing.db.dialect.update_returning, expect)
        is_(testing.db.dialect.delete_returning, expect)

    def test_no_sane_rowcount_returning(self):
        # SQLite cannot report a rowcount for UPDATE/DELETE ... RETURNING,
        # which is why the ORM falls back to a post-SELECT for server
        # onupdate (computed) columns even when RETURNING is available.
        is_(sqlite.dialect.supports_sane_rowcount_returning, False)


class GeneratedColumnDDLCompileTest(fixtures.TestBase, AssertsCompiledSQL):
    __dialect__ = "sqlite"

    def test_virtual_default(self):
        m = MetaData()
        t = Table(
            "t",
            m,
            Column("id", Integer, primary_key=True),
            Column("a", Integer),
            Column("b", Integer, Computed("a * 2")),
        )
        self.assert_compile(
            CreateTable(t),
            "CREATE TABLE t ("
            "id INTEGER NOT NULL, "
            "a INTEGER, "
            "b INTEGER GENERATED ALWAYS AS (a * 2), "
            "PRIMARY KEY (id))",
        )

    def test_stored_and_virtual_explicit(self):
        m = MetaData()
        t = Table(
            "t",
            m,
            Column("a", Integer),
            Column("v", Integer, Computed("a * 2", persisted=False)),
            Column("s", Integer, Computed("a * 3", persisted=True)),
        )
        self.assert_compile(
            CreateTable(t),
            "CREATE TABLE t ("
            "a INTEGER, "
            "v INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL, "
            "s INTEGER GENERATED ALWAYS AS (a * 3) STORED)",
        )

    def test_quoted_identifiers_composite_pk_without_rowid(self):
        m = MetaData()
        t = Table(
            "weird table",
            m,
            Column("select", Integer, nullable=False),
            Column("From Col", Integer, nullable=False),
            Column(
                "full expr",
                String,
                Computed(
                    "\"select\" || '-' || \"From Col\"", persisted=True
                ),
            ),
            PrimaryKeyConstraint("select", "From Col"),
            sqlite_with_rowid=False,
        )
        self.assert_compile(
            CreateTable(t),
            'CREATE TABLE "weird table" ('
            '"select" INTEGER NOT NULL, '
            '"From Col" INTEGER NOT NULL, '
            '"full expr" VARCHAR GENERATED ALWAYS AS '
            "(\"select\" || '-' || \"From Col\") STORED, "
            'PRIMARY KEY ("select", "From Col")) WITHOUT ROWID',
        )


class GeneratedColumnReflectionRoundTripTest(fixtures.TablesTest):
    """create_all -> reflect -> create_all round trips of generated
    columns, Alembic style (reflect into a fresh MetaData, then use that
    MetaData to create and query a brand new database)."""

    __only_on__ = "sqlite"
    __backend__ = True

    @classmethod
    def define_tables(cls, metadata):
        Table(
            "gen_rt",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("a", Integer),
            Column("b", Integer, Computed("a * 2")),
            Column("c", Integer, Computed("a * 3", persisted=True)),
            Column("tail", String(10)),
        )

    def _reflect(self, bind):
        m2 = MetaData()
        return Table("gen_rt", m2, autoload_with=bind)

    def test_reflect_computed_columns(self):
        reflected = self._reflect(testing.db)

        # column order is preserved, generated columns included
        eq_(
            [c.name for c in reflected.columns],
            ["id", "a", "b", "c", "tail"],
        )

        b_comp = reflected.c.b.computed
        c_comp = reflected.c.c.computed
        eq_(b_comp.sqltext.text, "a * 2")
        is_(b_comp.persisted, False)
        eq_(c_comp.sqltext.text, "a * 3")
        is_(c_comp.persisted, True)

        # a reflected generated column acts as both a server default
        # (read-only, fetched after INSERT) and an onupdate (fetched
        # after UPDATE) so the ORM will refresh it on flush
        is_(reflected.c.b.server_default, b_comp)
        is_(reflected.c.b.server_onupdate, b_comp)
        is_(reflected.c.c.server_default, c_comp)
        is_(reflected.c.c.server_onupdate, c_comp)
        is_(reflected.c.a.server_default, None)

    def test_reflect_get_columns(self):
        cols = inspect(testing.db).get_columns("gen_rt")
        eq_([c["name"] for c in cols], ["id", "a", "b", "c", "tail"])
        eq_(
            {c["name"]: c.get("computed") for c in cols},
            {
                "id": None,
                "a": None,
                "b": {"sqltext": "a * 2", "persisted": False},
                "c": {"sqltext": "a * 3", "persisted": True},
                "tail": None,
            },
        )

    def test_reflected_ddl(self):
        reflected = self._reflect(testing.db)
        eq_(
            str(CreateTable(reflected).compile(dialect=testing.db.dialect)),
            "\n"
            "CREATE TABLE gen_rt (\n"
            "\tid INTEGER NOT NULL, \n"
            "\ta INTEGER, \n"
            "\tb INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL, \n"
            "\tc INTEGER GENERATED ALWAYS AS (a * 3) STORED, \n"
            "\ttail VARCHAR(10), \n"
            "\tPRIMARY KEY (id)\n"
            ")\n\n",
        )

    def test_alembic_style_round_trip(self):
        # reflect from the "old" database into a fresh MetaData
        reflected = self._reflect(testing.db)

        # create_all the reflected metadata on a brand new database,
        # as Alembic's offline/online migration flow would
        e2 = create_engine("sqlite://")
        reflected.metadata.create_all(e2)

        # the recreated table generates values identically
        with e2.begin() as conn:
            conn.execute(reflected.insert().values(a=5, tail="x"))
            eq_(
                conn.execute(
                    select(reflected.c.a, reflected.c.b, reflected.c.c)
                ).all(),
                [(5, 10, 15)],
            )

        # reflecting the recreated database yields the same DDL again
        re_reflected = Table(
            "gen_rt", MetaData(), autoload_with=e2
        )
        eq_(
            str(
                CreateTable(re_reflected).compile(dialect=e2.dialect)
            ),
            str(CreateTable(reflected).compile(dialect=e2.dialect)),
        )
        eq_(
            [c.name for c in re_reflected.columns],
            ["id", "a", "b", "c", "tail"],
        )
        e2.dispose()


class QuotedIdentifierWithoutRowidRoundTripTest(fixtures.TablesTest):
    """Generated columns on tables with quoted identifiers, a composite
    primary key and WITHOUT ROWID."""

    __only_on__ = "sqlite"
    __backend__ = True

    @classmethod
    def define_tables(cls, metadata):
        Table(
            "weird table",
            metadata,
            Column("select", Integer, nullable=False),
            Column("From Col", Integer, nullable=False),
            Column(
                "full expr",
                String,
                Computed(
                    "\"select\" || '-' || \"From Col\"", persisted=True
                ),
            ),
            PrimaryKeyConstraint("select", "From Col"),
            sqlite_with_rowid=False,
        )

    def test_reflect(self):
        reflected = Table("weird table", MetaData(), autoload_with=testing.db)

        eq_(
            [c.name for c in reflected.columns],
            ["select", "From Col", "full expr"],
        )
        eq_(
            [c.name for c in reflected.primary_key.columns],
            ["select", "From Col"],
        )
        comp = reflected.c["full expr"].computed
        eq_(comp.sqltext.text, "\"select\" || '-' || \"From Col\"")
        is_(comp.persisted, True)
        eq_(reflected.dialect_kwargs["sqlite_with_rowid"], False)

    def test_reflected_ddl(self):
        reflected = Table("weird table", MetaData(), autoload_with=testing.db)
        eq_(
            str(CreateTable(reflected).compile(dialect=testing.db.dialect)),
            "\n"
            'CREATE TABLE "weird table" (\n'
            '\t"select" INTEGER NOT NULL, \n'
            '\t"From Col" INTEGER NOT NULL, \n'
            '\t"full expr" VARCHAR GENERATED ALWAYS AS '
            "(\"select\" || '-' || \"From Col\") STORED, \n"
            '\tPRIMARY KEY ("select", "From Col")\n'
            ")\n WITHOUT ROWID\n\n",
        )

    def test_insert_select_generated_value(self):
        t = self.tables["weird table"]
        with testing.db.begin() as conn:
            conn.execute(t.insert().values({"select": 1, "From Col": 2}))
            eq_(
                conn.execute(select(t)).all(),
                [(1, 2, "1-2")],
            )

    def test_insert_returning_generated_value(self):
        t = self.tables["weird table"]
        with testing.db.begin() as conn:
            eq_(
                conn.execute(
                    t.insert()
                    .values({"select": 3, "From Col": 4})
                    .returning(t.c["full expr"])
                ).all(),
                [("3-4",)],
            )

    def test_alembic_style_round_trip(self):
        reflected = Table("weird table", MetaData(), autoload_with=testing.db)

        e2 = create_engine("sqlite://")
        reflected.metadata.create_all(e2)
        with e2.begin() as conn:
            conn.execute(
                reflected.insert().values({"select": 5, "From Col": 6})
            )
            eq_(conn.execute(select(reflected)).all(), [(5, 6, "5-6")])

        re_reflected = Table("weird table", MetaData(), autoload_with=e2)
        eq_(
            str(CreateTable(re_reflected).compile(dialect=e2.dialect)),
            str(CreateTable(reflected).compile(dialect=e2.dialect)),
        )
        eq_(re_reflected.dialect_kwargs["sqlite_with_rowid"], False)
        e2.dispose()


class GeneratedColumnCoreDMLTest(fixtures.TablesTest):
    """Core insert/update/delete ... RETURNING against generated columns,
    plus the read-only semantics SQLite enforces for them."""

    __only_on__ = "sqlite"
    __backend__ = True

    @classmethod
    def define_tables(cls, metadata):
        Table(
            "gen_dml",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("a", Integer),
            Column("b", Integer, Computed("a * 2")),
            Column("c", Integer, Computed("a * 3", persisted=True)),
        )

    def test_insert_excludes_generated_columns(self):
        t = self.tables.gen_dml
        eq_(
            str(t.insert().values(a=5).compile(dialect=testing.db.dialect)),
            "INSERT INTO gen_dml (a) VALUES (?)",
        )
        with testing.db.begin() as conn:
            conn.execute(t.insert().values(a=5))
            eq_(
                conn.execute(
                    select(t.c.a, t.c.b, t.c.c).order_by(t.c.id)
                ).all(),
                [(5, 10, 15)],
            )

    def test_insert_returning(self):
        t = self.tables.gen_dml
        with testing.db.begin() as conn:
            eq_(
                conn.execute(
                    t.insert().values(a=7).returning(t.c.b, t.c.c)
                ).all(),
                [(14, 21)],
            )

    def test_insert_returning_star(self):
        t = self.tables.gen_dml
        with testing.db.begin() as conn:
            eq_(
                conn.execute(t.insert().values(a=9).returning(t)).all(),
                [(1, 9, 18, 27)],
            )

    def test_update_returning_backfills(self):
        t = self.tables.gen_dml
        with testing.db.begin() as conn:
            conn.execute(t.insert().values(a=5))
            eq_(
                conn.execute(
                    t.update()
                    .where(t.c.a == 5)
                    .values(a=10)
                    .returning(t.c.b, t.c.c)
                ).all(),
                [(20, 30)],
            )
            eq_(
                conn.execute(select(t.c.a, t.c.b, t.c.c)).all(),
                [(10, 20, 30)],
            )

    def test_delete_returning(self):
        t = self.tables.gen_dml
        with testing.db.begin() as conn:
            conn.execute(t.insert().values(a=3))
            eq_(
                conn.execute(
                    t.delete().where(t.c.a == 3).returning(t.c.b)
                ).all(),
                [(6,)],
            )
            eq_(conn.execute(select(t)).all(), [])

    def test_insert_into_generated_column_is_rejected(self):
        t = self.tables.gen_dml
        with testing.db.begin() as conn:
            with expect_raises_message(
                exc.OperationalError,
                'cannot INSERT into generated column "b"',
            ):
                conn.execute(t.insert().values(a=1, b=2))

    def test_update_of_generated_column_is_rejected(self):
        t = self.tables.gen_dml
        with testing.db.begin() as conn:
            conn.execute(t.insert().values(a=1))
            with expect_raises_message(
                exc.OperationalError,
                'cannot UPDATE generated column "b"',
            ):
                conn.execute(t.update().values(b=5))


class GeneratedColumnORMTest(fixtures.DeclarativeMappedTest):
    """ORM flush populates generated columns through INSERT ... RETURNING
    and refreshes them after UPDATE."""

    __only_on__ = "sqlite"
    __backend__ = True

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class Widget(Base):
            __tablename__ = "widget"

            id = Column(Integer, primary_key=True)
            a = Column(Integer)
            b = Column(Integer, Computed("a * 2"))
            c = Column(Integer, Computed("a * 3", persisted=True))

    def test_flush_insert_uses_returning(self):
        Widget = self.classes.Widget

        s = fixture_session()
        w = Widget(a=4)
        s.add(w)

        with assert_engine(testing.db) as asserter:
            s.flush()

        asserter.assert_(
            CompiledSQL(
                "INSERT INTO widget (a) VALUES (?) "
                "RETURNING id, b, c",
                [{"a": 4}],
                dialect="sqlite",
            )
        )
        eq_(w.id, 1)
        eq_(w.b, 8)
        eq_(w.c, 12)

    def test_flush_update_refreshes_generated_columns(self):
        Widget = self.classes.Widget

        s = fixture_session()
        w = Widget(a=4)
        s.add(w)
        s.flush()

        w.a = 10
        with assert_engine(testing.db) as asserter:
            s.flush()
            # generated columns are expired by the UPDATE; reading them
            # back triggers the refresh SELECT
            eq_(w.b, 20)
            eq_(w.c, 30)

        # SQLite has no sane rowcount for UPDATE ... RETURNING, so the
        # ORM emits a plain UPDATE and back-fills the generated columns
        # with a follow-up SELECT
        asserter.assert_(
            CompiledSQL(
                "UPDATE widget SET a=? WHERE widget.id = ?",
                [{"a": 10, "widget_id": 1}],
                dialect="sqlite",
            ),
            CompiledSQL(
                "SELECT widget.b, widget.c FROM widget "
                "WHERE widget.id = ?",
                [{"pk_1": 1}],
                dialect="sqlite",
            ),
        )

    def test_expire_and_refresh(self):
        Widget = self.classes.Widget

        s = fixture_session()
        w = Widget(a=6)
        s.add(w)
        s.flush()
        eq_(w.b, 12)

        s.expire(w)
        eq_(w.b, 12)
        eq_(w.c, 18)

        s.refresh(w)
        eq_((w.a, w.b, w.c), (6, 12, 18))

    def test_orm_never_writes_generated_columns(self):
        # the ORM must not include the generated columns in the INSERT
        # column list at all; assigning to them is not supported by
        # SQLite and they are treated as read-only fetched values
        Widget = self.classes.Widget

        s = fixture_session()
        w = Widget(a=1)
        s.add(w)
        with assert_engine(testing.db) as asserter:
            s.flush()
        asserter.assert_(
            CompiledSQL(
                "INSERT INTO widget (a) VALUES (?) "
                "RETURNING id, b, c",
                [{"a": 1}],
                dialect="sqlite",
            )
        )


class LegacyNoReturningFallbackTest(fixtures.TestBase):
    """Behavior on a SQLite library without RETURNING (< 3.35), simulated
    by switching the dialect capabilities off: the ORM must still flush
    and populate generated columns, using a follow-up SELECT instead of
    INSERT ... RETURNING."""

    __only_on__ = "sqlite"

    def _no_returning_engine(self):
        engine = create_engine("sqlite://")
        engine.dialect.insert_returning = False
        engine.dialect.update_returning = False
        engine.dialect.delete_returning = False
        return engine

    def test_orm_flush_without_returning(self):
        engine = self._no_returning_engine()

        m = MetaData()
        t = Table(
            "legacy_t",
            m,
            Column("id", Integer, primary_key=True),
            Column("a", Integer),
            Column("b", Integer, Computed("a * 2")),
        )
        m.create_all(engine)

        Base = declarative_base()

        class Thing(Base):
            __table__ = t

        with Session(engine) as s:
            thing = Thing(a=4)
            s.add(thing)
            with assert_engine(engine) as asserter:
                s.flush()
                # without RETURNING the generated column is expired on
                # flush and back-filled with a follow-up SELECT
                eq_(thing.id, 1)
                eq_(thing.b, 8)
            asserter.assert_(
                CompiledSQL(
                    "INSERT INTO legacy_t (a) VALUES (?)",
                    [{"a": 4}],
                    dialect="sqlite",
                ),
                CompiledSQL(
                    "SELECT legacy_t.b FROM legacy_t "
                    "WHERE legacy_t.id = ?",
                    [{"pk_1": 1}],
                    dialect="sqlite",
                ),
            )

            thing.a = 9
            s.flush()
            eq_(thing.b, 18)
        engine.dispose()

    def test_core_insert_without_returning(self):
        engine = self._no_returning_engine()

        m = MetaData()
        t = Table(
            "legacy_core",
            m,
            Column("id", Integer, primary_key=True),
            Column("a", Integer),
            Column("b", Integer, Computed("a * 2")),
        )
        m.create_all(engine)

        with engine.begin() as conn:
            with assert_engine(engine) as asserter:
                conn.execute(t.insert().values(a=3))
            asserter.assert_(
                CompiledSQL(
                    "INSERT INTO legacy_core (a) VALUES (?)",
                    [{"a": 3}],
                    dialect="sqlite",
                )
            )
            eq_(conn.execute(select(t.c.a, t.c.b)).all(), [(3, 6)])
        engine.dispose()
