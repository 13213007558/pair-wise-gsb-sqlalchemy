"""身份切换类数据库的适配点演示（DBAPI 记录替身）。

替身只记录策略在何时发出了什么 SQL，用于核对事件挂钩的触发时机；
它不证明任何真实数据库的行为。RESET ROLE / DISCARD ALL /
mysql_reset_session 等在目标库上的语义必须在该库实测，
本文件中 SQLite 的任何结果都不可外推。
"""
import sqlite3

from sqlalchemy import create_engine, event, text


class Recorder:
    def __init__(self):
        self.statements = []
        self.fail_next_reset = False


RECORDER = Recorder()
SIMULATED_PREFIXES = ("RESET", "SET ROLE", "SET SESSION", "DISCARD")


class StubCursor:
    """记录所有 SQL；对模拟的外部语句只记录、不执行。"""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, parameters=()):
        RECORDER.statements.append(sql)
        if sql.lstrip().upper().startswith(SIMULATED_PREFIXES):
            if RECORDER.fail_next_reset:
                RECORDER.fail_next_reset = False
                raise sqlite3.OperationalError(
                    "simulated: RESET ROLE rejected by server"
                )
            return self
        self._real.execute(sql, parameters)
        return self

    def fetchone(self):
        return self._real.fetchone()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self._real.close()


class StubConnection(sqlite3.Connection):
    def cursor(self, *args, **kwargs):
        return StubCursor(super().cursor(*args, **kwargs))

    def rollback(self):
        RECORDER.statements.append("ROLLBACK")
        return super().rollback()


def creator():
    return sqlite3.connect(":memory:", factory=StubConnection)


engine = create_engine("sqlite://", creator=creator,
                       pool_reset_on_return="rollback")


@event.listens_for(engine, "reset")
def identity_reset(dbapi_connection, connection_record, reset_state):
    if reset_state.terminate_only:
        return  # 销毁分支：不发任何 SQL
    dbapi_connection.rollback()
    cursor = dbapi_connection.cursor()
    try:
        # 适配点：按目标库替换为 RESET ROLE / DISCARD ALL /
        # mysql_reset_session 等，并验证身份已回到低权限基线
        cursor.execute("RESET ROLE")
        cursor.execute("SET SESSION AUTHORIZATION DEFAULT")
        cursor.execute("DROP TABLE IF EXISTS temp.tmp_task_scratch")
        cursor.execute(
            "SELECT 1 FROM temp.sqlite_master "
            "WHERE name = 'tmp_task_scratch'"
        )
        if cursor.fetchone():
            raise RuntimeError("temp object survived reset")
    except Exception:
        raise  # 池将作废该连接
    finally:
        cursor.close()


print("== A. 正常归还：策略发出身份复位 SQL ==")
with engine.connect() as conn:
    conn.execute(text("CREATE TEMP TABLE tmp_task_scratch (x TEXT)"))
    conn.execute(text("SET ROLE fake_report_writer"))  # 模拟任务提权
    conn.commit()
with engine.connect() as conn:
    conn.execute(text("SELECT 1"))
print("  记录的 SQL:", RECORDER.statements)
assert "RESET ROLE" in RECORDER.statements
assert "SET SESSION AUTHORIZATION DEFAULT" in RECORDER.statements
print("  PASS: 归还时发出身份复位，临时表已清")

print("== B. detach：销毁分支不发任何 SQL ==")
RECORDER.statements.clear()
with engine.connect() as conn:
    conn.execute(text("SELECT 1"))
    conn.detach()
print("  detach 期间记录的 SQL:", RECORDER.statements)
assert not any(s.startswith(SIMULATED_PREFIXES) for s in RECORDER.statements)
print("  PASS: terminate_only 分支保持沉默")

print("== C. 复位失败：连接作废，下一使用者拿到新连接 ==")
RECORDER.statements.clear()
RECORDER.fail_next_reset = True
with engine.connect() as conn:
    doomed = conn.connection.driver_connection
with engine.connect() as conn:
    fresh = conn.connection.driver_connection
assert fresh is not doomed
print("  记录的 SQL:", RECORDER.statements)
print("  PASS: 复位失败后旧连接被丢弃")

print("STUB DEMO PASSED")
