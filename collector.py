import os
import requests
import psycopg2

DB_URL = os.environ.get("NEON_DATABASE_URL")
if DB_URL:
    DB_URL = DB_URL.strip().strip('"').replace("&channel_binding=require", "")

def main():
    if not DB_URL:
        raise ValueError("NEON_DATABASE_URL environment variable is missing.")

    # Fetch active markets and order book snapshots from Kalshi API
    url = "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000"
    resp = requests.get(url)
    resp.raise_for_status()
    markets = resp.json().get("markets", [])

    if not markets:
        print("No open markets found from Kalshi API.")
        return

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # Ensure tables exist (adjust schema as per your setup)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            ticker TEXT PRIMARY KEY,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS kalshi_book (
            ticker TEXT,
            ts TIMESTAMP DEFAULT now(),
            yes_bid INT,
            yes_ask INT,
            volume INT
        );
    """)

    for m in markets:
        ticker = m.get("ticker")
        title = m.get("title")
        cur.execute(
            "INSERT INTO kalshi_markets (ticker, title) VALUES (%s, %s) ON CONFLICT (ticker) DO UPDATE SET title = EXCLUDED.title;",
            (ticker, title)
        )

        yes_bid = m.get("yes_bid")
        yes_ask = m.get("yes_ask")
        volume = m.get("volume")

        cur.execute(
            "INSERT INTO kalshi_book (ticker, yes_bid, yes_ask, volume) VALUES (%s, %s, %s, %s);",
            (ticker, yes_bid, yes_ask, volume)
        )

    conn.commit()
    cur.close()
    conn.close()
    print(f"Successfully collected data for {len(markets)} markets.")

if __name__ == "__main__":
    main()
