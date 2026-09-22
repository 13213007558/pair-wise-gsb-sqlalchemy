"""Disconnect handling tests driven by a fully controllable DBAPI proxy.

These tests inject disconnect points at well defined DBAPI operations
(cursor execute, connection commit/rollback/close, connect time) and
assert on the three things that matter for recovery:

* how many SQL statements / connections were produced,
* whether the failing connection was returned to or invalidated in the pool,
* what transaction state the :class:`.Connection` is in afterwards and
  which SQLAlchemy exception ultimately propagates.

The scenarios mirror real driver failure modes: recycle, pre_ping,
reset_on_return, explicit and autocommit transactions, executemany,
the asyncio greenlet bridge and a checkout that is blocked in the queue
while another checkout invalidates the pool.
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import event
from sqlalchemy import exc
from sqlalchemy import pool
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy import testing
from sqlalchemy.engine import default
from sqlalchemy.testing import assert_raises_context_ok
from sqlalchemy.testing import eq_
from sqlalchemy.testing import expect_raises
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_
from sqlalchemy.testing import is_false
from sqlalchemy.testing import is_not_none
from sqlalchemy.testing import is_true
from sqlalchemy.testing.engines import testing_engine


class MockError(Exception):
    """Base class for the errors raised by the mock DBAPI."""


class MockDisconnect(MockError):
    """An error the dialect recognizes as a real disconnect."""


class MockNonDisconnect(MockError):
    """A plain DBAPI error; the connection must stay alive."""


class InterfaceError(MockError):
    pass


class DatabaseError(MockError):
    pass


class OperationalError(MockDisconnect, DatabaseError):
    pass


class ProgrammingError(MockNonDisconnect, DatabaseError):
    pass


class IntegrityError(MockNonDisconnect, DatabaseError):
    pass


class ProxyCursor:
    description = None
    rowcount = -1
    lastrowid = None
    arraysize = 1

    def __init__(self, dbapi, conn):
        self._dbapi = dbapi
        self._conn = conn
        # psycopg2 dialect post_exec reads cursor.connection.notices.
        self.connection = conn
        self.closed = False
        self.execute_count = 0
        self.executemany_count = 0

    def execute(self, statement, parameters=None):
        self.execute_count += 1
        self._dbapi.sql_count += 1
        if self._dbapi.is_ping(statement):
            self._dbapi.ping_count += 1
        self._conn.check("execute")
        self.description = [
            ("col_1", None, None, None, None, None, None, None, None)
        ]
        self.rowcount = 1
        return self

    def executemany(self, statement, parameters):
        self.executemany_count += 1
        self._dbapi.sql_count += 1
        self._conn.check("executemany")
        self.description = None
        self.rowcount = len(parameters)
        return self

    def fetchone(self):
        if self.description is None or self.closed:
            raise MockNonDisconnect("no result row")
        self.description = None
        return (1,)

    def fetchmany(self, size=None):
        return [self.fetchone()]

    def fetchall(self):
        return []

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *arg):
        self.close()


class ProxyConnection:
    """A tracked DBAPI connection with injectable failure points."""

    def __init__(self, dbapi, serial):
        self._dbapi = dbapi
        self.serial = serial
        self.closed = False
        self.rollback_count = 0
        self.commit_count = 0
        self.cursor_count = 0
        self.cursors = []
        self.notices = []
        self.isolation_level = None
        # operations on which this connection raises; the error is a
        # MockDisconnect unless the op is also in non_disconnect_ops.
        self.fail_on = set()
        self.non_disconnect_ops = set()

    def check(self, op):
        if op in self.fail_on:
            if op in self.non_disconnect_ops:
                raise ProgrammingError(
                    "%s failed, connection still usable" % op
                )
            raise OperationalError(
                "connection lost during %s" % op
            )

    def cursor(self, *arg, **kw):
        self.check("cursor")
        self.cursor_count += 1
        cursor = ProxyCursor(self._dbapi, self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commit_count += 1
        self.check("commit")

    def rollback(self):
        self.rollback_count += 1
        self.check("rollback")

    def close(self):
        # close() is invoked for both normal teardown and hard
        # invalidation (do_terminate delegates to do_close by default).
        first = not self.closed
        self.closed = True
        if first:
            self._dbapi.closed_conns.append(self)
        self.check("close")

    def set_isolation_level(self, level):
        self.isolation_level = level


class ControlledDBAPI:
    """Mock DBAPI module that records all traffic and injects failures."""

    paramstyle = "qmark"
    Error = MockError
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    OperationalError = OperationalError
    ProgrammingError = ProgrammingError
    IntegrityError = IntegrityError

    def __init__(self, ping_statement="SELECT 1"):
        self.ping_statement = ping_statement
        self.connections = []
        self.closed_conns = []
        self.sql_count = 0
        self.ping_count = 0
        self.connect_count = 0
        # when True, connect() itself raises a disconnect.
        self.stopped = False
        self._serial = 0

    def is_ping(self, statement):
        return str(statement).strip() == self.ping_statement

    def connect(self, *arg, **kw):
        self.connect_count += 1
        if self.stopped:
            raise OperationalError("database is stopped")
        self._serial += 1
        conn = ProxyConnection(self, self._serial)
        self.connections.append(conn)
        return conn

    def shutdown(self, *ops, stop=False):
        """Make all live (and optionally future) check()s fail on ops."""
        self.stopped = stop
        for conn in self.connections:
            conn.fail_on.update(ops)

    def restart(self):
        self.stopped = False

    def dispose(self):
        self.stopped = False
        for conn in self.connections:
            conn.fail_on = set()
            conn.non_disconnect_ops = set()


class DisconnectFixtureMixin:
    def _pool(self, dbapi, **kw):
        dialect = default.DefaultDialect()
        dialect.dbapi = dbapi
        dialect.is_disconnect = lambda e, conn, cursor: isinstance(
            e, MockDisconnect
        )
        kw.setdefault("dialect", dialect)
        kw.setdefault("pool_size", 1)
        kw.setdefault("max_overflow", 0)
        return pool.QueuePool(creator=lambda: dbapi.connect("mock://"), **kw)

    def _engine(self, dbapi=None, **kw):
        if dbapi is None:
            dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        engine = testing_engine(
            "postgresql+psycopg2://u:p@localhost/test",
            options=dict(module=dbapi, _initialize=False, **kw),
        )
        engine.dialect.is_disconnect = lambda e, conn, cursor: isinstance(
            e, MockDisconnect
        )
        # the real psycopg2 dialect looks up constants in the driver
        # module; emulate that against the controlled connection instead.
        def _set_isolation_level(dbapi_connection, level):
            dbapi_connection.set_isolation_level(level)

        engine.dialect.set_isolation_level = _set_isolation_level
        return engine


class PoolRecycleDisconnectTest(fixtures.TestBase, DisconnectFixtureMixin):
    """Pool-only tests; checkout/checkin/invalidation semantics."""

    dbapi = None

    def teardown_test(self):
        if self.dbapi is not None:
            self.dbapi.dispose()

    def test_recycle_closes_stale_connection_and_creates_new(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, recycle=20)

        fairy = p.connect()
        old = fairy.dbapi_connection
        fairy.close()

        p._recycle = 0
        time.sleep(0.01)

        fairy = p.connect()
        new = fairy.dbapi_connection
        is_not_none(new)
        is_false(new is old)
        fairy.close()

        eq_(dbapi.connect_count, 2)
        # recycle hard-terminates rather than re-pooling the stale conn.
        is_true(old.closed)
        eq_([c.serial for c in dbapi.closed_conns], [old.serial])
        # recycle is not a ping: no SELECT was emitted.
        eq_(dbapi.ping_count, 0)
        eq_(p.checkedin(), 1)

    def test_recycle_tolerates_broken_close(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, recycle=20)

        fairy = p.connect()
        old = fairy.dbapi_connection
        fairy.close()
        eq_(dbapi.connect_count, 1)

        # the stale connection now errors even on close(); recycling must
        # swallow that and still produce a working new connection.
        old.fail_on.add("close")
        p._recycle = 0
        time.sleep(0.01)

        fairy = p.connect()
        try:
            is_false(fairy.dbapi_connection is old)
            cursor = fairy.cursor()
            cursor.execute("SELECT 1")
            eq_(cursor.fetchone(), (1,))
        finally:
            fairy.close()

        eq_(dbapi.connect_count, 2)
        is_true(old.closed)

    def test_pre_ping_dead_connection_is_recycled(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, pre_ping=True)

        fairy = p.connect()
        old = fairy.dbapi_connection
        fairy.close()
        eq_(dbapi.ping_count, 0)

        # server goes away: ping execute now raises a disconnect.
        dbapi.shutdown("execute")

        fairy = p.connect()
        try:
            new = fairy.dbapi_connection
            is_false(new is old)
            new.fail_on.discard("execute")
            cursor = fairy.cursor()
            cursor.execute("SELECT 1")
            eq_(cursor.fetchone(), (1,))
        finally:
            fairy.close()

        # stale conn pinged once and terminated; the reconnected conn is
        # pinged again on the second checkout attempt before being handed out.
        is_true(dbapi.ping_count >= 1)
        eq_(dbapi.connect_count, 2)
        is_true(old.closed)
        eq_(p.checkedin(), 1)

    def test_pre_ping_db_remains_down_raises_disconnect(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, pre_ping=True)

        fairy = p.connect()
        old = fairy.dbapi_connection
        fairy.close()

        dbapi.shutdown("execute", stop=True)

        assert_raises_context_ok(MockDisconnect, p.connect)

        # the stale connection was pinged once; reconnect attempts also
        # failed and nothing live was left in the pool.
        eq_(dbapi.ping_count, 1)
        is_true(dbapi.connect_count >= 2)
        is_true(old.closed)
        # the record remains queued but holds no live connection; it will
        # reconnect lazily on next checkout.
        eq_(p.checkedin(), 1)
        is_(p._pool.queue[0].dbapi_connection, None)
        dbapi.restart()

    def test_pre_ping_non_disconnect_error_does_not_retry(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, pre_ping=True)

        fairy = p.connect()
        old = fairy.dbapi_connection
        fairy.close()

        old.fail_on.add("execute")
        old.non_disconnect_ops.add("execute")

        assert_raises_context_ok(MockNonDisconnect, p.connect)

        # non-disconnect: no reconnect attempt; record is queued but bare.
        eq_(dbapi.connect_count, 1)
        is_true(old.closed)
        is_(p._pool.queue[0].dbapi_connection, None)

    def test_reset_on_return_rollback_runs_on_checkin(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, reset_on_return="rollback")

        fairy = p.connect()
        dbapi_conn = fairy.dbapi_connection
        fairy.close()

        eq_(dbapi_conn.rollback_count, 1)
        eq_(dbapi_conn.commit_count, 0)
        eq_(p.checkedin(), 1)

    def test_reset_on_return_disconnect_invalidates_not_returns(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, reset_on_return="rollback")

        fairy = p.connect()
        dbapi_conn = fairy.dbapi_connection
        fairy.cursor().execute("some work")

        # connection dies; the reset rollback on checkin now raises.
        dbapi_conn.fail_on.add("rollback")
        fairy.close()

        # failed reset invalidates; the dead connection is not reusable.
        is_true(dbapi_conn.closed)
        eq_(dbapi.sql_count, 1)
        # the slot remains queued but its connection is gone.
        is_(p._pool.queue[0].dbapi_connection, None)

        fairy = p.connect()
        try:
            is_false(fairy.dbapi_connection is dbapi_conn)
        finally:
            fairy.close()
        eq_(dbapi.connect_count, 2)
        is_not_none(p._pool.queue[0].dbapi_connection)


class WaitingCheckoutDisconnectTest(
    fixtures.TestBase, DisconnectFixtureMixin
):
    """A checkout waiting in the queue must observe a concurrent pool
    invalidation and recycle instead of receiving the dead connection."""

    dbapi = None

    def teardown_test(self):
        if self.dbapi is not None:
            self.dbapi.dispose()

    @testing.requires.threading_with_mock
    def test_waiting_checkout_gets_recycled_after_pool_invalidation(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(dbapi, pool_size=1, max_overflow=0, timeout=10)

        holder_ready = threading.Event()
        invalidate_done = threading.Event()
        waiter_started = threading.Event()
        outcomes = {}

        def holder():
            fairy = p.connect()
            conn = fairy.dbapi_connection
            holder_ready.set()
            # wait until the other thread is blocked in the queue, then
            # kill this connection and hard-invalidate the pool before
            # returning it.
            waiter_started.wait(10)
            time.sleep(0.1)
            conn.fail_on.add("execute")
            fairy._connection_record.invalidate(
                MockDisconnect("server closed connection")
            )
            # mimic engine pool invalidation timestamp bump
            p._invalidate(fairy, MockDisconnect("server closed"))
            fairy.close()
            invalidate_done.set()

        def waiter():
            holder_ready.wait(10)
            waiter_started.set()
            fairy = p.connect()
            try:
                outcomes["serial"] = fairy.dbapi_connection.serial
                cursor = fairy.cursor()
                cursor.execute("SELECT 1")
                outcomes["row"] = cursor.fetchone()
            finally:
                fairy.close()

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        is_false(t1.is_alive())
        is_false(t2.is_alive())
        invalidate_done.wait(10)

        # waiter received a freshly created connection, not the dead one.
        eq_(dbapi.connect_count, 2)
        eq_(outcomes["serial"], 2)
        eq_(outcomes["row"], (1,))
        is_true(dbapi.connections[0].closed)
        eq_(p.checkedin(), 1)

    @testing.requires.threading_with_mock
    def test_waiting_checkout_times_out_while_connection_held(self):
        dbapi = ControlledDBAPI()
        self.dbapi = dbapi
        p = self._pool(
            dbapi, pool_size=1, max_overflow=0, timeout=0.2
        )

        def holder():
            fairy = p.connect()
            started.wait(10)
            time.sleep(0.6)
            fairy.close()

        started = threading.Event()
        outcomes = []

        def waiter():
            started.set()
            try:
                p.connect()
            except Exception as e:  # noqa: BLE001
                outcomes.append(e)

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)

        eq_(len(outcomes), 1)
        is_true(isinstance(outcomes[0], exc.TimeoutError))


class EngineDisconnectClassificationTest(
    fixtures.TestBase, DisconnectFixtureMixin
):
    """Statement execution disconnects at the Engine/Connection level.

    For each failure point we assert: statement counts, whether the
    DBAPI connection was invalidated out of the pool, the transaction
    state left on the Connection and the class of propagated exception.
    """

    dbapi = None
    engine = None

    def teardown_test(self):
        if self.engine is not None:
            self.engine.dispose()
        if self.dbapi is not None:
            self.dbapi.dispose()

    def test_execute_disconnect_invalidates_and_reconnects(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        conn = engine.connect()
        old = conn.connection.dbapi_connection
        eq_(conn.execute(select(1)).scalar(), 1)

        # SELECT #2 dies mid-statement with a recognized disconnect.
        dbapi.shutdown("execute")

        with expect_raises(exc.OperationalError) as ec:
            conn.execute(select(1))
        is_true(ec.error.connection_invalidated)
        is_true(isinstance(ec.error.orig, MockDisconnect))

        # connection facade stays open but marked invalidated; the dead
        # DBAPI connection was hard closed, not returned to the pool.
        is_false(conn.closed)
        is_true(conn.invalidated)
        is_true(old.closed)
        eq_(dbapi.connect_count, 1)
        eq_(dbapi.sql_count, 2)

        # autobegan transaction: must rollback before reuse.
        with expect_raises(exc.PendingRollbackError):
            conn.execute(select(1))

        # rollback clears the transaction; reconnect happens lazily on the
        # next statement, at which point invalidation is cleared.
        conn.rollback()
        eq_(conn.execute(select(1)).scalar(), 1)
        is_false(conn.invalidated)
        eq_(dbapi.connect_count, 2)
        conn.close()

    def test_execute_non_disconnect_error_is_not_invalidated(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        conn = engine.connect()
        dbapi_conn = conn.connection.dbapi_connection
        conn.execute(select(1))

        dbapi_conn.fail_on.add("execute")
        dbapi_conn.non_disconnect_ops.add("execute")

        # statement error -> ProgrammingError, not OperationalError;
        # connection remains usable and pooled.
        with expect_raises(exc.ProgrammingError) as ec:
            conn.execute(select(1))
        is_false(ec.error.connection_invalidated)

        is_false(conn.invalidated)
        is_false(dbapi_conn.closed)
        eq_(dbapi.connect_count, 1)

        # same connection can keep working after rollback.
        conn.rollback()
        dbapi_conn.fail_on.discard("execute")
        eq_(conn.execute(select(1)).scalar(), 1)
        conn.close()

        # reset rolled back, connection back in pool, not invalidated.
        eq_(dbapi.connect_count, 1)
        eq_(dbapi_conn.rollback_count, 2)

    def test_commit_disconnect_is_unknown_but_invalidates(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        conn = engine.connect()
        trans = conn.begin()
        conn.execute(select(1))
        old = conn.connection.dbapi_connection

        # a disconnect on COMMIT means the commit outcome is unknown;
        # SQLAlchemy must still invalidate rather than reuse the conn.
        dbapi.shutdown("commit")

        with expect_raises(exc.OperationalError) as ec:
            trans.commit()
        is_true(ec.error.connection_invalidated)
        is_true(isinstance(ec.error.orig, MockDisconnect))

        is_false(conn.closed)
        is_true(conn.invalidated)
        is_false(trans.is_active)
        is_true(old.closed)

        # explicit rollback clears the failed transaction; the next
        # statement reconnects lazily and clears invalidation.
        conn.rollback()
        eq_(conn.execute(select(1)).scalar(), 1)
        is_false(conn.invalidated)
        eq_(dbapi.connect_count, 2)
        conn.close()

    def test_rollback_disconnect_invalidates(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        conn = engine.connect()
        conn.execute(select(1))
        old = conn.connection.dbapi_connection

        dbapi.shutdown("rollback")

        with expect_raises(exc.OperationalError) as ec:
            conn.rollback()
        is_true(ec.error.connection_invalidated)

        is_true(conn.invalidated)
        is_true(old.closed)

        # rollback failed; the transaction is cleared, and the next
        # statement transparently reconnects.
        conn.rollback()
        eq_(conn.execute(select(1)).scalar(), 1)
        is_false(conn.invalidated)
        eq_(dbapi.connect_count, 2)
        conn.close()

    def test_autocommit_execute_disconnect_invalidates(self):
        engine = self._engine(
            execution_options={"isolation_level": "AUTOCOMMIT"}
        )
        self.engine = engine
        dbapi = self.dbapi

        conn = engine.connect()
        old = conn.connection.dbapi_connection
        eq_(conn.execute(select(1)).scalar(), 1)
        eq_(old.commit_count, 0)

        dbapi.shutdown("execute")

        with expect_raises(exc.OperationalError) as ec:
            conn.execute(select(1))
        is_true(ec.error.connection_invalidated)
        is_true(conn.invalidated)
        is_true(old.closed)
        # only the two user statements; no autobegin transaction commit,
        # and no reset rollback was emitted either.
        eq_(dbapi.sql_count, 2)
        eq_(old.commit_count, 0)
        eq_(old.rollback_count, 0)

        conn.rollback()
        eq_(conn.execute(select(1)).scalar(), 1)
        is_false(conn.invalidated)
        eq_(dbapi.connect_count, 2)
        conn.close()

    def test_executemany_disconnect_is_reported_as_multi_error(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        conn = engine.connect()
        trans = conn.begin()
        old = conn.connection.dbapi_connection

        stmt = text("insert into t (id) values (:id)")

        # first executemany succeeds, second disconnects mid-batch.
        conn.execute(stmt, [{"id": 1}, {"id": 2}])
        dbapi.shutdown("executemany")

        with expect_raises(exc.OperationalError) as ec:
            conn.execute(stmt, [{"id": 3}, {"id": 4}])
        is_true(ec.error.connection_invalidated)
        # executemany failures surface as multi-statement errors.
        is_true(ec.error.ismulti)

        is_true(conn.invalidated)
        is_true(trans.is_active)
        is_true(old.closed)
        eq_(dbapi.sql_count, 2)
        eq_(old.cursors[-1].executemany_count, 1)

        with expect_raises(exc.PendingRollbackError):
            trans.commit()

        trans.rollback()
        eq_(conn.execute(select(1)).scalar(), 1)
        is_false(conn.invalidated)
        eq_(dbapi.connect_count, 2)
        conn.close()

    def test_savepoint_rollback_disconnect_invalidates_connection(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        conn = engine.connect()
        trans = conn.begin()
        nested = conn.begin_nested()
        conn.execute(select(1))
        old = conn.connection.dbapi_connection

        # SAVEPOINT s was issued; rolling back to it now disconnects.
        dbapi.shutdown("execute")

        with expect_raises(exc.OperationalError) as ec:
            nested.rollback()
        is_true(ec.error.connection_invalidated)

        is_true(conn.invalidated)
        is_true(old.closed)
        # savepoint rollback went through cursor execute, not conn.rollback.
        eq_(old.rollback_count, 0)

        with expect_raises(exc.PendingRollbackError):
            conn.execute(select(1))

        trans.rollback()
        eq_(conn.execute(select(1)).scalar(), 1)
        is_false(conn.invalidated)
        eq_(dbapi.connect_count, 2)
        conn.close()



class DialectDisconnectDetectionTest(
    fixtures.TestBase, DisconnectFixtureMixin
):
    """The dialect.is_disconnect() decision and a handle_error listener
    overriding ctx.is_disconnect must agree with the resulting pool
    behavior."""

    dbapi = None
    engine = None

    def teardown_test(self):
        if self.engine is not None:
            self.engine.dispose()
        if self.dbapi is not None:
            self.dbapi.dispose()

    def test_handle_error_can_downgrade_disconnect(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        @event.listens_for(engine, "handle_error")
        def downgrade(ctx):
            ctx.is_disconnect = False

        conn = engine.connect()
        dbapi_conn = conn.connection.dbapi_connection
        conn.execute(select(1))

        dbapi.shutdown("execute")

        # dialect would say disconnect, but the listener downgrades; the
        # connection must not be invalidated or dropped from the pool.
        with expect_raises(exc.OperationalError) as ec:
            conn.execute(select(1))
        is_false(ec.error.connection_invalidated)
        is_false(conn.invalidated)
        is_false(dbapi_conn.closed)
        eq_(dbapi.connect_count, 1)

        event.remove(engine, "handle_error", downgrade)

    def test_handle_error_can_upgrade_non_disconnect(self):
        engine = self.engine = self._engine()
        dbapi = self.dbapi

        @event.listens_for(engine, "handle_error")
        def upgrade(ctx):
            if isinstance(ctx.original_exception, ProgrammingError):
                ctx.is_disconnect = True

        conn = engine.connect()
        old = conn.connection.dbapi_connection
        conn.execute(select(1))

        old.fail_on.add("execute")
        old.non_disconnect_ops.add("execute")

        # a plain statement error is upgraded to a disconnect, so the
        # connection is hard invalidated even though is_disconnect() alone
        # would have returned False.
        with expect_raises(exc.ProgrammingError) as ec:
            conn.execute(select(1))
        is_true(ec.error.connection_invalidated)
        is_true(conn.invalidated)
        is_true(old.closed)
        eq_(dbapi.connect_count, 1)

        event.remove(engine, "handle_error", upgrade)

# ---------------------------------------------------------------------------
# asyncio DBAPI implementation built on the generic greenlet bridge.
# ---------------------------------------------------------------------------

from sqlalchemy.connectors.asyncio import (
    AsyncAdapt_dbapi_connection as _AAC,
)
from sqlalchemy.connectors.asyncio import AsyncAdapt_dbapi_cursor as _ACur
from sqlalchemy.connectors.asyncio import AsyncAdapt_dbapi_module as _AMod
from sqlalchemy.dialects import registry as _registry
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.testing import async_test
from sqlalchemy.util.concurrency import await_ as _await


class _FakeAsyncCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []
        self.rowcount = -1
        self.lastrowid = None
        self.arraysize = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *arg):
        return False

    async def execute(self, operation, parameters=None):
        self._conn.check("execute")
        self.description = [("col_1", 0, 0, 0, 0, None, 0)]
        self._rows = [(1,)]
        self.rowcount = 1

    async def executemany(self, operation, parameters):
        self._conn.check("executemany")
        self.rowcount = len(parameters)

    async def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows

    async def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    async def close(self):
        pass

    def __aiter__(self):
        rows = self._rows

        async def _gen():
            for row in rows:
                yield row

        return _gen()


class _FakeAsyncConnection:
    def __init__(self, driver):
        self._driver = driver
        self.closed = False
        self.terminated = False
        self.fail_on = set()
        self.non_disconnect_ops = set()

    def check(self, op):
        if op in self.fail_on:
            if op in self.non_disconnect_ops:
                raise ProgrammingError(
                    "%s failed, connection still usable" % op
                )
            raise OperationalError("connection lost during %s" % op)

    def cursor(self, *arg, **kw):
        self.check("cursor")
        return _FakeAsyncCursor(self)

    async def commit(self):
        self.check("commit")

    async def rollback(self):
        self.check("rollback")

    async def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True
        self.closed = True


class _FakeAsyncDriver:
    def __init__(self):
        self.connect_count = 0
        self.connections = []
        self.stopped = False

    async def connect(self, *arg, **kw):
        self.connect_count += 1
        if self.stopped:
            raise OperationalError("database is stopped")
        conn = _FakeAsyncConnection(self)
        self.connections.append(conn)
        return conn


class _AsyncProxyCursor(_ACur):
    __slots__ = ()


class _AsyncProxyConnection(_AAC):
    _cursor_cls = _AsyncProxyCursor
    __slots__ = ()

    def terminate(self):
        self._connection.terminate()


class _AsyncProxyDBAPI(_AMod):
    def __init__(self, driver):
        super().__init__(driver)
        self.fake_driver = driver
        self.paramstyle = "qmark"
        self.Error = MockError
        self.InterfaceError = InterfaceError
        self.DatabaseError = DatabaseError
        self.OperationalError = OperationalError
        self.ProgrammingError = ProgrammingError
        self.IntegrityError = IntegrityError

    def connect(self, *arg, **kw):
        creator_fn = kw.pop("async_creator_fn", self.driver.connect)
        return _await(
            _AsyncProxyConnection.create(self, creator_fn(*arg, **kw))
        )


class _MockAsyncDialect(default.DefaultDialect):
    is_async = True
    has_terminate = True
    name = "mockasync"
    driver = "mockasync"
    supports_statement_cache = False

    @classmethod
    def import_dbapi(cls):
        return _AsyncDBAPI

    def create_connect_args(self, url):
        return [], {}

    def is_disconnect(self, e, conn, cursor):
        return isinstance(e, MockDisconnect)

    def do_terminate(self, dbapi_connection):
        # all asyncio dialects provide a sync terminate for hard
        # invalidation from inside the worker greenlet.
        dbapi_connection.terminate()


_AsyncDriver = _FakeAsyncDriver()
_AsyncDBAPI = _AsyncProxyDBAPI(_AsyncDriver)
_registry.register("mockasync", __name__, "_MockAsyncDialect")


def _async_pooled_raw_connections(engine):
    out = []
    q = engine.sync_engine.pool._pool
    # sync pools use a deque, the async pool an asyncio.Queue
    records = list(getattr(q, "queue", q._queue._queue))
    for rec in records:
        adapted = rec.dbapi_connection
        if adapted is not None:
            out.append(adapted._connection)
    return out


class AsyncGreenletBridgeDisconnectTest(fixtures.TestBase):
    """Disconnects raised across the asyncio greenlet bridge must be
    classified, invalidate the async DBAPI connection and reconnect.
    """

    def _engine(self, **kw):
        _AsyncDriver.stopped = False
        _AsyncDriver.connect_count = 0
        for conn in _AsyncDriver.connections:
            conn.fail_on = set()
            conn.non_disconnect_ops = set()
        return create_async_engine(
            "mockasync://",
            module=_AsyncDBAPI,
            _initialize=False,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
            **kw,
        )

    @async_test
    async def test_execute_disconnect_across_greenlet_bridge(self):
        engine = self._engine()
        try:
            conn = await engine.connect()
            eq_(await conn.scalar(select(1)), 1)

            raw = conn.sync_connection.connection.dbapi_connection._connection
            eq_(_AsyncDriver.connect_count, 1)

            # server goes away mid-statement; the OperationalError is
            # raised inside the greenlet and surfaces through await.
            raw.fail_on.add("execute")

            with expect_raises(exc.OperationalError) as ec:
                await conn.execute(select(1))
            is_true(ec.error.connection_invalidated)
            is_true(isinstance(ec.error.orig, MockDisconnect))

            is_true(conn.invalidated)
            is_false(conn.closed)
            # hard invalidated from inside the greenlet: do_terminate()
            # ran, rather than an awaiting close(), and the dead driver
            # connection is not referenced by the pool.
            is_true(raw.terminated)
            is_false(raw in _async_pooled_raw_connections(engine))

            # rollback clears the autobegan txn and the next statement
            # reconnects transparently across the bridge.
            await conn.rollback()
            eq_(await conn.scalar(select(1)), 1)
            is_false(conn.invalidated)
            eq_(_AsyncDriver.connect_count, 2)

            await conn.close()
        finally:
            await engine.dispose()

    @async_test
    async def test_pre_ping_disconnect_across_greenlet_bridge(self):
        engine = self._engine(pool_pre_ping=True)
        try:
            conn = await engine.connect()
            eq_(await conn.scalar(select(1)), 1)
            raw = conn.sync_connection.connection.dbapi_connection._connection
            await conn.close()

            raw.fail_on.add("execute")

            conn = await engine.connect()
            try:
                adapted = conn.sync_connection.connection.dbapi_connection
                new_raw = adapted._connection
                is_false(new_raw is raw)
                eq_(await conn.scalar(select(1)), 1)
            finally:
                await conn.close()

            is_true(raw.terminated)
            eq_(_AsyncDriver.connect_count, 2)
        finally:
            await engine.dispose()

    @async_test
    async def test_non_disconnect_error_keeps_async_connection(self):
        engine = self._engine()
        try:
            conn = await engine.connect()
            eq_(await conn.scalar(select(1)), 1)
            raw = conn.sync_connection.connection.dbapi_connection._connection

            raw.fail_on.add("execute")
            raw.non_disconnect_ops.add("execute")

            with expect_raises(exc.ProgrammingError) as ec:
                await conn.execute(select(1))
            is_false(ec.error.connection_invalidated)

            is_false(conn.invalidated)
            is_false(raw.terminated)
            eq_(_AsyncDriver.connect_count, 1)

            await conn.rollback()
            raw.fail_on.discard("execute")
            eq_(await conn.scalar(select(1)), 1)
            await conn.close()
        finally:
            await engine.dispose()
