# copyright by berlonak
# telegram: @Kilax123
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot import dashboard_view, home_view, csv_menu_view, export_csv, profile_view
from service import user_metrics
from storage import Database, CN_TZ


def _user(uid='111111'):
    reg = datetime.now(CN_TZ) - timedelta(days=20)
    return dict(uid=uid, directInvitation=True, registerDateTime=int(reg.timestamp() * 1000),
                deposit=True, trade=False, balanceVolume='1500', inviterSid='999')


def test_home_is_always_90_days(tmp_path):
    db = Database(tmp_path / "crm.sqlite3")
    settings = SimpleNamespace(deposit_alert_threshold=500, allowed_ids={1}, admin_id=1)
    text, _ = home_view(db, settings, True)
    assert "90 дней" in text
    assert "С объёмом за 90д" in text
    assert "Пополнения 90д" in text
    assert "Объём 90д" in text
    assert "Комиссии 90д" in text
    assert "30 дней" not in text


def test_csv_menu_has_30_60_90():
    _, markup = csv_menu_view()
    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}
    assert {"csv:30", "csv:60", "csv:90"} <= callbacks


def test_csv_header_uses_selected_period(tmp_path):
    db = Database(tmp_path / "crm.sqlite3")
    for days in (30, 60, 90):
        data = export_csv(db, days)
        text = data.getvalue().decode("utf-8-sig")
        assert "Последнее пополнение дата (90д)" in text
        assert "Последнее пополнение сумма" in text
        assert "Последнее пополнение валюта" in text
        assert f"Пополнения {days}д USDT" in text
        assert f"Объём {days}д USDT" in text
        assert f"Комиссии {days}д USDT" in text


def test_profile_and_dashboard_show_registration_and_latest_deposit(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([_user()], '999', 'now')
        end = datetime.now(CN_TZ)
        start = end - timedelta(days=89)
        dep_time = end - timedelta(days=3)
        dep = dict(uid='111111', bizTime=int(dep_time.timestamp() * 1000),
                   currencyName='USDT', currencyAmountVolume='777', assetType=30)
        db.save_deposit_results({'111111': [dep]}, start, end, 'sync')

        profile, _ = profile_view(db, '111111', 30)
        assert 'Регистрация:' in profile
        assert 'Последнее пополнение (90д):' in profile
        assert '777 USDT' in profile
        assert dep_time.strftime('%d.%m.%Y') in profile

        ctx = SimpleNamespace(user_data={'period': 30, 'group': 'all', 'sort': 'volume', 'page': 0})
        dashboard, _ = dashboard_view(db, ctx)
        assert 'Регистрация:' in dashboard
        assert 'Последнее пополнение:' in dashboard
        assert '777 USDT' in dashboard
    finally:
        db.close()
