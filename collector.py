import os
import psycopg2
import requests
from psycopg2.extras import execute_values

DATABASE_URL = os.environ["NEON_DATABASE_URL"]
URL = "https://api.elections.kalshi.com/trade-api/v2/markets"


def cents(m, legacy, dollars):
    v = m.get(legacy)
    if v:
        return int(v)
    d = m.get(dollars)
    return round(float(d) * 100) if d else 0


markets, cursor = [], None
for _ in range(20):  # max 20 pages of 1000
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

rows = []
for m in markets:
    ticker = m.get("ticker", "")
    if ticker.startswith("KXMVE"):
        continue
    bid = cents(m, "yes_bid", "yes_bid_dollars")
    ask = cents(m, "yes_ask", "yes_ask_dollars")
    vol = int(m.get("volume") or float(m.get("volume_fp") or 0))
    if bid == 0 or ask == 0:
        continue
    rows.append((ticker, m.get("title", ""), bid, ask, vol))

if markets and not rows:
    s = markets[0]
    print("DEBUG sample:", {k: s.get(k) for k in
          ["ticker", "yes_bid", "yes_bid_dollars", "yes_ask",
           "yes_ask_dollars", "volume", "volume_fp"]})

if rows:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    execute_values(
        cur,
        "INSERT INTO kalshi_markets (ticker, title) VALUES %s "
        "ON CONFLICT (ticker) DO NOTHING",
        [(t, ti) for t, ti, _, _, _ in rows],
    )
    execute_values(
        cur,
        "INSERT INTO kalshi_book (ticker, yes_bid, yes_ask, volume) VALUES %s",
        [(t, b, a, v) for t, _, b, a, v in rows],
    )
    conn.commit()
    cur.close()
    conn.close()

print(f"Saved {len(rows)} snapshots from {len(markets)} markets.")
