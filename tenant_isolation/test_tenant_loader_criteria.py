"""Real-loading tests: scope of tenant isolation via with_loader_criteria.

Two tenants plus cross-relationship rows are built in a temporary SQLite
database.  Every entry point (plain select, aliased, joinedload,
selectinload, lazy load, Session.get / identity map, Core select, text())
is exercised separately, asserting on BOTH the returned objects/collections
AND the SQL actually sent to the database.

Conclusions encoded here (SQLAlchemy 2.1.x, this repo):

* default config (include_aliases=False, propagate_to_loaders=True) covers
  plain selects, joins, joinedload, selectinload, lazy load and Session.get
  for objects not yet in the identity map;
* aliased() entities are NOT covered unless include_aliases=True;
* Core select of a Table and text() are different entry points and are NOT
  covered at all;
* the identity map is a different entry point: objects/collections already
  loaded in the same Session are served without SQL and are NOT re-filtered
  when the tenant changes;
* closure-variable tenant values are tracked in the cache key, so changing
  the tenant does not serve stale compiled SQL; calling a function inside
  the criteria lambda is rejected outright;
* propagate_to_loaders=False drops protection for joinedload and lazy
  loaders (selectinload still receives the option via its secondary
  statement).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, exc, ForeignKey, select, String, text
from sqlalchemy.orm import (
    aliased,
    DeclarativeBase,
    joinedload,
    Mapped,
    mapped_column,
    relationship,
    selectinload,
    Session,
    with_loader_criteria,
)
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))


class TenantMixin:
    """Unmapped base carrying the tenant column; the criteria target."""

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))


class User(TenantMixin, Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    addresses: Mapped[list["Address"]] = relationship(back_populates="user")


class Address(TenantMixin, Base):
    __tablename__ = "address"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    email: Mapped[str] = mapped_column(String(30))
    user: Mapped[User] = relationship(back_populates="addresses")


TENANT_A = 1
TENANT_B = 2
# address 4 belongs to user 1 (tenant A) but is itself tagged tenant B:
# probes that the related entity's own tenant column is what gets filtered.
CROSS_TENANT_ADDRESS = 4


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    with Session(eng) as sess:
        sess.add_all(
            [
                Tenant(id=TENANT_A, name="a"),
                Tenant(id=TENANT_B, name="b"),
                User(id=1, name="u1", tenant_id=TENANT_A),
                User(id=2, name="u2", tenant_id=TENANT_B),
                Address(id=1, user_id=1, email="a1@x", tenant_id=TENANT_A),
                Address(id=2, user_id=1, email="a2@x", tenant_id=TENANT_A),
                Address(id=3, user_id=2, email="a3@x", tenant_id=TENANT_B),
                Address(
                    id=CROSS_TENANT_ADDRESS,
                    user_id=1,
                    email="cross@x",
                    tenant_id=TENANT_B,
                ),
            ]
        )
        sess.commit()
    yield eng
    eng.dispose()


@pytest.fixture()
def tenant_ctx():
    """Mutable tenant handle, as provided by a request-scoped context var."""
    return {"tenant_id": TENANT_A}


@pytest.fixture()
def sql_log(engine):
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    yield statements
    event.remove(engine, "before_cursor_execute", capture)


@pytest.fixture()
def session_factory(engine, tenant_ctx):
    """Sessions wired with the canonical do_orm_execute tenant recipe."""

    def factory(**criteria_kwargs):
        sess = Session(engine)

        @event.listens_for(sess, "do_orm_execute")
        def add_tenant_criteria(orm_execute_state):
            if (
                orm_execute_state.is_select
                and not orm_execute_state.is_column_load
                and not orm_execute_state.is_relationship_load
            ):
                # bind the tenant to a plain local value: a closure over a
                # mutable context object (dict/contextvar) is rejected by
                # the lambda cache-key tracker, while a plain int/str is
                # tracked and keeps the compiled cache correct per tenant.
                current_tenant_id = tenant_ctx["tenant_id"]
                orm_execute_state.statement = (
                    orm_execute_state.statement.options(
                        with_loader_criteria(
                            TenantMixin,
                            lambda cls: cls.tenant_id == current_tenant_id,
                            **criteria_kwargs,
                        )
                    )
                )

        return sess

    return factory


def tenant_filtered(sql_log):
    """SQL statements in the log that carry a tenant_id predicate."""
    return [stmt for stmt, _ in sql_log if "tenant_id" in stmt.lower()]


# ---------------------------------------------------------------------------
# protected entry points, default configuration
# ---------------------------------------------------------------------------


def test_plain_select(session_factory, sql_log):
    sess = session_factory()
    users = sess.scalars(select(User).order_by(User.id)).all()

    assert [u.id for u in users] == [1]
    assert tenant_filtered(sql_log), sql_log


def test_explicit_join_filters_on_clause(session_factory, sql_log):
    sess = session_factory()
    rows = sess.execute(
        select(User.name, Address.email).join(User.addresses)
    ).all()

    assert sorted(email for _, email in rows) == ["a1@x", "a2@x"]
    join_sql = sql_log[-1][0]
    assert "address.tenant_id" in join_sql


def test_joinedload(session_factory, sql_log):
    sess = session_factory()
    users = sess.scalars(
        select(User).options(joinedload(User.addresses))
    ).unique().all()

    assert [(u.id, sorted(a.id for a in u.addresses)) for u in users] == [
        (1, [1, 2])
    ]
    join_sql = sql_log[-1][0]
    # joined eager load aliases the target table (address_1)
    assert "JOIN" in join_sql and "address_1.tenant_id" in join_sql


def test_selectinload(session_factory, sql_log):
    sess = session_factory()
    users = sess.scalars(
        select(User).options(selectinload(User.addresses))
    ).unique().all()

    assert [(u.id, sorted(a.id for a in u.addresses)) for u in users] == [
        (1, [1, 2])
    ]
    secondary_sql, secondary_params = sql_log[-1]
    assert "IN" in secondary_sql
    assert "address.tenant_id" in secondary_sql
    assert TENANT_A in (
        secondary_params
        if isinstance(secondary_params, tuple)
        else tuple(secondary_params)
    )


def test_lazy_load(session_factory, sql_log):
    sess = session_factory()
    user = sess.scalars(select(User)).one()
    sql_log.clear()

    addresses = user.addresses

    assert sorted(a.id for a in addresses) == [1, 2]
    lazy_sql = sql_log[-1][0]
    assert "address.tenant_id" in lazy_sql


def test_lazy_load_after_get(session_factory, sql_log):
    """get() loads carry the criteria options, so later lazy loads of the
    same instance stay filtered."""
    sess = session_factory()
    user = sess.get(User, 1)
    sql_log.clear()

    addresses = user.addresses

    assert sorted(a.id for a in addresses) == [1, 2]
    assert "address.tenant_id" in sql_log[-1][0]


def test_get_filters_when_not_in_identity_map(session_factory, sql_log):
    sess = session_factory()

    assert sess.get(User, 2) is None
    get_sql = sql_log[-1][0]
    assert "tenant_id" in get_sql


def test_closure_tenant_change_does_not_go_stale(
    session_factory, tenant_ctx, sql_log
):
    """Same Session, same statement shape, tenant changed between runs:
    the compiled-statement cache must not serve the old tenant's SQL."""
    sess = session_factory()

    assert [u.id for u in sess.scalars(select(User))] == [1]
    tenant_ctx["tenant_id"] = TENANT_B
    assert [u.id for u in sess.scalars(select(User))] == [2]

    select_params = [params for stmt, params in sql_log if "FROM user" in stmt]
    assert select_params[0] == (TENANT_A,) or TENANT_A in select_params[0]
    assert select_params[1] == (TENANT_B,) or TENANT_B in select_params[1]


# ---------------------------------------------------------------------------
# unprotected entry points / leak paths
# ---------------------------------------------------------------------------


def test_identity_map_get_bypasses_criteria(
    session_factory, tenant_ctx, sql_log
):
    """An object already in the identity map is returned without SQL, so a
    tenant switch on a reused Session leaks it."""
    tenant_ctx["tenant_id"] = TENANT_B
    sess = session_factory()
    user_b = sess.get(User, 2)
    assert user_b is not None

    tenant_ctx["tenant_id"] = TENANT_A
    sql_log.clear()
    leaked = sess.get(User, 2)

    assert leaked is user_b  # no SQL, no re-filtering
    assert sql_log == []


def test_loaded_collection_not_refiltered_on_tenant_switch(
    session_factory, tenant_ctx, sql_log
):
    sess = session_factory()
    user = sess.scalars(select(User)).one()
    assert sorted(a.id for a in user.addresses) == [1, 2]

    tenant_ctx["tenant_id"] = TENANT_B
    sql_log.clear()

    # stale collection served from the identity map, no SQL emitted
    assert sorted(a.id for a in user.addresses) == [1, 2]
    assert sql_log == []


def test_expire_all_does_not_cut_scalar_refresh(
    session_factory, tenant_ctx, sql_log
):
    """expire_all() is NOT a valid cut: refreshing an expired instance is a
    column-load path that the canonical recipe skips, so the expired object
    is re-fetched without any tenant predicate."""
    sess = session_factory()
    user = sess.scalars(select(User)).one()
    _ = user.addresses

    tenant_ctx["tenant_id"] = TENANT_B
    sess.expire_all()
    sql_log.clear()

    # leak: tenant A's row refreshed under a tenant B context
    assert sess.get(User, 1) is user
    refresh_sql = sql_log[-1][0]
    assert "WHERE" in refresh_sql
    assert "tenant_id" not in refresh_sql.split("WHERE")[-1]


def test_lazy_load_after_tenant_switch_keeps_load_time_tenant(
    session_factory, tenant_ctx, sql_log
):
    """The criteria option travels with each loaded instance; a lazy
    (re)load after the tenant changed still uses the tenant value captured
    when the instance was first loaded."""
    sess = session_factory()
    user = sess.scalars(select(User)).one()
    _ = user.addresses

    tenant_ctx["tenant_id"] = TENANT_B
    sess.expire(user, ["addresses"])
    sql_log.clear()

    assert sorted(a.id for a in user.addresses) == [1, 2]
    lazy_sql, lazy_params = sql_log[-1]
    assert "address.tenant_id" in lazy_sql
    assert TENANT_A in lazy_params  # stale: load-time tenant, not current


def test_expunge_all_cuts_the_stale_path(
    session_factory, tenant_ctx, sql_log
):
    """Recommended cut when a Session must be reused across tenants:
    expunge (or close) so nothing is served from the identity map and every
    new load is compiled under the current tenant."""
    sess = session_factory()
    user = sess.scalars(select(User)).one()
    _ = user.addresses

    tenant_ctx["tenant_id"] = TENANT_B
    sess.expunge_all()
    sql_log.clear()

    assert sess.get(User, 1) is None
    assert [u.id for u in sess.scalars(select(User))] == [2]
    assert tenant_filtered(sql_log)


def test_aliased_bypasses_by_default(session_factory, sql_log):
    user_alias = aliased(User)
    sess = session_factory()

    users = sess.scalars(select(user_alias)).all()

    assert sorted(u.id for u in users) == [1, 2]
    assert "WHERE" not in sql_log[-1][0]


def test_core_select_bypasses(session_factory, sql_log):
    sess = session_factory()

    rows = sess.execute(select(User.__table__)).all()

    assert len(rows) == 2
    assert "WHERE" not in sql_log[-1][0]


def test_text_sql_bypasses(session_factory, sql_log):
    sess = session_factory()

    rows = sess.execute(text("SELECT * FROM user")).all()

    assert len(rows) == 2


# ---------------------------------------------------------------------------
# configuration matrix
# ---------------------------------------------------------------------------


def test_include_aliases_covers_aliased(session_factory, sql_log):
    user_alias = aliased(User)
    sess = session_factory(include_aliases=True)

    users = sess.scalars(select(user_alias)).all()

    assert [u.id for u in users] == [1]
    assert "tenant_id" in sql_log[-1][0]


def test_propagate_to_loaders_false(session_factory, sql_log):
    """propagate_to_loaders=False: top-level select stays filtered, but
    joinedload and lazy load lose the tenant predicate (selectinload keeps
    it, because its secondary statement copies the parent options)."""
    sess = session_factory(propagate_to_loaders=False)

    # top-level still filtered
    assert [u.id for u in sess.scalars(select(User))] == [1]

    # joinedload: ON clause loses the tenant predicate -> cross-tenant row
    sess = session_factory(propagate_to_loaders=False)
    users = sess.scalars(
        select(User).options(joinedload(User.addresses))
    ).unique().all()
    assert sorted(a.id for a in users[0].addresses) == [
        1,
        2,
        CROSS_TENANT_ADDRESS,
    ]

    # lazy load: option not carried by the instance -> unfiltered
    sess = session_factory(propagate_to_loaders=False)
    user = sess.scalars(select(User)).one()
    sql_log.clear()
    assert sorted(a.id for a in user.addresses) == [1, 2, CROSS_TENANT_ADDRESS]
    lazy_sql = sql_log[-1][0]
    assert "tenant_id" not in lazy_sql.split("WHERE")[-1]

    # selectinload: secondary statement still carries the criteria
    sess = session_factory(propagate_to_loaders=False)
    users = sess.scalars(
        select(User).options(selectinload(User.addresses))
    ).unique().all()
    assert sorted(a.id for a in users[0].addresses) == [1, 2]


def test_function_call_inside_lambda_rejected(engine, tenant_ctx):
    """Calling a function inside the criteria lambda would bake the tenant
    value into the cached SQL; the library rejects it instead of serving
    stale results."""

    def current_tenant():
        return tenant_ctx["tenant_id"]

    sess = Session(engine)

    @event.listens_for(sess, "do_orm_execute")
    def add_bad_criteria(orm_execute_state):
        if orm_execute_state.is_select:
            orm_execute_state.statement = orm_execute_state.statement.options(
                with_loader_criteria(
                    TenantMixin,
                    lambda cls: cls.tenant_id == current_tenant(),
                )
            )

    with pytest.raises(exc.InvalidRequestError, match="Can't invoke"):
        sess.scalars(select(User)).all()
