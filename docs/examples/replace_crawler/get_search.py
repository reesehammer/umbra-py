import json
import urllib.request

url = (
    "https://api.umbra-py.space/search"
    "?bbox=85.49,28.26,85.56,28.32"
    "&datetime=2026-09-01/.."
    "&product_types=GEC"
    "&collections=umbra-open-data"
    "&limit=5"
)
for feature in json.load(urllib.request.urlopen(url))["features"]:
    print(feature["assets"]["gec"]["href"])
