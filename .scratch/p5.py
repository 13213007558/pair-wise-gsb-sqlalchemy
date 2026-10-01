import sqlalchemy as sa

url = "sqlite:///file:probe_concurrent?mode=memory&cache=shared"
keeper = sa.create_engine(url, connect_args={"uri": True})
kc = keeper.connect()
eng = sa.create_engine(url, connect_args={"uri": True})
with eng.begin() as conn:
    conn.execute(sa.text("create table t (id integer primary key, name text)"))

c1 = eng.connect()
c2 = eng.connect()
c1.execute(sa.text("insert into t values (1, 'a')"))  # autobegin, uncommitted
print("c1 inserted, uncommitted")
try:
    rows = c2.execute(sa.text("select * from t")).all()
    print("c2 read while c1 open:", rows)
except Exception as e:
    print("c2 read blocked:", type(e).__name__, e)
c1.commit()
print("c1 committed")
rows = c2.execute(sa.text("select * from t")).all()
print("c2 read after c1 commit:", rows)
c2.close()
c1.close()
eng.dispose()
kc.close()
keeper.dispose()
