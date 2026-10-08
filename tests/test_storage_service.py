# copyright by berlonak
# telegram: @Kilax123
from datetime import datetime, timedelta, time
from decimal import Decimal
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from storage import Database, CN_TZ
from service import CRM, status, user_metrics, totals, last_trade


def stamp(day):
    return int(datetime.combine(day, time(0), CN_TZ).timestamp() * 1000)


def user(uid, trade=True, direct=True, balance='2000'):
    return dict(uid=uid, directInvitation=direct, registerDateTime=stamp(
        datetime.now(CN_TZ).date() - timedelta(days=20)),
        deposit=True, trade=trade, balanceVolume=balance, inviterSid='999')


def commission(uid, day, vol, fee):
    return dict(uid=uid, commissionTime=stamp(day), tradingVolume=str(vol), commissionVolume=str(fee))


def test_metrics_status_alert_dedup_and_currency(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    today = datetime.now(CN_TZ).date()
    last_day = today - timedelta(days=6)
    end = today - timedelta(days=1)
    start = end - timedelta(days=89)
    db.save_users([user('111111', True, True), user('222222', False, False, '0')], '999', '2026-10-04T10:00:00')
    db.save_commissions([commission('111111', last_day, '12000.42', '41.05')], start, end,
                        '2026-10-04T10:00:00')
    startdt = datetime.now(CN_TZ) - timedelta(days=89)
    enddt = datetime.now(CN_TZ)
    db.save_deposits([
        dict(uid='111111', bizTime=int((enddt-timedelta(days=4)).timestamp()*1000),
             currencyName='USDT', currencyAmountVolume='900.10', assetType=30),
        dict(uid='111111', bizTime=int((enddt-timedelta(days=5)).timestamp()*1000),
             currencyName='BTC', currencyAmountVolume='0.02', assetType=30),
        dict(uid='333333', bizTime=int((enddt-timedelta(days=5)).timestamp()*1000),
             currencyName='USDT', currencyAmountVolume='9999999', assetType=30),
    ], startdt, enddt, '2026-10-04T10:00:00')
    m = user_metrics(db, '111111')
    assert m['is_direct'] == 1
    assert m['volume'] == Decimal('12000.42')
    assert m['commission'] == Decimal('41.05')
    assert m['deposit_usdt'] == Decimal('900.10')
    assert m['idle'] == 5
    assert m['status'].startswith('🔴')
    never = user_metrics(db, '222222')
    assert never['status'].startswith('⚪')
    assert never['balance'] == Decimal(0)
    crm = CRM(db, None)
    alerts = crm.pending_inactive_alerts(12345)
    assert [x['uid'] for x in alerts] == ['111111']
    db.mark_alert_sent(12345, '111111', m['last_trade'])
    assert crm.pending_inactive_alerts(12345) == []
    assert len(crm.pending_inactive_alerts(67890)) == 1  # each subscriber separately
    t = totals(db)
    assert t['active'] == 0 and t['inactive'] == 1 and t['never'] == 1
    assert t['deposits'] == Decimal('900.10')
    db.close()


def test_replace_daily_and_keep_rolling_90_days(tmp_path):
    db = Database(tmp_path/'crm.sqlite3')
    today = datetime.now(CN_TZ).date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=89)
    db.save_users([user('111111')], '999', '2026-10-04T10:00:00')
    db.save_commissions([commission('111111', start, '1000', '2'),
                         commission('111111', end, '500', '1')], start, end, 'sync1')
    assert len(db.daily('111111')) == 2
    new_end = end + timedelta(days=1)
    new_start = start + timedelta(days=1)
    db.save_commissions([commission('111111', end, '550', '1.25'),
                         commission('111111', new_end, '300', '0.5')],
                        end - timedelta(days=6), new_end, 'sync2', retention_start=new_start)
    results = db.daily('111111')
    assert len(results) == 2
    assert sum(Decimal(r['volume_usdt']) for r in results) == Decimal('850')
    assert db.meta('commission_start') == new_start.isoformat()
    db.close()


@pytest.mark.asyncio
async def test_refresh_partial_deposit_failure_does_not_forge_zero(tmp_path):
    class StubApi:
        async def invitees(self):
            return [user('111111', trade=True)], '999'
        async def commissions(self, start, end):
            return [commission('111111', end-timedelta(days=2), '210', '0.6')]
        async def deposits(self, invitee_uids, start, end):
            assert invitee_uids == "111111"
            raise RuntimeError('not authorized')
    db = Database(tmp_path/'crm.sqlite3')
    crm = CRM(db, StubApi())
    result = await crm.refresh()
    assert result['commissions_ok'] is True
    assert result['deposits_ok'] is False
    assert user_metrics(db, '111111')['deposit_usdt'] is None
    assert user_metrics(db, '111111')['volume'] == Decimal('210')
    assert any('Пополнения:' in x for x in result['errors'])
    db.close()


def test_bonus_only_and_unknown_trading(tmp_path):
    db = Database(tmp_path/'crm.sqlite3')
    end = datetime.now(CN_TZ).date()-timedelta(days=1)
    db.save_users([user('111111', trade=True)], '999', 'now')
    db.save_commissions([], end - timedelta(days=89), end, 'now')
    r = user_metrics(db, '111111')
    assert r['status'].startswith('❔')
    assert CRM(db,None).pending_inactive_alerts(12345) == []
    db.close()


@pytest.mark.asyncio
async def test_refresh_deposit_uids_come_from_invitees_not_commission_rows(tmp_path):
    seen = []
    class StubApi:
        async def invitees(self):
            return [user('111111', trade=True),
                    dict(user('222222', trade=False), deposit=False)], '999'
        async def commissions(self, start, end):
            return []
        async def deposits(self, uid, start, end):
            seen.append(uid)
            if uid == '111111':
                return [dict(uid=uid, bizTime=int((end - timedelta(days=3)).timestamp()*1000),
                             currencyName='USDT', currencyAmountVolume='90', assetType=30)]
            return []
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        result = await CRM(db, StubApi()).refresh()
        assert seen == ['111111', '222222']
        assert result['commissions_ok'] is True
        assert result['deposits_ok'] is True
        assert user_metrics(db, '111111')['deposit_usdt'] == Decimal('90')
        assert user_metrics(db, '222222')['deposit_usdt'] == Decimal('0')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_refresh_uses_each_referee_uid_for_direct_and_indirect(tmp_path):
    seen = []
    class StubApi:
        async def invitees(self):
            first = user('111111')
            second = user('222222')
            second['inviterSid'] = 777  # indirect branch
            third = user('333333')
            return [first, second, third], '123456789012345678'
        async def commissions(self, start, end):
            return []
        async def deposits(self, uid, start, end):
            seen.append(uid)
            return ([dict(uid=uid, bizTime=int((end-timedelta(days=2)).timestamp()*1000),
                          currencyName='USDT', currencyAmountVolume='200', assetType=30)]
                    if uid == '111111' else [])
    db = Database(tmp_path/'crm.sqlite3')
    try:
        result = await CRM(db, StubApi()).refresh()
        assert result['deposits_ok'] is True
        assert seen == ['111111', '222222', '333333']
        assert user_metrics(db, '111111')['deposit_usdt'] == Decimal('200')
        assert user_metrics(db, '222222')['deposit_usdt'] == Decimal('0')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_null_and_error_are_unknown_not_zero_and_successes_are_saved(tmp_path):
    class StubApi:
        async def invitees(self):
            return [user('111111'), user('222222'), user('333333'), user('444444')], '999'
        async def commissions(self, start, end):
            return []
        async def deposits(self, uid, start, end):
            if uid == '111111':
                return [dict(uid=uid, bizTime=int((end-timedelta(days=2)).timestamp()*1000),
                             currencyName='USDT', currencyAmountVolume='180', assetType=30)]
            if uid == '222222':
                return None    # storage-level "no confirmed answer" must stay unknown, not zero
            if uid == '333333':
                return []      # documented empty list, confirmed zero
            raise RuntimeError('not a referee')
    db = Database(tmp_path/'crm.sqlite3')
    try:
        result = await CRM(db, StubApi()).refresh()
        assert result['commissions_ok'] is True
        assert result['deposits_ok'] is False
        assert result['deposits_known'] == 2
        assert user_metrics(db, '111111')['deposit_usdt'] == Decimal('180')
        assert user_metrics(db, '222222')['deposit_usdt'] is None
        assert user_metrics(db, '333333')['deposit_usdt'] == Decimal('0')
        assert user_metrics(db, '444444')['deposit_usdt'] is None
        assert totals(db)['deposits'] is None
        assert totals(db)['deposits_partial'] == Decimal('180')
        assert totals(db)['deposits_known'] == 2
    finally:
        db.close()


def test_null_does_not_erase_prior_good_deposit_but_hides_it_as_unverified(tmp_path):
    db = Database(tmp_path/'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        end = datetime.now(CN_TZ)
        start = end - timedelta(days=89)
        record = dict(uid='111111', bizTime=int((end-timedelta(days=4)).timestamp()*1000),
                      currencyName='USDT', currencyAmountVolume='230', assetType=30)
        assert db.save_deposit_results({'111111': [record]}, start, end, 'sync1') == (1, 1, 0)
        assert user_metrics(db, '111111')['deposit_usdt'] == Decimal('230')
        assert db.save_deposit_results({'111111': None}, start, end, 'sync2') == (0, 0, 0)
        assert db.deposits('111111')[0]['amount'] == '230'
        assert user_metrics(db, '111111')['deposit_usdt'] is None
        assert db.save_deposit_results({'111111': []}, start, end, 'sync3') == (1, 0, 0)
        assert db.deposits('111111') == []
        assert user_metrics(db, '111111')['deposit_usdt'] == Decimal('0')
    finally:
        db.close()



def test_period_metrics_30_60_90(tmp_path):
    db = Database(tmp_path/'crm.sqlite3')
    try:
        today = datetime.now(CN_TZ).date()
        end = today - timedelta(days=1)
        start = end - timedelta(days=89)
        db.save_users([user('111111')], '999', 'now')
        db.save_commissions([
            commission('111111', end - timedelta(days=10), '100', '1'),
            commission('111111', end - timedelta(days=40), '200', '2'),
            commission('111111', end - timedelta(days=70), '300', '3'),
        ], start, end, 'now')
        assert user_metrics(db, '111111', 30)['volume'] == Decimal('100')
        assert user_metrics(db, '111111', 60)['volume'] == Decimal('300')
        assert user_metrics(db, '111111', 90)['volume'] == Decimal('600')
    finally:
        db.close()


def test_deposit_alert_baseline_then_new_event_and_per_subscriber(tmp_path):
    db = Database(tmp_path/'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        end = datetime.now(CN_TZ)
        start = end - timedelta(days=89)
        old = dict(uid='111111', bizTime=int((end-timedelta(days=2)).timestamp()*1000),
                   currencyName='USDT', currencyAmountVolume='900', assetType=30)
        # First confirmed snapshot is a baseline and must not alert.
        assert db.save_deposit_results({'111111': [old]}, start, end, 'sync1') == (1, 1, 0)
        assert db.pending_deposit_alerts(1001, Decimal('500')) == []

        new = dict(uid='111111', bizTime=int((end-timedelta(minutes=10)).timestamp()*1000),
                   currencyName='USDT', currencyAmountVolume='700', assetType=30)
        small = dict(uid='111111', bizTime=int((end-timedelta(minutes=5)).timestamp()*1000),
                     currencyName='USDT', currencyAmountVolume='500', assetType=30)
        result = db.save_deposit_results({'111111': [old, new, small]}, start, end, 'sync2')
        assert result == (1, 3, 2)
        pending = db.pending_deposit_alerts(1001, Decimal('500'))
        assert [x['amount'] for x in pending] == ['700']  # strictly greater than 500
        db.mark_deposit_alert_sent(1001, pending[0])
        assert db.pending_deposit_alerts(1001, Decimal('500')) == []
        assert [x['amount'] for x in db.pending_deposit_alerts(2002, Decimal('500'))] == ['700']
    finally:
        db.close()


# ---------------------------------------------------------------- V0.8 ----

def _dep(uid, when, amount='2000', currency='USDT'):
    return dict(uid=uid, bizTime=int(when.timestamp() * 1000), currencyName=currency,
                currencyAmountVolume=amount, assetType=30)


def _window():
    end = datetime.now(CN_TZ)
    return end - timedelta(days=89), end


def test_fresh_install_first_sync_is_silent_then_first_deposit_of_new_depositor_alerts(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([user('111111'), user('222222')], '999', 'now')
        start, end = _window()
        old = _dep('111111', end - timedelta(days=3), '900')
        # 222222 has never deposited: BingX data=null -> client returns [].
        assert db.save_deposit_results({'111111': [old], '222222': []}, start, end, 's1') == (2, 1, 0)
        assert db.pending_deposit_alerts(1, Decimal('500')) == []
        assert db.meta('deposit_alerts_armed_ms') is not None

        first = _dep('222222', end - timedelta(minutes=5), '2000')
        assert db.save_deposit_results({'111111': [old], '222222': [first]}, start, end, 's2') == (2, 2, 1)
        assert [(x['uid'], x['amount']) for x in db.pending_deposit_alerts(1, Decimal('500'))] == [
            ('222222', '2000')]
    finally:
        db.close()


def test_deposit_after_temporary_api_failure_still_alerts(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        start, end = _window()
        db.save_deposit_results({'111111': []}, start, end, 's1')       # ok, arms
        db.save_deposit_results({'111111': None}, start, end, 's2')     # API error -> unknown
        assert db.deposit_status('111111') == 'unknown'
        new = _dep('111111', end - timedelta(hours=1), '5000')
        assert db.save_deposit_results({'111111': [new]}, start, end, 's3') == (1, 1, 1)
        assert [x['amount'] for x in db.pending_deposit_alerts(1, Decimal('500'))] == ['5000']
    finally:
        db.close()


def test_new_referral_with_deposit_already_in_first_snapshot_alerts(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        start, end = _window()
        db.save_deposit_results({'111111': []}, start, end, 's1')       # arms
        # Referral registers and deposits between two syncs.
        db.save_users([user('111111'), user('333333')], '999', 'now')
        dep = _dep('333333', end - timedelta(minutes=30), '1200')
        db.save_deposit_results({'111111': [], '333333': [dep]}, start, end, 's2')
        assert [x['uid'] for x in db.pending_deposit_alerts(1, Decimal('500'))] == ['333333']
    finally:
        db.close()


def test_stale_deposit_surfacing_late_is_stored_but_not_alerted(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        start, end = _window()
        db.set_meta('deposit_alerts_armed_ms', int((end - timedelta(days=60)).timestamp() * 1000))
        stale = _dep('111111', end - timedelta(days=20), '3000')
        fresh = _dep('111111', end - timedelta(hours=2), '800')
        assert db.save_deposit_results({'111111': [stale, fresh]}, start, end, 's1') == (1, 2, 1)
        assert [x['amount'] for x in db.pending_deposit_alerts(1, Decimal('500'))] == ['800']
        assert user_metrics(db, '111111', 30)['deposit_usdt'] == Decimal('3800')
    finally:
        db.close()


def test_upgrade_of_existing_database_arms_without_alerting_history(tmp_path):
    path = tmp_path / 'crm.sqlite3'
    start, end = _window()
    old = _dep('111111', end - timedelta(days=10), '4000')
    db = Database(path)
    try:
        db.save_users([user('111111'), user('222222')], '999', 'now')
        db.save_deposits([old], start, end, 'v07')     # history written by an old version
        db.conn.execute("DELETE FROM meta WHERE key='deposit_alerts_armed_ms'")
        db.conn.commit()
    finally:
        db.close()

    db = Database(path)                                  # V0.8 starts on that database
    try:
        assert db.meta('deposit_alerts_armed_ms') is not None
        missed_long_ago = _dep('222222', end - timedelta(days=3), '1500')
        new = _dep('222222', end - timedelta(minutes=1), '700')
        db.save_deposit_results({'111111': [old], '222222': [missed_long_ago, new]}, start, end, 's1')
        assert [x['amount'] for x in db.pending_deposit_alerts(1, Decimal('500'))] == ['700']
    finally:
        db.close()


class _CommissionSpy:
    def __init__(self):
        self.calls = []
    async def invitees(self):
        return [user('111111')], '999'
    async def commissions(self, start, end):
        self.calls.append((start, end))
        return []
    async def deposits(self, uid, start, end):
        return []


@pytest.mark.asyncio
async def test_commission_sync_fills_gap_after_long_downtime(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        yesterday = datetime.now(CN_TZ).date() - timedelta(days=1)
        db.set_meta('commission_start', (yesterday - timedelta(days=89)).isoformat())
        db.set_meta('commission_as_of', (yesterday - timedelta(days=14)).isoformat())
        api = _CommissionSpy()
        await CRM(db, api).refresh()
        assert api.calls == [(yesterday - timedelta(days=13), yesterday)]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_commission_sync_normally_refreshes_last_7_days(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        yesterday = datetime.now(CN_TZ).date() - timedelta(days=1)
        db.set_meta('commission_start', (yesterday - timedelta(days=89)).isoformat())
        db.set_meta('commission_as_of', (yesterday - timedelta(days=1)).isoformat())
        api = _CommissionSpy()
        await CRM(db, api).refresh()
        assert api.calls == [(yesterday - timedelta(days=6), yesterday)]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_commission_gap_longer_than_90_days_is_capped(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        yesterday = datetime.now(CN_TZ).date() - timedelta(days=1)
        db.set_meta('commission_start', (yesterday - timedelta(days=200)).isoformat())
        db.set_meta('commission_as_of', (yesterday - timedelta(days=150)).isoformat())
        api = _CommissionSpy()
        await CRM(db, api).refresh()
        assert api.calls == [(yesterday - timedelta(days=89), yesterday)]
    finally:
        db.close()

# ---------------------------------------------------------------- V0.9 ----

def test_metrics_expose_latest_confirmed_deposit_independent_of_selected_period(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        start, end = _window()
        older = _dep('111111', end - timedelta(days=50), '1200')
        newer = _dep('111111', end - timedelta(days=7), '2500')
        db.save_deposit_results({'111111': [older, newer]}, start, end, 'sync')

        m30 = user_metrics(db, '111111', 30)
        m60 = user_metrics(db, '111111', 60)
        assert m30['deposit_usdt'] == Decimal('2500')
        assert m60['deposit_usdt'] == Decimal('3700')
        assert m30['last_deposit_known'] is True
        assert m30['last_deposit']['amount'] == '2500'
        assert m60['last_deposit']['amount'] == '2500'
        assert m30['last_deposit']['event_ms'] == newer['bizTime']
    finally:
        db.close()


def test_latest_deposit_is_hidden_when_deposit_snapshot_is_unverified(tmp_path):
    db = Database(tmp_path / 'crm.sqlite3')
    try:
        db.save_users([user('111111')], '999', 'now')
        start, end = _window()
        dep = _dep('111111', end - timedelta(days=2), '1000')
        db.save_deposit_results({'111111': [dep]}, start, end, 'sync1')
        assert user_metrics(db, '111111')['last_deposit']['amount'] == '1000'
        db.save_deposit_results({'111111': None}, start, end, 'sync2')
        metrics = user_metrics(db, '111111')
        assert metrics['last_deposit_known'] is False
        assert metrics['last_deposit'] is None
    finally:
        db.close()
