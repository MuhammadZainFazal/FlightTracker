"""Pure decision logic: no AWS calls, trivially unit-testable."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class AlertDecision:
    alert: bool
    reason: str = ""
    reset: bool = False  # forget the last alerted price (price went back above threshold)


def decide_alert(price, max_price, last_alerted):
    """Alert when the price is at/below the threshold and either we have not
    alerted yet or it has dropped further since the last alert. When the price
    climbs back above the threshold, reset so the next dip alerts again."""
    if price <= max_price:
        if last_alerted is None:
            return AlertDecision(True, "price is at or below your threshold")
        if price < last_alerted:
            return AlertDecision(True, f"price dropped further (last alert was at {last_alerted})")
        return AlertDecision(False)
    return AlertDecision(False, reset=last_alerted is not None)


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


# --- Date planner -----------------------------------------------------------

# Every tracked departure date is GRID_ANCHOR + k * spacing. The grid never moves,
# so a date keeps the same identity from day to day and builds a price history.
GRID_ANCHOR = date(2026, 1, 3)


def plan_dates(today: date, min_lead_days: int, lookahead_days: int, spacing_days: int, count: int) -> list:
    """Departure dates to track today: grid dates between today+min_lead and
    today+lookahead, nearest first, at most `count` of them."""
    earliest = today + timedelta(days=min_lead_days)
    latest = today + timedelta(days=lookahead_days)
    first_step = -(-(earliest - GRID_ANCHOR).days // spacing_days)  # ceiling division
    current = GRID_ANCHOR + timedelta(days=first_step * spacing_days)
    dates = []
    while current <= latest and len(dates) < count:
        dates.append(current)
        current += timedelta(days=spacing_days)
    return dates


def planned_watch_id(origin: str, destination: str, outbound: date, ret: date, adults: int, currency: str, max_stops: int) -> str:
    """Config is part of the id: change adults, currency or the stops filter and you
    get a fresh series instead of mixing incomparable prices into the old one."""
    return f"{origin}-{destination}-{outbound:%Y%m%d}-{ret:%Y%m%d}-{adults}p-{currency}-s{max_stops}".lower()