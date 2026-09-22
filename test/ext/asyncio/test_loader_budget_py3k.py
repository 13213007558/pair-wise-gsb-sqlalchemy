# test/ext/asyncio/test_loader_budget_py3k.py
"""Async tests for selectinload chunking and the eager-load graph
visit budget."""

from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import testing
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import LoaderBudget
from sqlalchemy.orm import relationship
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import LoaderBudgetExceeded
from sqlalchemy.testing import async_test
from sqlalchemy.testing import engines
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures
from sqlalchemy.testing.assertions import expect_raises_message
from sqlalchemy.testing.entities import ComparableEntity
from sqlalchemy.testing.schema import Column
from .test_engine_py3k import AsyncFixture as _AsyncFixture


class AsyncLoaderBudgetTest(_AsyncFixture, fixtures.DeclarativeMappedTest):
    __requires__ = ("async_dialect",)

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class A(ComparableEntity, Base):
            __tablename__ = "a"
            id = Column(Integer, primary_key=True)
            bs = relationship("B", order_by="B.id", back_populates="a")

        class B(ComparableEntity, Base):
            __tablename__ = "b"
            id = Column(Integer, primary_key=True)
            a_id = Column(ForeignKey("a.id"))
            a = relationship("A", back_populates="bs")

    @classmethod
    def insert_data(cls, connection):
        from sqlalchemy.orm import Session

        A, B = cls.classes("A", "B")
        session = Session(connection)
        session.add_all(
            [
                A(id=i, bs=[B(id=(i * 10) + j) for j in range(1, 4)])
                for i in range(1, 11)
            ]
        )
        session.commit()

    @testing.fixture
    def async_engine(self):
        return engines.testing_engine(asyncio=True, transfer_staticpool=True)

    @async_test
    async def test_chunk_size_and_budget_counters(self, async_engine):
        A = self.classes.A
        budget = LoaderBudget()
        async with AsyncSession(async_engine) as sess:
            result = await sess.scalars(
                select(A)
                .options(selectinload(A.bs, chunk_size=4))
                .order_by(A.id),
                execution_options={"orm_eager_load_budget": budget},
            )
            objs = result.all()
            eq_(len(objs), 10)
            for a in objs:
                eq_(len(a.bs), 3)
        eq_(budget.batches, 3)
        eq_(budget.parameters, 10)
        eq_(budget.visits, 10)

    @async_test
    async def test_execution_option_chunk_size(self, async_engine):
        A = self.classes.A
        budget = LoaderBudget()
        async with AsyncSession(async_engine) as sess:
            result = await sess.scalars(
                select(A).options(selectinload(A.bs)).order_by(A.id),
                execution_options={
                    "orm_selectinload_chunk_size": 5,
                    "orm_eager_load_budget": budget,
                },
            )
            eq_(len(result.all()), 10)
        eq_(budget.batches, 2)

    @async_test
    async def test_budget_exceeded_session_recovers(self, async_engine):
        A = self.classes.A
        async with AsyncSession(async_engine) as sess:
            with expect_raises_message(
                LoaderBudgetExceeded,
                "SQL batch limit of 1 would be exceeded",
            ):
                result = await sess.scalars(
                    select(A).options(selectinload(A.bs, chunk_size=4)),
                    execution_options={
                        "orm_eager_load_budget": {"max_batches": 1}
                    },
                )
                result.all()
            await sess.rollback()
            result = await sess.scalars(select(A))
            eq_(len(result.all()), 10)

    @async_test
    async def test_concurrent_executions_are_isolated(self, async_engine):
        # interleave two executions on one session: the first streams
        # with yield_per and pauses mid-stream, then a second execution
        # runs to completion.  each execution needs its own budget
        # tracker; if dict-configured budgets were shared, the combined
        # batch count would exceed the configured limits.
        A = self.classes.A

        stmt = (
            select(A)
            .options(selectinload(A.bs))
            .order_by(A.id)
            .execution_options(yield_per=5)
        )
        async with AsyncSession(async_engine) as sess:
            stream = await sess.stream(
                stmt,
                execution_options={
                    "orm_eager_load_budget": {"max_batches": 2}
                },
            )
            seen = 0
            async for a in stream.scalars():
                seen += 1
                break

            stmt2 = select(A).options(selectinload(A.bs)).order_by(A.id)
            result = await sess.scalars(
                stmt2,
                execution_options={
                    "orm_eager_load_budget": {"max_batches": 1}
                },
            )
            eq_(len(result.all()), 10)

            async for a in stream.scalars():
                seen += 1
            eq_(seen, 10)
