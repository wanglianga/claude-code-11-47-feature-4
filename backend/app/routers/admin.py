"""样例数据管理（开发/演示用）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..seed import apply_c2_revision, reseed, seed_large
from .deps import get_db, jsonable

router = APIRouter(prefix="/admin", tags=["样例数据"])


@router.post("/reseed")
def reseed_all(with_large: bool = True, conn=Depends(get_db)):
    """重建小型可手算路网（默认含较大样例）。"""
    reseed(conn)
    large_info = None
    if with_large:
        large_info = seed_large(conn)
    return jsonable({"ok": True, "large_sample_appended": bool(with_large), "detail": large_info})


@router.post("/apply-c2-revision")
def apply_revision(conn=Depends(get_db)):
    """录入第二截止点修订（E7 重开 + E2 新封路）。"""
    return jsonable(apply_c2_revision(conn))
