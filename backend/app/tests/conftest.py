"""pytest 公共夹具：每个测试会话重建一次样例数据库。"""
import os

os.environ.setdefault("FLOOD_DATABASE_URL", "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev")
os.environ.setdefault("FLOOD_REDIS_URL", "redis://127.0.0.1:6379/0")

import pytest

from app.db import get_conn, init_db
from app.seed import C1, C2, T0, reseed, seed_large


@pytest.fixture(scope="session")
def db():
    init_db()
    conn = get_conn()
    reseed(conn)
    seed_large(conn)
    yield conn
    conn.close()


@pytest.fixture()
def fresh_db():
    """需要 C2 修订前状态的测试用：重建小型+大区样例。"""
    conn = get_conn()
    reseed(conn)
    seed_large(conn)
    yield conn
    conn.close()


# 常用时间常量，供各测试引用
CONSTANTS = {"T0": T0, "C1": C1, "C2": C2}
