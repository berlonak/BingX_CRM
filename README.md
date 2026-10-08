# BingX CRM

Read-only Telegram bot for BingX affiliate partners. It pulls referral data from the
BingX Agent API, stores it in a local SQLite database, and presents rankings, per-user
cards, alerts, daily reports and CSV exports in Telegram. There is no web interface.

The bot's interface is in Russian. A Russian version of this README is available in
[README.ru.md](README.ru.md); per-release change notes are in `UPDATE_V0.*.md`.

## Features

- **Dashboard**: fixed 90-day summary — number of referrals, active/inactive counts,
  current balance, deposits, trading volume and commissions.
- **Trader ranking**: all referrals for a 30 / 60 / 90-day period, five per page.
  - Filters: all / active / inactive / direct / indirect.
  - Sorting: volume / commission / balance / deposits.
  - Each row shows the registration date and the latest confirmed deposit.
- **UID card**: status, registration date, last trade, latest deposit, balance and
  period totals, plus deposit history and the last 14 days of trading.
  Open it with `/uid <UID>` or by sending a UID as a message.
- **Inactivity alerts**: a referral is inactive after more than 3 completed days
  without trading.
- **Deposit alerts**: sent for newly seen USDT deposits strictly above
  `DEPOSIT_ALERT_THRESHOLD_USDT` (default 500). Deposits made before the bot was
  installed and deposits older than 7 days that surface late are stored without alerting.
- **CSV export** for 30, 60 or 90 days.
- **Schedule** (Moscow time): sync at 07:15, 14:00 and 20:00; daily report at 08:00.
  Failed scheduled syncs are retried after 5 and 15 minutes. After downtime, missing
  trading days are backfilled (up to 90 days).
- **Access control**: only Telegram IDs listed in `ALLOWED_TELEGRAM_IDS`, only in private
  chats. Every allowed ID receives alerts and the daily report; only `ADMIN_TELEGRAM_ID`
  sees the manual sync button.
- **BingX client**: HMAC-SHA256 signed GET requests, client-side rate limiting
  (< 2 requests/s) and retries on transient errors. Credentials and signed URLs are not logged.

## Tech Stack

- Python 3 (uses `zoneinfo`, `dataclasses`, `asyncio`)
- [python-telegram-bot](https://python-telegram-bot.org/) with the `job-queue` extra
- httpx
- python-dotenv
- SQLite (`sqlite3` from the standard library)
- pytest and pytest-asyncio for tests

## Requirements

- Python 3.9 or newer (the code uses `zoneinfo` and built-in generics such as `frozenset[int]`;
  tests were run on Python 3.12)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A BingX Agent (affiliate) API key and secret with read access

## Installation

```bash
git clone <repository-url>
cd BingX_CRM
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env   # then edit .env
```

## Configuration

All settings are read from `.env` in the project directory.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Telegram bot token |
| `BINGX_API_KEY` | yes | BingX Agent API key |
| `BINGX_SECRET_KEY` | yes | BingX Agent API secret |
| `ALLOWED_TELEGRAM_IDS` | yes | Telegram user IDs allowed to use the bot, separated by commas, spaces, `;` or newlines |
| `ADMIN_TELEGRAM_ID` | no | One of the allowed IDs; can trigger manual sync. Defaults to the smallest allowed ID |
| `COMMISSION_API_VERSION` | no | `v1` or `v2`; default `v1`. If `v2` rejects the date range, the client falls back to `v1` |
| `DEPOSIT_ALERT_THRESHOLD_USDT` | no | Deposit alert threshold in USDT, strict "greater than"; default `500` |
| `DATABASE_PATH` | no | SQLite path, relative to the project directory; default `data/bingx_crm.sqlite3` |

Never commit `.env` or the database; both are listed in `.gitignore`.

## Usage

```bash
python main.py
```

Or use the helper scripts, which check for `.env`, install dependencies and start the bot:

- Linux/macOS: `./start.sh`
- Windows: `start.bat`

Telegram commands:

- `/start`: main dashboard
- `/uid <UID>`: open a referral card (sending a bare UID also works)
- `/id`: show your Telegram ID

### Withdrawal diagnostics

`diagnose_withdrawals.py` is an interactive, read-only script. It probes several BingX GET
endpoints to see whether referral withdrawal data is available. It never initiates a
withdrawal.

```bash
python diagnose_withdrawals.py
```

## Testing

```bash
python -m pip install pytest pytest-asyncio
python -m pytest -q tests
```

Tests use mocked HTTP transports and temporary databases. They need no real keys or network access.

## Project Structure

```
main.py                   entry point
config.py                 .env parsing and validation
bingx.py                  signed BingX Agent API client
service.py                sync and reporting logic
storage.py                SQLite storage
bot.py                    Telegram UI, alerts and scheduled jobs
diagnose_withdrawals.py   read-only withdrawal endpoint probe
tests/                    pytest suite
data/                     runtime database location (not versioned)
```

## Notes

- **Withdrawal alerts are not implemented.** The public BingX Agent API has no method for
  referral withdrawals.
- The deposit threshold applies only to USDT. Other coins are not converted.
- "Balance" is BingX's `balanceVolume` (current net assets). It is not necessarily the
  amount available for withdrawal.
- Deposit history is limited to BingX's rolling 90-day window.
