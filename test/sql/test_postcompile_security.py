"""Security regression tests for the post-compile parameter path.

The initial compile of a statement only emits ``__[POSTCOMPILE_x]``
tokens for ``expanding`` and ``literal_execute`` bind parameters; the
final SQL text is rewritten immediately before execution by
``Compiled._process_parameters_for_postcompile``.  These tests guard
that second, post-compile rewrite:

* values that look like SQL syntax (quotes, backslashes), like bind
  placeholders (``:name``, ``%(name)s``, ``?``, ``$1``) or like
  non-ASCII text must be delivered to the DBAPI as *data*, never
  spliced into the SQL string as structure;
* ``literal_execute`` values must be rendered by the literal renderer
  of their declared type (e.g. quoted strings with doubled quotes,
  unquoted integers, ``TypeDecorator.process_literal_param``), never
  by generic ``str()`` concatenation;
* the expanded parameter layout (placeholder order, per-element bind
  processors) must stay aligned with the rewritten statement.

Evidence scope
--------------

* SQLite cases execute end-to-end against an in-memory database.  The
  final SQL string and the DBAPI parameter collection are captured
  with a ``before_cursor_execute`` listener and asserted together with
  the returned rows.
* The other built-in dialects (PostgreSQL, MySQL, MSSQL, Oracle) are
  exercised compile-only: the expanded statement text and the
  parameter layout produced by ``_process_parameters_for_postcompile``
  are compared against hand-written expectations.  No DBAPI round
  trip is claimed for those dialects.

All expected SQL strings and parameter layouts below are hand-written
constants; none of them is derived from SQLAlchemy's own escaping or
rendering helpers, so a regression in those helpers cannot make the
expectations drift along with it.

"""

import pytest

from sqlalchemy import bindparam
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import exc
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import tuple_
from sqlalchemy import TypeDecorator
from sqlalchemy.dialects import mssql
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects import oracle
from sqlalchemy.dialects import postgresql
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures

metadata = MetaData()
sec_t = Table(
    "sec_t",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100)),
)

#: values that must survive the post-compile rewrite as pure data.
#: quotes, backslashes, placeholder-shaped text and unicode.
HOSTILE_VALUES = [
    "O'Brien",
    'x" OR "1"="1',
    "back\\slash",
    ":nm_1",
    "%(nm)s",
    "?",
    "$1",
    "名字",
    "🦊'",
]

#: an injection attempt: if this value were spliced into the SQL
#: unescaped, the predicate would match every row.
INJECTION_VALUE = "x' OR '1'='1"


def _norm(sql_string):
    """normalize whitespace so expectations stay readable."""
    return " ".join(sql_string.split())


class Recorder:
    """captures the final statement and parameters sent to the DBAPI."""

    def __init__(self):
        self.records = []

    def __call__(
        self, conn, cursor, statement, parameters, context, executemany
    ):
        self.records.append((statement, parameters))

    @property
    def last(self):
        return self.records[-1]


@pytest.fixture()
def sqlite_conn():
    engine = create_engine("sqlite://")
    recorder = Recorder()
    event.listen(engine, "before_cursor_execute", recorder)
    with engine.begin() as conn:
        metadata.create_all(conn)
    with engine.connect() as conn:
        yield conn, recorder
    engine.dispose()


@pytest.fixture()
def loaded_conn(sqlite_conn):
    conn, recorder = sqlite_conn
    conn.execute(
        sec_t.insert(),
        [
            dict(id=idx, name=value)
            for idx, value in enumerate(HOSTILE_VALUES, 1)
        ],
    )
    conn.execute(sec_t.insert().values(id=100, name=INJECTION_VALUE))
    conn.commit()
    recorder.records.clear()
    return conn, recorder


class TestExpandingSQLite(fixtures.TestBase):
    """expanding IN rewritten at execution time; executed on SQLite."""

    def test_plain_in_hostile_values_stay_data(self, loaded_conn):
        conn, recorder = loaded_conn
        wanted = ["O'Brien", ":nm_1", "名字", "?", "back\\slash"]

        stmt = (
            select(sec_t.c.id)
            .where(sec_t.c.name.in_(bindparam("nm", expanding=True)))
            .order_by(sec_t.c.id)
        )
        rows = conn.execute(stmt, {"nm": wanted}).fetchall()

        eq_(rows, [(1,), (3,), (4,), (6,), (8,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t "
            "WHERE sec_t.name IN (?, ?, ?, ?, ?) ORDER BY sec_t.id",
        )
        # every value arrives as a DBAPI parameter, in placeholder order
        eq_(parameters, tuple(wanted))
        # no value text leaks into the SQL string (apart from the "?"
        # datum, which is indistinguishable from a qmark placeholder)
        for value in wanted:
            if value != "?":
                assert value not in statement

    def test_injection_value_matches_only_itself(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name.in_(bindparam("nm", expanding=True))
        )
        rows = conn.execute(stmt, {"nm": [INJECTION_VALUE]}).fetchall()

        # only the row holding the literal value matches; an unescaped
        # splice would have returned every row in the table
        eq_(rows, [(100,)])
        statement, parameters = recorder.last
        eq_(parameters, (INJECTION_VALUE,))
        assert INJECTION_VALUE not in statement

    def test_tuple_in_hostile_values_stay_data(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = (
            select(sec_t.c.id)
            .where(
                tuple_(sec_t.c.id, sec_t.c.name).in_(
                    bindparam("tp", expanding=True)
                )
            )
            .order_by(sec_t.c.id)
        )
        rows = conn.execute(
            stmt, {"tp": [(1, "O'Brien"), (8, "名字"), (2, "not me")]}
        ).fetchall()

        eq_(rows, [(1,), (8,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t "
            "WHERE (sec_t.id, sec_t.name) IN (VALUES (?, ?), (?, ?), (?, ?)) "
            "ORDER BY sec_t.id",
        )
        eq_(parameters, (1, "O'Brien", 8, "名字", 2, "not me"))

    def test_empty_set_renders_empty_subquery(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name.in_(bindparam("nm", expanding=True))
        )
        rows = conn.execute(stmt, {"nm": []}).fetchall()

        eq_(rows, [])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name IN "
            "(SELECT 1 FROM (SELECT 1) WHERE 1!=1)",
        )
        eq_(parameters, ())

    def test_tuple_empty_set_renders_empty_subquery(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            tuple_(sec_t.c.id, sec_t.c.name).in_(
                bindparam("tp", expanding=True)
            )
        )
        rows = conn.execute(stmt, {"tp": []}).fetchall()

        eq_(rows, [])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE (sec_t.id, sec_t.name) IN "
            "(SELECT 1, 1 FROM (SELECT 1, 1) WHERE 1!=1)",
        )
        eq_(parameters, ())

    def test_repeated_expanding_param_expands_both_sites(self, loaded_conn):
        """the same expanding parameter used twice must expand at both
        sites with aligned placeholder order."""
        conn, recorder = loaded_conn

        stmt = (
            select(sec_t.c.id)
            .where(
                sec_t.c.name.in_(bindparam("nm", expanding=True))
                | sec_t.c.name.in_(bindparam("nm", expanding=True))
            )
            .order_by(sec_t.c.id)
        )
        rows = conn.execute(stmt, {"nm": ["O'Brien", "%(nm)s"]}).fetchall()

        eq_(rows, [(1,), (5,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name IN (?, ?) "
            "OR sec_t.name IN (?, ?) ORDER BY sec_t.id",
        )
        eq_(parameters, ("O'Brien", "%(nm)s", "O'Brien", "%(nm)s"))

    def test_interleaved_params_keep_positional_order(self, loaded_conn):
        """expansion must not shift the positional index of the plain
        bind parameters around it (parameter index misalignment)."""
        conn, recorder = loaded_conn

        stmt = (
            select(sec_t.c.id)
            .where(
                sec_t.c.id > bindparam("lo"),
                sec_t.c.name.in_(bindparam("nm", expanding=True)),
                sec_t.c.id < bindparam("hi"),
            )
            .order_by(sec_t.c.id)
        )
        rows = conn.execute(
            stmt, {"lo": 0, "nm": ["O'Brien", "back\\slash"], "hi": 10}
        ).fetchall()

        eq_(rows, [(1,), (3,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.id > ? "
            "AND sec_t.name IN (?, ?) AND sec_t.id < ? ORDER BY sec_t.id",
        )
        # exact positional layout: lo, expanded elements, hi
        eq_(parameters, (0, "O'Brien", "back\\slash", 10))


class PrefixedString(TypeDecorator):
    """stores values with a ``P:`` prefix; renders literals with ``L:``."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value == "FAIL":
            raise ValueError("bind processor rejected value")
        return "P:" + value

    def process_result_value(self, value, dialect):
        return value[2:] if value is not None else None

    def process_literal_param(self, value, dialect):
        if value == "FAIL":
            raise ValueError("literal processor rejected value")
        return "L:" + value


class TestTypeDecoratorPostCompile(fixtures.TestBase):
    """per-element type handling must survive the post-compile rewrite."""

    @pytest.fixture()
    def typed_conn(self):
        typed_metadata = MetaData()
        typed_t = Table(
            "typed_t",
            typed_metadata,
            Column("id", Integer, primary_key=True),
            Column("name", PrefixedString(100)),
        )
        engine = create_engine("sqlite://")
        recorder = Recorder()
        event.listen(engine, "before_cursor_execute", recorder)
        with engine.begin() as conn:
            typed_metadata.create_all(conn)
            conn.execute(
                typed_t.insert(),
                [dict(id=1, name="alpha"), dict(id=2, name="beta")],
            )
        with engine.connect() as conn:
            yield conn, typed_t, recorder
        engine.dispose()

    def test_expanding_elements_run_bind_processors(self, typed_conn):
        """each expanded element gets the TypeDecorator bind processor;
        its absence (raw values reaching the DBAPI) is the 'missing
        type handling' regression."""
        conn, typed_t, recorder = typed_conn

        stmt = (
            select(typed_t.c.id)
            .where(typed_t.c.name.in_(bindparam("nm", expanding=True)))
            .order_by(typed_t.c.id)
        )
        rows = conn.execute(stmt, {"nm": ["alpha", "beta"]}).fetchall()

        eq_(rows, [(1,), (2,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT typed_t.id FROM typed_t "
            "WHERE typed_t.name IN (?, ?) ORDER BY typed_t.id",
        )
        eq_(parameters, ("P:alpha", "P:beta"))

    def test_literal_execute_uses_type_literal_processor(self, typed_conn):
        """literal_execute renders via process_literal_param of the
        type, then the impl's quoting - not generic str()."""
        conn, typed_t, recorder = typed_conn

        stmt = select(typed_t.c.id).where(
            typed_t.c.name == bindparam("nm", literal_execute=True)
        )
        # the stored form is "P:alpha"; the literal form is 'L:alpha',
        # so no row matches - the assertion targets the rendered SQL
        rows = conn.execute(stmt, {"nm": "alpha"}).fetchall()

        eq_(rows, [])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT typed_t.id FROM typed_t WHERE typed_t.name = 'L:alpha'",
        )
        eq_(parameters, ())

    def test_bind_processor_failure_then_reexecute(self, typed_conn):
        """after a bind processor raises mid-expansion, the same
        compiled statement must still expand cleanly."""
        conn, typed_t, recorder = typed_conn

        stmt = (
            select(typed_t.c.id)
            .where(typed_t.c.name.in_(bindparam("nm", expanding=True)))
            .order_by(typed_t.c.id)
        )
        with pytest.raises(exc.StatementError, match="bind processor"):
            conn.execute(stmt, {"nm": ["alpha", "FAIL"]})

        rows = conn.execute(stmt, {"nm": ["alpha", "beta"]}).fetchall()
        eq_(rows, [(1,), (2,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT typed_t.id FROM typed_t "
            "WHERE typed_t.name IN (?, ?) ORDER BY typed_t.id",
        )
        eq_(parameters, ("P:alpha", "P:beta"))

        # and again with a different expansion width
        rows = conn.execute(stmt, {"nm": ["beta"]}).fetchall()
        eq_(rows, [(2,)])
        statement, parameters = recorder.last
        assert "IN (?)" in statement
        eq_(parameters, ("P:beta",))

    def test_literal_processor_failure_then_reexecute(self, typed_conn):
        """same recovery guarantee when the literal renderer fails."""
        conn, typed_t, recorder = typed_conn

        stmt = select(typed_t.c.id).where(
            typed_t.c.name == bindparam("nm", literal_execute=True)
        )
        with pytest.raises(
            exc.StatementError, match="Could not render literal value"
        ):
            conn.execute(stmt, {"nm": "FAIL"})

        conn.execute(stmt, {"nm": "alpha"}).fetchall()
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT typed_t.id FROM typed_t WHERE typed_t.name = 'L:alpha'",
        )
        eq_(parameters, ())


class TestLiteralExecuteSQLite(fixtures.TestBase):
    """explicit literal_execute rendered per type rules; on SQLite."""

    def test_string_literal_quotes_doubled(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name == bindparam("nm", literal_execute=True)
        )
        rows = conn.execute(stmt, {"nm": "O'Brien"}).fetchall()

        eq_(rows, [(1,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name = 'O''Brien'",
        )
        eq_(parameters, ())

    def test_string_literal_backslash_and_unicode(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name == bindparam("nm", literal_execute=True)
        )
        rows = conn.execute(stmt, {"nm": "back\\slash"}).fetchall()
        eq_(rows, [(3,)])
        eq_(
            _norm(recorder.last[0]),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name = 'back\\slash'",
        )

        rows = conn.execute(stmt, {"nm": "名字"}).fetchall()
        eq_(rows, [(8,)])
        eq_(
            _norm(recorder.last[0]),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name = '名字'",
        )

    def test_string_literal_placeholder_shaped_text(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name == bindparam("nm", literal_execute=True)
        )
        rows = conn.execute(stmt, {"nm": "%(nm)s"}).fetchall()

        eq_(rows, [(5,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name = '%(nm)s'",
        )
        eq_(parameters, ())

    def test_injection_value_stays_quoted(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name == bindparam("nm", literal_execute=True)
        )
        rows = conn.execute(stmt, {"nm": INJECTION_VALUE}).fetchall()

        # exact match only; a broken renderer would match every row
        eq_(rows, [(100,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t "
            "WHERE sec_t.name = 'x'' OR ''1''=''1'",
        )
        eq_(parameters, ())

    def test_integer_literal_not_quoted(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.id == bindparam("ident", literal_execute=True)
        )
        rows = conn.execute(stmt, {"ident": 3}).fetchall()

        eq_(rows, [(3,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.id = 3",
        )
        eq_(parameters, ())

    def test_string_typed_numeric_value_is_quoted(self, loaded_conn):
        """a string-typed parameter renders '5', not 5: the type's
        literal rule wins over generic str() of the value."""
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name
            == bindparam("nm", literal_execute=True, type_=String)
        )
        conn.execute(stmt, {"nm": "5"}).fetchall()

        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name = '5'",
        )
        eq_(parameters, ())

    def test_literal_execute_expanding_in(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = (
            select(sec_t.c.id)
            .where(
                sec_t.c.name.in_(
                    bindparam("nm", expanding=True, literal_execute=True)
                )
            )
            .order_by(sec_t.c.id)
        )
        rows = conn.execute(stmt, {"nm": ["O'Brien", "名字"]}).fetchall()

        eq_(rows, [(1,), (8,)])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t "
            "WHERE sec_t.name IN ('O''Brien', '名字') ORDER BY sec_t.id",
        )
        eq_(parameters, ())

    def test_literal_execute_expanding_empty_set(self, loaded_conn):
        conn, recorder = loaded_conn

        stmt = select(sec_t.c.id).where(
            sec_t.c.name.in_(
                bindparam("nm", expanding=True, literal_execute=True)
            )
        )
        rows = conn.execute(stmt, {"nm": []}).fetchall()

        eq_(rows, [])
        statement, parameters = recorder.last
        eq_(
            _norm(statement),
            "SELECT sec_t.id FROM sec_t WHERE sec_t.name IN "
            "(SELECT 1 FROM (SELECT 1) WHERE 1!=1)",
        )
        eq_(parameters, ())


class TestExecutemanyRejected(fixtures.TestBase):
    """post-compile parameters are incompatible with executemany and
    must be refused, not silently mis-executed."""

    def test_expanding_executemany_rejected(self, sqlite_conn):
        conn, recorder = sqlite_conn
        stmt = sec_t.update().where(
            sec_t.c.name.in_(bindparam("nm", expanding=True))
        )
        with pytest.raises(
            exc.StatementError,
            match=r"'expanding' parameters can't be used with executemany",
        ):
            conn.execute(stmt, [{"nm": ["a"]}, {"nm": ["b"]}])

    def test_literal_execute_executemany_rejected(self, sqlite_conn):
        conn, recorder = sqlite_conn
        stmt = sec_t.update().where(
            sec_t.c.id == bindparam("ident", literal_execute=True)
        )
        with pytest.raises(
            exc.StatementError,
            match=r"'literal_execute' .* can't be used with executemany",
        ):
            conn.execute(stmt, [{"ident": 1}, {"ident": 2}])

    def test_expanding_literal_execute_executemany_rejected(
        self, sqlite_conn
    ):
        conn, recorder = sqlite_conn
        stmt = sec_t.update().where(
            sec_t.c.name.in_(
                bindparam("nm", expanding=True, literal_execute=True)
            )
        )
        with pytest.raises(
            exc.StatementError, match="can't be used with executemany"
        ):
            conn.execute(stmt, [{"nm": ["a"]}, {"nm": ["b"]}])


class TestCrossDialectCompileOnly(fixtures.TestBase):
    """compile-only comparison for the remaining built-in dialects.

    Evidence scope: these assertions cover the expanded SQL text and
    the DBAPI parameter layout produced by
    ``_process_parameters_for_postcompile`` only; no statement is
    executed against these backends here.
    """

    INJECTION = "x'); DROP TABLE sec_t;--"

    @pytest.fixture(
        params=["postgresql", "mysql", "mssql", "oracle"], ids=lambda v: v
    )
    def expanded(self, request):
        dialect = {
            "postgresql": postgresql.dialect,
            "mysql": mysql.dialect,
            "mssql": mssql.dialect,
            "oracle": oracle.dialect,
        }[request.param]()
        stmt = select(sec_t).where(
            sec_t.c.name.in_(bindparam("nm", expanding=True)),
            sec_t.c.id == bindparam("ident"),
            sec_t.c.name.in_(bindparam("nm", expanding=True))
            | sec_t.c.id.in_(bindparam("ids", expanding=True)),
        )
        compiled = stmt.compile(dialect=dialect)
        params = dict(
            compiled.construct_params(
                {"nm": ["a'b", self.INJECTION], "ident": 5, "ids": [7]}
            )
        )
        expanded_state = compiled._process_parameters_for_postcompile(params)
        return request.param, expanded_state, params

    EXPECTED_STATEMENTS = {
        "postgresql": (
            "SELECT sec_t.id, sec_t.name FROM sec_t WHERE sec_t.name IN "
            "(%(nm_1)s::VARCHAR, %(nm_2)s::VARCHAR) "
            "AND sec_t.id = %(ident)s::INTEGER "
            "AND (sec_t.name IN (%(nm_1)s::VARCHAR, %(nm_2)s::VARCHAR) "
            "OR sec_t.id IN (%(ids_1)s::INTEGER))"
        ),
        "mysql": (
            "SELECT sec_t.id, sec_t.name FROM sec_t WHERE sec_t.name IN "
            "(%s, %s) AND sec_t.id = %s "
            "AND (sec_t.name IN (%s, %s) OR sec_t.id IN (%s))"
        ),
        "mssql": (
            "SELECT sec_t.id, sec_t.name FROM sec_t WHERE sec_t.name IN "
            "(:nm_1, :nm_2) AND sec_t.id = :ident "
            "AND (sec_t.name IN (:nm_1, :nm_2) OR sec_t.id IN (:ids_1))"
        ),
        "oracle": (
            "SELECT sec_t.id, sec_t.name FROM sec_t WHERE sec_t.name IN "
            "(:nm_1, :nm_2) AND sec_t.id = :ident "
            "AND (sec_t.name IN (:nm_1, :nm_2) OR sec_t.id IN (:ids_1))"
        ),
    }

    def test_expanded_statement_layout(self, expanded):
        dialect_name, expanded_state, params = expanded
        eq_(
            _norm(expanded_state.statement),
            self.EXPECTED_STATEMENTS[dialect_name],
        )
        # the hostile values are only ever parameter data
        assert "a'b" not in expanded_state.statement
        assert self.INJECTION not in expanded_state.statement

    def test_expanded_parameter_layout(self, expanded):
        dialect_name, expanded_state, params = expanded
        eq_(
            expanded_state.parameter_expansion,
            {"nm": ["nm_1", "nm_2"], "ids": ["ids_1"]},
        )
        # original list-valued keys are replaced by per-element keys
        eq_(
            params,
            {
                "ident": 5,
                "nm_1": "a'b",
                "nm_2": self.INJECTION,
                "ids_1": 7,
            },
        )

    def test_positional_layout_mysql(self, expanded):
        """positional dialects must keep every placeholder site aligned,
        including the repeated expansion of ``nm``."""
        dialect_name, expanded_state, params = expanded
        if dialect_name == "mysql":
            eq_(
                expanded_state.positiontup,
                ["nm_1", "nm_2", "ident", "nm_1", "nm_2", "ids_1"],
            )
        else:
            eq_(expanded_state.positiontup, None)

    LITERAL_EXPECTED = {
        "postgresql": "sec_t.name = 'O''Brien\\x' AND sec_t.id = 5",
        "mysql": "sec_t.name = 'O''Brien\\\\x' AND sec_t.id = 5",
        "mssql": "sec_t.name = 'O''Brien\\x' AND sec_t.id = 5",
        "oracle": "sec_t.name = 'O''Brien\\x' AND sec_t.id = 5",
    }

    @pytest.mark.parametrize(
        "dialect_name", ["postgresql", "mysql", "mssql", "oracle"]
    )
    def test_literal_execute_per_dialect_quoting(self, dialect_name):
        """literal_execute follows each dialect's legal literal form
        (MySQL additionally escapes the backslash); no generic str()."""
        dialect = {
            "postgresql": postgresql.dialect,
            "mysql": mysql.dialect,
            "mssql": mssql.dialect,
            "oracle": oracle.dialect,
        }[dialect_name]()
        stmt = select(sec_t).where(
            sec_t.c.name == bindparam("nm", literal_execute=True),
            sec_t.c.id == bindparam("ident", literal_execute=True),
        )
        compiled = stmt.compile(dialect=dialect)
        params = dict(
            compiled.construct_params({"nm": "O'Brien\\x", "ident": 5})
        )
        expanded_state = compiled._process_parameters_for_postcompile(params)

        eq_(
            _norm(expanded_state.statement),
            "SELECT sec_t.id, sec_t.name FROM sec_t WHERE "
            + self.LITERAL_EXPECTED[dialect_name],
        )
        # nothing left to bind: values are fully rendered as literals
        eq_(params, {})
