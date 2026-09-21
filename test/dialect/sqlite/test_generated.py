"""SQLite generated column and RETURNING integration tests."""

import re
import sqlite3
from types import SimpleNamespace

from sqlalchemy import Column
from sqlalchemy import Computed
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import testing
from sqlalchemy.dialects.sqlite import base as sqlite
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable
from sqlalchemy.testing import eq_
from sqlalchemy.testing import eq_ignore_whitespace
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_
from sqlalchemy.testing import is_false
from sqlalchemy.testing import is_true


def _normalize_sql(sql):
    return re.sub(r"\s+", " ", sql).strip()


def _old_sqlite_dbapi():
    return SimpleNamespace(
        apilevel="2.0",
        connect=sqlite3.connect,
        DatabaseError=sqlite3.DatabaseError,
        Error=sqlite3.Error,
        IntegrityError=sqlite3.IntegrityError,
        OperationalError=sqlite3.OperationalError,
        PARSE_COLNAMES=sqlite3.PARSE_COLNAMES,
        PARSE_DECLTYPES=sqlite3.PARSE_DECLTYPES,
        paramstyle="qmark",
        ProgrammingError=sqlite3.ProgrammingError,
        Row=sqlite3.Row,
        sqlite_version="3.34.0",
        sqlite_version_info=(3, 34, 0),
        threadsafety=1,
    )


class GeneratedColumnDDLTest(fixtures.TestBase):
    __only_on__ = "sqlite"

    @testing.combinations(
        (None, ""),
        (True, " STORED"),
        (False, " VIRTUAL"),
        argnames="persisted,expected_suffix",
    )
    def test_create_generated_column(self, persisted, expected_suffix):
        metadata = MetaData()
        computed_kwargs = (
            {} if persisted is None else {"persisted": persisted}
        )
        table = Table(
            "generated_ddl",
            metadata,
            Column("a", Integer, primary_key=True),
            Column("b", Integer, nullable=False),
            Column(
                "c",
                Integer,
                Computed("b + 1", **computed_kwargs),
            ),
            sqlite_with_rowid=False,
        )

        eq_ignore_whitespace(
            str(
                CreateTable(table).compile(dialect=sqlite.dialect())
            ),
            "\nCREATE TABLE generated_ddl ("
            "\n\ta INTEGER NOT NULL, "
            "\n\tb INTEGER NOT NULL, "
            f"\n\tc INTEGER GENERATED ALWAYS AS (b + 1)"
            f"{expected_suffix}, "
            "\n\tPRIMARY KEY (a)"
            "\n)\n WITHOUT ROWID\n\n",
        )


class GeneratedColumnIntegrationTest(fixtures.TestBase):
    __only_on__ = "sqlite"
    __backend__ = True
    __requires__ = (
        "computed_columns",
        "insert_returning",
        "update_returning",
    )

    def test_reflect_create_all_and_values(self, connection):
        connection.exec_driver_sql(
            """
            CREATE TABLE source_table (
                "part one" INTEGER NOT NULL,
                "part two" INTEGER NOT NULL,
                "label, source" VARCHAR(20),
                "q""x" VARCHAR(20),
                "generated value" VARCHAR(40)
                    GENERATED ALWAYS AS
                    ("q""x" || ": " || "label, source" || ' => '
                     || ("part one" + "part two")) VIRTUAL,
                stored_total INTEGER
                    GENERATED ALWAYS AS
                    ("part one" + "part two") STORED,
                PRIMARY KEY ("part one", "part two")
            ) WITHOUT ROWID
            """
        )

        source_metadata = MetaData()
        source = Table(
            "source_table", source_metadata, autoload_with=connection
        )

        eq_(
            [column.name for column in source.c],
            [
                "part one",
                "part two",
                "label, source",
                'q"x',
                "generated value",
                "stored_total",
            ],
        )

        generated = source.c["generated value"]
        stored = source.c.stored_total
        is_(generated.primary_key, False)
        is_(stored.primary_key, False)
        is_true(bool(generated.computed))
        is_true(bool(stored.computed))
        eq_(generated.computed.persisted, False)
        eq_(stored.computed.persisted, True)
        eq_(
            re.sub(
                r"\s+",
                " ",
                generated.computed.sqltext.text,
            ),
            re.sub(
                r"\s+",
                " ",
                '"q""x" || ": " || "label, source" || '
                "' => ' || (\"part one\" + \"part two\")",
            ),
        )
        eq_(stored.computed.sqltext.text, '"part one" + "part two"')
        eq_(source.kwargs["sqlite_with_rowid"], False)
        eq_(
            [column.name for column in source.primary_key],
            ["part one", "part two"],
        )

        roundtrip_engine = create_engine(
            "sqlite://", creator=lambda: sqlite3.connect(":memory:")
        )
        with roundtrip_engine.begin() as roundtrip_connection:
            source_metadata.create_all(roundtrip_connection)
            roundtrip = Table(
                "source_table", MetaData(), autoload_with=roundtrip_connection
            )
            insert_values = {
                "part one": 2,
                "part two": 3,
                "label, source": "answer",
                'q"x': "question",
            }
            update_where = (
                roundtrip.c["part one"] == 2
            ) & (roundtrip.c["part two"] == 3)
            insert_sql = str(
                roundtrip.insert()
                .values(insert_values)
                .compile(dialect=roundtrip_connection.dialect)
            )
            update_sql = str(
                roundtrip.update()
                .where(update_where)
                .values({"part two": 4})
                .compile(dialect=roundtrip_connection.dialect)
            )
            assert '"generated value"' not in insert_sql
            assert "stored_total" not in insert_sql
            assert '"generated value"' not in update_sql
            assert "stored_total" not in update_sql
            insert_returning_sql = _normalize_sql(
                str(
                    roundtrip.insert()
                    .values(
                        {
                            "part one": 7,
                            "part two": 8,
                            "label, source": "second",
                            'q"x': "second",
                        }
                    )
                    .returning(roundtrip)
                    .compile(dialect=roundtrip_connection.dialect)
                )
            )
            update_returning_sql = _normalize_sql(
                str(
                    roundtrip.update()
                    .where(
                        (roundtrip.c["part one"] == 7)
                        & (roundtrip.c["part two"] == 8)
                    )
                    .values({"part two": 9})
                    .returning(roundtrip)
                    .compile(dialect=roundtrip_connection.dialect)
                )
            )
            eq_(
                insert_returning_sql,
                'INSERT INTO source_table ("part one", "part two", '
                '"label, source", "q""x") VALUES (?, ?, ?, ?) '
                'RETURNING "part one", "part two", "label, source", '
                '"q""x", "generated value", stored_total',
            )
            eq_(
                update_returning_sql,
                'UPDATE source_table SET "part two"=? '
                'WHERE source_table."part one" = ? '
                'AND source_table."part two" = ? '
                'RETURNING "part one", "part two", "label, source", '
                '"q""x", "generated value", stored_total',
            )

            eq_(roundtrip.kwargs["sqlite_with_rowid"], False)
            eq_(
                [column.name for column in roundtrip.c],
                [column.name for column in source.c],
            )
            eq_(
                roundtrip.c["generated value"].computed.persisted,
                False,
            )
            eq_(roundtrip.c.stored_total.computed.persisted, True)

            result = roundtrip_connection.execute(
                roundtrip.insert()
                .values(insert_values)
                .returning(roundtrip)
            ).one()
            eq_(result._mapping["stored_total"], 5)
            eq_(
                result._mapping["generated value"],
                "question: answer => 5",
            )

            updated = roundtrip_connection.execute(
                roundtrip.update()
                .where(update_where)
                .values({"part two": 4})
                .returning(roundtrip)
            ).one()
            eq_(updated._mapping["stored_total"], 6)
            eq_(
                updated._mapping["generated value"],
                "question: answer => 6",
            )

            final = roundtrip_connection.execute(
                select(
                    roundtrip.c.stored_total,
                    roundtrip.c["generated value"],
                )
            ).one()
            eq_(tuple(final), (6, "question: answer => 6"))

    def test_reflect_generated_column_with_expression_commas(
        self, connection
    ):
        connection.exec_driver_sql(
            """
            CREATE TABLE expression_commas (
                source VARCHAR(20),
                first_value INTEGER
                    GENERATED ALWAYS AS (instr(source, ',')),
                second_value INTEGER
                    GENERATED ALWAYS AS (instr(source, ':')),
                UNIQUE(first_value)
            )
            """
        )

        reflected = Table(
            "expression_commas", MetaData(), autoload_with=connection
        )
        eq_(
            reflected.c.first_value.computed.sqltext.text,
            "instr(source, ',')",
        )
        eq_(
            reflected.c.second_value.computed.sqltext.text,
            "instr(source, ':')",
        )


class GeneratedColumnORMTest(fixtures.TestBase):
    __only_on__ = "sqlite"
    __backend__ = True
    __requires__ = ("computed_columns",)

    def test_orm_flush_and_refresh(self, connection):
        base = declarative_base()

        class GeneratedRow(base):
            __tablename__ = "generated_orm"

            a = Column(Integer, primary_key=True)
            b = Column(Integer, primary_key=True)
            value = Column(Integer, nullable=False)
            total = Column(Integer, Computed("a + b + value"))

            __table_args__ = {"sqlite_with_rowid": False}

        base.metadata.create_all(connection)

        with Session(connection) as session:
            row = GeneratedRow(a=1, b=2, value=3)
            session.add(row)
            session.flush()
            eq_(row.total, 6)

            row.value = 10
            session.flush()
            eq_(row.total, 13)

            session.refresh(row)
            eq_((row.a, row.b, row.value, row.total), (1, 2, 10, 13))

        eq_(
            connection.execute(
                select(GeneratedRow.total).where(
                    (GeneratedRow.a == 1) & (GeneratedRow.b == 2)
                )
            ).scalar(),
            13,
        )


class OldSQLiteCapabilityTest(fixtures.TestBase):
    __only_on__ = "sqlite"

    def test_version_3_34_disables_returning(self):
        dialect = sqlite.dialect(dbapi=_old_sqlite_dbapi())

        is_false(dialect.insert_returning)
        is_false(dialect.update_returning)
        is_false(dialect.delete_returning)
        eq_(dialect.server_version_info, (3, 34, 0))

    def test_version_3_35_enables_returning(self):
        dbapi = _old_sqlite_dbapi()
        dbapi.sqlite_version = "3.35.0"
        dbapi.sqlite_version_info = (3, 35, 0)
        dialect = sqlite.dialect(dbapi=dbapi)

        is_true(dialect.insert_returning)
        is_true(dialect.update_returning)
        is_true(dialect.delete_returning)

    def test_orm_postfetch_without_returning(self):
        engine = create_engine("sqlite://", module=_old_sqlite_dbapi())
        base = declarative_base()

        class GeneratedRow(base):
            __tablename__ = "generated_old_sqlite"

            a = Column(Integer, primary_key=True)
            b = Column(Integer, primary_key=True)
            value = Column(Integer)
            total = Column(Integer, Computed("a + b + value"))

            __table_args__ = {"sqlite_with_rowid": False}

        base.metadata.create_all(engine)
        statements = []

        def record_statement(
            conn, cursor, statement, parameters, context, executemany
        ):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)

        with Session(engine) as session:
            row = GeneratedRow(a=1, b=2, value=4)
            session.add(row)
            session.flush()
            eq_(row.total, 7)

            row.value = 9
            session.flush()
            eq_(row.total, 12)

            session.refresh(row)
            eq_((row.a, row.b, row.value, row.total), (1, 2, 9, 12))
            session.commit()

        with engine.begin() as connection:
            eq_(
                connection.execute(select(GeneratedRow.__table__)).one(),
                (1, 2, 9, 12),
            )
        assert not any(
            "RETURNING" in statement for statement in statements
        )

    def test_core_dml_without_returning(self):
        engine = create_engine("sqlite://", module=_old_sqlite_dbapi())
        metadata = MetaData()
        table = Table(
            "generated_old_core",
            metadata,
            Column("a", Integer, primary_key=True),
            Column("b", Integer, primary_key=True),
            Column("value", Integer),
            Column("total", Integer, Computed("a + b + value")),
            sqlite_with_rowid=False,
        )
        metadata.create_all(engine)

        with engine.begin() as connection:
            connection.execute(
                table.insert(),
                {"a": 1, "b": 2, "value": 3},
            )
            connection.execute(
                table.update().where(
                    (table.c.a == 1) & (table.c.b == 2)
                ),
                {"value": 10},
            )
            eq_(
                connection.execute(
                    select(table.c.total).where(
                        (table.c.a == 1) & (table.c.b == 2)
                    )
                ).scalar(),
                13,
            )
