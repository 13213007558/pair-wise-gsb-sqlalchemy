import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "u"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


eng = sa.create_engine("sqlite://", echo=True)
Base.metadata.create_all(eng)

s = Session(eng)
s.begin()
s.add(User(name="u2"))
s.commit()

s.begin()
n1 = s.begin_nested()
s.add(User(name="u3"))
n1.commit()
s.rollback()
print("rows after outer rollback:", s.execute(sa.text("select name from u")).scalars().all())
s.close()
