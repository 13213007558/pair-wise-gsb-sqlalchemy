import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "u"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


def fresh():
    e = sa.create_engine("sqlite://")
    Base.metadata.create_all(e)
    return e


# pattern 1: pending flush inside begin_nested establishes BEGIN first
s = Session(fresh())
s.begin()
s.add(User(name="u0"))
n1 = s.begin_nested()  # flushes u0 -> implicit BEGIN -> savepoint nested
s.add(User(name="u3"))
n1.commit()
s.rollback()
print("P1 rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()

# pattern 2: explicit flush before begin_nested
s = Session(fresh())
s.begin()
s.add(User(name="u0"))
s.flush()
n1 = s.begin_nested()
s.add(User(name="u3"))
n1.commit()
s.rollback()
print("P2 rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()

# pattern 3: SELECT before begin_nested (no DML) - still broken?
s = Session(fresh())
s.begin()
s.execute(sa.text("select 1"))
n1 = s.begin_nested()
s.add(User(name="u3"))
n1.commit()
s.rollback()
print("P3 rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()

# pattern 4: autocommit=False connect arg (documented remedy)
e = sa.create_engine("sqlite://", connect_args={"autocommit": False})
Base.metadata.create_all(e)
s = Session(e)
s.begin()
n1 = s.begin_nested()
s.add(User(name="u3"))
n1.commit()
s.rollback()
print("P4 rows:", s.execute(sa.text("select name from u")).scalars().all())
s.close()
