import sqlalchemy as sa
from sqlalchemy.orm import Session, DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "u"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)


def fresh():
    e = sa.create_engine("sqlite://")
    Base.metadata.create_all(e)
    return e


# E: flush fails inside savepoint, user does NOT roll back nested, keeps going
s = Session(fresh())
s.begin()
s.add(User(name="ed"))
s.flush()
nested = s.begin_nested()
s.add(User(name="ed"))
try:
    s.flush()
except sa.exc.IntegrityError:
    print("E: IntegrityError")
print("E: is_active:", s.is_active, "in_nested:", s.in_nested_transaction())
try:
    s.add(User(name="more"))
    s.flush()
    print("E: second flush OK")
except sa.exc.PendingRollbackError as e:
    print("E: PendingRollbackError on reuse:", str(e)[:70])
except Exception as e:
    print("E: other:", type(e).__name__, str(e)[:70])
# recover via nested rollback
nested.rollback()
s.add(User(name="ok"))
s.commit()
print("E: rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()

# F: autoflush triggers IntegrityError inside savepoint
s = Session(fresh())
s.begin()
s.add(User(name="ed"))
s.flush()
nested = s.begin_nested()
s.add(User(name="ed"))
try:
    s.execute(sa.text("select 1"))  # core execute does not autoflush
    from sqlalchemy import select
    s.scalars(select(User))  # ORM query triggers autoflush
except sa.exc.IntegrityError:
    print("F: autoflush IntegrityError")
nested.rollback()
s.add(User(name="zed"))
s.commit()
print("F: rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()

# G: delete inside savepoint, rollback, object usable again
s = Session(fresh())
u = User(name="ed")
s.add(u)
s.commit()
nested = s.begin_nested()
s.delete(u)
s.flush()
print("G: deleted in txn:", s.execute(sa.text("select count(*) from u")).scalar())
nested.rollback()
print("G: u in session:", u in s, "deleted:", u in s.deleted)
print("G: name access:", u.name)
s.commit()
print("G: rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()

# H: expire_on_commit with nested commit
s = Session(fresh(), expire_on_commit=True)
u = User(name="ed")
s.add(u)
s.commit()
u.name = "edward"
nested = s.begin_nested()
nested.commit()
from sqlalchemy import inspect as sa_inspect
print("H: expired after nested commit:", sa_inspect(u).expired)
print("H: name reloads:", u.name)
s.rollback()
s.close()

# I: outer rollback after nested commit discards everything
s = Session(fresh())
s.begin()
s.add(User(name="l0"))
s.flush()
n1 = s.begin_nested()
s.add(User(name="l1"))
s.flush()
n1.commit()
s.rollback()
print("I: rows after outer rollback:", s.execute(sa.text("select name from u")).scalars().all())
s.close()
