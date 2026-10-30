import os, sys
sys.path.insert(0, "src/checker")
from providers import SerpApiProvider

provider = SerpApiProvider("unused", api_key=os.environ["SERPAPI_KEY"])
print(provider.fetch({
    "origin": "AMS", "destination": "LHE", "outboundDate": "2026-12-20",
    "returnDate": "2027-01-10", "tripType": 1, "adults": 1, "currency": "EUR",
}))