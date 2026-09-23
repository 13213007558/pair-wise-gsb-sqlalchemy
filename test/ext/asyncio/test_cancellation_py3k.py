# test/ext/asyncio/test_cancellation_py3k.py
# Copyright (C) 2005-2025 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""Tests that asyncio task cancellation at any point of a connection's
lifecycle (pool checkout wait, driver connect, execute, fetch, commit,
result close, reset-on-return) leaves the connection pool ledger
consistent: every checked out connection is either returned to the pool
or invalidated and closed, exactly once, and the pool remains usable
for subsequent checkouts.

"""

import asyncio

from sqlalchemy import event
from sqlalchemy import testing
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.testing import async_test
from sqlalchemy.testing import engines
from sqlalchemy.testing import eq_
from sqlalchemy.testing import expect_raises
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_true
from sqlalchemy.util import await_only


class PoolLedger:
    """Records pool events so that the sequence of connect / reset /
    invalidate / close / checkin operations can be replayed and asserted
    for each cancellation point."""

    def __init__(self):
        self.events = []

    def listen(self, engine):
        sync_engine = engine.sync_engine
        for name in (
            "connect",
            "checkout",
            "checkin",
            "reset",
            "invalidate",
            "soft_invalidate",
            "close",
            "detach",
        ):
            event.listen(sync_engine, name, self._listener(name))

    def _listener(self, name):
        def listen(*arg):
            self.events.append(name)

        return listen

    def count(self, name):
        return self.events.count(name)


async def wait_ledger(engine, checkedout, checkedin=None, timeout=5):
    """poll the pool ledger until it reaches the expected state."""
    pool = engine.sync_engine.pool
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        if pool.checkedout() == checkedout and (
            checkedin is None or pool.checkedin() == checkedin
        ):
            return
        if loop.time() > deadline:
            raise AssertionError(
                "pool ledger did not reach checkedout=%r checkedin=%r; "
                "currently checkedout=%r checkedin=%r overflow=%r"
                % (
                    checkedout,
                    checkedin,
                    pool.checkedout(),
                    pool.checkedin(),
                    pool.overflow(),
                )
            )
        await asyncio.sleep(0.01)


class CancelAtEachPointTest(fixtures.TestBase):
    __requires__ = ("async_dialect",)

    def _engine(self, **kw):
        options = {
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
            "pool_timeout": 2,
            "use_reaper": False,
        }
        options.update(kw)
        return engines.testing_engine(asyncio=True, options=options)

    @async_test
    async def test_cancel_checkout_wait(self):
        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))

                # pool is exhausted; this task blocks waiting for a
                # connection to be returned
                task = asyncio.ensure_future(engine.connect().start())
                await asyncio.sleep(0.1)
                task.cancel()
                with expect_raises(asyncio.CancelledError):
                    await task

                # ledger is unchanged; only the holder is checked out
                eq_(engine.sync_engine.pool.checkedout(), 1)
                eq_(ledger.count("checkout"), 1)

            await wait_ledger(engine, 0, 1)

            # pool is still usable
            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)

            eq_(ledger.count("connect"), 1)
            eq_(ledger.count("checkout"), 2)
            eq_(ledger.count("checkin"), 2)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_driver_connect(self):
        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        connect_entered = asyncio.Event()
        control = {"arm": True}

        @event.listens_for(engine.sync_engine, "connect")
        def on_connect(dbapi_connection, connection_record):
            if control["arm"]:
                connect_entered.set()
                await_only(asyncio.sleep(30))

        try:
            task = asyncio.ensure_future(engine.connect().start())
            await asyncio.wait_for(connect_entered.wait(), 5)
            task.cancel()
            with expect_raises(asyncio.CancelledError):
                await task
            control["arm"] = False

            # the half-initialized connection was closed and not leaked;
            # the fresh record was discarded, restoring the overflow
            # count, so the pool ledger is closed
            eq_(engine.sync_engine.pool.checkedout(), 0)
            eq_(engine.sync_engine.pool.checkedin(), 0)
            eq_(ledger.events, ["connect", "close"])

            # pool is still usable
            async with engine.connect() as conn:
                eq_(await conn.scalar(text("select 1")), 1)
            eq_(
                ledger.events,
                [
                    "connect",
                    "close",
                    "connect",
                    "checkout",
                    "reset",
                    "checkin",
                ],
            )
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_execute(self):
        """cancellation before the driver execute call (in an event
        hook) returns the connection to the pool normally."""

        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        execute_entered = asyncio.Event()
        control = {"arm": True}

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            if control["arm"]:
                execute_entered.set()
                await_only(asyncio.sleep(30))

        try:
            async with engine.connect() as conn:
                task = asyncio.ensure_future(conn.execute(text("select 1")))
                await asyncio.wait_for(execute_entered.wait(), 5)
                task.cancel()
                with expect_raises(asyncio.CancelledError):
                    await task
                control["arm"] = False

            # the driver was never called, so the connection is reset
            # and returned to the pool exactly once
            await wait_ledger(engine, 0, 1)
            eq_(
                ledger.events,
                ["connect", "checkout", "reset", "checkin"],
            )

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_driver_execute(self):
        """cancellation within the driver execute call invalidates the
        connection, which is closed and returned exactly once."""

        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        execute_entered = asyncio.Event()
        control = {"arm": True}

        @event.listens_for(engine.sync_engine, "do_execute")
        def do_execute(cursor, statement, parameters, context):
            if control["arm"]:
                execute_entered.set()
                await_only(asyncio.sleep(30))
            return False

        try:
            async with engine.connect() as conn:
                task = asyncio.ensure_future(conn.execute(text("select 1")))
                await asyncio.wait_for(execute_entered.wait(), 5)
                task.cancel()
                with expect_raises(asyncio.CancelledError):
                    await task
                control["arm"] = False

            # cancellation during the driver call invalidates the
            # connection; it is closed and the record returned exactly once
            await wait_ledger(engine, 0, 1)
            eq_(
                ledger.events,
                ["connect", "checkout", "invalidate", "close", "checkin"],
            )

            # pool is still usable; the invalidated record is recycled
            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
            eq_(ledger.count("connect"), 2)
            eq_(ledger.count("checkout"), 2)
            eq_(ledger.count("checkin"), 2)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_streaming_fetch(self):
        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        entered = asyncio.Event()

        async def worker():
            async with engine.connect() as conn:
                result = await conn.stream(
                    text("select 1 union all select 2")
                )
                async for row in result:
                    entered.set()
                    await asyncio.sleep(30)

        try:
            task = asyncio.ensure_future(worker())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with expect_raises(asyncio.CancelledError):
                await task

            # the interrupted streaming result does not prevent the
            # connection from being reset and returned exactly once
            await wait_ledger(engine, 0, 1)
            eq_(ledger.count("connect"), 1)
            eq_(ledger.count("checkout"), 1)
            eq_(ledger.count("reset"), 1)
            eq_(ledger.count("checkin"), 1)
            eq_(ledger.count("invalidate"), 0)

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_commit(self):
        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        commit_entered = asyncio.Event()
        control = {"arm": True}

        @event.listens_for(engine.sync_engine, "commit")
        def on_commit(conn):
            if control["arm"]:
                commit_entered.set()
                await_only(asyncio.sleep(30))

        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
                task = asyncio.ensure_future(conn.commit())
                await asyncio.wait_for(commit_entered.wait(), 5)
                task.cancel()
                with expect_raises(asyncio.CancelledError):
                    await task
                control["arm"] = False

            # the connection context manager exit rolls back and returns
            # the connection to the pool exactly once
            await wait_ledger(engine, 0, 1)
            eq_(ledger.count("reset"), 1)
            eq_(ledger.count("checkin"), 1)
            eq_(ledger.count("invalidate"), 0)

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_reset_on_return(self):
        """cancellation while the pool is resetting a connection for
        return (rollback-on-return) must still invalidate, close and
        check in the connection record exactly once."""

        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        reset_entered = asyncio.Event()
        control = {"arm": True}

        @event.listens_for(engine.sync_engine, "reset")
        def on_reset(dbapi_connection, connection_record, reset_state):
            if control["arm"] and reset_state.asyncio_safe:
                reset_entered.set()
                await_only(asyncio.sleep(30))

        try:
            conn = await engine.connect()
            await conn.execute(text("select 1"))

            task = asyncio.ensure_future(conn.close())
            await asyncio.wait_for(reset_entered.wait(), 5)
            task.cancel()
            with expect_raises(asyncio.CancelledError):
                await task
            control["arm"] = False

            # the record was invalidated, closed and returned to the
            # pool exactly once, synchronously within the cancelled task
            eq_(engine.sync_engine.pool.checkedout(), 0)
            eq_(engine.sync_engine.pool.checkedin(), 1)
            is_true(conn.closed)
            eq_(
                ledger.events,
                [
                    "connect",
                    "checkout",
                    "reset",
                    "invalidate",
                    "close",
                    "checkin",
                ],
            )

            # pool is still usable; the invalidated record is recycled
            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
            eq_(ledger.count("connect"), 2)
            eq_(ledger.count("checkout"), 2)
            eq_(ledger.count("checkin"), 2)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_session_close(self):
        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        reset_entered = asyncio.Event()
        control = {"arm": True}

        @event.listens_for(engine.sync_engine, "reset")
        def on_reset(dbapi_connection, connection_record, reset_state):
            if control["arm"] and reset_state.asyncio_safe:
                reset_entered.set()
                await_only(asyncio.sleep(30))

        try:
            sess = AsyncSession(engine)
            await sess.execute(text("select 1"))

            task = asyncio.ensure_future(sess.close())
            await asyncio.wait_for(reset_entered.wait(), 5)
            task.cancel()
            with expect_raises(asyncio.CancelledError):
                await task
            control["arm"] = False

            eq_(engine.sync_engine.pool.checkedout(), 0)
            eq_(engine.sync_engine.pool.checkedin(), 1)
            eq_(ledger.count("reset"), 1)
            eq_(ledger.count("invalidate"), 1)
            eq_(ledger.count("close"), 1)
            eq_(ledger.count("checkin"), 1)

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_with_savepoint(self):
        """savepoints do not change the pool ledger when the surrounding
        task is cancelled."""

        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        entered = asyncio.Event()

        async def worker():
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
                await conn.begin_nested()
                await conn.execute(text("select 1"))
                entered.set()
                await asyncio.sleep(30)

        try:
            task = asyncio.ensure_future(worker())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with expect_raises(asyncio.CancelledError):
                await task

            await wait_ledger(engine, 0, 1)
            eq_(ledger.count("connect"), 1)
            eq_(ledger.count("checkout"), 1)
            eq_(ledger.count("reset"), 1)
            eq_(ledger.count("checkin"), 1)

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()

    @async_test
    async def test_cancel_inside_stream_ctxmanager(self):
        engine = self._engine()
        ledger = PoolLedger()
        ledger.listen(engine)
        entered = asyncio.Event()

        async def worker():
            async with engine.connect() as conn:
                async with conn.stream(text("select 1")) as result:
                    entered.set()
                    await asyncio.sleep(30)

        try:
            task = asyncio.ensure_future(worker())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with expect_raises(asyncio.CancelledError):
                await task

            await wait_ledger(engine, 0, 1)
            eq_(ledger.count("checkout"), 1)
            eq_(ledger.count("reset"), 1)
            eq_(ledger.count("checkin"), 1)

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()

    @async_test
    async def test_concurrent_tasks_cancel(self):
        """cancelling some of several concurrent tasks does not change
        the final pool ledger counts."""

        engine = self._engine(pool_size=2, max_overflow=1)
        ledger = PoolLedger()
        ledger.listen(engine)
        entered = asyncio.Event()
        release = asyncio.Event()
        counter = {"n": 0}

        async def worker():
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
                counter["n"] += 1
                if counter["n"] >= 3:
                    entered.set()
                await release.wait()

        try:
            tasks = [asyncio.ensure_future(worker()) for _ in range(3)]
            await asyncio.wait_for(entered.wait(), 5)
            tasks[0].cancel()
            tasks[1].cancel()
            with expect_raises(asyncio.CancelledError):
                await tasks[0]
            with expect_raises(asyncio.CancelledError):
                await tasks[1]
            release.set()
            await tasks[2]

            await wait_ledger(engine, 0)
            eq_(ledger.count("checkout"), 3)
            eq_(ledger.count("checkin"), 3)
            eq_(ledger.count("reset"), 3)

            async with engine.connect() as conn2:
                eq_(await conn2.scalar(text("select 1")), 1)
        finally:
            await engine.dispose()
