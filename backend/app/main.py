"""FastAPI 应用入口。

本项目为全栈平台（含 React 前端页面），按任务约定不强制 Swagger UI；
但 FastAPI 自带的 /docs 与 /openapi.json 仍保留，方便接口联调。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_db
from .routers import admin, comparisons, events, exposure, scenarios, sources


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="防汛需求与道路情景覆盖数据分析平台",
    description="时变路网可达人口与证据限制分析（不做选址、分配或调运）",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sources.router)
app.include_router(scenarios.router)
app.include_router(events.router)
app.include_router(comparisons.router)
app.include_router(exposure.router)
app.include_router(admin.router)


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok", "service": "flood-coverage-api"}


@app.get("/", tags=["系统"])
def root():
    return {
        "service": "防汛需求与道路情景覆盖数据分析平台",
        "docs": "/docs",
        "boundary": "仅计算可达人口与证据限制，不做安置点选址、人员分配或物资调运",
    }
