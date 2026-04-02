from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path


class Settings(BaseSettings):
    # AI
    anthropic_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Trading
    trading_mode: str = "paper"          # paper | live
    initial_portfolio_usd: float = 100.0

    # Database
    db_path: str = "data/polyfarm.db"
    log_level: str = "INFO"

    # Live trading (Phase 3) — optional at startup
    polygon_rpc_url: str = "https://polygon-rpc.com"
    wallet_private_key: str = ""
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    def db_dir(self) -> Path:
        p = Path(self.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
