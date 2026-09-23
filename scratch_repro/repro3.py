import asyncio
import sys

sys.path.insert(0, "scratch_repro")

from fakedriver import Control

from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.ext.asyncio import create_async_engine

registry.register("fake", "fakedriver", "FakeDialect")


async def main():
    control = Control()
    control.block_on.add("rollback")

    orig_gate = control.gate
    import traceback

    async def gate(name):
        if name == "rollback":
            traceback.print_stack()
        await orig_gate(name)

    control.gate = gate
    engine = create_async_engine(
        "fake://",
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"control": control},
    )
    pool = engine.sync_engine.pool
    for name in ("connect", "checkout", "checkin", "reset", "close", "invalidate"):
        event.listen(
            pool, name,
            lambda *a, _n=name, **k: print("POOL EVENT:", _n),
        )

    async def body():
        print("body: entering connect")
        async with engine.connect() as c:
            print("body: connected")
            await c.execute(text("SELECT 1"))
            print("body: executed")
            await c.commit()
            print("body: committed")
        print("body: exited")

    t = asyncio.create_task(body())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "rollback" in control.events:
            break
    print("events so far:", control.events)
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
    p = engine.sync_engine.pool
    print("after: checkedin=%d checkedout=%d overflow=%d" % (p.checkedin(), p.checkedout(), p.overflow()))
    print("driver events:", control.events)
    async with engine.connect() as c:
        await c.execute(text("SELECT 1"))
    print("reuse ok")
    print("driver events:", control.events)
    await engine.dispose()


asyncio.run(main())
