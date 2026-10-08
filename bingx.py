# copyright by berlonak
# telegram: @Kilax123
"""Signed, rate-limited, read-only BingX Agent API client.

References:
- BingX Agent API reference: https://github.com/BingX-API/api-ai-skills/blob/main/skills/agent/api-reference.md
- Supplied 'Invite Commission API Documentation 1107' (v1 method).
"""
from __future__ import annotations
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncio
import hashlib
import hmac
import logging
import time as clock
import httpx

LOG = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")
API = "https://open-api.bingx.com"


class BingXError(RuntimeError):
    pass


class BingXClient:
    def __init__(self, api_key: str, secret: str, version: str = "v2", transport=None):
        self.api_key = api_key
        self.secret = secret
        self.version = version
        self.client = httpx.AsyncClient(base_url=API, timeout=25.0, transport=transport,
                                        headers={"X-BX-APIKEY": api_key,
                                                 "X-SOURCE-KEY": "BX-AI-SKILL"})
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, params: dict, allow_null: bool = False):
        """Send signed GET; cap this process to <2 req/s per IP and retry transient failures."""
        for attempt in range(5):
            async with self._lock:
                wait = 0.62 - (clock.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                values = {**params, "timestamp": int(clock.time() * 1000)}
                # IMPORTANT: the signed parameter string MUST be byte-for-byte
                # identical to the query we actually send (before "signature").
                # Passing the original dict to httpx reordered parameters for
                # commission/deposit requests and caused BingX error 100001.
                query = "&".join(f"{key}={value}" for key, value in sorted(values.items()))
                signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
                self._last_request = clock.monotonic()
                try:
                    # Current Agent endpoints use ASCII/digit-only parameter values.
                    # Construct the actual URL from the very same canonical query.
                    response = await self.client.get(f"{path}?{query}&signature={signature}")
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    if attempt == 4:
                        raise BingXError(f"BingX: сетевая ошибка ({type(e).__name__})") from e
                    await asyncio.sleep(2 ** attempt)
                    continue
            try:
                data = response.json()
            except ValueError as e:
                if attempt < 4 and response.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise BingXError(f"BingX: HTTP {response.status_code}, неверный JSON") from e
            if not isinstance(data, dict):
                raise BingXError(f"{path}: BingX вернул JSON типа {type(data).__name__}, ожидался объект")
            code = data.get("code")
            if response.status_code in (429, 500, 502, 503, 504) or code in (100410, 100500):
                if attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                    continue
            if response.status_code != 200 or code != 0:
                # Deliberately avoid printing signed URLs, credentials or full responses.
                raise BingXError(f"{path}: HTTP {response.status_code}, code={code}, "
                                 f"message={str(data.get('msg', 'unknown'))[:180]}")
            result = data.get("data")
            if result is None and allow_null:
                # Caller explicitly accepts code=0 + data=null and decides what it means.
                return None
            if not isinstance(result, dict):
                # A HTTP 200 / code=0 response can still have null or absent data.
                # The API does NOT document null as equivalent to an empty list.
                # Preserve the last valid snapshot, expose only safe response metadata.
                keys = ",".join(sorted(str(k) for k in data.keys()))[:100]
                msg = str(data.get("msg", ""))[:100]
                raise BingXError(
                    f"{path}: нет объекта data (тип={type(result).__name__}, "
                    f"code={code}, msg={msg!r}, поля={keys})"
                )
            return result
        raise BingXError(f"{path}: исчерпаны попытки")

    async def paginated(self, path: str, params: dict, page_size: int = 100):
        entries = []
        page = 1
        while page <= 1000:
            data = await self.get(path, {**params, "pageIndex": page, "pageSize": page_size})
            portion = data.get("list")
            if not isinstance(portion, list):
                raise BingXError(f"{path}: поле list отсутствует")
            entries.extend(portion)
            try:
                total = int(data.get("total", len(entries)))
            except (ValueError, TypeError):
                raise BingXError(f"{path}: неверное поле total")
            if len(entries) >= total:
                return entries, data
            if not portion:
                raise BingXError(f"{path}: пустая страница {page} при total={total}")
            page += 1
        raise BingXError(f"{path}: превышен лимит страниц")

    async def invitees(self):
        # Pagination without time filter returns all users (up to 10K with this method).
        rows, data = await self.paginated("/openApi/agent/v1/account/inviteAccountList", {})
        return rows, str(data.get("currentAgentUid", ""))

    async def commissions(self, start: date, end: date):
        """Fetch all daily commissions; retry using v1 if v2 rejects its date range.

        Some accounts reject documented v2 YYYYMMDD dates with
        `beginTime-over-366`, even for recent periods. The supplied agent v1
        specification accepts millisecond timestamps in seven-day windows.
        """
        try:
            return await self._commissions_for_version(start, end, self.version)
        except BingXError as error:
            message = str(error).lower()
            if (self.version != "v2" or "code=100400" not in message or
                    "begintime-over-366" not in message):
                raise
            LOG.warning("BingX v2 rejected the date range; trying v1 (7-day windows)")
            rows = await self._commissions_for_version(start, end, "v1")
            # Do not re-attempt unsupported v2 during every scheduled sync.
            self.version = "v1"
            return rows

    async def _commissions_for_version(self, start: date, end: date, version: str):
        result = []
        span = 29 if version == "v2" else 6
        path = ("/openApi/agent/v2/reward/commissionDataList" if version == "v2"
                else "/openApi/agent/v1/reward/commissionDataList")
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=span))
            if version == "v2":
                args = {"startTime": cursor.strftime("%Y%m%d"),
                        "endTime": stop.strftime("%Y%m%d")}
            else:
                # v1 PDF: timestamps in milliseconds, maximum 7 days per request.
                args = {"startTime": int(datetime.combine(cursor, time.min, CN_TZ).timestamp() * 1000),
                        "endTime": int(datetime.combine(stop, time.max, CN_TZ).timestamp() * 1000)}
            rows, _ = await self.paginated(path, args)
            result.extend(rows)
            cursor = stop + timedelta(days=1)
        return result

    async def deposits(self, uid: str, start: datetime, end: datetime):
        """Fetch deposits for a single INVITED user's short UID.

        Confirmed by live diagnostic: a referee UID succeeds, inviterSid and
        currentAgentUid are rejected.

        V0.8: BingX answers code=0 + data=null when the referee has NO deposits
        in the requested window.  Verified on the production database: all 47
        such UIDs had no deposits in the 90-day window (42 never deposited, 5
        deposited before it), and BingX never returned an empty list `[]` for
        anyone.  Therefore code=0/data=null is a successful empty result.
        Real failures (HTTP errors, code != 0, malformed data) still raise
        BingXError and are stored as "unknown" by the CRM.
        """
        uid = str(uid)
        if not uid.isdecimal() or int(uid) <= 0:
            raise BingXError("Некорректный UID реферала")
        path = "/openApi/agent/v1/asset/depositDetailList"
        rows = []
        for page in range(1, 1001):
            data = await self.get(path, {
                "uid": uid,
                "bizType": 1,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "pageIndex": page,
                "pageSize": 100,
            }, allow_null=True)
            if data is None:
                # code=0 + data=null: no (more) deposits for this UID in the window.
                return rows
            portion = data.get("list")
            if not isinstance(portion, list):
                raise BingXError(f"{path}: поле list отсутствует для UID {uid}")
            if any(str(r.get("uid")) != uid for r in portion if isinstance(r, dict)):
                raise BingXError(f"{path}: в ответе есть чужой UID; данные не сохранены")
            if any(not isinstance(r, dict) for r in portion):
                raise BingXError(f"{path}: неверный тип депозитной записи")
            rows.extend(portion)
            try:
                total = int(data.get("total", len(rows)))
            except (ValueError, TypeError):
                raise BingXError(f"{path}: неверное поле total")
            if len(rows) >= total:
                return rows
            if not portion:
                raise BingXError(f"{path}: пустая страница {page} при total={total}")
        raise BingXError(f"{path}: превышен лимит страниц")
