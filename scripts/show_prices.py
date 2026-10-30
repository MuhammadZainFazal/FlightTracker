#!/usr/bin/env python3
"""Print the stored price history of a watch (or list all watches).

  python scripts/show_prices.py                       # list active watches
  python scripts/show_prices.py ams-lhe-20261220-20270110
"""
import argparse

import boto3
from boto3.dynamodb.conditions import Key


def table_name(stack: str) -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "TableName")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("watch_id", nargs="?")
    p.add_argument("--stack", default="flight-tracker")
    args = p.parse_args()
    table = boto3.resource("dynamodb").Table(table_name(args.stack))

    if not args.watch_id:
        items = table.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("WATCH"))["Items"]
        for w in items:
            print(f"{w['id']:40} max {w['maxPrice']} {w['currency']}  last {w.get('lastPrice', '-')}  lowest {w.get('lowestSeen', '-')}")
        return

    items = table.query(
        KeyConditionExpression=Key("PK").eq(f"WATCH#{args.watch_id}") & Key("SK").begins_with("PRICE#")
    )["Items"]
    for i in items:
        print(f"{i['SK'][6:]}  {i['price']:>8} {i['currency']}  {i.get('priceLevel', ''):8} {i.get('airlines', '')}")


if __name__ == "__main__":
    main()
