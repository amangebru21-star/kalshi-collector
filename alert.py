import os
import pandas as pd
import psycopg2
import requests

DB_URL = os.environ.get("NEON_DATABASE_URL")
if DB_URL:
    DB_URL = DB_URL.strip().strip('"').replace("&channel_binding=require", "")

WEBHOOK = os.environ.get("WEBHOOK")
LOOKBACK = "24 hours"
MIN_SNAPS = 6
MIN_ABS = 50
Z = 3.0

def main():
    if not DB_URL:
        raise ValueError("NEON_DATABASE_URL environment variable is missing.")

    q = f"""
    WITH d AS (
        SELECT ticker, ts, yes_bid, yes_ask,
               volume - LAG(volume) OVER (PARTITION BY ticker ORDER BY ts) AS dvol
        FROM kalshi_book
        WHERE ts > now() - interval '{LOOKBACK}'
    )
    SELECT d.ticker, m.title, d.ts, d.yes_bid, d.yes_ask, d.dvol
    FROM d JOIN kalshi_markets m USING (ticker)
    WHERE d.dvol IS NOT NULL
    ORDER BY d.ticker, d.ts
    """

    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(q)
        df = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])

    if df.empty:
        print("No market data found for the given lookback period.")
        return

    latest = df.ts.max()
    alerts = []
    for tkr, g in df.groupby("ticker"):
        if len(g) < MIN_SNAPS:
            continue

        last = g.iloc[-1]
        if last.ts < latest - pd.Timedelta(minutes=5):
            continue  # stale: not in the newest collector run

        base = g.dvol.iloc[:-1]
        thresh = max(MIN_ABS, base.mean() + Z * base.std(ddof=0))
        if last.dvol >= thresh:
            alerts.append(
                f"🔥 {last.title[:60]}\n{tkr}\n+{last.dvol:.0f} vol "
                f"(norm ~{base.mean():.0f}) | bid {last.yes_bid} / ask {last.yes_ask}"
            )

    msg = "\n\n".join(alerts) or "No volume spikes."
    print(msg)

    if alerts and WEBHOOK:
        requests.post(WEBHOOK, json={"content": msg[:1900]})

if __name__ == "__main__":
    main()
