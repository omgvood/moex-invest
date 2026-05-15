from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from requests.exceptions import RequestException

from config import CBR_KEY_RATE, DATA_DIR, LAST_PRICES_BATCH, SQLITE_PATH, excel_path
from storage import init_db, save_snapshot_sqlite, write_excel
from tbank_client import (
    Bond,
    Coupon,
    get_bond_coupons,
    get_last_prices_map,
    get_tradable_bonds,
    open_client,
)
from ytm import (
    YEAR_SECONDS,
    calc_yield_to,
    detect_put_date,
    expand_amortization,
    is_floater,
    next_coupon_annual_rate,
)


STATUS_FILE = DATA_DIR / "snapshot_status.json"


def _is_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _read_status() -> dict | None:
    if not STATUS_FILE.exists():
        return None
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_status(state: dict) -> None:
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


RISK_LEVEL_MAP = {
    "RISK_LEVEL_LOW": "Низкий",
    "RISK_LEVEL_MODERATE": "Средний",
    "RISK_LEVEL_HIGH": "Высокий",
    "RISK_LEVEL_UNSPECIFIED": "",
    "1": "Низкий",
    "2": "Средний",
    "3": "Высокий",
    "0": "",
}


def map_risk_level(raw: str) -> str:
    return RISK_LEVEL_MAP.get(raw, raw or "")


def format_cbr_formula(spread: float) -> str:
    if abs(spread) < 0.01:
        return "CBR_RATE"
    sign = "+" if spread > 0 else "-"
    return f"CBR_RATE {sign} {abs(spread):.2f}"


def build_row(
    bond: Bond,
    last_price_pct: float | None,
    coupons: list[Coupon],
    now: datetime,
    snapshot_ts: datetime,
) -> dict:
    nominal = bond.nominal
    # T-Bank API отдаёт цену облигации в процентах от номинала (77.31 = 77.31%).
    # Храним и показываем именно %, а в расчёты YTM/текущей доходности берём
    # абсолютную цену в валюте номинала.
    price_pct = last_price_pct
    price_abs = (price_pct / 100 * nominal) if (price_pct is not None and nominal) else None

    # Универсальная "текущая ставка купона" — по следующему известному купону.
    # Для регулярных бумаг совпадает со ставкой годовых; для флоатеров / переменных
    # купонов это самая надёжная оценка (сумма по году занижена, если известен
    # только ближайший купон).
    coupon_yield_pct = next_coupon_annual_rate(
        coupons, nominal, bond.coupon_quantity_per_year, now
    )
    current_yield_pct = (
        coupon_yield_pct * 100 / price_pct
        if (coupon_yield_pct is not None and price_pct)
        else None
    )

    floater = is_floater(coupons)
    put_date = None if floater else detect_put_date(coupons, now)

    yield_date: datetime | None = None
    yield_date_type: str | None = None
    yield_pct: float | None = None
    yield_formula: str | None = None

    if floater:
        yield_date_type = "Плавающий купон"
        if coupon_yield_pct is not None:
            yield_formula = format_cbr_formula(coupon_yield_pct - CBR_KEY_RATE)
    elif put_date:
        yield_date = put_date
        yield_date_type = "Последний известный купон"
        if price_abs is not None:
            try:
                y = calc_yield_to(price_abs + bond.aci, coupons, put_date, nominal, now)
                if y is not None:
                    yield_pct = y * 100
            except Exception:
                pass
    elif bond.maturity_date:
        yield_date = bond.maturity_date
        yield_date_type = "Погашение"
        if price_abs is not None:
            try:
                if bond.amortization_flag:
                    # Восстанавливаем график амортизации из убывающих купонов:
                    # терминальный номинал = 0 (всё гасится частями).
                    cash_coupons, terminal_nominal = expand_amortization(
                        coupons, nominal, bond.coupon_quantity_per_year, now,
                    )
                else:
                    cash_coupons, terminal_nominal = coupons, nominal
                y = calc_yield_to(
                    price_abs + bond.aci, cash_coupons, bond.maturity_date,
                    terminal_nominal, now,
                )
                if y is not None:
                    yield_pct = y * 100
            except Exception:
                pass

    years_to_date = (
        (yield_date - now).total_seconds() / YEAR_SECONDS
        if (yield_date and yield_date > now)
        else None
    )

    return {
        "snapshot_ts": snapshot_ts.isoformat(),
        "isin": bond.isin,
        "figi": bond.figi,
        "name": bond.name,
        "maturity_date": bond.maturity_date.date().isoformat() if bond.maturity_date else None,
        "yield_date": yield_date.date().isoformat() if yield_date else None,
        "yield_date_type": yield_date_type,
        "yield_pct": yield_pct,
        "yield_formula": yield_formula,
        "years_to_date": years_to_date,
        "last_price_pct": price_pct,
        "coupon_yield_pct": coupon_yield_pct,
        "current_yield_pct": current_yield_pct,
        "coupon_quantity_per_year": bond.coupon_quantity_per_year,
        "aci": bond.aci,
        "aci_currency": bond.aci_currency,
        "nominal": nominal,
        "nominal_currency": bond.nominal_currency,
        "risk_level": map_risk_level(bond.risk_level),
        "amortization": bool(bond.amortization_flag),
        "perpetual": bool(bond.perpetual_flag),
        "floater": floater,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Снапшот облигаций T-Bank в Excel + SQLite")
    parser.add_argument("--limit", type=int, default=None,
                        help="Обработать не более N облигаций (для теста)")
    parser.add_argument("--force", action="store_true",
                        help="Запустить даже если другой снапшот уже идёт")
    args = parser.parse_args()

    # Гард против параллельных запусков
    existing = _read_status()
    if existing and existing.get("running") and _is_pid_alive(existing.get("pid", 0)) and not args.force:
        print(
            f"Снапшот уже запущен (pid {existing['pid']}). "
            "Используйте --force, чтобы всё равно запустить.",
            file=sys.stderr,
        )
        sys.exit(2)

    load_dotenv()
    token = os.getenv("TBANK_TOKEN")
    started_at = datetime.now(timezone.utc).isoformat()
    if not token:
        _write_status({
            "running": False, "pid": os.getpid(), "phase": "error",
            "current": 0, "total": 0,
            "started_at": started_at, "finished_at": started_at,
            "error": "TBANK_TOKEN не найден. Заполните .env",
        })
        print("ERROR: TBANK_TOKEN не найден. Заполните .env", file=sys.stderr)
        sys.exit(1)

    def status(phase: str, current: int = 0, total: int = 0,
               error: str | None = None, finished: bool = False) -> None:
        _write_status({
            "running": not finished and error is None,
            "pid": os.getpid(),
            "phase": phase,
            "current": current,
            "total": total,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat() if (finished or error) else None,
            "error": error,
        })

    snapshot_ts = datetime.now(timezone.utc)
    now = snapshot_ts
    total = 0

    try:
        status("starting")
        print(f"[{snapshot_ts:%Y-%m-%d %H:%M:%S %Z}] Снапшот стартует")

        init_db(SQLITE_PATH)

        with open_client(token) as client:
            status("catalog")
            print("1/3  Каталог облигаций...")
            bonds = get_tradable_bonds(client)
            if args.limit:
                bonds = bonds[: args.limit]
            total = len(bonds)
            print(f"     найдено: {total}")

            status("prices", current=0, total=total)
            print("2/3  Последние цены...")
            figis = [b.figi for b in bonds]
            prices = get_last_prices_map(client, figis, LAST_PRICES_BATCH)
            print(f"     цены получены: {len(prices)}")

            print("3/3  Купоны + расчёт YTM...")
            status("coupons", current=0, total=total)
            rows: list[dict] = []
            horizon = now + timedelta(days=365 * 50)
            t0 = time.monotonic()
            for i, b in enumerate(bonds, 1):
                try:
                    coupons = get_bond_coupons(client, b.figi, now, horizon)
                except RequestException as e:
                    print(f"     [warn] {b.isin}: {e}", file=sys.stderr)
                    coupons = []

                rows.append(build_row(b, prices.get(b.figi), coupons, now, snapshot_ts))

                if i % 5 == 0 or i == total:
                    status("coupons", current=i, total=total)
                if i % 25 == 0 or i == total:
                    elapsed = time.monotonic() - t0
                    rate = i / elapsed if elapsed else 0
                    eta = (total - i) / rate if rate else 0
                    print(f"     {i}/{total}  ~{rate:.1f}/s  ETA {eta/60:.1f} мин")

        status("saving", current=total, total=total)
        print("Пишу SQLite...")
        save_snapshot_sqlite(SQLITE_PATH, rows)

        xlsx = excel_path(snapshot_ts.astimezone())
        print(f"Пишу Excel: {xlsx}")
        write_excel(xlsx, rows)

        status("done", current=total, total=total, finished=True)
        print(f"Готово. Строк: {len(rows)}")

    except Exception as e:  # noqa: BLE001
        status("error", current=total, total=total, error=str(e)[:300])
        raise


if __name__ == "__main__":
    main()
