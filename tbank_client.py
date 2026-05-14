from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional

import requests

BASE_URL = "https://invest-public-api.tinkoff.ru/rest"
INSTRUMENTS_SVC = "tinkoff.public.invest.api.contract.v1.InstrumentsService"
MARKETDATA_SVC = "tinkoff.public.invest.api.contract.v1.MarketDataService"


@dataclass
class Bond:
    isin: str
    figi: str
    name: str
    maturity_date: Optional[datetime]
    nominal: float
    nominal_currency: str
    aci: float
    aci_currency: str
    coupon_quantity_per_year: int
    risk_level: str
    amortization_flag: bool
    perpetual_flag: bool


@dataclass
class Coupon:
    coupon_date: Optional[datetime]
    pay_one_bond: float
    coupon_type: str


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _money_to_decimal(m: Optional[dict]) -> float:
    if not m:
        return 0.0
    units = int(m.get("units", 0) or 0)
    nano = int(m.get("nano", 0) or 0)
    return units + nano / 1e9


def _quotation_to_decimal(q: Optional[dict]) -> Optional[float]:
    if not q:
        return None
    units = int(q.get("units", 0) or 0)
    nano = int(q.get("nano", 0) or 0)
    return units + nano / 1e9


# Backwards-compatible helper (used by ytm.py historically)
money_to_decimal = _money_to_decimal


class TBankClient:
    def __init__(self, token: str, timeout: float = 30.0):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "accept": "application/json",
        })
        self.timeout = timeout

    def call(self, service: str, method: str, body: dict) -> dict:
        url = f"{BASE_URL}/{service}/{method}"
        for attempt in range(6):
            r = self.session.post(url, json=body, timeout=self.timeout)
            if r.status_code == 429:
                reset = (
                    r.headers.get("x-ratelimit-reset")
                    or r.headers.get("X-RateLimit-Reset")
                    or "2"
                )
                try:
                    delay = max(1, int(float(reset)))
                except ValueError:
                    delay = 2
                time.sleep(min(delay, 30))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise requests.HTTPError(
                    f"{method} {r.status_code}: {r.text[:200]}",
                    response=r,
                )
            return r.json()
        raise RuntimeError(f"{method}: retries exhausted")

    def close(self) -> None:
        self.session.close()


@contextmanager
def open_client(token: str) -> Iterator[TBankClient]:
    c = TBankClient(token)
    try:
        yield c
    finally:
        c.close()


def get_tradable_bonds(client: TBankClient) -> list[Bond]:
    resp = client.call(
        INSTRUMENTS_SVC, "Bonds",
        {"instrumentStatus": "INSTRUMENT_STATUS_BASE"},
    )
    bonds: list[Bond] = []
    for b in resp.get("instruments", []):
        if not (b.get("apiTradeAvailableFlag") and b.get("buyAvailableFlag")):
            continue
        nominal_m = b.get("nominal") or {}
        aci_m = b.get("aciValue") or {}
        bonds.append(Bond(
            isin=b.get("isin", "") or "",
            figi=b.get("figi", "") or "",
            name=b.get("name", "") or "",
            maturity_date=_parse_iso(b.get("maturityDate")),
            nominal=_money_to_decimal(nominal_m),
            nominal_currency=nominal_m.get("currency", "") or "",
            aci=_money_to_decimal(aci_m),
            aci_currency=aci_m.get("currency", "") or "",
            coupon_quantity_per_year=int(b.get("couponQuantityPerYear", 0) or 0),
            risk_level=str(b.get("riskLevel", "") or ""),
            amortization_flag=bool(b.get("amortizationFlag", False)),
            perpetual_flag=bool(b.get("perpetualFlag", False)),
        ))
    return bonds


def get_last_prices_map(
    client: TBankClient, figis: list[str], batch: int = 200
) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(0, len(figis), batch):
        chunk = figis[i:i + batch]
        resp = client.call(MARKETDATA_SVC, "GetLastPrices", {"figi": chunk})
        for lp in resp.get("lastPrices", []):
            price = _quotation_to_decimal(lp.get("price"))
            if price is not None:
                out[lp["figi"]] = price
    return out


def get_bond_coupons(
    client: TBankClient, figi: str, from_: datetime, to: datetime
) -> list[Coupon]:
    body = {
        "figi": figi,
        "from": from_.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": to.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    resp = client.call(INSTRUMENTS_SVC, "GetBondCoupons", body)
    out: list[Coupon] = []
    for e in resp.get("events", []):
        out.append(Coupon(
            coupon_date=_parse_iso(e.get("couponDate")),
            pay_one_bond=_money_to_decimal(e.get("payOneBond")),
            coupon_type=str(e.get("couponType", "") or ""),
        ))
    return out
