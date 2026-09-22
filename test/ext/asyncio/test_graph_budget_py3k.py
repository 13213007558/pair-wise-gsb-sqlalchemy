from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import testing
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import GraphBudget
from sqlalchemy.orm import GraphBudgetExceeded
from sqlalchemy.orm import relationship
from sqlalchemy.orm import selectinload
from sqlalchemy.testing import async_test
from sqlalchemy.testing import engines
from sqlalchemy.testing import eq_
from sqlalchemy.testing.schema import Column

from .test_session_py3k import AsyncFixture

class GraphBudgetTest(AsyncFixture):

    @testing.fixture
    def async_engine(self):
        return engines.testing_engine(asyncio=True, transfer_staticpool=True)

    @async_test
    async def test_chunk_size_and_budget(self, async_engine):
        from sqlalchemy.orm import DeclarativeBase

        class Base(DeclarativeBase):
            pass

        class Parent(Base):
            __tablename__ = "gbasync_parent"
            id = Column(Integer, primary_key=True)
            children = relationship(
                "Child", order_by="Child.id"
            )

        class Child(Base):
            __tablename__ = "gbasync_child"
            id = Column(Integer, primary_key=True)
            parent_id = Column(ForeignKey("gbasync_parent.id"))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(async_engine) as session:
            session.add_all(
                [
                    Parent(
                        id=i,
                        children=[Child(id=i * 10 + j) for j in range(1, 4)],
                    )
                    for i in range(1, 9)
                ]
            )
            await session.commit()

        async with AsyncSession(async_engine) as session:
            budget = GraphBudget()
            result = await session.execute(
                select(Parent).options(
                    selectinload(Parent.children, chunk_size=3)
                ),
                execution_options={"orm_graph_budget": budget},
            )
            ps = result.scalars().all()

        eq_(budget.statement_count, 3)
        eq_(budget.parameter_count, 8)
        eq_(budget.identity_count, 32)
        eq_(
            [[c.id for c in p.children] for p in ps],
            [[i * 10 + j for j in (1, 2, 3)] for i in range(1, 9)],
        )

    @async_test
    async def test_budget_exceeded_session_reusable(self, async_engine):
        from sqlalchemy.orm import DeclarativeBase

        class Base(DeclarativeBase):
            pass

        class Parent(Base):
            __tablename__ = "gbasync_parent2"
            id = Column(Integer, primary_key=True)
            children = relationship("Child2")

        class Child2(Base):
            __tablename__ = "gbasync_child2"
            id = Column(Integer, primary_key=True)
            parent_id = Column(ForeignKey("gbasync_parent2.id"))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(async_engine) as session:
            session.add_all(
                [
                    Parent(id=i, children=[Child2(id=i * 10)])
                    for i in range(1, 9)
                ]
            )
            await session.commit()

        async with AsyncSession(async_engine) as session:
            budget = GraphBudget(max_statements=1)
            try:
                result = await session.execute(
                    select(Parent).options(
                        selectinload(Parent.children, chunk_size=3)
                    ),
                    execution_options={"orm_graph_budget": budget},
                )
                result.scalars().all()
                assert False, "expected GraphBudgetExceeded"
            except GraphBudgetExceeded:
                assert budget.statement_count <= 1

            ps = (
                (await session.execute(select(Parent))).scalars().all()
            )
            eq_(len(ps), 8)
