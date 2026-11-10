"""实测：rollback 在 SQLite 上能清除什么、不能清除什么。

结论用于决定 reset 策略，不随数据库不同而改变的部分只有一点：
rollback 只管事务，不管会话。临时对象是否事务性因库而异，
会话设置（PRAGMA / SET / ROLE）在任何库上都不归 rollback 管。
"""
import sqlite3

conn = sqlite3.connect(":memory:", isolation_level=None)
cur = conn.cursor()

print("== 1. 未提交的 DML：rollback 能清除 ==")
cur.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, ref TEXT)")
cur.execute("BEGIN")
cur.execute("INSERT INTO jobs VALUES (1, 'FAKE-ORDER-1001')")
cur.execute("ROLLBACK")
print("  rollback 后行数:", cur.execute("SELECT count(*) FROM jobs").fetchone()[0])

print("== 2. 事务内的 DDL（SQLite 特有）：rollback 能清除 ==")
cur.execute("BEGIN")
cur.execute("CREATE TEMP TABLE tmp_in_txn (x TEXT)")
cur.execute("ROLLBACK")
gone = cur.execute(
    "SELECT count(*) FROM temp.sqlite_master WHERE name='tmp_in_txn'"
).fetchone()[0] == 0
print("  事务内建的临时表已消失:", gone)

print("== 3. 已提交的临时对象：rollback 不能清除 ==")
cur.execute("CREATE TEMP TABLE tmp_task_scratch (ref TEXT)")
cur.execute("INSERT INTO tmp_task_scratch VALUES ('FAKE-ORDER-1002')")
cur.execute("BEGIN")
cur.execute("INSERT INTO jobs VALUES (9, 'FAKE-ORDER-1009')")
cur.execute("ROLLBACK")  # 回滚的是新事务，与已提交的临时表无关
row = cur.execute("SELECT ref FROM tmp_task_scratch").fetchone()
print("  rollback 后临时表仍在，数据:", row)

print("== 4. 会话级设置：rollback 不能清除 ==")
cur.execute("PRAGMA foreign_keys = ON")
cur.execute("BEGIN")
cur.execute("INSERT INTO jobs VALUES (2, 'FAKE-ORDER-1003')")
cur.execute("ROLLBACK")
print("  rollback 后 foreign_keys =",
      cur.execute("PRAGMA foreign_keys").fetchone()[0])

print("== 5. 事务句柄本身：rollback 能清除 ==")
cur.execute("BEGIN")
cur.execute("ROLLBACK")
print("  in_transaction:", conn.in_transaction)
