import sqlalchemy as sa

url = "sqlite:///file:probe_concurrent3?mode=memory&cache=shared"
keeper = sa.create_engine(url, connect_args={"uri": True})
kc = keeper.connect()
eng = sa.create_engine(url, connect_args={"uri": True})
with eng.begin() as conn:
    conn.execute(sa.text("create table t (id integer primary key, name text)"))

c2 = eng.connect()
print("c2 read:", c2.execute(sa.text("select count(*) from t")).scalar())
# c2's transaction stays open
c1 = eng.connect()
try:
    c1.execute(sa.text("insert into t values (1, 'a')"))
    c1.commit()
    print("c1 wrote and committed while c2 txn open")
except Exception as e:
    print("c1 blocked:", type(e).__name__, str(e)[:80])
print("c2 sees:", c2.execute(sa.text("select * from t")).all())
c2.rollback()
print("c2 after re-read:", c2.execute(sa.text("select * from t")).all())
c2.rollback()
c1.close()
c2.close()
eng.dispose()
kc.close()
keeper.dispose()
