# copyright by berlonak
# telegram: @Kilax123
"""Run: python main.py"""
import logging
from config import read_settings
from bingx import BingXClient
from storage import Database
from service import CRM
from bot import create_application


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep service logs useful; hide noisy transport/scheduler internals.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    settings = read_settings()
    db = Database(settings.db_path)
    api = BingXClient(settings.api_key, settings.secret_key, settings.commission_version)
    crm = CRM(db, api)
    app = create_application(settings, db, crm)
    print("BingX CRM запущен. Доступные Telegram ID:",
          ", ".join(map(str, sorted(settings.allowed_ids))))
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
