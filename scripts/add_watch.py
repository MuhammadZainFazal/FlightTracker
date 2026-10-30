#!/usr/bin/env python3
"""Add a flight watch to the deployed table.

Example:
  python scripts/add_watch.py --origin AMS --destination LHE \
      --out 2026-12-20 --ret 2027-01-10 --adults 2 --currency EUR --max-price 1400
"""
import argparse
import re
import sys
from datetime import datetime
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError


def table_name(stack: str) -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "TableName")


def iso_date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")  # raises on a bad date
    return value


def iata(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{3}", value):
        raise argparse.ArgumentTypeError("use a 3-letter IATA code, e.g. AMS")
    return value.upper()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stack", default="flight-tracker", help="CloudFormation stack name")
    p.add_argument("--origin", required=True, type=iata)
    p.add_argument("--destination", required=True, type=iata)
    p.add_argument("--out", required=True, type=iso_date, help="outbound date YYYY-MM-DD")
    p.add_argument("--ret", type=iso_date, help="return date YYYY-MM-DD (omit for one-way)")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="EUR")
    p.add_argument("--gl", default="nl", help="Google country used for pricing (keep constant!)")
    p.add_argument("--stops", type=int, choices=[1, 2, 3], help="1=nonstop only, 2=max 1 stop, 3=max 2 stops")
    p.add_argument("--max-price", required=True, type=Decimal, help="alert at or below this price")
    p.add_argument("--max-stops", type=int, choices=[0, 1, 2], help="0 = direct only, 1 = at most 1 stop, 2 = at most 2 stops")
    args = p.parse_args()

    watch_id = f"{args.origin}-{args.destination}-{args.out.replace('-', '')}".lower()
    if args.ret:
        watch_id += f"-{args.ret.replace('-', '')}"

    item = {
        "PK": f"WATCH#{watch_id}", "SK": "META", "GSI1PK": "WATCH", "id": watch_id,
        "origin": args.origin, "destination": args.destination, "outboundDate": args.out,
        "tripType": 1 if args.ret else 2, "adults": args.adults, "currency": args.currency.upper(),
        "gl": args.gl, "maxPrice": args.max_price, "active": True,
        "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    if args.ret:
        item["returnDate"] = args.ret
    if args.stops:
        item["stops"] = args.stops

    table = boto3.resource("dynamodb").Table(table_name(args.stack))
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"Watch {watch_id} already exists.", file=sys.stderr)
            return 1
        raise
    print(f"Added watch {watch_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
