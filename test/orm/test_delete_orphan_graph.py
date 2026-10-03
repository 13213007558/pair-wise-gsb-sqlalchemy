from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy import event
from sqlalchemy import exc
from sqlalchemy import ForeignKey
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import testing
from sqlalchemy import update
from sqlalchemy.orm import attributes
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import exc as orm_exc
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Session
from sqlalchemy.testing import assert_raises_message
from sqlalchemy.testing import eq_
from sqlalchemy.testing import is_
from sqlalchemy.testing import is_true
from sqlalchemy.testing import fixtures
from sqlalchemy.testing.assertions import expect_raises
from sqlalchemy.testing.assertsql import CompiledSQL
from sqlalchemy.testing.engines import testing_engine


class Base(DeclarativeBase):
    pass


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(30))

    children: Mapped[list[Node]] = relationship(
        lambda: Node,
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    parent: Mapped[Node | None] = relationship(
        lambda: Node,
        remote_side=[id],
        back_populates="children",
    )


class ActiveNode(Base):
    __tablename__ = "active_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("active_nodes.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(30))

    children: Mapped[list[ActiveNode]] = relationship(
        lambda: ActiveNode,
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )
    parent: Mapped[ActiveNode | None] = relationship(
        lambda: ActiveNode,
        remote_side=[id],
        back_populates="children",
    )


class DeleteOrphanGraphTest(
    fixtures.TestBase, testing.AssertsExecutionResults
):
    __only_on__ = "sqlite"

    @testing.fixture
    def database(self):
        engine = testing_engine("sqlite://")

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(
            dbapi_connection, connection_record
        ):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine)
        try:
            yield engine
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def _history(self, instance, attribute_name):
        return attributes.get_state_history(
            attributes.instance_state(instance), attribute_name
        )

    def _rows(self, session):
        return session.execute(
            select(Node.id, Node.parent_id, Node.name).order_by(Node.id)
        ).all()

    def _active_rows(self, session):
        return session.execute(
            select(
                ActiveNode.id, ActiveNode.parent_id, ActiveNode.name
            ).order_by(ActiveNode.id)
        ).all()

    def _seed(self, session):
        old_parent = Node(name="old parent")
        new_parent = Node(name="new parent")
        child = Node(name="child")
        old_parent.children.append(child)
        session.add_all([old_parent, new_parent])
        session.commit()
        return old_parent, new_parent, child

    def test_pending_orphan_removed_before_flush_is_expunged(
        self, database
    ):
        with Session(database) as session:
            parent = Node(name="parent")
            child = Node(name="child")
            parent.children.append(child)
            session.add(parent)
            session.flush()

            new_child = Node(name="new child")
            parent.children.append(new_child)
            is_true(new_child in session)

            parent.children.remove(new_child)

            eq_(
                self._rows(session),
                [(1, None, "parent"), (2, 1, "child")],
            )
            eq_(parent.children, [child])
            is_true(new_child not in session)
            is_true(inspect(new_child).transient)

    def test_scalar_reparent_keeps_persistent_orphan(self, database):
        with Session(database, autoflush=False) as session:
            old_parent, new_parent, child = self._seed(session)

            child.parent = new_parent
            with self.assert_execution(
                database,
                CompiledSQL(
                    "UPDATE nodes SET parent_id=:parent_id "
                    "WHERE nodes.id = :nodes_id",
                    {
                        "parent_id": new_parent.id,
                        "nodes_id": child.id,
                    },
                ),
            ):
                session.flush()
            old_history = self._history(old_parent, "children")
            new_history = self._history(new_parent, "children")

            eq_(list(old_history.added), [])
            eq_(list(old_history.unchanged), [])
            eq_(list(old_history.deleted), [])
            eq_(list(new_history.added), [])
            eq_(list(new_history.unchanged), [child])
            eq_(list(new_history.deleted), [])
            is_true(inspect(child).persistent)
            is_true(not inspect(child).deleted)

            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                    (child.id, new_parent.id, "child"),
                ],
            )
            eq_(old_parent.children, [])
            eq_(new_parent.children, [child])

    def test_collection_reparent_without_partial_flush(self, database):
        with Session(database, autoflush=False) as session:
            old_parent, new_parent, child = self._seed(session)

            old_parent.children.remove(child)
            new_parent.children.append(child)

            session.flush()

            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                    (child.id, new_parent.id, "child"),
                ],
            )
            eq_(old_parent.children, [])
            eq_(new_parent.children, [child])
            is_(child.parent, new_parent)

    def test_partial_flush_between_remove_and_append_raises(self, database):
        with Session(database) as session:
            old_parent, new_parent, child = self._seed(session)
            child_state = inspect(child)

            old_parent.children.remove(child)
            eq_(self._history(old_parent, "children").deleted, [child])
            is_true(child_state.persistent)

            with self.sql_execution_asserter(database) as asserter:
                assert_raises_message(
                    exc.InvalidRequestError,
                    r"Instance '<Node at .+>' has been deleted\.  "
                    r"Use the make_transient\(\) function to send "
                    r"this object back to the transient state\.",
                    new_parent.children.append,
                    child,
                )
            asserter.assert_(
                CompiledSQL(
                    "DELETE FROM nodes WHERE nodes.id = :id",
                    [{"id": child.id}],
                ),
                CompiledSQL(
                    "SELECT nodes.id, nodes.parent_id, nodes.name "
                    "FROM nodes WHERE nodes.id = :pk_1",
                    [{"pk_1": new_parent.id}],
                ),
                CompiledSQL(
                    "SELECT nodes.id, nodes.parent_id, nodes.name "
                    "FROM nodes WHERE :param_1 = nodes.parent_id",
                    [{"param_1": new_parent.id}],
                ),
            )

            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                ],
            )
            is_true(child_state.deleted)
            eq_(old_parent.children, [])
            eq_(new_parent.children, [])

            session.rollback()
            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                    (child.id, old_parent.id, "child"),
                ],
            )
            is_true(child_state.persistent)
            eq_(old_parent.children, [child])
            eq_(new_parent.children, [])

    def test_database_cascade_with_passive_deletes(self, database):
        with Session(database) as session:
            parent = Node(name="parent")
            child = Node(name="child")
            parent.children.append(child)
            session.add(parent)
            session.commit()
            parent_id = parent.id
            child_id = child.id

            session.delete(parent)
            with self.assert_execution(
                database,
                CompiledSQL(
                    "DELETE FROM nodes WHERE nodes.id = :id",
                    [{"id": parent_id}],
                ),
            ):
                session.flush()

            eq_(self._rows(session), [])
            is_true(inspect(parent).deleted)
            is_true(inspect(child).persistent)
            is_true(not inspect(child).deleted)
            is_true(child in session)
            eq_(parent.children, [])

            session.commit()
            is_true(inspect(parent).detached)
            is_true(inspect(child).persistent)
            is_true(session.get(Node, parent_id) is None)
            is_true(session.get(Node, child_id) is None)

    def test_orm_cascade_deletes_child_before_parent(self, database):
        with Session(database) as session:
            parent = ActiveNode(name="parent")
            child = ActiveNode(name="child")
            parent.children.append(child)
            session.add(parent)
            session.commit()
            parent_id = parent.id
            child_id = child.id

            session.delete(parent)
            with self.assert_execution(
                database,
                CompiledSQL(
                    "DELETE FROM active_nodes WHERE active_nodes.id = :id",
                    [{"id": child_id}],
                ),
                CompiledSQL(
                    "DELETE FROM active_nodes WHERE active_nodes.id = :id",
                    [{"id": parent_id}],
                ),
            ):
                session.flush()

            eq_(self._active_rows(session), [])
            is_true(inspect(parent).deleted)
            is_true(inspect(child).deleted)
            is_true(child not in session)

    def test_bulk_update_bypasses_identity_map(self, database):
        with Session(database) as session:
            old_parent, new_parent, child = self._seed(session)

            session.bulk_update_mappings(
                Node,
                [
                    {
                        "id": child.id,
                        "parent_id": new_parent.id,
                        "name": "bulk child",
                    }
                ],
            )

            eq_(child.parent_id, old_parent.id)
            eq_(child.name, "child")
            eq_(old_parent.children, [])
            eq_(new_parent.children, [child])
            is_(child.parent, old_parent)
            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                    (child.id, new_parent.id, "bulk child"),
                ],
            )

            session.expire_all()
            refreshed_child = session.get(Node, child.id)
            eq_(refreshed_child.parent_id, new_parent.id)
            eq_(refreshed_child.name, "bulk child")
            eq_(old_parent.children, [])
            is_(refreshed_child.parent, new_parent)
            eq_(new_parent.children, [refreshed_child])

    def test_bulk_delete_requires_expiry_to_reconcile_session(
        self, database
    ):
        with Session(database) as session:
            parent = Node(name="parent")
            child = Node(name="child")
            parent.children.append(child)
            session.add(parent)
            session.commit()
            parent_id = parent.id
            child_id = child.id

            session.execute(
                delete(Node).where(Node.id == parent.id),
                execution_options={"synchronize_session": False},
            )

            eq_(self._rows(session), [])
            is_true(inspect(parent).persistent)
            is_true(inspect(child).persistent)
            eq_(parent.children, [])

            session.expire_all()
            with expect_raises(orm_exc.ObjectDeletedError):
                parent.name
            with expect_raises(orm_exc.ObjectDeletedError):
                child.name
            is_true(session.get(Node, parent_id) is None)
            is_true(session.get(Node, child_id) is None)
            eq_(
                session.scalars(select(Node).order_by(Node.id)).all(), []
            )

    def test_savepoint_rollback_restores_reparent_history(
        self, database
    ):
        with Session(database) as session:
            old_parent, new_parent, child = self._seed(session)

            savepoint = session.begin_nested()
            child.parent = new_parent
            session.flush()
            eq_(child.parent_id, new_parent.id)
            eq_(old_parent.children, [])
            eq_(new_parent.children, [child])

            savepoint.rollback()

            eq_(child.parent_id, old_parent.id)
            is_(child.parent, old_parent)
            eq_(old_parent.children, [])
            eq_(new_parent.children, [])
            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                    (child.id, old_parent.id, "child"),
                ],
            )

            session.expire_all()
            eq_(old_parent.children, [child])
            eq_(new_parent.children, [])

            session.commit()
            eq_(
                self._rows(session),
                [
                    (old_parent.id, None, "old parent"),
                    (new_parent.id, None, "new parent"),
                    (child.id, old_parent.id, "child"),
                ],
            )

    def test_savepoint_fk_failure_rollback_database_and_snapshot(
        self, database
    ):
        with Session(database) as session:
            parent = Node(name="parent")
            child = Node(name="child")
            parent.children.append(child)
            session.add(parent)
            session.commit()

            with expect_raises(exc.IntegrityError):
                session.begin_nested()
                session.execute(
                    update(Node)
                    .where(Node.id == child.id)
                    .values(parent_id=999)
                )

            session.rollback()
            eq_(child.parent_id, parent.id)
            eq_(parent.children, [child])
            eq_(
                self._rows(session),
                [
                    (parent.id, None, "parent"),
                    (child.id, parent.id, "child"),
                ],
            )

    def test_merge_detached_scalar_reparent(self, database):
        with Session(database) as session:
            old_parent, new_parent, child = self._seed(session)
            old_parent_id = old_parent.id
            new_parent_id = new_parent.id
            child_id = child.id

        with Session(database) as session:
            new_parent = session.get(Node, new_parent_id)
            detached_child = Node(
                id=child_id,
                name="merged child",
                parent_id=new_parent_id,
            )

            merged_child = session.merge(detached_child)
            is_(merged_child.parent, new_parent)
            session.commit()

            eq_(
                self._rows(session),
                [
                    (old_parent_id, None, "old parent"),
                    (new_parent_id, None, "new parent"),
                    (child_id, new_parent_id, "merged child"),
                ],
            )
            eq_(new_parent.children, [merged_child])

    def test_merge_uncommitted_deleted_instance_reinserts_row(
        self, database
    ):
        with Session(database) as session:
            parent = ActiveNode(name="parent")
            child = ActiveNode(name="child")
            parent.children.append(child)
            session.add(parent)
            session.commit()
            parent_id = parent.id
            child_id = child.id

            parent = session.get(ActiveNode, parent_id)
            child = session.get(ActiveNode, child_id)
            session.delete(child)
            session.flush()
            is_true(inspect(child).deleted)

            merged_child = session.merge(child)
            is_true(merged_child is not child)
            with self.assert_execution(
                database,
                CompiledSQL(
                    "INSERT INTO active_nodes (id, parent_id, name) "
                    "VALUES (:id, :parent_id, :name)",
                    {
                        "id": child_id,
                        "parent_id": parent_id,
                        "name": "child",
                    },
                ),
            ):
                session.commit()

            eq_(
                self._active_rows(session),
                [
                    (parent_id, None, "parent"),
                    (child_id, parent_id, "child"),
                ],
            )
            is_true(inspect(merged_child).persistent)
            is_true(not inspect(merged_child).deleted)

    def test_concurrent_sessions_observe_database_cascade_after_expiry(
        self, database
    ):
        with Session(
            database, expire_on_commit=False
        ) as first_session, Session(
            database, expire_on_commit=False
        ) as second_session:
            parent = Node(name="parent")
            child = Node(name="child")
            parent.children.append(child)
            first_session.add(parent)
            first_session.commit()

            second_parent = second_session.get(Node, parent.id)
            second_child = second_session.get(Node, child.id)
            eq_(second_parent.children, [second_child])

            first_session.delete(
                first_session.get(Node, parent.id)
            )
            first_session.commit()

            is_true(inspect(second_parent).persistent)
            is_true(inspect(second_child).persistent)
            eq_(second_parent.children, [second_child])
            eq_(
                second_session.execute(select(Node.id)).all(), []
            )

            second_session.expire_all()
            is_true(second_session.get(Node, parent.id) is None)
            is_true(second_session.get(Node, child.id) is None)
