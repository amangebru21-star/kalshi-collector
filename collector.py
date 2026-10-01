import os
import psycopg2
import requests

DATABASE_URL = os.environ["NEON_DATABASE_URL"]
URL = "https://api.elections.kalshi.com/trade-api/v2/markets"

markets, cursor = [], None
for _ in range(10):  # max 10 pages
    params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
    if cursor:
        params["cursor"] = cursor
    r = requests.get(URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    markets += data.get("markets", [])
    cursor = data.get("cursor")
    if not cursor:
        break

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()
saved = 0

for m in markets:
    ticker = m.get("ticker", "")
    yes_bid = m.get("yes_bid") or 0
    yes_ask = m.get("yes_ask") or 0
    volume = m.get("volume") or 0

    if ticker.startswith("KXMVE") or yes_bid == 0 or yes_ask == 0:
        continue

    cur.execute(
        "INSERT INTO kalshi_markets (ticker, title) VALUES (%s, %s) "
        "ON CONFLICT (ticker) DO NOTHING",
        (ticker, m.get("title", "")),
    )
    cur.execute(
        "INSERT INTO kalshi_book (ticker, yes_bid, yes_ask, volume) "
        "VALUES (%s, %s, %s, %s)",
        (ticker, yes_bid, yes_ask, volume),
    )
    saved += 1

conn.commit()
cur.close()
conn.close()
print(f"Saved {saved} snapshots from {len(markets)} markets.")
