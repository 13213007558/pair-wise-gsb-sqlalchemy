import asyncio
import gc

from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def make_engine(**kw):
    return create_async_engine(
        "sqlite+aiosqlite:///scratch_repro/test.db",
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        **kw
    )


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


async def scenario_checkout_wait():
    print("=== scenario: cancel during checkout wait")
    engine = make_engine()
    events = []
    listen(engine, events)
    conn1 = await engine.connect()

    async def get():
        async with engine.connect() as c:
            await c.execute(text("select 1"))

    t = asyncio.create_task(get())
    await asyncio.sleep(0.2)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("task cancelled ok")
    await conn1.close()
    await asyncio.sleep(0.2)
    print("after:", status(engine))
    async with engine.connect() as c:
        await c.execute(text("select 1"))
    print("reuse ok:", status(engine))
    print("events:", events)
    await engine.dispose()


async def scenario_cancel_in_block():
    print("=== scenario: cancel inside connect block")
    engine = make_engine()
    events = []
    listen(engine, events)

    async def work():
        async with engine.connect() as c:
            await c.execute(text("select 1"))
            await asyncio.sleep(10)

    t = asyncio.create_task(work())
    await asyncio.sleep(0.2)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("task cancelled ok")
    await asyncio.sleep(0.3)
    gc.collect()
    print("after:", status(engine))
    async with engine.connect() as c:
        await c.execute(text("select 1"))
    print("reuse ok:", status(engine))
    print("events:", events)
    await engine.dispose()


async def scenario_cancel_in_begin():
    print("=== scenario: cancel inside engine.begin block")
    engine = make_engine()
    events = []
    listen(engine, events)

    async def work():
        async with engine.begin() as c:
            await c.execute(text("select 1"))
            await asyncio.sleep(10)

    t = asyncio.create_task(work())
    await asyncio.sleep(0.2)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("task cancelled ok")
    await asyncio.sleep(0.3)
    gc.collect()
    print("after:", status(engine))
    async with engine.connect() as c:
        await c.execute(text("select 1"))
    print("reuse ok:", status(engine))
    print("events:", events)
    await engine.dispose()


async def main():
    await scenario_checkout_wait()
    await scenario_cancel_in_block()
    await scenario_cancel_in_begin()


asyncio.run(main())
