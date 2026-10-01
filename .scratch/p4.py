import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "u"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)


async def main():
    eng = create_async_engine("sqlite+aiosqlite://")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    s = AsyncSession(eng)
    await s.begin()
    s.add(User(name="ed"))
    await s.flush()
    nested = await s.begin_nested()
    s.add(User(name="ed"))
    try:
        await s.flush()
    except sa.exc.IntegrityError:
        print("async: IntegrityError in savepoint")
    await nested.rollback()
    s.add(User(name="jack"))
    await s.commit()
    rows = (await s.execute(sa.text("select name from u order by id"))).scalars().all()
    print("async: rows:", rows)
    await s.close()

    # two-level
    s = AsyncSession(eng)
    await s.begin()
    n1 = await s.begin_nested()
    s.add(User(name="l1"))
    await s.flush()
    n2 = await s.begin_nested()
    s.add(User(name="l2"))
    await s.flush()
    await n2.rollback()
    await n1.commit()
    await s.commit()
    rows = (await s.execute(sa.text("select name from u order by id"))).scalars().all()
    print("async two-level rows:", rows)
    await s.close()
    await eng.dispose()


asyncio.run(main())
