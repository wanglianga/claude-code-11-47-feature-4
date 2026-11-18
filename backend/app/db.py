"""PostgreSQL 连接与初始化。"""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_conn() -> psycopg.Connection:
    """同步连接（引擎为 CPU 密集的本地计算，使用同步驱动 + 线程池即可）。

    使用 autocommit：所有写流程在服务层显式 commit；只读请求不会留下持有快照的
    长事务，避免与重建（TRUNCATE/DDL）互相阻塞。
    """
    conn = psycopg.connect(
        settings.database_url, row_factory=dict_row, autocommit=True
    )
    return conn


def init_db() -> None:
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
