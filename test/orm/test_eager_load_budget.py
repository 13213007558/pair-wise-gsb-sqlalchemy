# test/orm/test_eager_load_budget.py
"""Tests for configurable selectinload batch chunking and the eager-load
graph visit budget (LoaderBudget)."""

from sqlalchemy import exc as sa_exc
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import testing
from sqlalchemy.orm import aliased
from sqlalchemy.orm import contains_eager
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import LoaderBudget
from sqlalchemy.orm import relationship
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import LoaderBudgetExceeded
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures
from sqlalchemy.testing.assertions import expect_raises_message
from sqlalchemy.testing.assertsql import CompiledSQL
from sqlalchemy.testing.entities import ComparableEntity
from sqlalchemy.testing.fixtures import fixture_session
from sqlalchemy.testing.schema import Column


class ChunkSizeOptionTest(fixtures.DeclarativeMappedTest):
    """chunk_size as a loader option / execution option."""

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
        A, B = cls.classes("A", "B")
        from sqlalchemy.orm import Session

        session = Session(connection)
        session.add_all(
            [
                A(id=i, bs=[B(id=(i * 10) + j) for j in range(1, 4)])
                for i in range(1, 11)
            ]
        )
        session.commit()

    def test_loader_option_chunk_size(self):
        A, B = self.classes("A", "B")
        session = fixture_session()
        budget = LoaderBudget()

        def go():
            for a in session.scalars(
                select(A)
                .options(selectinload(A.bs, chunk_size=4))
                .order_by(A.id),
                execution_options={"orm_eager_load_budget": budget},
            ):
                a.bs

        self.assert_sql_execution(
            testing.db,
            go,
            CompiledSQL("SELECT a.id FROM a ORDER BY a.id", {}),
            CompiledSQL(
                "SELECT b.a_id AS b_a_id, b.id AS b_id "
                "FROM b WHERE b.a_id IN "
                "(__[POSTCOMPILE_primary_keys]) ORDER BY b.id",
                {"primary_keys": [1, 2, 3, 4]},
            ),
            CompiledSQL(
                "SELECT b.a_id AS b_a_id, b.id AS b_id "
                "FROM b WHERE b.a_id IN "
                "(__[POSTCOMPILE_primary_keys]) ORDER BY b.id",
                {"primary_keys": [5, 6, 7, 8]},
            ),
            CompiledSQL(
                "SELECT b.a_id AS b_a_id, b.id AS b_id "
                "FROM b WHERE b.a_id IN "
                "(__[POSTCOMPILE_primary_keys]) ORDER BY b.id",
                {"primary_keys": [9, 10]},
            ),
        )
        eq_(budget.batches, 3)
        eq_(budget.parameters, 10)
        eq_(budget.visits, 10)

    def test_execution_option_chunk_size(self):
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(A).options(selectinload(A.bs)).order_by(A.id),
            execution_options={
                "orm_selectinload_chunk_size": 5,
                "orm_eager_load_budget": budget,
            },
        ).all()
        eq_(len(result), 10)
        for a in result:
            eq_(len(a.bs), 3)
        eq_(budget.batches, 2)
        eq_(budget.parameters, 10)

    def test_loader_option_takes_precedence_over_execution_option(self):
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        session.scalars(
            select(A).options(selectinload(A.bs, chunk_size=4)).order_by(A.id),
            execution_options={
                "orm_selectinload_chunk_size": 500,
                "orm_eager_load_budget": budget,
            },
        ).all()
        eq_(budget.batches, 3)

    def test_exact_batch_boundary(self):
        # 10 rows at chunk size 5: exactly two batches, no empty third
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()

        def go():
            session.scalars(
                select(A)
                .options(selectinload(A.bs, chunk_size=5))
                .order_by(A.id),
                execution_options={"orm_eager_load_budget": budget},
            ).all()

        self.assert_sql_count(testing.db, go, 3)
        eq_(budget.batches, 2)
        eq_(budget.parameters, 10)

    def test_m2o_chunk_size_dedups_keys(self):
        # 30 B rows reference 10 A rows; m2o selectin dedups the FK keys
        # so each batch carries unique keys only
        A, B = self.classes("A", "B")
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(B).options(selectinload(B.a, chunk_size=4)).order_by(B.id),
            execution_options={"orm_eager_load_budget": budget},
        ).all()
        eq_(len(result), 30)
        for b in result:
            eq_(b.a.id, b.id // 10)
        # 10 distinct keys, 4 per batch -> 3 batches
        eq_(budget.batches, 3)
        eq_(budget.parameters, 10)
        eq_(budget.visits, 30)

    def test_default_chunk_size_unchanged(self):
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        session.scalars(
            select(A).options(selectinload(A.bs)),
            execution_options={"orm_eager_load_budget": budget},
        ).all()
        eq_(budget.batches, 1)

    def test_invalid_chunk_size_loader_option(self):
        A = self.classes.A
        for bad in (0, -1, "100"):
            expect_raises_message(
                sa_exc.ArgumentError,
                "selectinload chunk_size must be a positive integer",
                lambda: selectinload(A.bs, chunk_size=bad),
            )

    def test_invalid_chunk_size_execution_option(self):
        A = self.classes.A
        session = fixture_session()
        expect_raises_message(
            sa_exc.ArgumentError,
            "selectinload chunk size must be a positive integer",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs)),
                execution_options={"orm_selectinload_chunk_size": 0},
            ).all(),
        )


class CompositeKeyChunkTest(fixtures.DeclarativeMappedTest):
    """chunk_size is a bound-parameter budget: composite keys consume
    multiple parameters per key tuple, so batches are scaled down."""

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class C(ComparableEntity, Base):
            __tablename__ = "c"
            pk1 = Column(Integer, primary_key=True)
            pk2 = Column(Integer, primary_key=True)
            ds = relationship("D", order_by="D.id", back_populates="c")

        class D(ComparableEntity, Base):
            __tablename__ = "d"
            id = Column(Integer, primary_key=True)
            c_pk1 = Column(Integer)
            c_pk2 = Column(Integer)
            __table_args__ = (
                ForeignKeyConstraint(["c_pk1", "c_pk2"], ["c.pk1", "c.pk2"]),
            )
            c = relationship("C", back_populates="ds")

    @classmethod
    def insert_data(cls, connection):
        from sqlalchemy.orm import Session

        C, D = cls.classes("C", "D")
        session = Session(connection)
        session.add_all(
            [
                C(pk1=i, pk2=i * 100, ds=[D(id=(i * 10) + j) for j in (1, 2)])
                for i in range(1, 6)
            ]
        )
        session.commit()

    def test_composite_pk_chunking_counts_parameters(self):
        # chunk_size=4 with two-column keys -> two key tuples per batch,
        # so five parents need three batches of 4, 4, 2 parameters
        C = self.classes.C
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(C)
            .options(selectinload(C.ds, chunk_size=4))
            .order_by(C.pk1),
            execution_options={"orm_eager_load_budget": budget},
        ).all()
        eq_(len(result), 5)
        for c in result:
            eq_(len(c.ds), 2)
        eq_(budget.batches, 3)
        eq_(budget.parameters, 10)
        eq_(budget.visits, 5)

    def test_composite_m2o_chunking(self):
        C, D = self.classes("C", "D")
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(D).options(selectinload(D.c, chunk_size=3)).order_by(D.id),
            execution_options={"orm_eager_load_budget": budget},
        ).all()
        eq_(len(result), 10)
        for d in result:
            eq_(d.c.pk1, d.id // 10)
        # five distinct composite keys, one key (2 params) per batch
        eq_(budget.batches, 5)
        eq_(budget.parameters, 10)
        eq_(budget.visits, 10)


class LoaderBudgetTest(fixtures.DeclarativeMappedTest):
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
        # one B with no parent, for none_states coverage
        session.add(B(id=999, a_id=None))
        session.commit()

    def test_max_batches_exceeded(self):
        A = self.classes.A
        session = fixture_session()
        expect_raises_message(
            LoaderBudgetExceeded,
            "SQL batch limit of 2 would be exceeded",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs, chunk_size=4)),
                execution_options={
                    "orm_eager_load_budget": {"max_batches": 2}
                },
            ).all(),
        )
        # session remains usable after the failed load
        session.rollback()
        eq_(len(session.scalars(select(A)).all()), 10)

    def test_max_parameters_exceeded(self):
        A = self.classes.A
        session = fixture_session()
        expect_raises_message(
            LoaderBudgetExceeded,
            "bound parameter limit of 6 would be exceeded",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs, chunk_size=4)),
                execution_options={
                    "orm_eager_load_budget": {"max_parameters": 6}
                },
            ).all(),
        )

    def test_max_visits_exceeded(self):
        A = self.classes.A
        session = fixture_session()
        expect_raises_message(
            LoaderBudgetExceeded,
            "object identity visit limit of 5 would be exceeded",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs, chunk_size=4)),
                execution_options={"orm_eager_load_budget": {"max_visits": 5}},
            ).all(),
        )

    def test_budget_instance_counters_accumulate(self):
        # a user-supplied budget accumulates across executions, which
        # makes the totals directly inspectable
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        for _ in range(2):
            session.scalars(
                select(A).options(selectinload(A.bs, chunk_size=5)),
                execution_options={"orm_eager_load_budget": budget},
            ).all()
        eq_(budget.batches, 4)
        eq_(budget.parameters, 20)
        eq_(budget.visits, 20)

    def test_dict_budget_is_per_execution(self):
        # each execution gets its own tracker when a dict is given, so
        # sequential and concurrent executions do not interfere
        A = self.classes.A
        session = fixture_session()
        stmt = select(A).options(selectinload(A.bs, chunk_size=5))
        for _ in range(3):
            result = session.scalars(
                stmt,
                execution_options={
                    "orm_eager_load_budget": {"max_batches": 2}
                },
            ).all()
            eq_(len(result), 10)

    def test_invalid_budget_option(self):
        A = self.classes.A
        session = fixture_session()
        expect_raises_message(
            sa_exc.ArgumentError,
            "orm_eager_load_budget execution option must be a "
            "LoaderBudget or a mapping",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs)),
                execution_options={"orm_eager_load_budget": 42},
            ).all(),
        )
        expect_raises_message(
            sa_exc.ArgumentError,
            "Unknown LoaderBudget limit",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs)),
                execution_options={"orm_eager_load_budget": {"max_rows": 3}},
            ).all(),
        )

    def test_invalid_budget_limit_values(self):
        expect_raises_message(
            sa_exc.ArgumentError,
            "LoaderBudget max_batches must be a non-negative integer",
            lambda: LoaderBudget(max_batches=-1),
        )

    def test_empty_result_set_emits_no_batches(self):
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(A).options(selectinload(A.bs)).where(A.id > 1000),
            execution_options={"orm_eager_load_budget": budget},
        ).all()
        eq_(result, [])
        eq_(budget.batches, 0)
        eq_(budget.visits, 0)

    def test_empty_collections_and_none_states(self):
        # m2o load where one B has NULL FK: the None state is populated
        # without any SQL, and is still counted as a visit
        B = self.classes.B
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(B).where(B.id == 999).options(selectinload(B.a)),
            execution_options={"orm_eager_load_budget": budget},
        ).all()
        eq_(len(result), 1)
        eq_(result[0].a, None)
        eq_(budget.batches, 0)
        eq_(budget.visits, 1)

    def test_budget_exceeded_result_cursor_cleanup(self):
        # after a budget failure mid-load, the session/connection are
        # still usable, matching existing error semantics
        A = self.classes.A
        session = fixture_session()
        expect_raises_message(
            LoaderBudgetExceeded,
            "SQL batch limit of 1 would be exceeded",
            lambda: session.scalars(
                select(A).options(selectinload(A.bs, chunk_size=4)),
                execution_options={
                    "orm_eager_load_budget": {"max_batches": 1}
                },
            ).all(),
        )
        eq_(session.execute(select(1)).scalar(), 1)
        session.rollback()
        eq_(len(session.scalars(select(A)).all()), 10)


class CyclicGraphTest(fixtures.DeclarativeMappedTest):
    """Cyclic and repeated paths are de-duplicated via the identity map;
    the budget constrains total graph visits."""

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class Node(ComparableEntity, Base):
            __tablename__ = "node"
            id = Column(Integer, primary_key=True)
            parent_id = Column(ForeignKey("node.id"))
            children = relationship(
                "Node", back_populates="parent", order_by="Node.id"
            )
            parent = relationship(
                "Node", back_populates="children", remote_side=[id]
            )

    @classmethod
    def insert_data(cls, connection):
        from sqlalchemy.orm import Session

        Node = cls.classes.Node
        session = Session(connection)
        # a tree: 1 -> 2 -> 4, 1 -> 3
        session.add(
            Node(
                id=1, children=[Node(id=2, children=[Node(id=4)]), Node(id=3)]
            )
        )
        # a data cycle: 10 <-> 11
        n10, n11 = Node(id=10), Node(id=11)
        session.add_all([n10, n11])
        session.commit()
        n10.children.append(n11)
        session.commit()
        n11.children.append(n10)
        session.commit()

    def test_recursive_load_graph_and_identity(self):
        Node = self.classes.Node
        session = fixture_session()
        budget = LoaderBudget()
        node = session.scalars(
            select(Node)
            .where(Node.id == 1)
            .options(selectinload(Node.children, recursion_depth=3)),
            execution_options={"orm_eager_load_budget": budget},
        ).one()
        eq_([c.id for c in node.children], [2, 3])
        eq_([c.id for c in node.children[0].children], [4])
        # repeated paths resolve to the same instances
        assert node.children[0].parent is node
        assert node.children[0].children[0].parent is node.children[0]
        eq_(budget.batches, 3)
        eq_(budget.parameters, 4)
        eq_(budget.visits, 4)

    def test_budget_terminates_data_cycle(self):
        # unlimited recursion over cyclic data would otherwise loop
        # forever; the budget caps total graph visits
        Node = self.classes.Node
        session = fixture_session()
        expect_raises_message(
            LoaderBudgetExceeded,
            "SQL batch limit of 5 would be exceeded",
            lambda: session.scalars(
                select(Node)
                .where(Node.id == 10)
                .options(selectinload(Node.children, recursion_depth=-1)),
                execution_options={
                    "orm_eager_load_budget": {"max_batches": 5}
                },
            ).all(),
        )
        session.rollback()

    def test_data_cycle_within_budget(self):
        # a finite recursion depth over cyclic data stays within budget
        # and de-duplicates the cycled entities
        Node = self.classes.Node
        session = fixture_session()
        budget = LoaderBudget(max_batches=10, max_visits=10)
        node = session.scalars(
            select(Node)
            .where(Node.id == 10)
            .options(selectinload(Node.children, recursion_depth=2)),
            execution_options={"orm_eager_load_budget": budget},
        ).one()
        child = node.children[0]
        eq_(child.id, 11)
        # the cycle comes back to the same identity-mapped instance
        assert child.children[0] is node
        assert child.parent is node


class DiamondGraphTest(fixtures.DeclarativeMappedTest):
    """Distinct loader options on converging paths are not merged with
    each other, while shared entities de-duplicate via identity map."""

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class A(ComparableEntity, Base):
            __tablename__ = "a"
            id = Column(Integer, primary_key=True)
            bs = relationship("B", back_populates="a")
            cs = relationship("C", back_populates="a")

        class B(ComparableEntity, Base):
            __tablename__ = "b"
            id = Column(Integer, primary_key=True)
            a_id = Column(ForeignKey("a.id"))
            d_id = Column(ForeignKey("d.id"))
            a = relationship("A", back_populates="bs")
            d = relationship("D")

        class C(ComparableEntity, Base):
            __tablename__ = "c"
            id = Column(Integer, primary_key=True)
            a_id = Column(ForeignKey("a.id"))
            d_id = Column(ForeignKey("d.id"))
            a = relationship("A", back_populates="cs")
            d = relationship("D")

        class D(ComparableEntity, Base):
            __tablename__ = "d"
            id = Column(Integer, primary_key=True)

    @classmethod
    def insert_data(cls, connection):
        from sqlalchemy.orm import Session

        A, B, C, D = cls.classes("A", "B", "C", "D")
        session = Session(connection)
        d1 = D(id=1)
        session.add_all(
            [
                d1,
                A(id=1, bs=[B(id=1, d=d1)], cs=[C(id=1, d=d1)]),
            ]
        )
        session.commit()

    def test_converging_paths_dedup_not_merged(self):
        A = self.classes.A
        B, C = self.classes("B", "C")
        session = fixture_session()
        budget = LoaderBudget()
        a = session.scalars(
            select(A)
            .options(
                selectinload(A.bs).selectinload(B.d),
                selectinload(A.cs).selectinload(C.d),
            )
            .execution_options(orm_eager_load_budget=budget),
        ).one()
        # both paths loaded the same D row: one shared instance
        assert a.bs[0].d is a.cs[0].d
        eq_(a.bs[0].d.id, 1)
        # but the two loader options were not merged: four separate
        # batches (bs, b.d, cs, c.d), visits counted per path
        eq_(budget.batches, 4)
        eq_(budget.visits, 4)


class InteropTest(fixtures.DeclarativeMappedTest):
    """Interop of chunking / budget with joinedload, contains_eager,
    lazy='raise' and aliased relationships."""

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class A(ComparableEntity, Base):
            __tablename__ = "a"
            id = Column(Integer, primary_key=True)
            bs = relationship("B", back_populates="a", lazy="raise")
            cs = relationship("C", back_populates="a")

        class B(ComparableEntity, Base):
            __tablename__ = "b"
            id = Column(Integer, primary_key=True)
            a_id = Column(ForeignKey("a.id"))
            a = relationship("A", back_populates="bs")

        class C(ComparableEntity, Base):
            __tablename__ = "c"
            id = Column(Integer, primary_key=True)
            a_id = Column(ForeignKey("a.id"))
            a = relationship("A", back_populates="cs")

    @classmethod
    def insert_data(cls, connection):
        from sqlalchemy.orm import Session

        A, B, C = cls.classes("A", "B", "C")
        session = Session(connection)
        session.add_all(
            [
                A(
                    id=i,
                    bs=[B(id=(i * 10) + j) for j in range(1, 3)],
                    cs=[C(id=(i * 10) + j) for j in range(1, 3)],
                )
                for i in range(1, 7)
            ]
        )
        session.commit()

    def test_joinedload_with_budget(self):
        # joinedload emits no SELECT IN batches; budget stays at zero
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        result = (
            session.scalars(
                select(A)
                .options(joinedload(A.bs))
                .order_by(A.id)
                .execution_options(orm_eager_load_budget=budget),
            )
            .unique()
            .all()
        )
        eq_(len(result), 6)
        for a in result:
            eq_(len(a.bs), 2)
        eq_(budget.batches, 0)
        eq_(budget.visits, 0)

    def test_contains_eager_with_selectin_budget(self):
        A, C = self.classes("A", "C")
        session = fixture_session()
        budget = LoaderBudget()
        result = (
            session.scalars(
                select(A)
                .join(A.cs)
                .options(
                    contains_eager(A.cs), selectinload(A.bs, chunk_size=3)
                )
                .order_by(A.id)
                .execution_options(orm_eager_load_budget=budget),
            )
            .unique()
            .all()
        )
        eq_(len(result), 6)
        for a in result:
            eq_(len(a.cs), 2)
            eq_(len(a.bs), 2)
        # contains_eager consumed the join; only the selectinload of
        # A.bs emitted batches
        eq_(budget.batches, 2)
        eq_(budget.visits, 6)

    def test_raiseload_relationship(self):
        # lazy="raise" relationships populated via selectinload are
        # usable; unpopulated ones still raise
        A = self.classes.A
        session = fixture_session()
        budget = LoaderBudget()
        a = session.scalars(
            select(A)
            .where(A.id == 1)
            .options(selectinload(A.bs))
            .execution_options(orm_eager_load_budget=budget),
        ).one()
        eq_(len(a.bs), 2)
        eq_(budget.batches, 1)

        def access_cs():
            a.cs

        expect_raises_message(
            sa_exc.InvalidRequestError,
            "is not available due to lazy='raise'",
            access_cs,
        )

    def test_aliased_class_chunking(self):
        A = self.classes.A
        alias = aliased(A)
        session = fixture_session()
        budget = LoaderBudget()
        result = session.scalars(
            select(alias)
            .options(selectinload(alias.bs, chunk_size=2))
            .order_by(alias.id)
            .execution_options(orm_eager_load_budget=budget),
        ).all()
        eq_(len(result), 6)
        for a in result:
            eq_(len(a.bs), 2)
        eq_(budget.batches, 3)
        eq_(budget.parameters, 6)

    def test_interleaved_streaming_executions_isolated(self):
        # two overlapping executions on one session each get their own
        # dict-configured budget tracker
        A = self.classes.A
        session = fixture_session()
        stmt = (
            select(A)
            .options(selectinload(A.cs))
            .order_by(A.id)
            .execution_options(yield_per=3)
        )
        stream = session.scalars(
            stmt,
            execution_options={"orm_eager_load_budget": {"max_batches": 2}},
        )
        iter_stream = iter(stream)
        next(iter_stream)
        # a second, full execution while the first is mid-stream
        stmt2 = select(A).options(selectinload(A.cs)).order_by(A.id)
        result2 = session.scalars(
            stmt2,
            execution_options={"orm_eager_load_budget": {"max_batches": 1}},
        ).all()
        eq_(len(result2), 6)
        eq_(len(list(iter_stream)) + 1, 6)
