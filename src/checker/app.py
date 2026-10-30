"""Daily flight price checker (EventBridge Scheduler -> this Lambda)."""
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import dataclass

import boto3

import store
from logic import decide_alert, month_key, plan_dates, planned_watch_id
from providers import NoResults, get_provider

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _describe_option(rank: int, option: dict, currency: str) -> list:
    stops = option.get("stops", 0)
    stop_text = "direct" if stops == 0 else f"{stops} stop" + ("s" if stops > 1 else "")
    minutes = option.get("totalDuration")
    duration = f", {minutes // 60}h{minutes % 60:02d} in total" if minutes else ""
    lines = [f"{rank}. {option['price']} {currency} - {option.get('airlines', '')}, {stop_text}{duration}"]
    for seg in option.get("segments", []):
        lines.append(
            f"   {seg.get('flight', '?')}  {seg.get('from', '?')} {seg.get('departs', '?')}"
            f" -> {seg.get('to', '?')} {seg.get('arrives', '?')}"
        )
    return lines


def build_alert(watch: dict, quote, reason: str):
    dates = f"depart {watch['outboundDate']}"
    if watch.get("returnDate"):
        dates += f", return {watch['returnDate']}"
    lines = [
        f"{watch['origin']} -> {watch['destination']} ({dates})",
        f"Passengers: {watch.get('adults', 1)}",
        "",
        f"Cheapest price now: {quote.price} {quote.currency}",
        f"Your threshold:     {watch['maxPrice']} {quote.currency}",
        f"Why you got this:   {reason}",
    ]
    if quote.price_level:
        line = f"Google says this is a '{quote.price_level}' price"
        if quote.typical_low is not None and quote.typical_high is not None:
            line += f" (typical range {quote.typical_low}-{quote.typical_high})"
        lines.append(line)
    if quote.options:
        lines += ["", "Cheapest options (outbound flights of each round trip):"]
        for rank, option in enumerate(quote.options, start=1):
            lines += _describe_option(rank, option, quote.currency)
    elif quote.airlines:
        lines += ["", f"Cheapest option: {quote.airlines}, {quote.stops} stop(s), {quote.total_duration} min in total"]
    if quote.link:
        lines += ["", quote.link]
    subject = f"Flight alert: {watch['origin']}->{watch['destination']} {quote.price} {quote.currency}"
    return subject[:100], "\n".join(lines)

@dataclass
class PendingAlert:
    """An alert waiting to be sent, plus the watch update to write once it was sent."""
    watch: dict
    quote: object
    reason: str
    updates: dict
    remove: list


def build_digest(pending: list):
    """One email for the whole run: a section per date, cheapest first."""
    pending = sorted(pending, key=lambda p: p.quote.price)
    if len(pending) == 1:
        return build_alert(pending[0].watch, pending[0].quote, pending[0].reason)
    best = pending[0]
    subject = (
        f"Flight alert: {len(pending)} dates below your price, best {best.quote.price} {best.quote.currency}"
        f" ({best.watch['origin']}->{best.watch['destination']})"
    )
    divider = "\n\n" + "-" * 44 + "\n\n"
    sections = [build_alert(p.watch, p.quote, p.reason)[1] for p in pending]
    return subject[:100], f"{len(pending)} dates are at or below your price. Cheapest first.{divider}" + divider.join(sections)


# --- Planner: decides WHICH dates to track -----------------------------------

def planner_config():
    """Read the route settings from env vars. None = planner off (manual watches only)."""
    origin = os.environ.get("ROUTE_ORIGIN", "").strip().upper()
    destination = os.environ.get("ROUTE_DESTINATION", "").strip().upper()
    if not origin or not destination:
        return None
    return {
        "origin": origin,
        "destination": destination,
        "duration": int(os.environ.get("TRIP_DURATION_DAYS", "21")),
        "currency": os.environ.get("CURRENCY", "EUR").upper(),
        "adults": int(os.environ.get("ADULTS", "1")),
        "max_stops": int(os.environ.get("MAX_STOPS", "1")),
        "max_price": Decimal(os.environ["MAX_PRICE"]),
        "lookahead": int(os.environ.get("LOOKAHEAD_DAYS", "90")),
        "min_lead": int(os.environ.get("MIN_LEAD_DAYS", "14")),
        "spacing": int(os.environ.get("DATE_SPACING_DAYS", "14")),
        "count": int(os.environ.get("SAMPLE_COUNT", "6")),
    }


def sync_planned_watches(table, watches, now, cfg):
    """Retire planner watches that left the window, create the ones that entered it.

    Only watches with source == "planner" are ever touched, so watches you added
    by hand are left alone. Returns the up-to-date list of active watches
    (built locally, because the GSI is only eventually consistent)."""
    desired = {}
    for outbound in plan_dates(now.date(), cfg["min_lead"], cfg["lookahead"], cfg["spacing"], cfg["count"]):
        ret = outbound + timedelta(days=cfg["duration"])
        wid = planned_watch_id(cfg["origin"], cfg["destination"], outbound, ret, cfg["adults"], cfg["currency"], cfg["max_stops"])
        desired[wid] = (outbound, ret)

    kept, retired = [], 0
    for watch in watches:
        if watch.get("source") == "planner" and watch["id"] not in desired:
            store.update_watch(table, watch["PK"], {"active": False}, remove=["GSI1PK"])
            retired += 1
        else:
            kept.append(watch)

    active_ids = {w["id"] for w in kept}
    created = 0
    for wid, (outbound, ret) in desired.items():
        if wid in active_ids:
            continue
        item = {
            "PK": f"WATCH#{wid}", "SK": "META", "GSI1PK": "WATCH", "id": wid, "source": "planner",
            "origin": cfg["origin"], "destination": cfg["destination"],
            "outboundDate": outbound.isoformat(), "returnDate": ret.isoformat(), "tripType": 1,
            "adults": cfg["adults"], "currency": cfg["currency"], "maxStops": cfg["max_stops"], 
            "maxPrice": cfg["max_price"],
            "active": True, "createdAt": now.isoformat(timespec="seconds"),
        }
        if store.create_planned_watch(table, item):
            kept.append(item)
            created += 1
    logger.info("planner synced", extra={"planner_created": created, "planner_retired": retired, "tracking": len(kept)})
    return kept


# --- Checker: prices ONE watch ------------------------------------------------

def check_watch(table, provider, watch, now, quota_limit, force):
    """Check one watch. Returns (status, pending_alert_or_None); raises on real failures.

    Alerts are NOT sent here: the handler collects them and sends one digest email."""
    pk, today = watch["PK"], now.date().isoformat()

    if watch["outboundDate"] < today:  # trip date has passed: stop tracking
        store.update_watch(table, pk, {"active": False}, remove=["GSI1PK"])
        return "expired", None

    if not force and watch.get("lastCheckedDate") == today:
        return "skipped", None  # already done today; protects the API quota on re-runs

    if provider.uses_quota:
        store.reserve_quota(table, month_key(now), quota_limit)  # may raise QuotaExceeded

    quote = provider.fetch(watch)
    store.save_price(table, pk, today, quote)

    decision = decide_alert(quote.price, watch["maxPrice"], watch.get("lastAlertedPrice"))
    updates = {"lastCheckedDate": today, "lastPrice": quote.price}
    lowest = watch.get("lowestSeen")
    if lowest is None or quote.price < lowest:
        updates["lowestSeen"] = quote.price

    if decision.alert:
        updates["lastAlertedPrice"] = quote.price
        # The watch is only marked as checked AFTER its email went out (see handler),
        # so a failed send means the next run does this watch again.
        return "alerted", PendingAlert(watch, quote, decision.reason, updates, [])

    remove = ["lastAlertedPrice"] if decision.reset else []
    store.update_watch(table, pk, updates, remove)  # written last: a failure above means a re-run redoes this watch
    return "checked", None


def handler(event, context):
    force = bool((event or {}).get("force"))
    now = datetime.now(timezone.utc)
    table = store.get_table(os.environ["TABLE_NAME"])
    topic_arn = os.environ["TOPIC_ARN"]
    quota_limit = int(os.environ.get("QUOTA_LIMIT", "240"))
    provider = get_provider(os.environ.get("PROVIDER", "fake"))
    sns = boto3.client("sns")

    watches = store.load_active_watches(table)
    cfg = planner_config()
    if cfg:
        try:
            watches = sync_planned_watches(table, watches, now, cfg)
        except Exception:  # a planner problem must not stop checks of existing watches
            logger.exception("planner failed")

    summary = {"checked": 0, "alerted": 0, "skipped": 0, "expired": 0, "no_results": 0, "failed": 0}
    pending_alerts = []
    quota_stopped = False

    for watch in watches:
        if cfg and watch.get("source") == "planner":
            watch = {**watch, "maxPrice": cfg["max_price"]}  # changing MAX_PRICE applies immediately
        try:
            status, pending = check_watch(table, provider, watch, now, quota_limit, force)
            if pending:
                pending_alerts.append(pending)
            summary["checked" if status == "alerted" else status] += 1
            if status == "alerted":
                summary["alerted"] += 1
            logger.info("watch processed", extra={"watch": watch["id"], "status": status})
        except store.QuotaExceeded:
            quota_stopped = True
            logger.warning("monthly API quota reached, stopping", extra={"limit": quota_limit})
            if store.notify_once(table, month_key(now), "quota"):
                sns.publish(
                    TopicArn=topic_arn,
                    Subject="Flight tracker: monthly API quota reached",
                    Message=f"The tracker used its {quota_limit} API calls for {month_key(now)} and has paused checks until next month.",
                )
            break
        except NoResults:
            summary["no_results"] += 1
            logger.warning("no flights found", extra={"watch": watch["id"]})
        except Exception:  # one bad watch must never block the others
            summary["failed"] += 1
            logger.exception("watch check failed", extra={"watch": watch["id"]})

    if pending_alerts:  # ONE email for the whole run
        subject, body = build_digest(pending_alerts)
        sns.publish(TopicArn=topic_arn, Subject=subject, Message=body)
        for item in pending_alerts:  # only now mark them as done
            store.update_watch(table, item.watch["PK"], item.updates, item.remove)
        logger.info("digest sent", extra={"dates": len(pending_alerts)})

    logger.info("run complete", extra={"summary": summary, "quota_stopped": quota_stopped})
    if summary["failed"]:
        raise RuntimeError(f"{summary['failed']} watch check(s) failed")  # trips the CloudWatch alarm
    return {**summary, "quota_stopped": quota_stopped}