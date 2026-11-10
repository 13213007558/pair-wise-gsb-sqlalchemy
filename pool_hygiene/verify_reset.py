"""归还方案演练：正常归还 / 异常退出 / reset 失败 / 连接失效 / detach。

每个场景打印 PASS/FAIL；结尾连续借出两次，验证第二位调用者
看不到第一位留下的受控测试状态。日志一律使用假业务数据。
"""
import logging
import os
import sys

from sqlalchemy import text

from reset_policy import create_hygienic_engine

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("verify")

DB_URL = f"sqlite:///{os.environ['TMPDIR']}/hygiene_demo.db"
if os.path.exists(DB_URL[10:]):
    os.remove(DB_URL[10:])

engine = create_hygienic_engine(DB_URL, pool_size=5)

with engine.begin() as conn:
    conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, ref TEXT)"))


def raw_id(conn):
    return id(conn.connection.driver_connection)


def report(name, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    if not ok:
        sys.exit(1)


# ---- 场景 1：正常归还，清理后连接被复用 ----
with engine.connect() as conn:
    first_id = raw_id(conn)
    conn.execute(text("CREATE TEMP TABLE tmp_task_scratch (ref TEXT)"))
    conn.execute(
        text("INSERT INTO tmp_task_scratch VALUES ('FAKE-ORDER-1001')")
    )
    conn.execute(text("PRAGMA foreign_keys = ON"))
    conn.commit()
with engine.connect() as conn:
    reused = raw_id(conn) == first_id
    temp_gone = conn.execute(
        text("SELECT count(*) FROM temp.sqlite_master "
             "WHERE name='tmp_task_scratch'")
    ).scalar() == 0
    pragma_ok = conn.execute(text("PRAGMA foreign_keys")).scalar() == 0
report("1 正常归还: 连接复用且状态干净", reused and temp_gone and pragma_ok,
       f"(reused={reused})")

# ---- 场景 2：任务异常退出，残留事务被清除 ----
try:
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO jobs VALUES (1, 'FAKE-ORDER-1002')")
        )
        raise RuntimeError("simulated task crash job=FAKE-JOB-1042")
except RuntimeError:
    pass
with engine.connect() as conn:
    rows = conn.execute(text("SELECT count(*) FROM jobs")).scalar()
report("2 异常退出: 未提交数据已回滚", rows == 0, f"(rows={rows})")

# ---- 场景 3：reset 自身失败，连接不得再被借出 ----
with engine.connect() as conn:
    broken_raw = conn.connection.driver_connection
    broken_raw.close()  # 模拟底层连接被掐断
with engine.connect() as conn:
    fresh_raw = conn.connection.driver_connection
    clean = conn.execute(
        text("SELECT count(*) FROM temp.sqlite_master "
             "WHERE name='tmp_task_scratch'")
    ).scalar() == 0
report("3 reset 失败: 旧连接作废, 借到全新连接",
       fresh_raw is not broken_raw and clean)

# ---- 场景 4：池中连接失效，pre_ping 自动换新 ----
engine2 = create_hygienic_engine(DB_URL, pool_pre_ping=True)
with engine2.connect() as conn:
    dead_raw = conn.connection.driver_connection
dead_raw.close()  # 连接在池中时被外部关闭
with engine2.connect() as conn:
    revived_raw = conn.connection.driver_connection
report("4 连接失效: pre_ping 检测并换新", revived_raw is not dead_raw)
engine2.dispose()

# ---- 场景 5：detach，销毁分支不执行任何 SQL ----
executed = []
from sqlalchemy import event


@event.listens_for(engine, "before_cursor_execute")
def spy(conn, cursor, statement, parameters, context, executemany):
    executed.append(statement)


with engine.connect() as conn:
    conn.execute(text("SELECT 1"))
    conn.detach()
detach_window = [s for s in executed if "DROP" in s or "PRAGMA" in s.upper()]
report("5 detach: 销毁分支未执行清理 SQL", detach_window == [],
       f"(intercepted={detach_window})")

# ---- 终验：连续借出两次，第二次看不到第一次的受控状态 ----
with engine.connect() as conn:
    conn.execute(text("CREATE TEMP TABLE tmp_task_scratch (ref TEXT)"))
    conn.execute(
        text("INSERT INTO tmp_task_scratch VALUES ('FAKE-ORDER-1099')")
    )
    conn.execute(text("PRAGMA foreign_keys = ON"))
    conn.commit()
with engine.connect() as conn:
    temp_gone = conn.execute(
        text("SELECT count(*) FROM temp.sqlite_master "
             "WHERE name='tmp_task_scratch'")
    ).scalar() == 0
    pragma_ok = conn.execute(text("PRAGMA foreign_keys")).scalar() == 0
report("终验: 第二次借出看不到第一次的临时表与 PRAGMA",
       temp_gone and pragma_ok)

print("ALL DRILLS PASSED")
