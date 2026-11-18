"""公共依赖与序列化工具。"""
from __future__ import annotations

import datetime
from typing import Any

from ..db import get_conn


def get_db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def jsonable(obj: Any) -> Any:
    """把数据库行递归转为 JSON 安全结构。"""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    return obj
