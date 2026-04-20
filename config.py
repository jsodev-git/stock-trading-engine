"""엔진 전역 설정 — 환경변수 로딩."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    db_host: str = os.getenv("DB_HOST", "localhost")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "stock_trading")
    db_user: str = os.getenv("DB_USER", "postgres")
    db_password: str = os.getenv("DB_PASSWORD", "")

    backend_url: str = os.getenv("BACKEND_URL", "http://localhost:8080")
    internal_api_key: str = os.getenv("INTERNAL_API_KEY", "")

    engine_mode: str = os.getenv("ENGINE_MODE", "DRY")
    market: str = os.getenv("MARKET", "KR")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def is_dry(self) -> bool:
        return self.engine_mode.upper() == "DRY"


config = Config()
