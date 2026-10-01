import sqlalchemy as sa

url = "sqlite:///file:probe_concurrent2?mode=memory&cache=shared"
keeper = sa.create_engine(url, connect_args={"uri": True})
kc = keeper.connect()
eng = sa.create_engine(url, connect_args={"uri": True}, echo=True)
with eng.begin() as conn:
    conn.execute(sa.text("create table t (id integer primary key, name text)"))

c1 = eng.connect()
c2 = eng.connect()
c1.execute(sa.text("insert into t values (1, 'a')"))
print("in_transaction c1:", c1.in_transaction())
rows = c2.execute(sa.text("select * from t")).all()
print("c2 read while c1 open:", rows)
c1.rollback()
print("after c1 rollback, c2 sees:", c2.execute(sa.text("select * from t")).all())
c2.close()
c1.close()
eng.dispose()
kc.close()
keeper.dispose()
