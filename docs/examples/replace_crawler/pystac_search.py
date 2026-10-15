from pystac_client import Client

search = Client.open("https://api.umbra-py.space/").search(
    collections=["umbra-open-data"],
    bbox=[85.49, 28.26, 85.56, 28.32],
    datetime="2026-09-01/..",
    query={"product_types": {"in": ["GEC"]}},
    limit=5,
    max_items=5,
)
for item in search.items():
    print(item.assets["gec"].href)
