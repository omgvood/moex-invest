from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

YEAR_SECONDS = 365.25 * 86400


def _future_coupons_sorted(coupons, now: datetime):
    return sorted(
        [c for c in coupons if c.coupon_date and c.coupon_date > now],
        key=lambda c: c.coupon_date,
    )


def is_floater(coupons) -> bool:
    """Все будущие купоны помечены FLOATING — это «настоящий» плавающий купон."""
    future = [c for c in coupons if c.coupon_date]
    if not future:
        return False
    return all(c.coupon_type and "FLOATING" in c.coupon_type for c in future)


def detect_put_date(coupons, now: datetime) -> Optional[datetime]:
    """Эвристика оферты: найти переход «ненулевые → нули, и дальше все нули»."""
    future = _future_coupons_sorted(coupons, now)
    if not future:
        return None
    if all(c.coupon_type and "FLOATING" in c.coupon_type for c in future):
        return None

    last_nonzero_idx = -1
    for i, c in enumerate(future):
        if (c.pay_one_bond or 0) > 0:
            last_nonzero_idx = i
    if last_nonzero_idx < 0:
        return None
    if last_nonzero_idx == len(future) - 1:
        return None
    return future[last_nonzero_idx].coupon_date


def _cashflows(coupons, end_date, nominal, now):
    """Денежные потоки до end_date (не включая после), плюс выплата номинала на end_date."""
    flows: list[tuple[float, float]] = []
    for c in coupons:
        cdate = c.coupon_date
        if not cdate or cdate <= now:
            continue
        if end_date and cdate > end_date:
            continue
        amt = c.pay_one_bond or 0.0
        if amt <= 0:
            continue
        years = (cdate - now).total_seconds() / YEAR_SECONDS
        flows.append((years, amt))
    if end_date and end_date > now and nominal:
        years = (end_date - now).total_seconds() / YEAR_SECONDS
        flows.append((years, nominal))
    return flows


def _npv_minus_price(rate, flows, dirty_price):
    return sum(cf / (1 + rate) ** t for t, cf in flows) - dirty_price


def _dnpv(rate, flows):
    return sum(-t * cf / (1 + rate) ** (t + 1) for t, cf in flows)


def _solve_yield(flows, dirty_price, initial=0.1, max_iter=100, tol=1e-7):
    y = initial
    for _ in range(max_iter):
        f = _npv_minus_price(y, flows, dirty_price)
        df = _dnpv(y, flows)
        if df == 0:
            break
        new_y = y - f / df
        if new_y <= -0.99:
            new_y = -0.5
        if abs(new_y - y) < tol:
            return new_y
        y = new_y

    lo, hi = -0.5, 5.0
    if _npv_minus_price(lo, flows, dirty_price) * _npv_minus_price(hi, flows, dirty_price) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if _npv_minus_price(mid, flows, dirty_price) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            return mid
    return (lo + hi) / 2


def calc_yield_to(
    dirty_price: float,
    coupons,
    end_date: Optional[datetime],
    nominal: float,
    now: datetime,
) -> Optional[float]:
    """Универсальный расчёт: end_date = дата погашения (→ YTM) или дата оферты (→ YTP)."""
    if dirty_price is None or dirty_price <= 0:
        return None
    flows = _cashflows(coupons, end_date, nominal, now)
    if not flows:
        return None
    return _solve_yield(flows, dirty_price)


def annual_coupon_sum(coupons, now: datetime) -> float:
    if not coupons:
        return 0.0
    horizon = now + timedelta(days=365)
    total = 0.0
    for c in coupons:
        cdate = c.coupon_date
        if cdate and now < cdate <= horizon:
            total += c.pay_one_bond or 0.0
    return total


def next_coupon_annual_rate(
    coupons, nominal: float, coupons_per_year: int, now: datetime
) -> Optional[float]:
    """Ставка следующего известного купона, % годовых от номинала.
    Корректно работает для флоатеров и переменных купонов, где известен только
    ближайший купон, а суммирование по году занижено."""
    if not nominal or not coupons_per_year:
        return None
    future = sorted(
        [c for c in coupons
         if c.coupon_date and c.coupon_date > now and (c.pay_one_bond or 0) > 0],
        key=lambda c: c.coupon_date,
    )
    if not future:
        return None
    return future[0].pay_one_bond * coupons_per_year / nominal * 100


def expand_amortization(
    coupons,
    current_nominal: float,
    coupons_per_year: int,
    now: datetime,
) -> tuple[list, float]:
    """Восстанавливает график амортизации из убывающих купонов.

    Для амортизируемых бумаг T-Bank API уже возвращает в `pay_one_bond` процент
    на ОСТАТОЧНЫЙ номинал каждого периода. То есть купоны убывают по мере того,
    как номинал гасится. Зная купон_n и зная, что для первого будущего периода
    остаточный номинал равен `current_nominal`, можно вычислить годовую ставку
    купона `r = первый_купон × N / current_nominal`. Дальше для каждого периода
    `nominal_n = купон_n × N / r`, и амортизация в этом периоде равна разнице
    `nominal_n − nominal_{n+1}` (для последнего периода — это сам `nominal_n`).

    Возвращает (coupons_с_встроенной_амортизацией, 0.0). Нулевой остаточный
    номинал в конце означает, что всё уже возвращено через амортизационные
    выплаты — calc_yield_to правильно не добавит ничего на дату погашения.

    Ограничения:
      • предполагает константную ставку `r` на всю бумагу (для step-up купонов
        даст погрешность);
      • амортизация считается в купонные даты (для бумаг с отдельным графиком
        амортизации модель приближённая).
    """
    if not coupons or not current_nominal or coupons_per_year <= 0:
        return list(coupons), current_nominal

    future = sorted(
        [c for c in coupons if c.coupon_date and c.coupon_date > now],
        key=lambda c: c.coupon_date,
    )
    if not future:
        return list(coupons), 0.0

    first = future[0]
    if not first.pay_one_bond or first.pay_one_bond <= 0:
        return list(coupons), current_nominal

    r = first.pay_one_bond * coupons_per_year / current_nominal
    if not (0.001 < r < 5):
        return list(coupons), current_nominal

    # Остаточный номинал на каждый будущий период (на начало периода).
    nominals: list[float] = []
    for c in future:
        amt = c.pay_one_bond or 0
        if amt <= 0:
            return list(coupons), current_nominal
        nominals.append(amt * coupons_per_year / r)

    # Купоны с встроенной амортизацией.
    augmented: list = []
    for i, c in enumerate(future):
        amort = nominals[i] - nominals[i + 1] if i + 1 < len(future) else nominals[i]
        if amort < 0:
            # step-up купон или другая нестандартная схема — наша эвристика
            # ломается, откатываемся на старую логику с полным номиналом в конце.
            return list(coupons), current_nominal
        augmented.append(SimpleNamespace(
            coupon_date=c.coupon_date,
            pay_one_bond=c.pay_one_bond + amort,
            coupon_type=c.coupon_type,
        ))

    return augmented, 0.0
