import os

import pandas as pd
import psycopg2
import requests

DB_URL = (
    os.environ.get("NEON_DATABASE_URL", "")
    .strip()
    .strip('"')
    .replace("&channel_binding=require", "")
)
WEBHOOK = os.environ.get("WEBHOOK")

# ---------------- tuning knobs ----------------
LOOKBACK_HOURS = 24
MIN_SNAPS = 6            # history intervals needed per market
MIN_GAP_MIN = 10         # ignore intervals shorter than this (manual re-runs)
Z = 3.0                  # std devs above that market's own normal rate
MIN_DVOL = 50            # min contracts traded since last snapshot
MIN_PRICE, MAX_PRICE = 10, 90   # skip longshot / near-certain markets (cents)
MAX_SPREAD = 6           # skip wide-spread markets (cents)
COOLDOWN_HOURS = 6       # don't re-alert the same ticker inside this window
MAX_ALERTS = 10          # per run, strongest first

SETUP = """
CREATE TABLE IF NOT EXISTS alert_history (
    id SERIAL PRIMARY KEY,
    ticker TEXT,
    ts TIMESTAMP DEFAULT now(),
    dvol INT,
    mid_price NUMERIC,
    alert_msg TEXT
);
ALTER TABLE alert_history
    ADD COLUMN IF NOT EXISTS yes_bid INT,
    ADD COLUMN IF NOT EXISTS yes_ask INT,
    ADD COLUMN IF NOT EXISTS vph NUMERIC,
    ADD COLUMN IF NOT EXISTS mult NUMERIC,
    ADD COLUMN IF NOT EXISTS dmid NUMERIC,
    ADD COLUMN IF NOT EXISTS mid_1h NUMERIC,
    ADD COLUMN IF NOT EXISTS mid_6h NUMERIC;
"""

# fill in "what did the price do afterwards" once enough time has passed
BACKFILL = """
UPDATE alert_history a SET {col} = (
    SELECT (b.yes_bid + b.yes_ask) / 2.0 FROM kalshi_book b
    WHERE b.ticker = a.ticker
      AND b.ts >= a.ts + interval '{h} hours'
      AND b.ts <  a.ts + interval '{h2} hours'
    ORDER BY b.ts LIMIT 1)
WHERE a.{col} IS NULL
  AND a.ts <= now() - interval '{h2} hours'
  AND a.ts >  now() - interval '3 days'
"""

RAW = """
SELECT b.ticker, m.title, b.ts, b.yes_bid, b.yes_ask, b.volume
FROM kalshi_book b JOIN kalshi_markets m ON m.ticker = b.ticker
WHERE b.ts > now() - make_interval(hours => %s)
ORDER BY b.ticker, b.ts
"""

INSERT = """
INSERT INTO alert_history
    (ticker, dvol, mid_price, yes_bid, yes_ask, vph, mult, dmid, alert_msg)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

SCORE = """
SELECT count(*),
       avg(sign(dmid) * ({col} - mid_price)),
       avg(((sign(dmid) * ({col} - mid_price)) > 0)::int),
       avg(yes_ask - yes_bid)
FROM alert_history
WHERE {col} IS NOT NULL AND dmid IS NOT NULL AND dmid <> 0
"""


def num(x):
    return None if pd.isna(x) else float(x)


def find_alerts(df, cooled):
    if df.empty:
        return []

    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values(["ticker", "ts"])
    g = df.groupby("ticker")

    df["dvol"] = g["volume"].diff()
    df["hrs"] = g["ts"].diff().dt.total_seconds() / 3600
    df["vph"] = df["dvol"] / df["hrs"]          # volume per HOUR, so uneven gaps compare fairly
    df.loc[df["hrs"] < MIN_GAP_MIN / 60, ["dvol", "vph"]] = float("nan")
    df["mid"] = (df["yes_bid"] + df["yes_ask"]) / 2
    df["dmid"] = g["mid"].diff()

    latest = df["ts"].max()
    out = []

    for tkr, t in df.groupby("ticker"):
        last = t.iloc[-1]
        if tkr in cooled or last["ts"] < latest - pd.Timedelta(minutes=5):
            continue  # cooling down, or not in the newest collector run

        hist = t["vph"].iloc[:-1].dropna()
        hist = hist[hist >= 0]
        if len(hist) < MIN_SNAPS or pd.isna(last["vph"]) or last["dvol"] < MIN_DVOL:
            continue

        mid = last["mid"]
        spread = last["yes_ask"] - last["yes_bid"]
        if not (MIN_PRICE <= mid <= MAX_PRICE) or spread > MAX_SPREAD:
            continue

        mean, std = hist.mean(), hist.std(ddof=0)
        if last["vph"] < mean + Z * std:
            continue

        mult = last["vph"] / max(mean, 1.0)
        dmid = 0.0 if pd.isna(last["dmid"]) else float(last["dmid"])
        arrow = "▲" if dmid > 0 else "▼" if dmid < 0 else "▬"
        msg = (
            f"🔥 {str(last['title'])[:60]}\n{tkr}\n"
            f"{arrow} mid {mid:.1f}¢ ({dmid:+.1f}) | bid {int(last['yes_bid'])} / "
            f"ask {int(last['yes_ask'])} (spread {int(spread)})\n"
            f"{last['vph']:.0f}/hr vs norm {mean:.0f}/hr ({mult:.1f}x) | "
            f"+{int(last['dvol'])} contracts"
        )
        out.append(dict(
            ticker=tkr, dvol=int(last["dvol"]), mid=float(mid),
            bid=int(last["yes_bid"]), ask=int(last["yes_ask"]),
            vph=float(last["vph"]), mult=float(mult), dmid=dmid, msg=msg,
        ))

    out.sort(key=lambda a: a["mult"], reverse=True)
    return out[:MAX_ALERTS]


def print_scorecard(cur):
    for col, label in (("mid_1h", "1h"), ("mid_6h", "6h")):
        cur.execute(SCORE.format(col=col))
        n, follow, hit, spr = cur.fetchone()
        if not n:
            print(f"Scorecard {label}: no scored alerts yet.")
        else:
            print(
                f"Scorecard {label}: n={n} | hit rate {float(hit):.0%} | "
                f"avg follow-through {float(follow):+.2f}¢ | avg spread {float(spr):.1f}¢"
            )


def main():
    if not DB_URL:
        raise ValueError("NEON_DATABASE_URL environment variable is missing.")

    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(SETUP)
        cur.execute(BACKFILL.format(col="mid_1h", h=1, h2=2))
        cur.execute(BACKFILL.format(col="mid_6h", h=6, h2=7))

        cur.execute(RAW, (LOOKBACK_HOURS,))
        df = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
        if df.empty:
            print("No market data found for the lookback period.")
            print_scorecard(cur)
            return

        cur.execute(
            "SELECT DISTINCT ticker FROM alert_history "
            "WHERE ts > now() - make_interval(hours => %s)",
            (COOLDOWN_HOURS,),
        )
        cooled = {r[0] for r in cur.fetchall()}

        alerts = find_alerts(df, cooled)
        for a in alerts:
            cur.execute(INSERT, (
                a["ticker"], a["dvol"], a["mid"], a["bid"], a["ask"],
                a["vph"], a["mult"], a["dmid"], a["msg"],
            ))

        print_scorecard(cur)

    if not alerts:
        print("No volume spikes.")
        return

    msg = f"📈 Kalshi volume spikes ({len(alerts)})\n\n" + "\n\n".join(a["msg"] for a in alerts)
    print(msg)
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg[:1900]}, timeout=15).raise_for_status()
        except requests.RequestException as e:
            print("Webhook failed:", e)


if __name__ == "__main__":
    main()
