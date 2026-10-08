# copyright by berlonak
# telegram: @Kilax123
"""Telegram-only UX. No web UI, group access, or unauthenticated commands."""
from __future__ import annotations
import asyncio
import csv
from datetime import datetime, time, timedelta
from io import BytesIO, StringIO
import html
import logging
import re

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Keyboard, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from config import Settings
from storage import Database, MSK, CN_TZ, amount, sum_money, epoch_ms
from service import CRM, all_metrics, compact_money, money, totals, user_metrics

LOG = logging.getLogger(__name__)
SIZE = 5
PERIODS = [30, 60, 90]
GROUPS = ["all", "active", "inactive", "direct", "indirect"]
SORTS = ["volume", "commission", "balance", "deposit_usdt"]


def btn(label, data):
    return Btn(label, callback_data=data)


def kb(rows):
    return Keyboard(rows)


def can_use(update: Update, settings: Settings) -> bool:
    who, chat = update.effective_user, update.effective_chat
    return bool(who and chat and chat.type == "private" and who.id in settings.allowed_ids)


def access_restricted(fn):
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not can_use(update, ctx.application.bot_data["settings"]):
            if update.callback_query:
                await update.callback_query.answer("Доступ запрещён", show_alert=True)
            elif update.effective_chat and update.effective_chat.type == "private" and update.effective_message:
                await update.effective_message.reply_text(
                    "⛔ Доступ запрещён. Telegram ID: " + str(update.effective_user.id))
            return
        return await fn(update, ctx)
    return wrapped


def get_db(ctx):
    return ctx.application.bot_data["db"]


async def show(update, content, keyboard=None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                content, reply_markup=keyboard, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
        except BadRequest as err:
            if "not modified" not in str(err).lower():
                raise
    else:
        await update.effective_message.reply_text(
            content, reply_markup=keyboard, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True)


async def send_message_retry(bot, chat_id, text, *, reply_markup=None,
                             parse_mode=ParseMode.HTML, attempts=3):
    delays = (2, 5, 15)
    for attempt in range(attempts):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Forbidden:
            LOG.warning("Telegram user %s blocked bot or never started it", chat_id)
            return False
        except TelegramError as exc:
            if attempt + 1 >= attempts:
                LOG.warning("Telegram message to %s failed after %s attempts: %s",
                            chat_id, attempts, exc)
                return False
            await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
    return False


def _period(ctx):
    return int(ctx.user_data.get("period", 30))


def home_view(db, settings, is_admin):
    # Главная сводка всегда за 90 дней. Период рейтинга/CSV выбирается отдельно.
    days = 90
    users = all_metrics(db, days)
    t = totals(db, users, days)
    balance = money(t["balance"]) + " USDT" if t["balance_known"] else "—"
    dep_total = (money(t["deposits"]) if t["deposits"] is not None else
                 (money(t["deposits_partial"]) + " (частично)" if t["deposits_known"] else "—"))
    as_of = db.meta("commission_as_of", "—")
    body = (
        "<b>📊 BINGX CRM</b>\n"
        "90 дней\n\n"
        f"👥 Рефералов: <b>{t['total']}</b>\n"
        f"🟢 Активных: <b>{t['active']}</b> · 🔴 Неактивных: <b>{t['inactive']}</b>\n"
        f"📈 С объёмом за 90д: <b>{t['with_volume']}</b>\n\n"
        f"💰 Баланс сейчас: <b>{balance}</b>\n"
        f"💳 Пополнения 90д: <b>{dep_total}</b>\n"
        f"📈 Объём 90д: <b>{money(t['volume'])} USDT</b>\n"
        f"💵 Комиссии 90д: <b>{money(t['commission'])} USDT</b>\n\n"
        f"Данные по торгам: {as_of}"
    )
    rows = [
        [btn("📊 Трейдеры", "dashboard")],
        [btn("🔎 Найти UID", "find"), btn("📥 CSV", "csv_menu")],
        [btn("⚙️ Статус", "settings")],
    ]
    if is_admin:
        rows.append([btn("🔄 Синхронизировать", "refresh")])
    return body, kb(rows)


def choose_dashboard(ctx, action):
    s = ctx.user_data
    if action in ("dashboard", "list"):
        s["group"] = "all"; s["sort"] = "volume"; s["period"] = 30; s["page"] = 0
    elif action == "inactive":  # backwards compatibility with old buttons
        s["group"] = "inactive"; s["sort"] = "volume"; s.setdefault("period", 30); s["page"] = 0
    elif action == "filter":
        current = s.get("group", "all")
        s["group"] = GROUPS[(GROUPS.index(current) + 1) % len(GROUPS)]
        s["page"] = 0
    elif action == "sort":
        current = s.get("sort", "volume")
        s["sort"] = SORTS[(SORTS.index(current) + 1) % len(SORTS)]
        s["page"] = 0
    elif action == "period":
        current = int(s.get("period", 30))
        s["period"] = PERIODS[(PERIODS.index(current) + 1) % len(PERIODS)]
        s["page"] = 0


def _filter_rows(rows, group):
    if group == "active":
        return [r for r in rows if r["status"].startswith("🟢")]
    if group == "inactive":
        return [r for r in rows if r["status"].startswith("🔴")]
    if group == "direct":
        return [r for r in rows if r["is_direct"]]
    if group == "indirect":
        return [r for r in rows if not r["is_direct"]]
    return rows


def _registration_text(item):
    reg = epoch_ms(item.get("register_ms"), MSK)
    return reg.strftime("%d.%m.%Y") if reg else "—"


def _last_deposit_text(item, *, with_time=False):
    if not item.get("last_deposit_known"):
        return "данные недоступны"
    dep = item.get("last_deposit")
    if not dep:
        return "не было за 90д"
    dt = epoch_ms(dep.get("event_ms"), MSK)
    if dt:
        stamp = dt.strftime("%d.%m.%Y %H:%M") if with_time else dt.strftime("%d.%m.%Y")
    else:
        stamp = "—"
    return f"{money(dep.get('amount'))} {dep.get('currency') or '?'} · {stamp}"


def dashboard_view(db, ctx):
    group = ctx.user_data.get("group", "all")
    order = ctx.user_data.get("sort", "volume")
    days = _period(ctx)
    rows = _filter_rows(all_metrics(db, days), group)
    rows.sort(key=lambda r: (r.get(order) is not None,
                            r.get(order) if r.get(order) is not None else -1), reverse=True)
    page_max = max(0, (len(rows) - 1) // SIZE)
    page = min(max(0, ctx.user_data.get("page", 0)), page_max)
    ctx.user_data["page"] = page

    names = {"all": "Все", "active": "Активные", "inactive": "Неактивные",
             "direct": "Прямые", "indirect": "Непрямые"}
    sorts = {"volume": "Объём", "commission": "Комиссия", "balance": "Баланс",
             "deposit_usdt": "Пополнения"}
    active = sum(r["status"].startswith("🟢") for r in rows)
    inactive = sum(r["status"].startswith("🔴") for r in rows)
    with_volume = sum((r["volume"] or 0) > 0 for r in rows)

    text = (f"<b>📊 ТРЕЙДЕРЫ · {days} ДНЕЙ</b>\n"
            f"{names[group]}: {len(rows)} · с объёмом: {with_volume}\n"
            f"🟢 {active} · 🔴 {inactive} · сортировка: {sorts[order]} ↓\n")
    buttons = []
    offset = page * SIZE
    for idx, item in enumerate(rows[offset:offset + SIZE], start=offset + 1):
        direct = "прямой" if item["is_direct"] else "непрямой"
        if item["status"].startswith("🔴"):
            state = f"🔴 {item['idle']} д."
        elif item["status"].startswith("🟢"):
            state = "🟢 активен"
        elif item["status"].startswith("⚪"):
            state = "⚪ не торговал"
        else:
            state = "❔ нет истории"
        text += (
            f"\n<b>{idx}.</b> {state} · <code>{item['uid']}</code> · {direct}\n"
            f"Регистрация: <b>{_registration_text(item)}</b>\n"
            f"Последнее пополнение: <b>{html.escape(_last_deposit_text(item))}</b>\n"
            f"Баланс: <b>{money(item['balance'])} USDT</b>\n"
            f"Объём: <b>{compact_money(item['volume'])} USDT</b> · "
            f"Комиссия: <b>{compact_money(item['commission'])} USDT</b>\n"
            f"Пополнения за {days}д: <b>{compact_money(item['deposit_usdt'])} USDT</b>"
        )
        buttons.append([btn(f"👤 {idx}. {item['uid']}", f"profile:{item['uid']}")])
    if not rows:
        text += "\nНет пользователей."
    text += f"\n\nСтраница {page + 1}/{page_max + 1}"
    buttons.append([btn(f"🗓 {days}д", "period"), btn(f"👥 {names[group]}", "filter")])
    buttons.append([btn(f"↕️ {sorts[order]}", "sort")])
    buttons.append([btn("◀️", "prev"), btn("▶️", "next")])
    buttons.append([btn("🏠 Главное", "home")])
    return text, kb(buttons)


def profile_view(db, uid, days=30):
    r = user_metrics(db, uid, days)
    if r is None:
        return "UID не найден.", kb([[btn("📊 Трейдеры", "dashboard")]])
    reg = epoch_ms(r["register_ms"], MSK)
    direct = "Прямой" if r["is_direct"] else "Непрямой"
    text = (
        f"<b>👤 UID <code>{r['uid']}</code></b>\n"
        f"{r['status']} · {direct}\n"
        f"Регистрация: <b>{reg.strftime('%d.%m.%Y') if reg else '—'}</b>\n"
        f"Последняя торговля: {r['last_trade'] or '—'} (UTC+8)\n"
        f"Последнее пополнение (90д): <b>{html.escape(_last_deposit_text(r, with_time=True))}</b>\n\n"
        f"<b>💰 Сейчас</b>\n"
        f"Баланс: <b>{money(r['balance'])} USDT</b>\n\n"
        f"<b>📊 За {days} дней</b>\n"
        f"Пополнения: <b>{money(r['deposit_usdt'])} USDT</b>\n"
        f"Объём: <b>{money(r['volume'])} USDT</b>\n"
        f"Комиссия: <b>{money(r['commission'])} USDT</b>"
    )
    return text, kb([
        [btn("💳 Пополнения", f"deposits:{uid}"), btn("📈 Торги", f"history:{uid}")],
        [btn("📊 К рейтингу", "backlist"), btn("🏠 Главное", "home")],
    ])


def history_view(db, uid):
    rows = db.daily(uid)
    if not db.user(uid):
        return "UID не найден.", kb([[btn("🏠 Главное", "home")]])
    text = f"<b>📈 ТОРГИ · {uid}</b>\nОбъём / комиссия, USDT\n"
    for r in rows[:14]:
        text += f"\n{r['day'][5:]} · {money(r['volume_usdt'])} / {money(r['commission_usdt'])}"
    if not rows:
        text += "\nНет данных."
    return text, kb([[btn("👤 Карточка", f"profile:{uid}")], [btn("🏠 Главное", "home")]])


def deposits_view(db, uid):
    rows = db.deposits(uid)
    text = f"<b>💳 ПОПОЛНЕНИЯ · {uid}</b>\nПоследние 90 дней\n"
    if db.deposit_status(uid) != "ok":
        text += "\nДанные недоступны."
    else:
        curr = {}
        for row in rows:
            c = row["currency"]
            curr[c] = curr.get(c, amount(0)) + amount(row["amount"])
        for currency, total in sorted(curr.items()):
            text += f"\n<b>{html.escape(currency)}:</b> {money(total)}"
        if not rows:
            text += "\nПополнений нет."
        for row in rows[:12]:
            dt = epoch_ms(row["event_ms"], MSK)
            text += (f"\n{dt.strftime('%d.%m.%Y %H:%M') if dt else '—'} · "
                     f"{money(row['amount'])} {html.escape(row['currency'])}")
    return text, kb([[btn("👤 Карточка", f"profile:{uid}")], [btn("🏠 Главное", "home")]])


def status_view(db, settings):
    t = totals(db, days=30)
    text = (
        "<b>⚙️ СТАТУС</b>\n\n"
        "Отчёт: 08:00 МСК\n"
        "Синхронизация: 07:15 · 14:00 · 20:00 МСК\n"
        "Неактивность: >3 дней\n"
        f"Алерт пополнения: >{money(settings.deposit_alert_threshold)} USDT\n"
        "Вывод-алерт: ждём API от BingX\n"
        f"Доступ: {len(settings.allowed_ids)} Telegram ID\n\n"
        f"Рефералы: {db.meta('users_synced_at', '—')}\n"
        f"Торги: {db.meta('commission_synced_at', '—')}\n"
        f"Пополнения: {db.meta('deposits_checked_at', '—')}\n"
        f"Пополнения подтверждены: {t['deposits_known']}/{t['total']} UID"
    )
    return text, kb([[btn("🏠 Главное", "home")]])



def csv_menu_view():
    return (
        "<b>📥 CSV</b>\nВыбери период выгрузки.",
        kb([
            [btn("30 дней", "csv:30"), btn("60 дней", "csv:60"), btn("90 дней", "csv:90")],
            [btn("🏠 Главное", "home")],
        ]),
    )


def export_csv(db, days=30):
    out = StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["UID", "Тип", "Статус", "Регистрация", "Баланс сейчас USDT",
                     "Последнее пополнение дата (90д)", "Последнее пополнение сумма",
                     "Последнее пополнение валюта",
                     f"Пополнения {days}д USDT", f"Объём {days}д USDT",
                     f"Комиссии {days}д USDT", "Последняя торговля UTC+8"])
    for item in all_metrics(db, days):
        reg = epoch_ms(item["register_ms"], MSK)
        dep = item.get("last_deposit") if item.get("last_deposit_known") else None
        dep_dt = epoch_ms(dep.get("event_ms"), MSK) if dep else None
        writer.writerow([
            item["uid"], "Прямой" if item["is_direct"] else "Непрямой", item["status"],
            reg.date().isoformat() if reg else "",
            item["balance"] if item["balance"] is not None else "",
            dep_dt.isoformat(timespec="minutes") if dep_dt else "",
            dep.get("amount", "") if dep else "",
            dep.get("currency", "") if dep else "",
            item["deposit_usdt"] if item["deposit_usdt"] is not None else "",
            item["volume"] if item["volume"] is not None else "",
            item["commission"] if item["commission"] is not None else "",
            item["last_trade"] or "",
        ])
    return BytesIO(("\ufeff" + out.getvalue()).encode("utf-8"))


@access_restricted
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = get_db(ctx)
    settings = ctx.application.bot_data["settings"]
    await show(update, *home_view(db, settings, update.effective_user.id == settings.admin_id))


@access_restricted
async def my_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Telegram ID: {update.effective_user.id}")


@access_restricted
async def uid_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not re.fullmatch(r"\d{4,20}", ctx.args[0]):
        await update.effective_message.reply_text("Формат: /uid 12345678")
        return
    await show(update, *profile_view(get_db(ctx), ctx.args[0], _period(ctx)))


@access_restricted
async def any_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if re.fullmatch(r"\d{4,20}", text):
        await show(update, *profile_view(get_db(ctx), text, _period(ctx)))
    else:
        await update.effective_message.reply_text("Отправь UID цифрами.")


@access_restricted
async def callbacks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data or ""
    db = get_db(ctx)
    settings = ctx.application.bot_data["settings"]

    if action in ("home", "menu"):
        await show(update, *home_view(db, settings, update.effective_user.id == settings.admin_id))
    elif action in ("dashboard", "list", "inactive", "filter", "sort", "period"):
        choose_dashboard(ctx, action)
        await show(update, *dashboard_view(db, ctx))
    elif action in ("next", "prev", "backlist"):
        if action == "next":
            ctx.user_data["page"] = ctx.user_data.get("page", 0) + 1
        elif action == "prev":
            ctx.user_data["page"] = ctx.user_data.get("page", 0) - 1
        await show(update, *dashboard_view(db, ctx))
    elif action.startswith("profile:") or action.startswith("history:") or action.startswith("deposits:"):
        kind, uid = action.split(":", 1)
        if not re.fullmatch(r"\d{4,20}", uid):
            return
        if kind == "profile":
            await show(update, *profile_view(db, uid, _period(ctx)))
        elif kind == "history":
            await show(update, *history_view(db, uid))
        else:
            await show(update, *deposits_view(db, uid))
    elif action == "find":
        await show(update, "<b>🔎 ПОИСК</b>\nОтправь UID.", kb([[btn("🏠 Главное", "home")]]))
    elif action == "settings":
        await show(update, *status_view(db, settings))
    elif action in ("csv", "csv_menu"):
        # Старые кнопки "csv" тоже ведут в выбор периода.
        await show(update, *csv_menu_view())
    elif action.startswith("csv:"):
        try:
            days = int(action.split(":", 1)[1])
        except (TypeError, ValueError):
            return
        if days not in PERIODS:
            await q.answer("Допустимые периоды: 30, 60, 90 дней", show_alert=True)
            return
        data = export_csv(db, days)
        data.seek(0)
        await q.message.reply_document(document=data, filename=f"bingx_traders_{days}d.csv",
                                       caption=f"📥 Трейдеры · {days} дней")
    elif action == "refresh":
        if update.effective_user.id != settings.admin_id:
            await q.answer("Только администратор", show_alert=True)
            return
        if ctx.application.bot_data["crm"].lock.locked():
            await q.message.reply_text("⏳ Синхронизация уже идёт.")
            return
        await q.message.reply_text("⏳ Синхронизация...")
        ctx.application.job_queue.run_once(
            manual_refresh, when=0, data={"telegram_id": update.effective_user.id})


async def send_inactive_alerts(ctx, only_id=None):
    settings = ctx.application.bot_data["settings"]
    crm = ctx.application.bot_data["crm"]
    ids = [only_id] if only_id is not None else sorted(settings.allowed_ids)
    for tid in ids:
        rows = crm.pending_inactive_alerts(tid)
        for user in rows:
            text = (
                "🔴 <b>НЕАКТИВЕН</b>\n\n"
                f"UID: <code>{user['uid']}</code>\n"
                f"Не торгует: <b>{user['idle']} д.</b>\n"
                f"Баланс: <b>{money(user['balance'])} USDT</b>\n"
                f"Объём 30д: <b>{compact_money(user['volume'])} USDT</b>\n"
                f"Комиссия 30д: <b>{compact_money(user['commission'])} USDT</b>"
            )
            sent = await send_message_retry(
                ctx.bot, tid, text,
                reply_markup=kb([[btn("👤 Открыть", f"profile:{user['uid']}")]]))
            if sent:
                crm.db.mark_alert_sent(tid, user["uid"], user["last_trade"])


async def send_deposit_alerts(ctx, only_id=None):
    settings = ctx.application.bot_data["settings"]
    crm = ctx.application.bot_data["crm"]
    ids = [only_id] if only_id is not None else sorted(settings.allowed_ids)
    for tid in ids:
        for event in crm.pending_deposit_alerts(tid, settings.deposit_alert_threshold):
            m = event["metrics"]
            event_dt = epoch_ms(event["event_ms"], MSK)
            text = (
                "🟢 <b>ПОПОЛНЕНИЕ</b>\n\n"
                f"UID: <code>{event['uid']}</code>\n"
                f"Сумма: <b>+{money(event['amount'])} {html.escape(event['currency'])}</b>\n"
                f"Время: {event_dt.strftime('%d.%m.%Y %H:%M МСК') if event_dt else '—'}\n\n"
                f"Баланс: <b>{money(m['balance'])} USDT</b>\n"
                f"Объём 30д: <b>{compact_money(m['volume'])} USDT</b>\n"
                f"Статус: {m['status']}"
            )
            sent = await send_message_retry(
                ctx.bot, tid, text,
                reply_markup=kb([[btn("👤 Открыть", f"profile:{event['uid']}")]]))
            if sent:
                crm.db.mark_deposit_alert_sent(tid, event)


async def perform_refresh(ctx, notify_id=None):
    crm = ctx.application.bot_data["crm"]
    result = await crm.refresh()
    if result.get("already_running"):
        if notify_id:
            await send_message_retry(ctx.bot, notify_id, "⏳ Синхронизация уже идёт.", parse_mode=None)
        return result

    if result.get("commissions_ok"):
        await send_inactive_alerts(ctx)
    if result.get("deposits_known", 0):
        await send_deposit_alerts(ctx)

    if notify_id:
        if result["errors"]:
            text = "⚠️ Частичная синхронизация.\n" + "\n".join(result["errors"])
        else:
            text = (f"✅ Готово. Рефералов: {result['users_count']}\n"
                    f"💳 Пополнения: {result['deposits_known']}/{result['users_count']} UID")
        await send_message_retry(ctx.bot, notify_id, text[:3900], parse_mode=None)
    elif result["errors"]:
        admin = ctx.application.bot_data["settings"].admin_id
        await send_message_retry(ctx.bot, admin,
                                 ("⚠️ Ошибка синхронизации:\n" + "\n".join(result["errors"]))[:3900],
                                 parse_mode=None)
    return result


async def scheduled_refresh(ctx):
    data = ctx.job.data or {}
    attempt = int(data.get("attempt", 0))
    result = await perform_refresh(ctx)
    if result and result.get("errors") and attempt < 2:
        delay = (5, 15)[attempt]
        ctx.application.job_queue.run_once(
            scheduled_refresh, when=timedelta(minutes=delay),
            data={"attempt": attempt + 1}, name=f"sync_retry_{attempt + 1}")
        LOG.warning("Scheduled sync retry #%s in %s minutes", attempt + 1, delay)


async def manual_refresh(ctx):
    await perform_refresh(ctx, ctx.job.data["telegram_id"])


def daily_report_text(db):
    as_of = db.meta("commission_as_of")
    if not as_of:
        return "📊 <b>ОТЧЁТ BINGX</b>\nНет торговых данных."
    t = totals(db, days=30)
    rows = db.daily(day=as_of)
    today_volume = sum_money(row["volume_usdt"] for row in rows)
    today_fee = sum_money(row["commission_usdt"] for row in rows)
    ids_traded = {row["uid"] for row in rows if amount(row["volume_usdt"]) > 0}
    deps = [d for d in db.deposits() if db.deposit_status(d["uid"]) == "ok"]
    dep_usdt = sum_money(
        row["amount"] for row in deps
        if row["currency"] == "USDT" and epoch_ms(row["event_ms"], MSK)
        and epoch_ms(row["event_ms"], MSK).date().isoformat() == as_of)
    day_dep = "—" if not t["deposits_known"] else money(dep_usdt) + " USDT"
    if t["deposits_known"] and t["deposits_known"] < t["total"]:
        day_dep += f" ({t['deposits_known']}/{t['total']} UID)"
    registrations = sum(bool(
        row["register_ms"] and (dt := epoch_ms(row["register_ms"], CN_TZ))
        and dt.date().isoformat() == as_of) for row in db.users())
    return (
        f"<b>📊 BINGX · {as_of}</b>\n\n"
        f"Новых рефералов: {registrations}\n"
        f"Торговали: {len(ids_traded)}\n"
        f"Неактивных: {t['inactive']}\n\n"
        f"Пополнения: {day_dep}\n"
        f"Объём: {money(today_volume)} USDT\n"
        f"Комиссии: {money(today_fee)} USDT"
    )


async def daily_report(ctx):
    db = get_db(ctx)
    yesterday = (datetime.now(CN_TZ).date() - timedelta(days=1)).isoformat()
    if db.meta("commission_as_of") != yesterday:
        # One self-healing attempt before declaring the report unavailable.
        await perform_refresh(ctx)

    content = (daily_report_text(db) if db.meta("commission_as_of") == yesterday
               else "⚠️ <b>ОТЧЁТ НЕДОСТУПЕН</b>\nНет данных за вчера.")
    settings = ctx.application.bot_data["settings"]
    data = ctx.job.data or {}
    targets = data.get("targets") or sorted(settings.allowed_ids)
    attempt = int(data.get("attempt", 0))
    failed = []
    for tid in targets:
        ok = await send_message_retry(
            ctx.bot, int(tid), content,
            reply_markup=kb([[btn("📊 Трейдеры", "dashboard")]]))
        if not ok:
            failed.append(int(tid))
    if failed and attempt < 2:
        ctx.application.job_queue.run_once(
            daily_report, when=timedelta(minutes=15),
            data={"targets": failed, "attempt": attempt + 1},
            name=f"report_retry_{attempt + 1}")
        LOG.warning("Daily report retry #%s scheduled for %s users", attempt + 1, len(failed))


async def initial_refresh(ctx):
    await perform_refresh(ctx)


async def shutdown_api(application: Application):
    await application.bot_data["crm"].api.close()


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, TelegramError):
        LOG.warning("Telegram temporary error: %s", err)
    else:
        LOG.exception("Unhandled bot error", exc_info=err)


def create_application(settings: Settings, db: Database, crm: CRM) -> Application:
    app = ApplicationBuilder().token(settings.bot_token).post_shutdown(shutdown_api).build()
    if app.job_queue is None:
        raise RuntimeError('JobQueue не установлен: pip install "python-telegram-bot[job-queue]"')
    app.bot_data.update(settings=settings, db=db, crm=crm)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("uid", uid_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))
    app.add_error_handler(error_handler)
    app.job_queue.run_daily(scheduled_refresh, time=time(7, 15, tzinfo=MSK), name="morning_sync")
    app.job_queue.run_daily(daily_report, time=time(8, 0, tzinfo=MSK), name="morning_report")
    app.job_queue.run_daily(scheduled_refresh, time=time(14, 0, tzinfo=MSK), name="afternoon_sync")
    app.job_queue.run_daily(scheduled_refresh, time=time(20, 0, tzinfo=MSK), name="evening_sync")
    app.job_queue.run_once(initial_refresh, when=4, name="initial_sync")
    return app
