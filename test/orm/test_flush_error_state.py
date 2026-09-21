# test/orm/test_flush_error_state.py
# Copyright (C) 2005-2026 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""Session / identity-map state after flush-time constraint violations.

When a flush fails (here, a SQLite UNIQUE constraint violation) the
affected transaction boundary is rolled back immediately:

* a failed flush against the outer transaction issues a full ROLLBACK,
* a failed flush inside of Session.begin_nested issues
  ROLLBACK TO SAVEPOINT.

The session snapshot taken at that boundary is then restored - pending
objects become transient again and are expelled from the identity map,
previously persistent objects are expired - and every attempt to emit
further SQL raises PendingRollbackError until the failed transaction is
rolled back explicitly.  The tests below pin down these state
transitions for outer vs. nested rollback, autoflush-triggered flushes,
commit failures, expire_on_commit, event ordering, session.close() /
reuse and the asyncio session.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy import exc as sa_exc
from sqlalchemy import ForeignKey
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import testing
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import relationship
from sqlalchemy.testing import assert_raises
from sqlalchemy.testing import config
from sqlalchemy.testing import engines
from sqlalchemy.testing import eq_
from sqlalchemy.testing import expect_raises
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_false
from sqlalchemy.testing import is_true
from sqlalchemy.testing.assertsql import CountStatements
from sqlalchemy.testing.fixtures import fixture_session
from sqlalchemy.testing.schema import Column


_TRACKED_TRANSACTION_EVENTS = (
    "before_commit",
    "after_commit",
    "before_flush",
    "after_flush",
    "after_flush_postexec",
    "after_rollback",
    "after_soft_rollback",
    "after_transaction_end",
)


class _FlushErrorFixture(fixtures.DeclarativeMappedTest):
    __sparse_driver_backend__ = True

    @classmethod
    def setup_classes(cls):
        Base = cls.DeclarativeBasic

        class User(Base):
            __tablename__ = "users"
            id = Column(Integer, primary_key=True)
            name = Column(String(50), nullable=False, unique=True)
            addresses = relationship(
                "Address",
                back_populates="user",
                cascade="all, delete-orphan",
                order_by="Address.id",
            )

        class Address(Base):
            __tablename__ = "addresses"
            id = Column(Integer, primary_key=True)
            email_address = Column(String(50), nullable=False, unique=True)
            user_id = Column(Integer, ForeignKey("users.id"))
            user = relationship("User", back_populates="addresses")

    def _names(self, session):
        User = self.classes.User
        return session.execute(
            select(User.name).order_by(User.name)
        ).scalars().all()

    def _user_row_count(self):
        with config.db.connect() as conn:
            return conn.scalar(
                select(func.count()).select_from(self.classes.User)
            )

    def _track_events(self, session, names=_TRACKED_TRANSACTION_EVENTS):
        log = []

        for name in names:

            def record(session_, *args, _name=name):
                log.append(_name)

            event.listen(session, name, record)
        return log

    def _track_instance_events(self, session):
        log = []

        for name in (
            "pending_to_persistent",
            "pending_to_transient",
            "persistent_to_detached",
        ):

            def record(session_, instance, _name=name):
                log.append((_name, instance))

            event.listen(session, name, record)
        return log


class OuterTransactionStateTest(_FlushErrorFixture):
    """A failed flush at the outer boundary rolls the whole transaction."""

    def _setup_failure(self):
        User, Address = self.classes.User, self.classes.Address
        s = fixture_session()

        u1 = User(
            name="ed", addresses=[Address(email_address="ed@example.com")]
        )
        s.add(u1)
        s.commit()
        a1 = u1.addresses[0]

        u2 = User(
            name="ed",
            addresses=[Address(email_address="other@example.com")],
        )
        s.add(u2)
        return s, u1, a1, u2

    def test_failed_flush_expels_pending_and_expires_persistent(self):
        s, u1, a1, u2 = self._setup_failure()

        assert_raises(sa_exc.IntegrityError, s.flush)

        # failed pending objects and their cascaded dependencies are
        # expelled from the identity map and returned to transient state
        assert u2 not in s
        assert u2.addresses[0] not in s
        is_true(inspect(u2).transient)
        is_false(inspect(u2).pending)

        # objects that existed before the failed flush remain in the
        # session but are expired by the snapshot restore
        assert u1 in s
        assert a1 in s
        is_true(inspect(u1).expired)
        eq_(len(s.identity_map), 2)

        # the deactivated outer transaction is still the current one
        is_true(s.in_transaction())

    def test_no_sql_emitted_while_transaction_blocked(self):
        s, u1, a1, u2 = self._setup_failure()

        assert_raises(sa_exc.IntegrityError, s.flush)

        # expired attribute refresh and new queries are refused before
        # the error is acknowledged, without any implicit SQL
        with self.sql_execution_asserter() as asserter:
            assert_raises(sa_exc.PendingRollbackError, lambda: u1.name)
            assert_raises(
                sa_exc.PendingRollbackError,
                lambda: s.execute(select(self.classes.User)),
            )
        asserter.assert_(CountStatements(0))

    def test_repeated_flush_is_noop_but_transaction_still_blocked(self):
        s, u1, a1, u2 = self._setup_failure()

        assert_raises(sa_exc.IntegrityError, s.flush)

        # the snapshot restore removed all pending work, so another
        # flush has nothing to do and emits no SQL ...
        with self.sql_execution_asserter() as asserter:
            s.flush()
        asserter.assert_(CountStatements(0))

        # ... yet the transaction itself remains deactivated
        assert_raises(sa_exc.PendingRollbackError, s.begin_nested)
        assert_raises(sa_exc.PendingRollbackError, s.commit)
        s.rollback()

    def test_rollback_restores_state_and_allows_reuse(self):
        s, u1, a1, u2 = self._setup_failure()

        assert_raises(sa_exc.IntegrityError, s.flush)

        # the database ROLLBACK already happened during the failed
        # flush, so the explicit rollback itself emits no SQL
        with self.sql_execution_asserter() as asserter:
            s.rollback()
        asserter.assert_(CountStatements(0))
        is_false(s.in_transaction())

        # persistent attributes can refresh again, and the failed
        # INSERT is visible in neither the identity map nor the database
        eq_(u1.name, "ed")
        assert u2 not in s
        eq_(self._names(s), ["ed"])

        u3 = self.classes.User(name="jack")
        s.add(u3)
        s.commit()
        eq_(self._names(s), ["ed", "jack"])
        eq_(self._user_row_count(), 2)

    def test_failed_commit_has_same_boundaries(self):
        s, u1, a1, u2 = self._setup_failure()

        assert_raises(sa_exc.IntegrityError, s.commit)

        assert u2 not in s
        is_true(inspect(u1).expired)
        is_true(s.in_transaction())
        assert_raises(sa_exc.PendingRollbackError, s.commit)

        s.rollback()
        eq_(u1.name, "ed")
        eq_(self._names(s), ["ed"])

        u3 = self.classes.User(name="jack")
        s.add(u3)
        s.commit()
        eq_(self._names(s), ["ed", "jack"])


class NestedTransactionStateTest(_FlushErrorFixture):
    """A failed flush inside begin_nested() rolls back to a SAVEPOINT."""

    def _setup_outer(self, name="ed"):
        s = fixture_session()
        u1 = self.classes.User(name=name)
        s.add(u1)
        s.commit()
        return s, u1

    @testing.only_on("sqlite", "checks SAVEPOINT / ROLLBACK TO SAVEPOINT")
    def test_failed_flush_rolls_back_to_savepoint(self):
        s, u1 = self._setup_outer()

        statements = []

        def capture(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            statements.append(statement)

        event.listen(config.db, "before_cursor_execute", capture)
        try:
            nested = s.begin_nested()
            s.add(self.classes.User(name="ed"))
            assert_raises(sa_exc.IntegrityError, s.flush)
        finally:
            event.remove(config.db, "before_cursor_execute", capture)

        # the SAVEPOINT was rolled back, never released; the outer
        # transaction was untouched
        is_true(
            any(st.startswith("ROLLBACK TO SAVEPOINT") for st in statements)
        )
        is_false(
            any(st.startswith("RELEASE SAVEPOINT") for st in statements)
        )

        nested.rollback()
        u3 = self.classes.User(name="jack")
        s.add(u3)
        s.commit()
        eq_(self._names(s), ["ed", "jack"])

    def test_nested_rollback_restores_identity_map_and_outer_transaction(self):
        User, Address = self.classes.User, self.classes.Address
        s, u1 = self._setup_outer()

        nested = s.begin_nested()
        u2 = User(
            name="ed",
            addresses=[Address(email_address="other@example.com")],
        )
        s.add(u2)

        assert_raises(sa_exc.IntegrityError, s.flush)

        # the failed row never reached the outer transaction and is
        # expelled from the identity map; outer objects are expired
        assert u2 not in s
        assert u2.addresses[0] not in s
        assert u1 in s
        is_true(inspect(u1).expired)

        # the SAVEPOINT already rolled back at the database level; this
        # just pops the deactivated nested transaction off the stack
        with self.sql_execution_asserter() as asserter:
            nested.rollback()
        asserter.assert_(CountStatements(0))

        # the outer transaction continues and commits normally
        u3 = User(name="jack")
        s.add(u3)
        s.commit()
        eq_(self._names(s), ["ed", "jack"])
        eq_(self._user_row_count(), 2)

    def test_outer_commit_blocked_until_nested_rollback(self):
        s, u1 = self._setup_outer()

        nested = s.begin_nested()
        s.add(self.classes.User(name="ed"))
        assert_raises(sa_exc.IntegrityError, s.flush)

        # catching the exception is not enough: the deactivated nested
        # transaction is still the current transaction
        assert_raises(sa_exc.PendingRollbackError, s.commit)

        nested.rollback()
        u3 = self.classes.User(name="jack")
        s.add(u3)
        s.commit()
        eq_(self._names(s), ["ed", "jack"])

    def test_second_flush_inside_nested(self):
        s, u1 = self._setup_outer()

        nested = s.begin_nested()
        u2 = self.classes.User(name="jack")
        s.add(u2)
        s.flush()
        is_true(inspect(u2).persistent)

        # mutate the newly persistent row into a constraint violation
        # and flush a second time inside the same SAVEPOINT
        u2.name = "ed"
        assert_raises(sa_exc.IntegrityError, s.flush)

        # snapshot restore rewinds the SAVEPOINT: u2 never existed
        # outside of it and is expelled even though it had an identity
        assert u2 not in s

        nested.rollback()
        eq_(self._names(s), ["ed"])
        s.commit()
        eq_(self._names(s), ["ed"])
        eq_(self._user_row_count(), 1)

    def test_begin_nested_flushes_pending_work_at_outer_boundary(self):
        s, u1 = self._setup_outer()

        s.add(self.classes.User(name="ed"))

        # begin_nested() flushes pending work before taking the
        # SAVEPOINT, so this failure occurs at the outer boundary -
        # there is no nested transaction to roll back
        assert_raises(sa_exc.IntegrityError, s.begin_nested)

        assert_raises(sa_exc.PendingRollbackError, s.commit)
        s.rollback()

        u3 = self.classes.User(name="jack")
        s.add(u3)
        s.commit()
        eq_(self._names(s), ["ed", "jack"])


class AutoFlushFailureTest(_FlushErrorFixture):
    """Constraint errors triggered implicitly by autoflush."""

    def _setup_failure(self):
        s = fixture_session()
        u1 = self.classes.User(name="ed")
        s.add(u1)
        s.commit()
        s.add(self.classes.User(name="ed"))
        return s

    def test_autoflush_failure_raises_from_query(self):
        s = self._setup_failure()

        User = self.classes.User
        assert_raises(
            sa_exc.IntegrityError, lambda: s.execute(select(User)).all()
        )

        is_true(s.in_transaction())

    def test_query_after_autoflush_failure_emits_no_sql(self):
        s = self._setup_failure()

        assert_raises(
            sa_exc.IntegrityError,
            lambda: s.execute(select(self.classes.User)).all(),
        )

        with self.sql_execution_asserter() as asserter:
            assert_raises(
                sa_exc.PendingRollbackError,
                lambda: s.execute(select(self.classes.User)).all(),
            )
        asserter.assert_(CountStatements(0))

        s.rollback()
        eq_(self._names(s), ["ed"])

    def test_recovery_after_autoflush_failure(self):
        s = self._setup_failure()

        assert_raises(
            sa_exc.IntegrityError,
            lambda: s.execute(select(self.classes.User)).all(),
        )
        s.rollback()

        # autoflush and explicit flush work again; valid rows persist
        u2 = self.classes.User(name="jack")
        s.add(u2)
        s.flush()
        s.commit()
        eq_(self._names(s), ["ed", "jack"])
        eq_(self._user_row_count(), 2)


class CommitFailureExpireTest(_FlushErrorFixture):

    @testing.combinations((True,), (False,), argnames="expire_on_commit")
    def test_successful_commit_expiration(self, expire_on_commit):
        s = fixture_session(expire_on_commit=expire_on_commit)
        u1 = self.classes.User(name="ed")
        s.add(u1)
        s.commit()
        eq_(inspect(u1).expired, expire_on_commit)

    @testing.combinations((True,), (False,), argnames="expire_on_commit")
    def test_failed_commit_expires_persistent_regardless_of_flag(
        self, expire_on_commit
    ):
        s = fixture_session(expire_on_commit=expire_on_commit)
        u1 = self.classes.User(name="ed")
        s.add(u1)
        s.commit()

        # make sure the persistent row is unexpired before the failure
        eq_(u1.name, "ed")
        is_false(inspect(u1).expired)

        u2 = self.classes.User(name="ed")
        s.add(u2)
        assert_raises(sa_exc.IntegrityError, s.commit)

        # snapshot restore expires persistent objects even when
        # expire_on_commit=False; no after_commit expiration path runs
        is_true(inspect(u1).expired)
        assert u2 not in s
        is_true(s.in_transaction())

        s.rollback()
        eq_(u1.name, "ed")
        eq_(self._names(s), ["ed"])


class EventOrderTest(_FlushErrorFixture):
    """Session event ordering around failed and successful flushes."""

    def _commit_one(self):
        s = fixture_session()
        s.add(self.classes.User(name="ed"))
        s.commit()
        return s

    def test_successful_commit_event_order(self):
        s = fixture_session()
        log = self._track_events(s)

        s.add(self.classes.User(name="ed"))
        s.commit()

        eq_(
            log,
            [
                "before_commit",
                "before_flush",
                "after_flush",
                "after_flush_postexec",
                "after_transaction_end",
                "after_commit",
                "after_transaction_end",
            ],
        )

    def test_failed_commit_event_order(self):
        s = self._commit_one()
        log = self._track_events(s)

        s.add(self.classes.User(name="ed"))
        assert_raises(sa_exc.IntegrityError, s.commit)

        eq_(
            log,
            [
                "before_commit",
                "before_flush",
                "after_rollback",
                "after_transaction_end",
                "after_soft_rollback",
            ],
        )

        del log[:]
        s.rollback()
        eq_(log, ["after_transaction_end", "after_soft_rollback"])

    def test_failed_flush_then_rollback_event_order(self):
        s = self._commit_one()
        log = self._track_events(s)

        s.add(self.classes.User(name="ed"))
        assert_raises(sa_exc.IntegrityError, s.flush)

        eq_(
            log,
            [
                "before_flush",
                "after_rollback",
                "after_transaction_end",
                "after_soft_rollback",
            ],
        )

        del log[:]
        s.rollback()
        eq_(log, ["after_transaction_end", "after_soft_rollback"])

    def test_nested_error_and_recovery_event_order(self):
        User, Address = self.classes.User, self.classes.Address
        s = fixture_session()
        u1 = User(name="ed")
        s.add(u1)
        s.commit()

        log = self._track_events(s)

        nested = s.begin_nested()
        s.add(User(name="ed", addresses=[Address(email_address="o@x")]))
        assert_raises(sa_exc.IntegrityError, s.flush)

        nested.rollback()

        s.add(User(name="jack"))
        s.commit()

        eq_(
            log,
            [
                "before_flush",
                "after_rollback",
                "after_transaction_end",
                "after_soft_rollback",
                "after_transaction_end",
                "after_soft_rollback",
                "before_commit",
                "before_flush",
                "after_flush",
                "after_flush_postexec",
                "after_transaction_end",
                "after_commit",
                "after_transaction_end",
            ],
        )

    def test_instance_state_events(self):
        User, Address = self.classes.User, self.classes.Address
        s = fixture_session()
        log = self._track_instance_events(s)

        u1 = User(name="ed", addresses=[Address(email_address="ed@x")])
        s.add(u1)
        s.commit()

        eq_([name for name, obj in log], ["pending_to_persistent"] * 2)
        eq_({obj for name, obj in log}, {u1, u1.addresses[0]})

        del log[:]
        u2 = User(name="ed", addresses=[Address(email_address="other@x")])
        s.add(u2)
        assert_raises(sa_exc.IntegrityError, s.flush)

        eq_([name for name, obj in log], ["pending_to_transient"] * 2)
        eq_({obj for name, obj in log}, {u2, u2.addresses[0]})

        s.rollback()


class CloseAndReuseTest(_FlushErrorFixture):
    """session.close() after a failed flush and subsequent reuse."""

    def _failed_session(self):
        s = fixture_session()
        u1 = self.classes.User(name="ed")
        s.add(u1)
        s.commit()
        s.add(self.classes.User(name="ed"))
        assert_raises(sa_exc.IntegrityError, s.flush)
        return s, u1

    def test_close_after_error_emits_no_sql_and_clears_identity_map(self):
        s, u1 = self._failed_session()

        with self.sql_execution_asserter() as asserter:
            s.close()
        asserter.assert_(CountStatements(0))

        eq_(len(s.identity_map), 0)
        assert u1 not in s
        is_true(inspect(u1).detached)

    def test_session_reusable_after_close(self):
        s, u1 = self._failed_session()
        s.close()

        u2 = self.classes.User(name="jack")
        s.add(u2)
        s.commit()

        eq_(self._names(s), ["ed", "jack"])
        eq_(self._user_row_count(), 2)
        s.close()


class AsyncSessionStateTest(_FlushErrorFixture):
    """Same boundaries for AsyncSession; runs against an async dialect."""

    __requires__ = ("async_dialect",)

    @testing.fixture
    def async_engine(self):
        return engines.testing_engine(
            asyncio=True, options={"sqlite_share_pool": True}
        )

    @testing.async_test
    async def test_failed_flush_state_and_recovery(self, async_engine):
        User = self.classes.User
        s = AsyncSession(async_engine)
        try:
            u1 = User(name="ed")
            s.add(u1)
            await s.commit()

            u2 = User(name="ed")
            s.add(u2)
            with expect_raises(sa_exc.IntegrityError):
                await s.flush()

            assert u2 not in s
            assert u1 in s
            is_true(inspect(u1).expired)
            is_true(s.in_transaction())

            with expect_raises(sa_exc.PendingRollbackError):
                await s.execute(select(User))

            await s.rollback()
            u3 = User(name="jack")
            s.add(u3)
            await s.commit()

            eq_(
                (
                    await s.execute(select(User.name).order_by(User.name))
                ).scalars().all(),
                ["ed", "jack"],
            )
        finally:
            await s.close()

    @testing.async_test
    async def test_nested_rollback_continues_outer_transaction(
        self, async_engine
    ):
        User = self.classes.User
        s = AsyncSession(async_engine)
        try:
            u1 = User(name="ed")
            s.add(u1)
            await s.commit()

            class _AbortNested(Exception):
                pass

            # exiting the async context manager with an exception
            # rolls back the deactivated SAVEPOINT, equivalent to
            # nested.rollback() on a synchronous session
            with expect_raises(_AbortNested):
                async with s.begin_nested():
                    s.add(User(name="ed"))
                    with expect_raises(sa_exc.IntegrityError):
                        await s.flush()

                    # while the failed nested transaction is still
                    # current, the outer transaction cannot commit
                    with expect_raises(sa_exc.PendingRollbackError):
                        await s.commit()
                    raise _AbortNested()

            s.add(User(name="jack"))
            await s.commit()

            eq_(
                (
                    await s.execute(select(User.name).order_by(User.name))
                ).scalars().all(),
                ["ed", "jack"],
            )
        finally:
            await s.close()

    @testing.async_test
    async def test_failed_commit_event_order(self, async_engine):
        User = self.classes.User
        s = AsyncSession(async_engine)
        try:
            u1 = User(name="ed")
            s.add(u1)
            await s.commit()

            log = self._track_events(s.sync_session)

            s.add(User(name="ed"))
            with expect_raises(sa_exc.IntegrityError):
                await s.commit()

            eq_(
                log,
                [
                    "before_commit",
                    "before_flush",
                    "after_rollback",
                    "after_transaction_end",
                    "after_soft_rollback",
                ],
            )

            await s.rollback()
        finally:
            await s.close()

    @testing.async_test
    async def test_failed_commit_expires_with_expire_on_commit_off(
        self, async_engine
    ):
        User = self.classes.User
        s = AsyncSession(async_engine, expire_on_commit=False)
        try:
            u1 = User(name="ed")
            s.add(u1)
            await s.commit()
            is_false(inspect(u1).expired)

            u2 = User(name="ed")
            s.add(u2)
            with expect_raises(sa_exc.IntegrityError):
                await s.commit()

            is_true(inspect(u1).expired)
            assert u2 not in s

            await s.rollback()
        finally:
            await s.close()
