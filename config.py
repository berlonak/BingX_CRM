# copyright by berlonak
# telegram: @Kilax123
"""Application configuration. Secrets never belong in source control."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_key: str
    secret_key: str
    allowed_ids: frozenset[int]
    admin_id: int
    db_path: Path
    commission_version: str
    deposit_alert_threshold: Decimal


def read_settings() -> Settings:
    keys = ("BOT_TOKEN", "BINGX_API_KEY", "BINGX_SECRET_KEY", "ALLOWED_TELEGRAM_IDS")
    missing = [k for k in keys if not os.getenv(k, "").strip()]
    if missing:
        raise ValueError("Заполни в .env: " + ", ".join(missing))

    try:
        raw_ids = os.environ["ALLOWED_TELEGRAM_IDS"]
        parts = [part for part in re.split(r"[\s,;]+", raw_ids.strip()) if part]
        ids = frozenset(int(part) for part in parts)
        if not ids or any(x <= 0 for x in ids):
            raise ValueError("invalid IDs")
        admin = int(os.getenv("ADMIN_TELEGRAM_ID", str(next(iter(sorted(ids))))))
    except ValueError as exc:
        raise ValueError("ALLOWED_TELEGRAM_IDS / ADMIN_TELEGRAM_ID: укажи числовые Telegram ID") from exc
    if admin not in ids:
        raise ValueError("ADMIN_TELEGRAM_ID должен входить в ALLOWED_TELEGRAM_IDS")

    version = os.getenv("COMMISSION_API_VERSION", "v1").lower().strip()
    if version not in ("v1", "v2"):
        raise ValueError("COMMISSION_API_VERSION должен быть v1 или v2")

    raw_threshold = os.getenv("DEPOSIT_ALERT_THRESHOLD_USDT", "500").strip()
    try:
        threshold = Decimal(raw_threshold)
        if not threshold.is_finite() or threshold < 0:
            raise ValueError("invalid threshold")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("DEPOSIT_ALERT_THRESHOLD_USDT должен быть числом >= 0") from exc

    path = Path(os.getenv("DATABASE_PATH", "data/bingx_crm.sqlite3"))
    if not path.is_absolute():
        path = BASE_DIR / path

    return Settings(
        os.environ["BOT_TOKEN"].strip(),
        os.environ["BINGX_API_KEY"].strip(),
        os.environ["BINGX_SECRET_KEY"].strip(),
        ids,
        admin,
        path,
        version,
        threshold,
    )
