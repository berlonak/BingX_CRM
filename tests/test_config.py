# copyright by berlonak
# telegram: @Kilax123
from config import read_settings


def test_multiple_allowed_ids_with_common_separators(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_TOKEN', '123:abc')
    monkeypatch.setenv('BINGX_API_KEY', 'key')
    monkeypatch.setenv('BINGX_SECRET_KEY', 'secret')
    monkeypatch.setenv('ALLOWED_TELEGRAM_IDS', '111222333, 123456789;987654321\n555666777')
    monkeypatch.setenv('ADMIN_TELEGRAM_ID', '111222333')
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'db.sqlite3'))
    settings = read_settings()
    assert settings.allowed_ids == frozenset({111222333, 123456789, 987654321, 555666777})
    assert settings.admin_id == 111222333


def test_deposit_threshold_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_TOKEN', '123:abc')
    monkeypatch.setenv('BINGX_API_KEY', 'key')
    monkeypatch.setenv('BINGX_SECRET_KEY', 'secret')
    monkeypatch.setenv('ALLOWED_TELEGRAM_IDS', '123')
    monkeypatch.setenv('ADMIN_TELEGRAM_ID', '123')
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.delenv('DEPOSIT_ALERT_THRESHOLD_USDT', raising=False)
    assert str(read_settings().deposit_alert_threshold) == '500'
    monkeypatch.setenv('DEPOSIT_ALERT_THRESHOLD_USDT', '750.50')
    assert str(read_settings().deposit_alert_threshold) == '750.50'
