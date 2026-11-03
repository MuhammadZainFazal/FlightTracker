import json
from decimal import Decimal
from pathlib import Path

import pytest

from providers import FakeProvider, NoResults, ProviderError, SerpApiProvider, parse_serpapi_response

FIXTURE = Path(__file__).parent / "fixtures" / "serpapi_ams_lhe.json"


def load():
    return json.loads(FIXTURE.read_text())


def test_parses_cheapest_itinerary_and_insights():
    quote = parse_serpapi_response(load(), "EUR")
    assert quote.price == Decimal("1670")
    assert quote.currency == "EUR"
    assert quote.airlines == "Qatar Airways"
    assert quote.stops == 1
    assert quote.total_duration == 735
    assert quote.price_level == "typical"
    assert (quote.typical_low, quote.typical_high) == (Decimal("1400"), Decimal("2150"))
    assert quote.link.startswith("https://www.google.com/travel/flights")


def test_stores_the_three_cheapest_options_in_price_order():
    quote = parse_serpapi_response(load(), "EUR")
    assert [o["price"] for o in quote.options] == [Decimal("1670"), Decimal("1820"), Decimal("1853")]


def test_option_contains_flight_numbers_times_and_layovers():
    first = parse_serpapi_response(load(), "EUR").options[0]
    assert first["stops"] == 1 and first["airlines"] == "Qatar Airways" and first["totalDuration"] == 735
    assert first["segments"][0] == {"flight": "QR 274", "airline": "Qatar Airways", "from": "AMS", "to": "DOH",
                                    "departs": "2026-11-28 15:05", "arrives": "2026-11-28 23:20"}
    assert first["segments"][1]["flight"] == "QR 628" and first["segments"][1]["arrives"] == "2026-11-29 07:20"
    assert first["layovers"] == [{"airport": "DOH", "minutes": 160}]


def test_other_flights_are_ignored_when_best_flights_exist():
    data = load()
    data["other_flights"][0]["price"] = 100   # cheaper, but not one of Google's top picks
    assert parse_serpapi_response(data, "EUR").price == Decimal("1670")


def test_falls_back_to_other_flights_when_there_are_no_best_flights():
    data = load()
    del data["best_flights"]
    quote = parse_serpapi_response(data, "EUR")
    assert quote.price == Decimal("1675") and len(quote.options) == 1


def test_fewer_than_three_options_is_fine():
    data = load()
    data["best_flights"] = data["best_flights"][:1]
    assert len(parse_serpapi_response(data, "EUR").options) == 1


def test_works_without_price_insights():
    data = load()
    del data["price_insights"]
    quote = parse_serpapi_response(data, "EUR")
    assert quote.price_level is None and quote.typical_low is None


def test_no_results_error():
    with pytest.raises(NoResults):
        parse_serpapi_response({"error": "Google Flights hasn't returned any results for this query."}, "EUR")


def test_empty_response_is_no_results():
    with pytest.raises(NoResults):
        parse_serpapi_response({"best_flights": [], "other_flights": []}, "EUR")


def test_other_api_error_is_provider_error():
    with pytest.raises(ProviderError) as info:
        parse_serpapi_response({"error": "Invalid API key."}, "EUR")
    assert not isinstance(info.value, NoResults)


def test_request_params_round_trip():
    watch = {"origin": "AMS", "destination": "LHE", "outboundDate": "2026-12-20", "returnDate": "2027-01-10",
             "tripType": 1, "adults": Decimal("2"), "currency": "EUR"}
    params = SerpApiProvider("/x", api_key="k").build_params(watch)
    assert params["engine"] == "google_flights"
    assert params["return_date"] == "2027-01-10"
    assert params["type"] == "1" and params["adults"] == "2"
    assert "stops" not in params           # no filter unless maxStops is set
    assert "api_key" not in params         # only added at call time


def test_max_stops_maps_to_serpapi_stops_codes():
    base = {"origin": "AMS", "destination": "LHE", "outboundDate": "2026-12-20", "tripType": 2}
    build = lambda max_stops: SerpApiProvider("/x", api_key="k").build_params({**base, "maxStops": max_stops})
    assert build(0)["stops"] == "1"                   # direct only
    assert build(Decimal("1"))["stops"] == "2"        # at most 1 stop (DynamoDB hands back Decimals)
    assert build(2)["stops"] == "3"                   # at most 2 stops


def test_one_way_has_no_return_date():
    watch = {"origin": "AMS", "destination": "LHE", "outboundDate": "2026-12-20", "tripType": 2}
    assert "return_date" not in SerpApiProvider("/x", api_key="k").build_params(watch)


def test_fake_provider_is_deterministic_with_three_options():
    watch = {"id": "ams-lhe-test", "currency": "EUR", "origin": "AMS", "destination": "LHE", "outboundDate": "2026-12-20"}
    first, second = FakeProvider().fetch(watch), FakeProvider().fetch(watch)
    assert first.price == second.price and 500 <= first.price < 900
    assert len(first.options) == 3 and first.options[0]["segments"][0]["flight"] == "FA 100"