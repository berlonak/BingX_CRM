# copyright by berlonak
# telegram: @Kilax123
"""Read-only diagnostic for referral withdrawal data.

This script NEVER initiates a withdrawal. It only probes GET endpoints using the
same Agent API credentials as the CRM. Public BingX Agent docs expose referral
deposits but not referral withdrawals, while Partner Hub itself shows both.
The first probe tests the strongest hypothesis: the existing asset detail
endpoint with bizType=2. Other probes test plausible read-only endpoint names.

Run:
    python diagnose_withdrawals.py

Use a referral UID that definitely withdrew funds during the last 90 days.
Do not paste .env, API keys, signatures, cookies, or authorization headers.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bingx import BingXClient, BingXError
from config import read_settings

CN_TZ = ZoneInfo("Asia/Shanghai")


def masked(uid: str) -> str:
    uid = str(uid)
    return "*" * max(0, len(uid) - 4) + uid[-4:]


async def probe(client: BingXClient, label: str, path: str, params: dict, *, allow_null=False):
    try:
        data = await client.get(path, params, allow_null=allow_null)
    except BingXError as exc:
        print(f"{label}: {exc}")
        return False

    if data is None:
        print(f"{label}: HTTP/API OK, но data отсутствует (результат неоднозначен)")
        return False

    rows = data.get("list")
    if not isinstance(rows, list):
        print(f"{label}: API OK, но поля list нет; data-поля={','.join(sorted(map(str, data.keys())))}")
        return False

    total = data.get("total", len(rows))
    print(f"{label}: OK, total={total}, записей на странице={len(rows)}")
    if rows:
        sample = rows[0] if isinstance(rows[0], dict) else {}
        fields = ",".join(sorted(map(str, sample.keys())))
        print(f"  поля записи: {fields[:500]}")
        print(f"  uid записи: {masked(sample.get('uid', ''))}; "
              f"bizType={sample.get('bizType')!r}; assetTypeName={sample.get('assetTypeName')!r}")
    return True


async def main():
    settings = read_settings()
    uid = input("UID реферала, который ТОЧНО выводил деньги за последние 90 дней: ").strip()
    if not uid.isdecimal():
        raise SystemExit("Нужен числовой UID.")

    days_raw = input("Сколько дней проверить [90]: ").strip()
    days = int(days_raw or "90")
    if days < 1 or days > 90:
        raise SystemExit("Период должен быть 1..90 дней.")

    client = BingXClient(settings.api_key, settings.secret_key, settings.commission_version)
    try:
        relation = await client.get("/openApi/agent/v1/account/inviteRelationCheck", {"uid": uid})
        print(f"Реферал {masked(uid)}: inviteResult={relation.get('inviteResult')}, "
              f"directInvitation={relation.get('directInvitation')}")
        if relation.get("inviteResult") is not True:
            raise SystemExit("BingX не подтверждает реферальную связь для этого UID.")

        end = datetime.now(CN_TZ) - timedelta(minutes=2)
        start = end - timedelta(days=days - 1)
        common = {
            "uid": uid,
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "pageIndex": 1,
            "pageSize": 100,
        }

        print("\nПроверяем только read-only GET-запросы. Никакого вывода средств скрипт не делает.\n")

        # Strongest hypothesis: the documented referral asset-detail endpoint has
        # an undocumented withdrawal business type. Public docs only state 1=Deposit.
        await probe(
            client,
            "A. depositDetailList с bizType=2",
            "/openApi/agent/v1/asset/depositDetailList",
            {**common, "bizType": 2},
            allow_null=True,
        )

        # Plausible names for the Partner Hub's unpublished read-only endpoint.
        candidates = [
            ("B. withdrawDetailList",
             "/openApi/agent/v1/asset/withdrawDetailList", common),
            ("C. withdrawalDetailList",
             "/openApi/agent/v1/asset/withdrawalDetailList", common),
            ("D. depositWithdrawDetailList + bizType=2",
             "/openApi/agent/v1/asset/depositWithdrawDetailList", {**common, "bizType": 2}),
            ("E. depositWithdrawalDetailList + bizType=2",
             "/openApi/agent/v1/asset/depositWithdrawalDetailList", {**common, "bizType": 2}),
        ]
        for label, path, params in candidates:
            await probe(client, label, path, params)

        print("\nВажно: /openApi/api/v3/capital/withdraw/history здесь специально НЕ тестируется.")
        print("Он показывает выводы владельца API-ключа, а не произвольного реферала, поэтому для CRM не подходит.")
        print("\nПришли мне только этот консольный вывод. .env, ключи, cookies и signed URL не присылай.")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
