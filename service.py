# copyright by berlonak
# telegram: @Kilax123
"""Reporting and synchronization logic, independent of Telegram."""
from __future__ import annotations
import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import logging

from storage import Database, amount, sum_money, ZERO, CN_TZ, MSK
from bingx import BingXClient

LOG = logging.getLogger(__name__)


def money(value, places=2):
    if value is None:
        return "—"
    number = amount(value)
    text = f"{number:,.{places}f}".replace(",", " ") if places else f"{number:,.0f}".replace(",", " ")
    return text.rstrip("0").rstrip(".") if places else text


def compact_money(value):
    if value is None:
        return "—"
    number = amount(value)
    absolute = abs(number)
    if absolute >= Decimal("1000000000"):
        return f"{number / Decimal('1000000000'):.2f}".rstrip("0").rstrip(".") + "B"
    if absolute >= Decimal("1000000"):
        return f"{number / Decimal('1000000'):.2f}".rstrip("0").rstrip(".") + "M"
    if absolute >= Decimal("1000"):
        return f"{number / Decimal('1000'):.1f}".rstrip("0").rstrip(".") + "K"
    return money(number)


def last_trade(db, uid):
    days = [item["day"] for item in db.daily(uid) if amount(item["volume_usdt"]) > ZERO]
    return max(days) if days else None


def idle_days(last_day, as_of):
    if not last_day or not as_of:
        return None
    return max(0, (date.fromisoformat(as_of) - date.fromisoformat(last_day)).days)


def status(db, user):
    if not db.meta("commission_synced_at"):
        return "⏳ Нет статистики", None
    last = last_trade(db, user["uid"])
    if not last:
        return ("⚪ Не торговал" if not user["has_traded"] else "❔ История недоступна"), None
    days = idle_days(last, db.meta("commission_as_of"))
    return (f"🔴 Неактивен {days} д." if days is not None and days > 3 else "🟢 Активен"), days


def _period_start_day(db, days: int):
    as_of = db.meta("commission_as_of")
    if not as_of:
        return None
    return (date.fromisoformat(as_of) - timedelta(days=max(1, int(days)) - 1)).isoformat()


def _period_deposit_start_ms(days: int):
    start_day = datetime.now(MSK).date() - timedelta(days=max(1, int(days)) - 1)
    return int(datetime.combine(start_day, time.min, MSK).timestamp() * 1000)


def user_metrics(db, uid, days=90):
    user = db.user(uid)
    if not user:
        return None
    start_day = _period_start_day(db, days)
    daily = db.daily(uid, start_day=start_day) if start_day else []
    deps = db.deposits(uid, start_ms=_period_deposit_start_ms(days))
    # BingX Agent API gives deposit history only for the rolling 90-day window.
    # Keep the latest confirmed record from that full stored window independent
    # of the currently selected 30/60/90-day dashboard period.
    all_deps = db.deposits(uid)
    good_c = bool(db.meta("commission_synced_at"))
    good_d = db.deposit_status(uid) == "ok"
    last_deposit = all_deps[0] if good_d and all_deps else None
    stat, idle = status(db, user)
    return {
        **user,
        "period": int(days),
        "volume": sum_money(x["volume_usdt"] for x in daily) if good_c else None,
        "commission": sum_money(x["commission_usdt"] for x in daily) if good_c else None,
        "deposit_usdt": sum_money(x["amount"] for x in deps if x["currency"] == "USDT") if good_d else None,
        "deposits": deps if good_d else None,
        "last_deposit": last_deposit,
        "last_deposit_known": good_d,
        "balance": amount(user["balance_usdt"]) if user["balance_usdt"] is not None else None,
        "last_trade": last_trade(db, uid),
        "status": stat,
        "idle": idle,
    }


def all_metrics(db, days=90):
    return [user_metrics(db, u["uid"], days) for u in db.users()]


def totals(db, users=None, days=90):
    rows = users if users is not None else all_metrics(db, days)
    return {
        "total": len(rows),
        "active": sum(u["status"].startswith("🟢") for u in rows),
        "inactive": sum(u["status"].startswith("🔴") for u in rows),
        "never": sum(u["status"].startswith("⚪") for u in rows),
        "unknown": sum(u["status"].startswith(("❔", "⏳")) for u in rows),
        "with_volume": sum((u["volume"] or ZERO) > ZERO for u in rows),
        "balance": sum_money(u["balance"] for u in rows if u["balance"] is not None),
        "balance_known": sum(u["balance"] is not None for u in rows),
        "deposits": (sum_money(u["deposit_usdt"] for u in rows) if rows and
                     all(u["deposit_usdt"] is not None for u in rows) else None),
        "deposits_partial": sum_money(u["deposit_usdt"] for u in rows if u["deposit_usdt"] is not None),
        "deposits_known": sum(u["deposit_usdt"] is not None for u in rows),
        "volume": (sum_money(u["volume"] for u in rows) if db.meta("commission_synced_at") else None),
        "commission": (sum_money(u["commission"] for u in rows) if db.meta("commission_synced_at") else None),
    }


class CRM:
    def __init__(self, db: Database, api: BingXClient):
        self.db = db
        self.api = api
        self.lock = asyncio.Lock()

    async def refresh(self):
        """Collect all three datasets; preserve prior good snapshots on partial failure."""
        if self.lock.locked():
            return {"already_running": True, "errors": []}
        async with self.lock:
            errors = []
            now = datetime.now(MSK).isoformat(timespec="seconds")
            try:
                rows, parent = await self.api.invitees()
                invitee_rows = rows
                if not parent.isdecimal():
                    raise ValueError("API не вернул currentAgentUid")
                self.db.save_users(rows, parent, now)
                LOG.info("Referrals loaded: %s", len(rows))
            except Exception as exc:
                LOG.warning("Referral sync failed: %s", exc)
                return {
                    "errors": [f"Рефералы: {exc}"], "commissions_ok": False,
                    "deposits_ok": False, "deposits_known": 0,
                    "users_count": len(self.db.users()), "new_deposit_events": 0,
                }

            yesterday = datetime.now(CN_TZ).date() - timedelta(days=1)
            start_day = yesterday - timedelta(days=89)
            old_start = self.db.meta("commission_start")
            old_as_of = self.db.meta("commission_as_of")
            if old_start and old_as_of and old_start <= start_day.isoformat():
                # Refresh the last 7 days (late corrections) AND everything missed
                # since the last stored day, e.g. after the bot was down > 7 days.
                resume = date.fromisoformat(old_as_of) + timedelta(days=1)
                query_start = max(start_day, min(yesterday - timedelta(days=6), resume))
            else:
                query_start = start_day
            comm_ok = False
            try:
                rows = await self.api.commissions(query_start, yesterday)
                count = self.db.save_commissions(rows, query_start, yesterday, now,
                                                 retention_start=start_day)
                LOG.info("Daily commission records saved: %s", count)
                comm_ok = True
            except Exception as exc:
                LOG.warning("Commission sync failed: %s", exc)
                errors.append(f"Торговая статистика: {exc}")

            deposit_ok = False
            successes = 0
            new_deposit_events = 0
            failures = []
            try:
                end = datetime.now(CN_TZ) - timedelta(minutes=2)
                begin = end - timedelta(days=89)
                outcomes = {}
                for item in invitee_rows:
                    uid = str(item["uid"])
                    try:
                        outcomes[uid] = await self.api.deposits(uid, begin, end)
                    except Exception as exc:
                        LOG.warning("Deposit sync failed for UID %s: %s", uid, exc)
                        outcomes[uid] = None
                        failures.append(uid)
                successes, count, new_deposit_events = self.db.save_deposit_results(
                    outcomes, begin, end, now)
                deposit_ok = successes == len(invitee_rows)
                LOG.info("Deposits: %s/%s UIDs confirmed, %s records; %s request errors; %s new events",
                         successes, len(invitee_rows), count, len(failures), new_deposit_events)
                if failures and successes == 0:
                    errors.append(f"Пополнения: API отклонил запросы для {len(failures)} UID; "
                                  "нет ни одного подтверждённого ответа")
            except Exception as exc:
                LOG.warning("Deposit sync failed: %s", exc)
                errors.append(f"Пополнения: {exc}")

            return {
                "errors": errors,
                "commissions_ok": comm_ok,
                "deposits_ok": deposit_ok,
                "deposits_known": successes,
                "users_count": len(self.db.users()),
                "new_deposit_events": new_deposit_events,
            }

    def pending_inactive_alerts(self, telegram_id):
        as_of = self.db.meta("commission_as_of")
        if not as_of:
            return []
        result = []
        for row in all_metrics(self.db, 30):
            if (row["last_trade"] and row["idle"] is not None and row["idle"] > 3
                    and not self.db.has_sent_alert(telegram_id, row["uid"], row["last_trade"])):
                result.append(row)
        return sorted(result, key=lambda r: r["volume"] or ZERO, reverse=True)

    def pending_deposit_alerts(self, telegram_id, threshold):
        result = []
        for event in self.db.pending_deposit_alerts(telegram_id, threshold):
            metrics = user_metrics(self.db, event["uid"], 30)
            if metrics is None:
                continue
            result.append({**event, "metrics": metrics})
        return result
