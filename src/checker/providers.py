"""Flight price providers behind one tiny interface.

Swapping SerpApi for another source means writing one new class here.
Standard library only, so the Lambda package has no dependencies to build.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TOP_OPTIONS = 3  # how many itineraries are stored per watch per day


class ProviderError(Exception):
    """The provider failed (network, auth, bad response)."""


class NoResults(ProviderError):
    """The provider answered but found no flights for this search."""


@dataclass
class Quote:
    price: Decimal                         # cheapest of the stored options
    currency: str
    airlines: str = ""
    stops: Optional[int] = None
    total_duration: Optional[int] = None   # minutes
    price_level: Optional[str] = None      # low / typical / high (Google's verdict)
    typical_low: Optional[Decimal] = None
    typical_high: Optional[Decimal] = None
    link: Optional[str] = None
    options: list = field(default_factory=list)  # up to TOP_OPTIONS itineraries, cheapest first


def _dec(value):
    return None if value is None else Decimal(str(value))


def _parse_option(option: dict) -> dict:
    """Turn one SerpApi itinerary into a compact, storable dict."""
    segments = []
    for s in option.get("flights") or []:
        dep, arr = s.get("departure_airport") or {}, s.get("arrival_airport") or {}
        segment = {
            "flight": s.get("flight_number"), "airline": s.get("airline"),
            "from": dep.get("id"), "to": arr.get("id"),
            "departs": dep.get("time"), "arrives": arr.get("time"),
        }
        segments.append({k: v for k, v in segment.items() if v is not None})
    layovers = [{"airport": l.get("id"), "minutes": l.get("duration")} for l in option.get("layovers") or []]
    airlines = ", ".join(dict.fromkeys(s["airline"] for s in segments if s.get("airline")))
    parsed = {
        "price": _dec(option["price"]),
        "totalDuration": option.get("total_duration"),
        "stops": max(len(segments) - 1, 0),
        "airlines": airlines,
        "segments": segments,
        "layovers": layovers,
    }
    return {k: v for k, v in parsed.items() if v is not None}


def parse_serpapi_response(data: dict, currency: str) -> Quote:
    """Build a Quote from a SerpApi google_flights response.

    best_flights are Google's own top picks (a balance of price, duration and
    stops); other_flights is everything else, and holds all results when Google
    does not split them. So: use best_flights if present, otherwise other_flights.
    Never mix the two, or odd itineraries sneak into the price series.
    """
    error = data.get("error")
    if error:
        if "hasn't returned any results" in error.lower():
            raise NoResults(error)
        raise ProviderError(f"SerpApi error: {error}")

    pool = [o for o in (data.get("best_flights") or []) if "price" in o]
    if not pool:
        pool = [o for o in (data.get("other_flights") or []) if "price" in o]
    if not pool:
        raise NoResults("response contained no priced flights")

    options = [_parse_option(o) for o in sorted(pool, key=lambda o: o["price"])[:TOP_OPTIONS]]
    cheapest = options[0]
    insights = data.get("price_insights") or {}
    typical = insights.get("typical_price_range") or [None, None]

    return Quote(
        price=cheapest["price"],
        currency=currency,
        airlines=cheapest.get("airlines", ""),
        stops=cheapest.get("stops"),
        total_duration=cheapest.get("totalDuration"),
        price_level=insights.get("price_level"),
        typical_low=_dec(typical[0]),
        typical_high=_dec(typical[1]),
        link=(data.get("search_metadata") or {}).get("google_flights_url"),
        options=options,
    )


class SerpApiProvider:
    uses_quota = True
    endpoint = "https://serpapi.com/search"

    def __init__(self, key_param_name: str, api_key: Optional[str] = None):
        self._key_param_name = key_param_name
        self._api_key = api_key

    def _key(self) -> str:
        if self._api_key is None:  # fetched once, reused while the Lambda stays warm
            import boto3
            ssm = boto3.client("ssm")
            self._api_key = ssm.get_parameter(Name=self._key_param_name, WithDecryption=True)["Parameter"]["Value"]
        return self._api_key

    def build_params(self, watch: dict) -> dict:
        trip_type = int(watch.get("tripType", 1))
        params = {
            "engine": "google_flights",
            "departure_id": watch["origin"],
            "arrival_id": watch["destination"],
            "outbound_date": watch["outboundDate"],
            "type": str(trip_type),
            "adults": str(int(watch.get("adults", 1))),
            "currency": watch.get("currency", "EUR"),
            "hl": "en",
            "gl": watch.get("gl", "nl"),
            # Keep every setting constant over time, otherwise the price
            # history gets artificial jumps.
            "deep_search": "true",
        }
        if trip_type == 1:
            params["return_date"] = watch["returnDate"]
        max_stops = watch.get("maxStops")
        if max_stops is not None and 0 <= int(max_stops) <= 2:
            # Our maxStops 0/1/2 -> SerpApi stops 1 (nonstop only) / 2 (1 stop or fewer) / 3 (2 stops or fewer)
            params["stops"] = str(int(max_stops) + 1)
        return params

    def fetch(self, watch: dict) -> Quote:
        params = self.build_params(watch)
        params["api_key"] = self._key()
        request = Request(f"{self.endpoint}?{urlencode(params)}", headers={"User-Agent": "flight-tracker/0.1"})
        try:
            with urlopen(request, timeout=50) as response:
                data = json.loads(response.read())
        except HTTPError as e:  # "from None": never let the URL (it holds the key) reach the logs
            raise ProviderError(f"SerpApi HTTP {e.code}") from None
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            raise ProviderError(f"SerpApi request failed: {type(e).__name__}") from None
        return parse_serpapi_response(data, params["currency"])


class FakeProvider:
    """Deterministic made-up prices for testing the whole loop without API usage."""
    uses_quota = False

    def fetch(self, watch: dict) -> Quote:
        day = datetime.now(timezone.utc).date().isoformat()
        seed = int(hashlib.sha256(f"{watch['id']}{day}".encode()).hexdigest(), 16)
        base = 500 + seed % 400
        options = [
            {
                "price": Decimal(base + extra), "totalDuration": 700 + extra, "stops": 1, "airlines": "FakeAir",
                "segments": [
                    {"flight": "FA 100", "airline": "FakeAir", "from": watch["origin"], "to": "DOH",
                     "departs": f"{watch['outboundDate']} 09:00", "arrives": f"{watch['outboundDate']} 16:00"},
                    {"flight": "FA 200", "airline": "FakeAir", "from": "DOH", "to": watch["destination"],
                     "departs": f"{watch['outboundDate']} 18:00", "arrives": f"{watch['outboundDate']} 23:30"},
                ],
                "layovers": [{"airport": "DOH", "minutes": 120}],
            }
            for extra in (0, 60, 120)
        ]
        return Quote(
            price=options[0]["price"], currency=watch.get("currency", "EUR"), airlines="FakeAir", stops=1,
            total_duration=700, price_level="typical", typical_low=Decimal(450), typical_high=Decimal(950),
            link="https://example.com/fake-flight-search", options=options,
        )


def get_provider(name: str):
    if name == "serpapi":
        return SerpApiProvider(os.environ["SERPAPI_KEY_PARAM"])
    if name == "fake":
        return FakeProvider()
    raise ValueError(f"unknown provider: {name}")