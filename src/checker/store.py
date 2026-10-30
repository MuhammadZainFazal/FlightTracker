"""All DynamoDB access in one place.

Single-table layout:
  WATCH#<id>  | META             | the watch definition (+ GSI1PK="WATCH" while active)
  WATCH#<id>  | PRICE#YYYY-MM-DD | one price observation per watch per day
  USAGE#YYYY-MM | SERPAPI        | monthly API call counter (quota guard)
  USAGE#YYYY-MM | NOTIFIED#<x>   | marker so a notification is sent only once a month
"""
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

DAY = 86400


class QuotaExceeded(Exception):
    """The monthly API call budget is used up."""


def get_table(name: str):
    return boto3.resource("dynamodb").Table(name)


def _conditional_failed(error: ClientError) -> bool:
    return error.response["Error"]["Code"] == "ConditionalCheckFailedException"


def load_active_watches(table) -> list:
    """Query the sparse GSI: only active watches are in it."""
    items = []
    kwargs = {"IndexName": "GSI1", "KeyConditionExpression": Key("GSI1PK").eq("WATCH")}
    while True:
        response = table.query(**kwargs)
        items.extend(response["Items"])
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def save_price(table, watch_pk: str, day: str, quote) -> None:
    """Idempotent: the date is part of the key, so re-running a day overwrites."""
    item = {
        "PK": watch_pk,
        "SK": f"PRICE#{day}",
        "price": quote.price,
        "currency": quote.currency,
        "airlines": quote.airlines,
        "stops": quote.stops,
        "totalDuration": quote.total_duration,
        "priceLevel": quote.price_level,
        "typicalLow": quote.typical_low,
        "typicalHigh": quote.typical_high,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ttl": int(time.time()) + 365 * DAY,  # keep one year of history
    }
    table.put_item(Item={k: v for k, v in item.items() if v is not None})


def update_watch(table, watch_pk: str, set_attrs: dict, remove=()) -> None:
    names, values, assignments = {}, {}, []
    for i, (attr, value) in enumerate(set_attrs.items()):
        names[f"#s{i}"] = attr
        values[f":v{i}"] = value
        assignments.append(f"#s{i} = :v{i}")
    expression = "SET " + ", ".join(assignments)
    if remove:
        removals = []
        for j, attr in enumerate(remove):
            names[f"#r{j}"] = attr
            removals.append(f"#r{j}")
        expression += " REMOVE " + ", ".join(removals)
    table.update_item(
        Key={"PK": watch_pk, "SK": "META"},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def reserve_quota(table, month: str, limit: int) -> int:
    """Atomically claim one API call for this month, or raise QuotaExceeded.

    The call is counted before it is made, so a crash mid-request can only
    over-count, never let you exceed the plan."""
    try:
        response = table.update_item(
            Key={"PK": f"USAGE#{month}", "SK": "SERPAPI"},
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one, #t = :ttl",
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":limit": limit, ":ttl": int(time.time()) + 400 * DAY},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["count"])
    except ClientError as e:
        if _conditional_failed(e):
            raise QuotaExceeded() from e
        raise


def notify_once(table, month: str, name: str) -> bool:
    """True the first time it is called for (month, name), False afterwards."""
    try:
        table.put_item(
            Item={"PK": f"USAGE#{month}", "SK": f"NOTIFIED#{name}", "ttl": int(time.time()) + 400 * DAY},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as e:
        if _conditional_failed(e):
            return False
        raise

def create_planned_watch(table, item: dict) -> bool:
    """Create a planner watch (or reactivate an inactive one). False if it is already active."""
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) OR #a = :off",
            ExpressionAttributeNames={"#a": "active"},
            ExpressionAttributeValues={":off": False},
        )
        return True
    except ClientError as e:
        if _conditional_failed(e):
            return False
        raise