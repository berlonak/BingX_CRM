# copyright by berlonak
# telegram: @Kilax123
"""SQLite storage. All financial amounts are stored as decimal strings, not floats."""
from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
MSK = ZoneInfo("Europe/Moscow")
ZERO = Decimal("0")

# Deposit alerts (V0.8). Alerts are "armed" once: at the first successful deposit
# sync of a fresh database, or at upgrade time for an existing one.  After that,
# every deposit seen for the first time is alertable, for any UID, regardless of
# whether earlier syncs for that UID failed.  Two guards prevent spam:
#  - deposits made long before arming are never alerted (grace window below);
#  - deposits older than ALERT_MAX_AGE_MS that surface late (e.g. after a long
#    API outage for one UID) are stored silently instead of alerting weeks later.
ALERT_ARM_GRACE_MS = 24 * 3600 * 1000
ALERT_MAX_AGE_MS = 7 * 24 * 3600 * 1000


def now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def amount(value) -> Decimal:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else ZERO
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def sum_money(values):
    return sum((amount(v) for v in values), ZERO)


def epoch_ms(value, tz=CN_TZ):
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                is_direct INTEGER NOT NULL,
                register_ms INTEGER,
                has_deposit INTEGER NOT NULL,
                has_traded INTEGER NOT NULL,
                balance_usdt TEXT,
                inviter_uid TEXT,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_commissions (
                uid TEXT NOT NULL,
                day TEXT NOT NULL,
                volume_usdt TEXT NOT NULL,
                commission_usdt TEXT NOT NULL,
                spot_usdt TEXT NOT NULL DEFAULT '0',
                swap_usdt TEXT NOT NULL DEFAULT '0',
                PRIMARY KEY(uid, day)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_day ON daily_commissions(day);
            CREATE TABLE IF NOT EXISTS deposits (
                uid TEXT NOT NULL,
                event_ms INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                PRIMARY KEY(uid, event_ms, currency, amount, asset_type)
            );
            CREATE INDEX IF NOT EXISTS idx_deposit_event ON deposits(event_ms);
            CREATE TABLE IF NOT EXISTS deposit_coverage (
                uid TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sent_alerts (
                telegram_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                last_trade_day TEXT NOT NULL,
                PRIMARY KEY(telegram_id, uid, last_trade_day)
            );
            CREATE TABLE IF NOT EXISTS deposit_alert_events (
                uid TEXT NOT NULL,
                event_ms INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                alertable INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                PRIMARY KEY(uid, event_ms, currency, amount, asset_type)
            );
            CREATE INDEX IF NOT EXISTS idx_dep_alert_event_ms ON deposit_alert_events(event_ms);
            CREATE TABLE IF NOT EXISTS sent_deposit_alerts (
                telegram_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                event_ms INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                PRIMARY KEY(telegram_id, uid, event_ms, currency, amount, asset_type)
            );
        """)
        # Upgrade safety: every deposit already present before this version is a baseline,
        # never a "new deposit" alert. This prevents alert spam after deployment.
        baseline_seen = self.meta("deposits_synced_at", "migration")
        self.conn.execute("""
            INSERT OR IGNORE INTO deposit_alert_events
            (uid,event_ms,currency,amount,asset_type,alertable,first_seen)
            SELECT uid,event_ms,currency,amount,asset_type,0,? FROM deposits
        """, (baseline_seen,))
        # V0.8 upgrade: a database that already holds deposit history is armed now.
        # A fresh database is armed at the end of its first successful deposit sync.
        if self.meta("deposit_alerts_armed_ms") is None:
            has_history = self.conn.execute(
                "SELECT 1 FROM deposit_alert_events LIMIT 1").fetchone() is not None
            if has_history:
                self.conn.execute("INSERT INTO meta(key,value) VALUES('deposit_alerts_armed_ms',?)",
                                  (str(now_ms()),))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def meta(self, key, default=None):
        item = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return item["value"] if item else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.conn.commit()

    def save_users(self, rows, parent_uid, now):
        with self.conn:
            for row in rows:
                uid = str(row["uid"])
                balance = row.get("balanceVolume")
                balance = str(balance) if balance not in (None, "") else None
                self.conn.execute("""
                    INSERT INTO users(uid,is_direct,register_ms,has_deposit,has_traded,balance_usdt,inviter_uid,last_seen)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET
                    is_direct=excluded.is_direct,register_ms=excluded.register_ms,
                    has_deposit=excluded.has_deposit,has_traded=excluded.has_traded,
                    balance_usdt=excluded.balance_usdt,inviter_uid=excluded.inviter_uid,
                    last_seen=excluded.last_seen
                """, (uid, int(row.get("directInvitation") is True),
                      row.get("registerDateTime") or row.get("registerTime"),
                      int(row.get("deposit") is True), int(row.get("trade") is True),
                      balance, str(row.get("inviterSid", "")), now))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('parent_uid',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (parent_uid,))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('users_synced_at',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))

    def save_commissions(self, rows, start: date, end: date, now, retention_start: date | None = None):
        known_users = {x[0] for x in self.conn.execute("SELECT uid FROM users")}
        consolidated = {}
        for r in rows:
            uid = str(r.get("uid", ""))
            dt = epoch_ms(r.get("commissionTime"))
            if uid not in known_users or dt is None:
                continue
            day = dt.date().isoformat()
            if not start.isoformat() <= day <= end.isoformat():
                continue
            vol = amount(r.get("tradingVolume"))
            spot = amount(r.get("spotTradingVolume"))
            swap = amount(r.get("swapTradingVolume"))
            if vol == ZERO and (spot or swap):
                vol = (spot + swap + amount(r.get("stdTradingVolume")) +
                       amount(r.get("extCopyTradingVolume")) + amount(r.get("mt5TradingVolume")))
            fee = amount(r.get("commissionVolume"))
            if fee == ZERO:
                fee = sum_money(r.get(k) for k in (
                    "spotCommissionVolume", "swapCommissionVolume", "stdCommissionVolume",
                    "extCopyCommissionVolume", "mt5CommissionVolume"))
            key = (uid, day)
            previous = consolidated.get(key, (ZERO, ZERO, ZERO, ZERO))
            consolidated[key] = (previous[0] + vol, previous[1] + fee,
                                 previous[2] + spot, previous[3] + swap)
        with self.conn:
            self.conn.execute("DELETE FROM daily_commissions WHERE day BETWEEN ? AND ?",
                              (start.isoformat(), end.isoformat()))
            self.conn.executemany("INSERT INTO daily_commissions VALUES (?,?,?,?,?,?)", [
                (uid, day, *(str(x) for x in values))
                for (uid, day), values in consolidated.items()
            ])
            cutoff = retention_start or start
            self.conn.execute("DELETE FROM daily_commissions WHERE day < ?", (cutoff.isoformat(),))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('commission_synced_at',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('commission_as_of',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (end.isoformat(),))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('commission_start',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cutoff.isoformat(),))
        return len(consolidated)

    def save_deposits(self, rows, start: datetime, end: datetime, now: str):
        """Legacy helper used by tests/older code. Existing records become a baseline."""
        known_users = {x[0] for x in self.conn.execute("SELECT uid FROM users")}
        values = []
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        for r in rows:
            uid = str(r.get("uid", ""))
            try:
                moment = int(r.get("bizTime"))
            except (ValueError, TypeError):
                continue
            if uid in known_users and start_ms <= moment <= end_ms:
                values.append((uid, moment, str(r.get("currencyName") or "?").upper(),
                               str(amount(r.get("currencyAmountVolume"))), str(r.get("assetType", ""))))
        with self.conn:
            self.conn.execute("DELETE FROM deposits")
            self.conn.executemany("INSERT OR IGNORE INTO deposits VALUES (?,?,?,?,?)", values)
            self.conn.executemany("""
                INSERT OR IGNORE INTO deposit_alert_events
                (uid,event_ms,currency,amount,asset_type,alertable,first_seen)
                VALUES (?,?,?,?,?,0,?)
            """, [(*v, now) for v in values])
            self.conn.executemany("INSERT INTO deposit_coverage(uid,status,checked_at) VALUES(?,'ok',?) "
                                  "ON CONFLICT(uid) DO UPDATE SET status=excluded.status, "
                                  "checked_at=excluded.checked_at", [(uid, now) for uid in known_users])
            self.conn.execute("INSERT INTO meta(key,value) VALUES('deposits_synced_at',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
        return len(values)

    def save_deposit_results(self, outcomes: dict, start: datetime, end: datetime, now: str):
        """Save each successful UID independently and queue genuinely new deposits.

        V0.8: a deposit is alertable when it is seen for the first time AND alerts
        were armed before this sync AND it is not stale (see module constants).
        This no longer depends on the UID's previous sync status, so the FIRST
        deposit of a referral and deposits after a temporary API failure alert too.
        """
        known_users = {r[0] for r in self.conn.execute("SELECT uid FROM users")}
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        armed_raw = self.meta("deposit_alerts_armed_ms")
        armed_ms = int(armed_raw) if armed_raw else None
        alert_from_ms = (max(armed_ms - ALERT_ARM_GRACE_MS, end_ms - ALERT_MAX_AGE_MS)
                         if armed_ms is not None else None)
        success_count = 0
        record_count = 0
        new_alertable_count = 0
        with self.conn:
            for uid in known_users:
                rows = outcomes.get(uid)
                if rows is None:
                    state = "unknown"
                else:
                    state = "ok"
                    success_count += 1
                    values = []
                    for r in rows:
                        if str(r.get("uid")) != uid:
                            raise ValueError("UID депозитной записи не совпадает с запросом")
                        try:
                            moment = int(r["bizTime"])
                        except (ValueError, TypeError, KeyError) as exc:
                            raise ValueError("Некорректная дата депозита") from exc
                        if start_ms <= moment <= end_ms:
                            values.append((uid, moment, str(r.get("currencyName") or "?").upper(),
                                           str(amount(r.get("currencyAmountVolume"))),
                                           str(r.get("assetType", ""))))
                    for value in values:
                        alertable = alert_from_ms is not None and value[1] >= alert_from_ms
                        cursor = self.conn.execute("""
                            INSERT OR IGNORE INTO deposit_alert_events
                            (uid,event_ms,currency,amount,asset_type,alertable,first_seen)
                            VALUES (?,?,?,?,?,?,?)
                        """, (*value, int(alertable), now))
                        if cursor.rowcount and alertable:
                            new_alertable_count += 1
                    self.conn.execute("DELETE FROM deposits WHERE uid=?", (uid,))
                    self.conn.executemany("INSERT OR IGNORE INTO deposits VALUES (?,?,?,?,?)", values)
                    record_count += len(values)
                self.conn.execute("INSERT INTO deposit_coverage(uid,status,checked_at) VALUES(?,?,?) "
                                  "ON CONFLICT(uid) DO UPDATE SET status=excluded.status, "
                                  "checked_at=excluded.checked_at", (uid, state, now))
            self.conn.execute("DELETE FROM deposits WHERE event_ms < ?", (start_ms,))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('deposits_checked_at',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
            if success_count:
                self.conn.execute("INSERT INTO meta(key,value) VALUES('deposits_synced_at',?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
                if armed_ms is None:
                    # Fresh install: everything seen in this first sync is the baseline.
                    self.conn.execute("INSERT INTO meta(key,value) VALUES('deposit_alerts_armed_ms',?) "
                                      "ON CONFLICT(key) DO NOTHING", (str(now_ms()),))
        return success_count, record_count, new_alertable_count

    def deposit_status(self, uid):
        row = self.conn.execute("SELECT status FROM deposit_coverage WHERE uid=?", (str(uid),)).fetchone()
        if row is not None:
            return row["status"]
        return "ok" if self.meta("deposits_synced_at") and not self.meta("deposits_checked_at") else "unknown"

    def users(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM users")]

    def user(self, uid):
        r = self.conn.execute("SELECT * FROM users WHERE uid=?", (str(uid),)).fetchone()
        return dict(r) if r else None

    def daily(self, uid=None, day=None, start_day=None):
        conditions, args = [], []
        if uid is not None:
            conditions.append("uid=?"); args.append(str(uid))
        if day is not None:
            conditions.append("day=?"); args.append(str(day))
        if start_day is not None:
            conditions.append("day>=?"); args.append(str(start_day))
        sql = "SELECT * FROM daily_commissions" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY day DESC", args)]

    def deposits(self, uid=None, start_ms=None):
        conditions, args = [], []
        if uid is not None:
            conditions.append("uid=?"); args.append(str(uid))
        if start_ms is not None:
            conditions.append("event_ms>=?"); args.append(int(start_ms))
        sql = "SELECT * FROM deposits" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY event_ms DESC", args)]

    def has_sent_alert(self, telegram_id, uid, last_day):
        return self.conn.execute(
            "SELECT 1 FROM sent_alerts WHERE telegram_id=? AND uid=? AND last_trade_day=?",
            (telegram_id, str(uid), str(last_day))).fetchone() is not None

    def mark_alert_sent(self, telegram_id, uid, last_day):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO sent_alerts VALUES (?,?,?)",
                              (telegram_id, str(uid), str(last_day)))

    def pending_deposit_alerts(self, telegram_id, threshold):
        rows = self.conn.execute("""
            SELECT e.* FROM deposit_alert_events e
            LEFT JOIN sent_deposit_alerts s
              ON s.telegram_id=? AND s.uid=e.uid AND s.event_ms=e.event_ms
             AND s.currency=e.currency AND s.amount=e.amount AND s.asset_type=e.asset_type
            WHERE e.alertable=1 AND s.telegram_id IS NULL
            ORDER BY e.event_ms ASC
        """, (int(telegram_id),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            # The partner endpoint gives native currency amounts, not USD valuation.
            # The configured dollar-like threshold is therefore applied only to USDT.
            if item["currency"] == "USDT" and amount(item["amount"]) > amount(threshold):
                result.append(item)
        return result

    def mark_deposit_alert_sent(self, telegram_id, event):
        with self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO sent_deposit_alerts
                (telegram_id,uid,event_ms,currency,amount,asset_type)
                VALUES (?,?,?,?,?,?)
            """, (int(telegram_id), str(event["uid"]), int(event["event_ms"]),
                  str(event["currency"]), str(event["amount"]), str(event["asset_type"])))
