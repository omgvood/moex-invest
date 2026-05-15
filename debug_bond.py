"""Дамп всех данных по одной облигации: метаданные + купоны + цена.

Запуск:
    python debug_bond.py RU000A10B4A4
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from tbank_client import (
    INSTRUMENTS_SVC, MARKETDATA_SVC, TBankClient, open_client,
)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python debug_bond.py <ISIN>")
        sys.exit(1)
    isin = sys.argv[1].upper()

    load_dotenv()
    token = os.getenv("TBANK_TOKEN")
    if not token:
        print("ERROR: TBANK_TOKEN не найден.", file=sys.stderr)
        sys.exit(1)

    with open_client(token) as client:
        # 1. Каталог — найти FIGI и весь сырой dict по этой бумаге
        print("=" * 60)
        print(f"Ищем {isin} в каталоге...")
        resp = client.call(INSTRUMENTS_SVC, "Bonds",
                           {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        target = None
        for b in resp.get("instruments", []):
            if b.get("isin") == isin:
                target = b
                break
        if not target:
            print(f"Не найдено: {isin}")
            sys.exit(1)

        print(f"Найдено: {target.get('name')}  figi={target.get('figi')}")
        print()
        print("=" * 60)
        print("Все поля Bond (из API):")
        for k in sorted(target.keys()):
            print(f"  {k}: {target[k]}")

        figi = target["figi"]

        # 2. Последняя цена
        print()
        print("=" * 60)
        print("Последняя цена:")
        prices = client.call(MARKETDATA_SVC, "GetLastPrices", {"figi": [figi]})
        for lp in prices.get("lastPrices", []):
            print(f"  {lp}")

        # 3. Все купоны (от 2 лет в прошлое до 50 лет в будущее)
        print()
        print("=" * 60)
        now = datetime.now(timezone.utc)
        from_ = (now - timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = (now + timedelta(days=365 * 50)).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = client.call(INSTRUMENTS_SVC, "GetBondCoupons",
                           {"figi": figi, "from": from_, "to": to})
        events = resp.get("events", [])
        print(f"Всего купонных событий: {len(events)}")
        print()
        print(f"{'дата':<22} {'тип':<28} {'сумма':>10}  {'период':>6}  {'#':>3}")
        for e in events:
            d = e.get("couponDate", "")[:10]
            t = e.get("couponType", "")
            m = e.get("payOneBond", {})
            amt = int(m.get("units", 0)) + int(m.get("nano", 0)) / 1e9
            cur = m.get("currency", "")
            period = e.get("couponPeriod", "?")
            num = e.get("couponNumber", "?")
            print(f"  {d:<20} {t:<28} {amt:>8.2f} {cur}  {period:>6}  {num:>3}")

        # 4. Пробуем GetBondsEvents если такой метод есть
        print()
        print("=" * 60)
        print("Пробуем GetBondsEvents (может вернуть амортизацию):")
        try:
            resp = client.call(INSTRUMENTS_SVC, "GetBondEvents",
                               {"figi": figi, "from": from_, "to": to})
            print(f"  Ответ: {resp}")
        except Exception as e:
            print(f"  GetBondEvents недоступен: {e}")


if __name__ == "__main__":
    main()
