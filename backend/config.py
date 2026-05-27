"""Application settings — env vars + ~/.poly-scraper/ data dir."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    return Path.home() / ".poly-scraper"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8000
    frontend_origin: str = "http://localhost:3000"

    data_dir: Path = Field(default_factory=_default_data_dir)

    jwt_secret: str = "change-me-on-first-run"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 15

    # Default RPC: publicnode.com (no key required, ~250ms). The original
    # `polygon-rpc.com` started requiring API keys in 2026 ("tenant disabled")
    # — override via POLYGON_RPC_URL env if you have a paid Alchemy/Infura/
    # QuickNode endpoint.
    polygon_rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"

    the_odds_api_key: str | None = None

    # Webshare datacenter proxies as a comma-separated list of
    # `host:port:user:pass` (single line). Empty → scrapers run direct.
    webshare_proxies: str | None = None
    # Failures in a row before a proxy is parked in cooldown.
    proxy_block_threshold: int = 3
    # How long a burned proxy stays out of rotation.
    proxy_cooldown_minutes: int = 10

    @property
    def db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "db.sqlite"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def salt_path(self) -> Path:
        return self.data_dir / "salt.bin"

    @property
    def verifier_path(self) -> Path:
        """Fernet-encrypted known plaintext — used to verify the master password
        is correct on login without needing a wallet to already be configured."""
        return self.data_dir / "verifier.bin"


settings = Settings()
