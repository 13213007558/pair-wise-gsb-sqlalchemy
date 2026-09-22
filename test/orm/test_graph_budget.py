from __future__ import annotations

from sqlalchemy import event
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import testing
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import aliased
from sqlalchemy.orm import contains_eager
from sqlalchemy.orm import GraphBudget
from sqlalchemy.orm import GraphBudgetExceeded
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import raiseload
from sqlalchemy.orm import relationship
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session
from sqlalchemy.testing import assert_raises_message
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_
from sqlalchemy.testing.schema import Column


class _Fixture(fixtures.DeclarativeMappedTest):
    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class Parent(Base):
            __tablename__ = "gb_parent"
            id = Column(Integer, primary_key=True)
            children = relationship(
                "Child",
                order_by="Child.id",
                back_populates="parent",
            )

        class Child(Base):
            __tablename__ = "gb_child"
            id = Column(Integer, primary_key=True)
            parent_id = Column(ForeignKey("gb_parent.id"))
            parent = relationship("Parent", back_populates="children")

        class CompositeParent(Base):
            __tablename__ = "gb_cparent"
            k1 = Column(Integer, primary_key=True)
            k2 = Column(Integer, primary_key=True)
            kids = relationship(
                "CompositeChild", order_by="CompositeChild.k1"
            )

        class CompositeChild(Base):
            __tablename__ = "gb_ckid"
            k1 = Column(Integer, primary_key=True)
            k2 = Column(Integer, primary_key=True)
            p1 = Column(Integer)
            p2 = Column(Integer)
            __table_args__ = (
                ForeignKeyConstraint(
                    ["p1", "p2"], ["gb_cparent.k1", "gb_cparent.k2"]
                ),
            )

        class Node(Base):
            __tablename__ = "gb_node"
            id = Column(Integer, primary_key=True)
            parent_id = Column(ForeignKey("gb_node.id"))
            children = relationship(
                "Node",
                order_by="Node.id",
                back_populates="parent",
            )
            parent = relationship(
                "Node", back_populates="children", remote_side=[id]
            )

    @classmethod
    def insert_data(cls, connection):
        Parent = cls.classes.Parent
        Child = cls.classes.Child
        CParent = cls.classes.CompositeParent
        CChild = cls.classes.CompositeChild
        Node = cls.classes.Node

        with Session(connection) as session:
            session.add_all(
                [
                    Parent(
                        id=i,
                        children=[
                            Child(id=i * 10 + j) for j in range(1, 4)
                        ],
                    )
                    for i in range(1, 11)
                ]
            )
            session.add_all(
                [
                    CParent(
                        k1=i,
                        k2=100 + i,
                        kids=[
                            CChild(k1=i, k2=200 + i),
                            CChild(k1=i, k2=300 + i),
                        ],
                    )
                    for i in range(1, 6)
                ]
            )

            root = Node(id=1)
            n2 = Node(id=2, parent=root)
            n3 = Node(id=3, parent=root)
            Node(id=4, parent=n2)
            Node(id=5, parent=n3)
            session.add_all([root, n2, n3])
            session.commit()


class ChunkSizeTest(_Fixture):
    def _sql_parameter_counts(self, session):
        counts = []

        @event.listens_for(session.bind, "before_cursor_execute")
        def listener(conn, cursor, statement, params, context, executemany):
            if " IN (" in statement or " IN(" in statement:
                counts.append(len(params))

        return counts, listener

    def test_loader_option_chunk_size(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            counts, listener = self._sql_parameter_counts(session)
            try:
                ps = (
                    session.execute(
                        select(Parent).options(
                            selectinload(Parent.children, chunk_size=3)
                        )
                    )
                    .scalars()
                    .all()
                )
            finally:
                event.remove(
                    session.bind, "before_cursor_execute", listener
                )

        eq_(counts, [3, 3, 3, 1])
        eq_(
            [[c.id for c in p.children] for p in ps],
            [[i * 10 + j for j in (1, 2, 3)] for i in range(1, 11)],
        )

    def test_execution_option_chunk_size(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            counts, listener = self._sql_parameter_counts(session)
            try:
                session.execute(
                    select(Parent).options(selectinload(Parent.children)),
                    execution_options={"orm_selectin_chunk_size": 4},
                ).scalars().all()
            finally:
                event.remove(
                    session.bind, "before_cursor_execute", listener
                )

        eq_(counts, [4, 4, 2])

    def test_loader_option_overrides_execution_option(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            counts, listener = self._sql_parameter_counts(session)
            try:
                session.execute(
                    select(Parent).options(
                        selectinload(Parent.children, chunk_size=2)
                    ),
                    execution_options={"orm_selectin_chunk_size": 9},
                ).scalars().all()
            finally:
                event.remove(
                    session.bind, "before_cursor_execute", listener
                )

        eq_(counts, [2, 2, 2, 2, 2])

    def test_statement_execution_option_chunk_size(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            counts, listener = self._sql_parameter_counts(session)
            try:
                session.execute(
                    select(Parent)
                    .options(selectinload(Parent.children))
                    .execution_options(
                        orm_selectin_chunk_size=5
                    )
                ).scalars().all()
            finally:
                event.remove(
                    session.bind, "before_cursor_execute", listener
                )

        eq_(counts, [5, 5])

    def test_composite_primary_key_expands_per_column(self):
        CParent = self.classes.CompositeParent
        with Session(testing.db) as session:
            counts, listener = self._sql_parameter_counts(session)
            try:
                session.execute(
                    select(CParent).options(
                        selectinload(CParent.kids, chunk_size=2)
                    )
                ).scalars().all()
            finally:
                event.remove(
                    session.bind, "before_cursor_execute", listener
                )

        eq_(counts, [4, 4, 2])

    def test_empty_collection_emits_no_chunk(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            counts, listener = self._sql_parameter_counts(session)
            try:
                ps = (
                    session.execute(
                        select(Parent)
                        .where(Parent.id > 1000)
                        .options(selectinload(Parent.children, chunk_size=2))
                    )
                    .scalars()
                    .all()
                )
            finally:
                event.remove(
                    session.bind, "before_cursor_execute", listener
                )
        eq_(ps, [])
        eq_(counts, [])

    def test_invalid_loader_chunk_size(self):
        Parent = self.classes.Parent
        for bad in (0, -1, 1.5, "2", True):
            with Session(testing.db) as session:
                assert_raises_message(
                    ArgumentError,
                    "chunk_size must be a positive integer",
                    session.execute(
                        select(Parent).options(
                            selectinload(Parent.children, chunk_size=bad)
                        )
                    ).scalars().all,
                )

    def test_invalid_execution_option_chunk_size(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            assert_raises_message(
                ArgumentError,
                "chunk_size must be a positive integer, got 0",
                session.execute(
                    select(Parent).options(selectinload(Parent.children)),
                    execution_options={"orm_selectin_chunk_size": 0},
                ).scalars().all,
            )

    def test_distinct_loader_options_are_not_merged(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            def go():
                session.execute(
                    select(Parent).options(
                        selectinload(Parent.children, chunk_size=2),
                        selectinload(Parent.children, chunk_size=3),
                    )
                ).scalars().all()

            assert_raises_message(
                Exception, "Loader strategies for", go
            )

    def test_identity_map_dedup_across_chunks(self):
        Child = self.classes.Child
        with Session(testing.db) as session:
            ps = (
                session.execute(
                    select(self.classes.Parent).options(
                        selectinload(self.classes.Parent.children, chunk_size=2)
                    )
                )
                .scalars()
                .all()
            )
            eq_(
                len(
                    [
                        state
                        for state in session.identity_map.all_states()
                        if state.class_ is Child
                    ]
                ),
                30,
            )
            for parent in ps:
                for child in parent.children:
                    is_(child.parent, parent)


class GraphBudgetTest(_Fixture):
    def test_statement_budget(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget(max_statements=2)
            assert_raises_message(
                GraphBudgetExceeded,
                "max_statements=2",
                session.execute(
                    select(Parent).options(
                        selectinload(Parent.children, chunk_size=4)
                    ),
                    execution_options={"orm_graph_budget": budget},
                ).scalars().all,
            )
            eq_(budget.statement_count, 2)

    def test_statement_level_budget_execution_option(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget(max_statements=2)
            stmt = (
                select(Parent)
                .options(selectinload(Parent.children, chunk_size=4))
                .execution_options(orm_graph_budget=budget)
            )
            assert_raises_message(
                GraphBudgetExceeded,
                "max_statements=2",
                session.execute(stmt).scalars().all,
            )
            eq_(budget.statement_count, 2)

    def test_parameter_budget_cumulative(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget(max_parameters=7)
            assert_raises_message(
                GraphBudgetExceeded,
                "cumulative parameter count 8",
                session.execute(
                    select(Parent).options(
                        selectinload(Parent.children, chunk_size=4)
                    ),
                    execution_options={"orm_graph_budget": budget},
                ).scalars().all,
            )
            eq_(budget.parameter_count, 4)
            eq_(budget.statement_count, 1)

    def test_parameter_budget_per_statement_upfront(self):
        CParent = self.classes.CompositeParent
        with Session(testing.db) as session:
            budget = GraphBudget(max_parameters=3)
            assert_raises_message(
                GraphBudgetExceeded,
                "uses up to 4 bound parameters",
                session.execute(
                    select(CParent).options(
                        selectinload(CParent.kids, chunk_size=2)
                    ),
                    execution_options={"orm_graph_budget": budget},
                ).scalars().all,
            )
            eq_(budget.statement_count, 0)

    def test_parameter_budget_auto_shrinks_default_chunk(self):
        CParent = self.classes.CompositeParent
        with Session(testing.db) as session:
            # five composite parents; two binds per identity.  No explicit
            # chunk_size, so the loader shrinks batches to fit per
            # statement.  The cumulative limit still bounds the graph.
            budget = GraphBudget(max_parameters=10)
            session.execute(
                select(CParent).options(selectinload(CParent.kids)),
                execution_options={"orm_graph_budget": budget},
            ).scalars().all()
            eq_(budget.statement_count, 1)
            eq_(budget.parameter_count, 10)

            # same data with an explicit small chunk_size also splits
            budget3 = GraphBudget()
            session.execute(
                select(CParent).options(
                    selectinload(CParent.kids, chunk_size=2)
                ),
                execution_options={"orm_graph_budget": budget3},
            ).scalars().all()
            eq_(budget3.statement_count, 3)
            eq_(budget3.parameter_count, 10)

            # a tighter cumulative bound stops after the first fitted batch
            budget2 = GraphBudget(max_parameters=6)
            assert_raises_message(
                GraphBudgetExceeded,
                "cumulative parameter count",
                session.execute(
                    select(CParent).options(selectinload(CParent.kids)),
                    execution_options={"orm_graph_budget": budget2},
                ).scalars().all,
            )
            eq_(budget2.statement_count, 1)
            eq_(budget2.parameter_count, 6)

    def test_parameter_budget_too_small_for_any_identity(self):
        CParent = self.classes.CompositeParent
        with Session(testing.db) as session:
            budget = GraphBudget(max_parameters=1)
            assert_raises_message(
                GraphBudgetExceeded,
                "requires at least 2 bound parameter",
                session.execute(
                    select(CParent).options(selectinload(CParent.kids)),
                    execution_options={"orm_graph_budget": budget},
                ).scalars().all,
            )

    def test_identity_budget_counts_distinct_identities(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget(max_identities=15)
            assert_raises_message(
                GraphBudgetExceeded,
                "max_identities=15",
                session.execute(
                    select(Parent).options(selectinload(Parent.children)),
                    execution_options={"orm_graph_budget": budget},
                ).scalars().all,
            )
            eq_(budget.identity_count, 15)

    def test_identity_budget_cyclic_graph(self):
        Node = self.classes.Node
        with Session(testing.db) as session:
            budget = GraphBudget()
            root = (
                session.execute(
                    select(Node).options(
                        selectinload(Node.children, recursion_depth=10)
                    ),
                    execution_options={"orm_graph_budget": budget},
                )
                .scalars()
                .first()
            )
            eq_(budget.identity_count, 5)
            is_(root.children[0].parent, root)

    def test_identity_budget_existing_entities_counted_once(self):
        Node = self.classes.Node
        with Session(testing.db) as session:
            session.execute(select(Node)).scalars().all()
            budget = GraphBudget()
            session.execute(
                select(Node).options(
                    selectinload(Node.children, recursion_depth=10)
                ),
                execution_options={"orm_graph_budget": budget},
            ).scalars().all()
            eq_(budget.identity_count, 5)

    def test_joinedload_counts_identities_no_statements(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget()
            session.execute(
                select(Parent)
                .options(joinedload(Parent.children))
                .order_by(Parent.id),
                execution_options={"orm_graph_budget": budget},
            ).unique().scalars().all()
            eq_(budget.statement_count, 0)
            eq_(budget.identity_count, 40)

    def test_joinedload_identity_budget_raises(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget(max_identities=3)
            assert_raises_message(
                GraphBudgetExceeded,
                "max_identities=3",
                session.execute(
                    select(Parent).options(joinedload(Parent.children)),
                    execution_options={"orm_graph_budget": budget},
                ).unique().scalars().all,
            )
            eq_(budget.identity_count, 3)

    def test_contains_eager_counts_identities(self):
        Parent = self.classes.Parent
        Child = self.classes.Child
        with Session(testing.db) as session:
            budget = GraphBudget()
            children = (
                session.execute(
                    select(Child)
                    .join(Child.parent)
                    .options(contains_eager(Child.parent))
                    .order_by(Child.id),
                    execution_options={"orm_graph_budget": budget},
                )
                .unique()
                .scalars()
                .all()
            )
            eq_(budget.statement_count, 0)
            eq_(budget.identity_count, 40)
            is_(children[0].parent.children[0], children[0])

    def test_raiseload_emits_no_postloads(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            budget = GraphBudget()
            ps = (
                session.execute(
                    select(Parent).options(raiseload(Parent.children)),
                    execution_options={"orm_graph_budget": budget},
                )
                .scalars()
                .all()
            )
            eq_(budget.statement_count, 0)
            eq_(budget.identity_count, 10)
            assert_raises_message(
                Exception,
                "lazy='raise'",
                lambda: ps[0].children,
            )

    def test_reset(self):
        budget = GraphBudget(
            max_statements=5, max_parameters=100, max_identities=50
        )
        budget.charge_statement(12)
        budget.charge_identity((object, (1,), None))
        eq_(budget.statement_count, 1)
        eq_(budget.parameter_count, 12)
        eq_(budget.identity_count, 1)
        budget.reset()
        eq_(budget.statement_count, 0)
        eq_(budget.parameter_count, 0)
        eq_(budget.identity_count, 0)
        budget.charge_identity((object, (1,), None))
        eq_(budget.identity_count, 1)

    def test_invalid_limits(self):
        for kw in (
            {"max_statements": -1},
            {"max_parameters": -2},
            {"max_identities": -3},
        ):
            assert_raises_message(
                ValueError, "non-negative integer", GraphBudget, **kw
            )

    def test_aliased_relationship_interop(self):
        Parent = self.classes.Parent
        with Session(testing.db) as session:
            palias = aliased(Parent)
            budget = GraphBudget()
            ps = (
                session.execute(
                    select(palias)
                    .options(selectinload(palias.children, chunk_size=3))
                    .order_by(palias.id),
                    execution_options={"orm_graph_budget": budget},
                )
                .scalars()
                .all()
            )
            eq_(len(ps), 10)
            eq_(budget.parameter_count, 10)
            eq_(budget.statement_count, 4)
            eq_(budget.identity_count, 40)
