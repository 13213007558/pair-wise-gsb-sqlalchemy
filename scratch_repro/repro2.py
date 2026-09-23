import asyncio
import sys

sys.path.insert(0, "scratch_repro")

from fakedriver import Control
from fakedriver import FakeDBAPI

from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.ext.asyncio import create_async_engine

registry.register("fake", "fakedriver", "FakeDialect")


def make_engine(control, **kw):
    kw.setdefault("pool_size", 1)
    kw.setdefault("max_overflow", 0)
    kw.setdefault("pool_timeout", 5)
    kw.setdefault("connect_args", {"control": control})
    engine = create_async_engine("fake://", **kw)
    return engine


def status(engine):
    p = engine.sync_engine.pool
    return "checkedin=%d checkedout=%d overflow=%d" % (
        p.checkedin(),
        p.checkedout(),
        p.overflow(),
    )


def listen(engine, events):
    for name in ("connect", "checkout", "checkin", "reset", "close", "invalidate"):
        event.listen(
            engine.sync_engine.pool,
            name,
            lambda *a, _n=name, **k: events.append(_n),
        )


async def run_cancel_at(point, body):
    print("=== cancel at:", point)
    control = Control()
    control.block_on.add(point)
    engine = make_engine(control)
    pool_events = []
    listen(engine, pool_events)

    t = asyncio.create_task(body(engine))
    # wait until the driver reaches the blocking point
    for _ in range(100):
        await asyncio.sleep(0.01)
        if point in control.events:
            break
    assert point in control.events, "never reached %s: %s" % (point, control.events)
    t.cancel()
    control.release.set()
    try:
        await t
        print("task completed without CancelledError")
    except asyncio.CancelledError:
        print("task cancelled ok")
    except BaseException as e:
        print("task raised:", type(e).__name__, e)
    await asyncio.sleep(0.5)
    print("after:", status(engine))
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
        print("reuse ok:", status(engine))
    except Exception as e:
        print("REUSE FAILED:", type(e).__name__, e)
    print("driver events:", control.events)
    print("pool events:", pool_events)
    await engine.dispose()
    print()


async def body_connect(engine):
    async with engine.connect():
        pass


async def body_execute(engine):
    async with engine.connect() as c:
        await c.execute(text("SELECT 1"))


async def body_stream(engine):
    async with engine.connect() as c:
        result = await c.stream(text("SELECT 1"))
        await result.fetchmany(1)


async def body_commit(engine):
    async with engine.begin() as c:
        await c.execute(text("SELECT 1"))
        # commit happens on __aexit__


async def body_reset_return(engine):
    # no open transaction at close: reset-on-return rollback
    async with engine.connect() as c:
        await c.execute(text("SELECT 1"))
        await c.commit()


async def body_close_rollback(engine):
    # open transaction at close: rollback in Connection.close
    async with engine.connect() as c:
        await c.execute(text("SELECT 1"))


async def main():
    await run_cancel_at("connect", body_connect)
    await run_cancel_at("execute", body_execute)
    await run_cancel_at("fetchmany", body_stream)
    await run_cancel_at("commit", body_commit)
    await run_cancel_at("rollback", body_reset_return)
    await run_cancel_at("rollback", body_close_rollback)


asyncio.run(main())
