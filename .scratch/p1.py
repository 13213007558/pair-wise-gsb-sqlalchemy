import sqlalchemy as sa
from sqlalchemy.orm import Session, DeclarativeBase, Mapped, mapped_column
from sqlalchemy import event


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "u"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)


def fresh_engine():
    e = sa.create_engine("sqlite://", echo=False)
    Base.metadata.create_all(e)
    return e


eng = fresh_engine()

timeline = []
for name in [
    "after_begin",
    "after_commit",
    "after_rollback",
    "after_soft_rollback",
    "before_flush",
    "after_flush",
    "after_transaction_end",
]:
    event.listen(Session, name, (lambda n: (lambda *a, **k: timeline.append(n)))(name))

# Scenario A: IntegrityError inside savepoint, rollback nested, outer commits
s = Session(eng)
s.begin()
u1 = User(name="ed")
s.add(u1)
s.flush()
nested = s.begin_nested()
u2 = User(name="ed")  # duplicate
s.add(u2)
try:
    s.flush()
except sa.exc.IntegrityError:
    print("A: got IntegrityError")
nested.rollback()
print("A: u2 in session after nested rollback:", u2 in s)
u3 = User(name="jack")
s.add(u3)
s.commit()
print("A: rows:", s.execute(sa.text("select name from u order by id")).all())
print("A: timeline:", timeline)
s.close()

# Scenario B: value restore on nested rollback must not clobber newer values
timeline.clear()
s = Session(fresh_engine())
s.begin()
u = User(name="ed")
s.add(u)
s.flush()
u.name = "edward"  # dirty before savepoint
nested = s.begin_nested()
u.name = "eddy"  # dirty inside savepoint
s.flush()
nested.rollback()
print("B: name after nested rollback:", u.name)
u.name = "final"
s.commit()
print("B: name after commit:", u.name)
print("B: db:", s.execute(sa.text("select name from u")).all())
s.close()

# Scenario C: flush error outside savepoint -> PendingRollbackError
s = Session(fresh_engine())
s.begin()
s.add(User(name="dup"))
s.flush()
s.add(User(name="dup"))
try:
    s.flush()
except sa.exc.IntegrityError:
    print("C: got IntegrityError")
try:
    s.add(User(name="x"))
    s.flush()
except sa.exc.PendingRollbackError as e:
    print("C: PendingRollbackError:", str(e)[:60])
s.rollback()
print("C: usable after rollback:", s.execute(sa.text("select 1")).scalar())
s.close()

# Scenario D: two-level savepoints
s = Session(fresh_engine())
s.begin()
s.add(User(name="l0"))
s.flush()
n1 = s.begin_nested()
s.add(User(name="l1"))
s.flush()
n2 = s.begin_nested()
s.add(User(name="l2"))
s.flush()
n2.rollback()
print("D: after n2 rollback rows in-txn:", s.execute(sa.text("select name from u order by id")).scalars().all())
n1.commit()
s.commit()
print("D: final rows:", s.execute(sa.text("select name from u order by id")).scalars().all())
s.close()
