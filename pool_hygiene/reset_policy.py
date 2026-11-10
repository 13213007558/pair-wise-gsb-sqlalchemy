"""连接归还卫生策略：可直接并入 Engine 创建代码。

依据 proof_rollback.py 的实测结论选择策略：
  rollback 能清除   -> 未提交事务、事务内 DML/DDL、事务句柄
  rollback 不能清除 -> 已提交的临时对象、会话级设置（PRAGMA/SET/ROLE）
因此 pool_reset_on_return="rollback" 只是底线，目标状态必须在
reset 事件中显式恢复并验证；验证不过就抛出，让池作废该连接，
下一使用者只能拿到新连接。销毁分支（terminate_only）不执行任何 SQL。
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, event
from sqlalchemy.exc import DisconnectionError

logger = logging.getLogger("pool_hygiene")

CONTROLLED_TEMP_TABLES = ("tmp_task_scratch",)
BASELINE_PRAGMAS = {"foreign_keys": 0}


def _restore_baseline(cursor) -> None:
    for name in CONTROLLED_TEMP_TABLES:
        cursor.execute(f"DROP TABLE IF EXISTS temp.{name}")
    for pragma, value in BASELINE_PRAGMAS.items():
        cursor.execute(f"PRAGMA {pragma} = {value}")


def _assert_baseline(cursor) -> None:
    for name in CONTROLLED_TEMP_TABLES:
        cursor.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE name = ?", (name,)
        )
        if cursor.fetchone():
            raise RuntimeError(f"temp object survived reset: {name}")
    for pragma, value in BASELINE_PRAGMAS.items():
        cursor.execute(f"PRAGMA {pragma}")
        if cursor.fetchone()[0] != value:
            raise RuntimeError(f"pragma {pragma} not at baseline {value}")


def create_hygienic_engine(url="sqlite://", **kwargs):
    kwargs.setdefault("pool_reset_on_return", "rollback")
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "reset")
    def hygienic_reset(dbapi_connection, connection_record, reset_state):
        if reset_state.terminate_only:
            # detach / GC 销毁分支：连接随即关闭，只销毁，不发任何 SQL
            return
        # 先结束残留事务：否则清理动作会被池随后的 rollback 抵消
        dbapi_connection.rollback()
        cursor = dbapi_connection.cursor()
        try:
            _restore_baseline(cursor)
            _assert_baseline(cursor)
        except Exception as exc:
            logger.warning(
                "reset unverified, discarding connection "
                "tenant=fake-tenant-007 job=FAKE-JOB-1042 error=%r",
                exc,
            )
            # 抛出 -> 池捕获并 invalidate -> 该连接不会回到池中
            raise
        finally:
            cursor.close()

    @event.listens_for(engine, "checkout")
    def checkout_guard(dbapi_connection, connection_record, connection_proxy):
        # 纵深防御：借出前确认基线；不合格则作废，池自动换新连接重试
        cursor = dbapi_connection.cursor()
        try:
            _assert_baseline(cursor)
        except Exception as exc:
            logger.error(
                "checkout guard rejected connection "
                "tenant=fake-tenant-007 job=FAKE-JOB-1042 error=%r",
                exc,
            )
            raise DisconnectionError(
                "connection failed baseline check"
            ) from exc
        finally:
            cursor.close()

    @event.listens_for(engine, "invalidate")
    def log_invalidate(dbapi_connection, connection_record, exception):
        logger.info(
            "connection invalidated tenant=fake-tenant-007 reason=%r",
            exception,
        )

    return engine
