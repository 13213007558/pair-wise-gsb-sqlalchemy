import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "u"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)


NAMES = [
    "after_begin",
    "before_flush",
    "after_flush",
    "after_flush_postexec",
    "after_commit",
    "after_rollback",
    "after_soft_rollback",
    "after_transaction_end",
]


def recorder(session):
    tl = []
    for n in NAMES:
        event.listen(session, n, (lambda x: (lambda *a, **k: tl.append(x)))(n))
    return tl


def fresh():
    e = sa.create_engine("sqlite://")
    Base.metadata.create_all[e](e) if False else Base.metadata.create_all(e)
    return e


# T1: nested rollback
s = Session(fresh())
tl = recorder(s)
s.begin()
s.add(User(name="ed"))
s.flush()
nested = s.begin_nested()
s.add(User(name="jack"))
s.flush()
nested.rollback()
s.commit()
print("T1 nested-rollback:", tl)
s.close()

# T2: nested commit
s = Session(fresh())
tl = recorder(s)
s.begin()
s.add(User(name="ed"))
s.flush()
nested = s.begin_nested()
s.add(User(name="jack"))
s.flush()
nested.commit()
s.commit()
print("T2 nested-commit:", tl)
s.close()

# T3: IntegrityError inside savepoint then nested rollback then commit
s = Session(fresh())
tl = recorder(s)
s.begin()
s.add(User(name="ed"))
s.flush()
nested = s.begin_nested()
s.add(User(name="ed"))
try:
    s.flush()
except sa.exc.IntegrityError:
    pass
nested.rollback()
s.add(User(name="jack"))
s.commit()
print("T3 integrity-nested:", tl)
s.close()

# T4: two-level savepoint, inner rollback
s = Session(fresh())
tl = recorder(s)
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
n1.commit()
s.commit()
print("T4 two-level:", tl)
s.close()

# T5: outer rollback after nested commit
s = Session(fresh())
tl = recorder(s)
s.begin()
s.add(User(name="l0"))
s.flush()
n1 = s.begin_nested()
s.add(User(name="l1"))
s.flush()
n1.commit()
s.rollback()
print("T5 outer-rollback:", tl)
s.close()
