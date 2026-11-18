"""应用配置。所有连接信息来自环境变量，容器与本地开发均可覆盖。"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLOOD_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 常规出发窗口（分钟偏移，相对人口基准日 00:00），启动后自动预计算
    precompute_windows: str = "0,30,60,90,120"
    default_deadline_minutes: int = 60
    cors_origins: str = "http://localhost:5173,http://localhost:8080"


settings = Settings()
