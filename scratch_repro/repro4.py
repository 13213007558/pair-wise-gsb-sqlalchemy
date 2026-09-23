import asyncio
import sys

sys.path.insert(0, "scratch_repro")

from fakedriver import Control

from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.ext.asyncio import create_async_engine

registry.register("fake", "fakedriver", "FakeDialect")


def status(engine):
    p = engine.sync_engine.pool
    return "checkedin=%d checkedout=%d overflow=%d" % (
        p.checkedin(), p.checkedout(), p.overflow())


async def scenario(name, body, block, warmup=True):
    print("=== scenario:", name)
    control = Control()
    engine = create_async_engine(
        "fake://", pool_size=1, max_overflow=0, pool_timeout=5,
        connect_args={"control": control},
    )
    pool_events = []
    for ev in ("connect", "checkout", "checkin", "reset", "close", "invalidate"):
        event.listen(engine.sync_engine.pool, ev,
                     lambda *a, _n=ev, **k: pool_events.append(_n))
    if warmup:
        async with engine.connect():
            pass
        control.events.clear()
        pool_events.clear()
    control.block_on = set(block)

    t = asyncio.create_task(body(engine))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if any(b in control.events for b in block):
            break
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


async def body_reset_return(engine):
    # committed before close: reset-on-return rollback at checkin
    conn = await engine.connect()
    await conn.execute(text("SELECT 1"))
    await conn.commit()
    await conn.close()


async def body_close_with_tx(engine):
    # open transaction at close: rollback inside Connection.close
    conn = await engine.connect()
    await conn.execute(text("SELECT 1"))
    await conn.close()


async def body_ctx_reset_return(engine):
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        await conn.commit()


async def main():
    await scenario("cancel during reset-on-return rollback (direct close)",
                   body_reset_return, {"rollback"})
    await scenario("cancel during tx rollback in close",
                   body_close_with_tx, {"rollback"})
    await scenario("cancel during reset-on-return rollback (ctx manager)",
                   body_ctx_reset_return, {"rollback"})


asyncio.run(main())
