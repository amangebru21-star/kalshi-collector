import os
import time

import psycopg2
import requests
from psycopg2.extras import execute_values

DB_URL = (
    os.environ.get("NEON_DATABASE_URL", "")
    .strip()
    .strip('"')
    .replace("&channel_binding=require", "")
)
URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
RETENTION_DAYS = 14
MAX_PAGES = 20  # 1000 markets per page


def cents(m, legacy, dollars):
    """Kalshi returns prices as legacy int cents and/or '0.5600' dollar strings."""
    v = m.get(legacy)
    if v:
        return int(v)
    d = m.get(dollars)
    return round(float(d) * 100) if d else 0


def fetch_markets():
    markets, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor

        for attempt in range(4):  # back off if rate limited
            r = requests.get(URL, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * 2**attempt)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Kalshi rate limit: gave up after 4 retries")

        data = r.json()
        markets += data.get("markets", [])
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.25)
    return markets


def main():
    if not DB_URL:
        raise ValueError("NEON_DATABASE_URL environment variable is missing.")

    markets = fetch_markets()

    # one row per ticker, quoted markets only
    rows = {}
    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker or ticker.startswith("KXMVE"):
            continue
        bid = cents(m, "yes_bid", "yes_bid_dollars")
        ask = cents(m, "yes_ask", "yes_ask_dollars")
        if bid == 0 or ask == 0:
            continue
        vol = int(float(m.get("volume") or m.get("volume_fp") or 0))
        rows[ticker] = (ticker, m.get("title", ""), bid, ask, vol)
    rows = list(rows.values())

    if markets and not rows:
        s = markets[0]
        print("DEBUG sample:", {k: s.get(k) for k in [
            "ticker", "yes_bid", "yes_bid_dollars", "yes_ask",
            "yes_ask_dollars", "volume", "volume_fp"]})

    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                "INSERT INTO kalshi_markets (ticker, title) VALUES %s "
                "ON CONFLICT (ticker) DO UPDATE SET title = EXCLUDED.title",
                [(t, ti) for t, ti, _, _, _ in rows],
            )
            execute_values(
                cur,
                "INSERT INTO kalshi_book (ticker, yes_bid, yes_ask, volume) VALUES %s",
                [(t, b, a, v) for t, _, b, a, v in rows],
            )
        # storage maintenance
        cur.execute(
            "DELETE FROM kalshi_book WHERE ts < now() - make_interval(days => %s)",
            (RETENTION_DAYS,),
        )
        purged = cur.rowcount

    print(f"Saved {len(rows)} snapshots from {len(markets)} markets; purged {purged} old rows.")


if __name__ == "__main__":
    main()
