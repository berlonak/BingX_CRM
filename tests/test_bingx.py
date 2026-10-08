# copyright by berlonak
# telegram: @Kilax123
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import hmac
import hashlib
import sys
from urllib.parse import urlencode
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import httpx
from bingx import BingXClient, BingXError


@pytest.mark.asyncio
async def test_signed_invitees_and_parent_uid():
    visited = []
    def responder(req):
        visited.append(req)
        pairs = dict(req.url.params)
        sig = pairs.pop('signature')
        query = urlencode(sorted(pairs.items()))
        expected = hmac.new(b'mock-secret', query.encode(), hashlib.sha256).hexdigest()
        assert sig == expected
        assert req.headers['X-BX-APIKEY'] == 'mock-key'
        assert req.headers['X-SOURCE-KEY'] == 'BX-AI-SKILL'
        assert req.url.path == '/openApi/agent/v1/account/inviteAccountList'
        return httpx.Response(200, json={'code':0,'data':{'list':[{'uid':111111}],
                                          'total':1,'currentAgentUid':999}})
    client = BingXClient('mock-key','mock-secret',transport=httpx.MockTransport(responder))
    try:
        rows, parent = await client.invitees()
        assert len(visited) == 1
        assert parent == '999' and len(rows) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('path, business', [
    ('/openApi/agent/v2/reward/commissionDataList',
     {'startTime': '20260901', 'endTime': '20260930'}),
    ('/openApi/agent/v1/reward/commissionDataList',
     {'startTime': 1790000000000, 'endTime': 1790100000000}),
    ('/openApi/agent/v1/asset/depositDetailList',
     {'uid': 1234567, 'bizType': 1, 'startTime': 1790000000000,
      'endTime': 1790100000000}),
])
async def test_signature_covers_exact_wire_query_in_canonical_order(path, business):
    """Regression: commission/deposit calls used to sign sorted params, send unsorted."""
    visited = []

    def responder(req):
        visited.append(req)
        raw = req.url.query.decode('ascii')
        canonical, actual_signature = raw.rsplit('&signature=', 1)
        pairs = canonical.split('&')
        assert pairs == sorted(pairs)
        assert actual_signature == hmac.new(
            b'mock-secret', canonical.encode(), hashlib.sha256).hexdigest()
        assert dict(req.url.params).get('signature') == actual_signature
        return httpx.Response(200, json={'code': 0, 'data': {'list': [], 'total': 0}})

    client = BingXClient('mock-key', 'mock-secret',
                         transport=httpx.MockTransport(responder))
    try:
        rows, data = await client.paginated(path, business)
        assert rows == [] and data['total'] == 0 and len(visited) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_v2_commission_windows_are_at_most_30_days():
    client = BingXClient('k','s',version='v2')
    calls = []
    async def pages(path, params):
        calls.append((path,params))
        return [], {'total':0}
    client.paginated=pages
    today = datetime(2026,10,1).date()
    try:
        await client.commissions(today-timedelta(days=89), today)
        assert len(calls) == 3
        assert all('/v2/' in path for path,_ in calls)
        assert all(len(p['startTime'])==8 and len(p['endTime'])==8 for _,p in calls)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_v1_commission_windows_are_at_most_7_days():
    client = BingXClient('k','s',version='v1')
    calls = []
    async def pages(path, params):
        calls.append((path,params))
        return [], {'total':0}
    client.paginated=pages
    today = datetime(2026,10,1).date()
    try:
        await client.commissions(today-timedelta(days=13), today)
        assert len(calls) == 2
        assert all('/v1/' in path for path,_ in calls)
        assert all(isinstance(p['startTime'],int) and isinstance(p['endTime'],int)
                   for _,p in calls)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_deposits_use_referee_uid_and_allow_pagination():
    calls = []
    def responder(req):
        pairs = dict(req.url.params)
        calls.append(pairs)
        assert pairs['uid'] == '87654321'
        assert pairs['bizType'] == '1'
        page = int(pairs['pageIndex'])
        records = ([{'uid': '87654321', 'bizTime': 1000}]
                   if page == 1 else [{'uid': '87654321', 'bizTime': 2000}])
        return httpx.Response(200, json={'code': 0, 'data': {'list': records, 'total': 2}})
    client = BingXClient('k', 's', transport=httpx.MockTransport(responder))
    try:
        stop = datetime.now().astimezone()
        rows = await client.deposits('87654321', stop-timedelta(days=89), stop)
        assert len(rows) == 2
        assert [int(x['pageIndex']) for x in calls] == [1, 2]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_v2_begin_time_over_366_falls_back_to_v1_and_remembers_it():
    client = BingXClient('k', 's', version='v2')
    calls = []

    async def pages(path, params):
        calls.append((path, params))
        if '/v2/' in path:
            raise BingXError(path + ': HTTP 200, code=100400, '
                             'message=beginTime-over-366. The query time range cannot exceed 366 days')
        return [], {'total': 0}

    client.paginated = pages
    try:
        today = datetime(2026, 10, 1).date()
        await client.commissions(today - timedelta(days=13), today)
        assert len(calls) == 3  # v2 failed, then two v1 7-day windows
        assert '/v2/' in calls[0][0]
        assert all('/v1/' in path for path, _ in calls[1:])
        assert client.version == 'v1'
        calls.clear()
        await client.commissions(today, today)
        assert len(calls) == 1 and '/v1/' in calls[0][0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_v2_does_not_mask_other_errors():
    client = BingXClient('k', 's', version='v2')

    async def pages(path, params):
        raise BingXError('code=100413, message=Invalid API key')

    client.paginated = pages
    try:
        today = datetime(2026, 10, 1).date()
        with pytest.raises(BingXError, match='100413'):
            await client.commissions(today, today)
        assert client.version == 'v2'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_null_deposit_data_means_no_deposits_in_window():
    # V0.8: verified on production data — BingX returns code=0/data=null instead of [].
    def responder(req):
        return httpx.Response(200, json={'code': 0, 'msg': 'SUCCESS', 'data': None})
    client = BingXClient('mock-key', 'mock-secret', transport=httpx.MockTransport(responder))
    try:
        result = await client.deposits('87654321', datetime.now().astimezone() - timedelta(days=1),
                                       datetime.now().astimezone())
        assert result == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_deposit_api_error_still_raises_not_empty():
    def responder(req):
        return httpx.Response(200, json={'code': 100400, 'msg': 'uid-illegal', 'data': None})
    client = BingXClient('mock-key', 'mock-secret', transport=httpx.MockTransport(responder))
    try:
        stop = datetime.now().astimezone()
        with pytest.raises(BingXError, match='code=100400'):
            await client.deposits('87654321', stop - timedelta(days=1), stop)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_valid_empty_deposit_list_is_success():
    def responder(req):
        return httpx.Response(200, json={'code': 0, 'msg': 'SUCCESS',
                                         'data': {'list': [], 'total': 0}})
    client = BingXClient('mock-key', 'mock-secret', transport=httpx.MockTransport(responder))
    try:
        rows = await client.deposits('87654321', datetime.now().astimezone()-timedelta(days=1),
                                     datetime.now().astimezone())
        assert rows == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_deposit_uid_mismatch_rejected_instead_of_misattributed():
    def responder(req):
        return httpx.Response(200, json={'code': 0, 'data': {'list': [
            {'uid': '999999', 'bizTime': 1000}], 'total': 1}})
    client = BingXClient('k', 's', transport=httpx.MockTransport(responder))
    try:
        stop = datetime.now().astimezone()
        with pytest.raises(BingXError, match='чужой UID'):
            await client.deposits('87654321', stop-timedelta(days=89), stop)
    finally:
        await client.close()
