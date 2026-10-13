import os
import time
import logging
import requests
import storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("collector")

DB_URL = (
    os.environ.get("NEON_DATABASE_URL", "")
    .strip()
    .strip('"')
    .replace("&channel_binding=require", "")
)
URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
MAX_PAGES = 20  # 1000 markets per page

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

    log.info("Connecting to database using lean storage module...")
    conn = storage.connect(DB_URL)
    
    try:
        # Ensure schema tables and partitions exist
        storage.init_schema(conn)
        log.info("Database schema verified.")

        markets = fetch_markets()
        log.info(f"Fetched {len(markets)} markets from Kalshi API.")

        # Filter out multivariate or empty tickers
        usable_markets = [m for m in markets if m.get("ticker") and not m.get("ticker", "").startswith("KXMVE")]

        if usable_markets:
            # save_snapshot handles metadata upserts, change-driven ticks, and deduplication!
            stats = storage.save_snapshot(conn, usable_markets)
            log.info(f"Snapshot saved successfully: {stats}")

        # Run storage maintenance (rollups, anchors, partition drops) if due
        m_stats = storage.maintain_if_due(conn)
        if m_stats:
            log.info(f"Maintenance performed: {m_stats}")
        else:
            log.info("Maintenance not due yet.")

    except Exception as e:
        log.error(f"Collector run failed: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
