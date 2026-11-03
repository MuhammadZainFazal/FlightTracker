"""Integration-style tests against moto (a fake AWS). Skipped if moto is not installed."""
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

pytest.importorskip("moto")
import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402


@pytest.fixture
def aws(monkeypatch):
    for key, value in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                       "AWS_DEFAULT_REGION": "eu-west-1", "PROVIDER": "fake", "TABLE_NAME": "t"}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        table = ddb.create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI1PK")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{"IndexName": "GSI1", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                                   {"AttributeName": "PK", "KeyType": "RANGE"}]}],
        )
        topic_arn = boto3.client("sns").create_topic(Name="alerts")["TopicArn"]
        monkeypatch.setenv("TOPIC_ARN", topic_arn)
        yield table


def add_watch(table, watch_id="ams-lhe-x", max_price=100000, outbound="2099-12-20"):
    table.put_item(Item={"PK": f"WATCH#{watch_id}", "SK": "META", "GSI1PK": "WATCH", "id": watch_id,
                         "origin": "AMS", "destination": "LHE", "outboundDate": outbound, "returnDate": "2099-12-30",
                         "tripType": 1, "adults": 2, "currency": "EUR", "maxPrice": Decimal(max_price), "active": True})


def test_quota_guard_stops_at_limit(aws):
    import store
    assert store.reserve_quota(aws, "2026-10", 2) == 1
    assert store.reserve_quota(aws, "2026-10", 2) == 2
    with pytest.raises(store.QuotaExceeded):
        store.reserve_quota(aws, "2026-10", 2)
    assert store.reserve_quota(aws, "2026-11", 2) == 1  # new month, new budget


def test_notify_once(aws):
    import store
    assert store.notify_once(aws, "2026-10", "quota") is True
    assert store.notify_once(aws, "2026-10", "quota") is False


def test_handler_stores_price_alerts_and_is_idempotent(aws):
    import app
    add_watch(aws)
    first = app.handler({}, None)
    assert first["alerted"] == 1 and first["failed"] == 0

    meta = aws.get_item(Key={"PK": "WATCH#ams-lhe-x", "SK": "META"})["Item"]
    assert meta["lastAlertedPrice"] == meta["lastPrice"]
    today = datetime.now(timezone.utc).date().isoformat()
    assert "Item" in aws.get_item(Key={"PK": "WATCH#ams-lhe-x", "SK": f"PRICE#{today}"})

    second = app.handler({}, None)
    assert second["skipped"] == 1 and second["alerted"] == 0  # same day: nothing repeated


def test_expired_watch_is_deactivated(aws):
    import app
    add_watch(aws, outbound="2020-01-01")
    assert app.handler({}, None)["expired"] == 1
    meta = aws.get_item(Key={"PK": "WATCH#ams-lhe-x", "SK": "META"})["Item"]
    assert meta["active"] is False and "GSI1PK" not in meta
