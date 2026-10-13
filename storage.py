"""Lean market-data storage for a Kalshi collector on a small Postgres (e.g. Neon free tier)."""
import os
import logging
from datetime import datetime, timedelta, timezone, date

import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger("storage")

DB_CAP_MB = float(os.getenv("DB_CAP_MB", "1000"))
HEARTBEAT_MIN = 60
ANCHOR_HORIZONS_MIN = (1440, 60, 5)
MAINTAIN_EVERY_HOURS = 20

LADDER = [(0.95, 2, "STOP_TICKS"), (0.85, 2, "TIGHT"), (0.70, 3, "TRIMMED"), (0.0, 4, "NORMAL")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_dim (
  id SERIAL PRIMARY KEY,
  ticker TEXT UNIQUE NOT NULL,
  event_ticker TEXT, series TEXT, strike_type TEXT,
  floor_strike REAL, cap_strike REAL,
  close_time TIMESTAMPTZ,
  result SMALLINT
);
CREATE TABLE IF NOT EXISTS latest (
  market_id INT PRIMARY KEY,
  yes_bid SMALLINT, yes_ask SMALLINT, volume INT, oi INT,
  last_seen TIMESTAMPTZ NOT NULL, last_tick_ts TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ticks (
  ts TIMESTAMPTZ NOT NULL, market_id INT NOT NULL,
  yes_bid SMALLINT, yes_ask SMALLINT, volume INT, oi INT
) PARTITION BY RANGE (ts);
CREATE TABLE IF NOT EXISTS bars_1h (
  hour TIMESTAMPTZ NOT NULL, market_id INT NOT NULL,
  open_ask SMALLINT, high_ask SMALLINT, low_ask SMALLINT, close_ask SMALLINT,
  close_bid SMALLINT, max_volume INT, last_oi INT, n INT,
  PRIMARY KEY (hour, market_id)
);
CREATE TABLE IF NOT EXISTS anchors (
  market_id INT NOT NULL, horizon_min INT NOT NULL,
  ts TIMESTAMPTZ, yes_bid SMALLINT, yes_ask SMALLINT,
  PRIMARY KEY (market_id, horizon_min)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

def connect(url: str | None = None):
    conn = psycopg2.connect(url or os.environ["DATABASE_URL"], options="-c timezone=UTC")
    conn.autocommit = False
    return conn

def cents(m: dict, key: str) -> int | None:
    v = m.get(key + "_dollars")
    try:
        if v not in (None, ""):
            c = round(float(v) * 100)
        else:
            v = m.get(key)
            if v in (None, ""):
                return None
            c = round(float(v))
    except (TypeError, ValueError):
        return None
    return c if 0 < c < 100 else None

def _f(v):
    try: return float(v) if v not in (None, "") else None
    except (TypeError, ValueError): return None

def _i(v):
    try: return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError): return None

def _ts(s):
    if not s: return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError: return None

def init_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()

def db_size_mb(conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        return cur.fetchone()[0] / 1_048_576.0

def budget_mode(conn) -> tuple[str, int, float]:
    size = db_size_mb(conn)
    frac = size / DB_CAP_MB
    for threshold, days, mode in LADDER:
        if frac >= threshold:
            return mode, days, size
    return "NORMAL", 4, size

def _part_name(d: date) -> str:
    return f"ticks_{d:%Y%m%d}"

def ensure_partitions(conn, now: datetime):
    with conn.cursor() as cur:
        for off in (0, 1):
            d = (now + timedelta(days=off)).date()
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {_part_name(d)} PARTITION OF ticks "
                f"FOR VALUES FROM ('{d:%Y-%m-%d}') TO ('{d + timedelta(days=1):%Y-%m-%d}')"
            )
    conn.commit()

def list_partitions(conn) -> list[tuple[str, date]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = 'ticks'"
        )
        out = []
        for (name,) in cur.fetchall():
            try:
                out.append((name, datetime.strptime(name.split("_")[1], "%Y%m%d").date()))
            except (IndexError, ValueError): pass
        return sorted(out, key=lambda x: x[1])

def save_snapshot(conn, markets: list[dict], series: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    mode, _, size = budget_mode(conn)
    ensure_partitions(conn, now)

    usable = [m for m in markets if m.get("ticker")]
    stats = {"seen": len(usable), "ticked": 0, "unchanged": 0, "mode": mode, "size_mb": round(size, 1)}
    if not usable: return stats

    dims = [(m["ticker"], m.get("event_ticker"), series or m.get("series_ticker"), m.get("strike_type"),
             _f(m.get("floor_strike")), _f(m.get("cap_strike")),
             _ts(m.get("close_time") or m.get("expiration_time"))) for m in usable]

    with conn.cursor() as cur:
        ids = {t: i for i, t in execute_values(
            cur,
            "INSERT INTO market_dim (ticker, event_ticker, series, strike_type, floor_strike, cap_strike, close_time) "
            "VALUES %s ON CONFLICT (ticker) DO UPDATE SET close_time = EXCLUDED.close_time "
            "RETURNING id, ticker", dims, fetch=True)}

        cur.execute("SELECT market_id, yes_bid, yes_ask, last_tick_ts FROM latest WHERE market_id = ANY(%s)",
                    (list(ids.values()),))
        prev = {r[0]: r for r in cur.fetchall()}

        tick_rows, latest_rows = [], []
        for m in usable:
            mid = ids[m["ticker"]]
            bid, ask = cents(m, "yes_bid"), cents(m, "yes_ask")
            vol, oi = _i(m.get("volume")), _i(m.get("open_interest"))
            p = prev.get(mid)
            changed = p is None or (p[1], p[2]) != (bid, ask)
            stale = p is None or p[3] is None or (now - p[3]) >= timedelta(minutes=HEARTBEAT_MIN)
            write_tick = (changed or stale) and mode != "STOP_TICKS"
            if write_tick:
                tick_rows.append((now, mid, bid, ask, vol, oi))
                stats["ticked"] += 1
            else:
                stats["unchanged"] += 1
            latest_rows.append((mid, bid, ask, vol, oi, now, now if write_tick else (p[3] if p else None)))

        if tick_rows:
            execute_values(cur, "INSERT INTO ticks (ts, market_id, yes_bid, yes_ask, volume, oi) VALUES %s", tick_rows)
        execute_values(
            cur,
            "INSERT INTO latest (market_id, yes_bid, yes_ask, volume, oi, last_seen, last_tick_ts) VALUES %s "
            "ON CONFLICT (market_id) DO UPDATE SET yes_bid = EXCLUDED.yes_bid, yes_ask = EXCLUDED.yes_ask, "
            "volume = EXCLUDED.volume, oi = EXCLUDED.oi, last_seen = EXCLUDED.last_seen, "
            "last_tick_ts = EXCLUDED.last_tick_ts", latest_rows)
    conn.commit()
    return stats

def build_anchors(conn) -> int:
    n = 0
    with conn.cursor() as cur:
        for h in ANCHOR_HORIZONS_MIN:
            cur.execute(
                """
                INSERT INTO anchors (market_id, horizon_min, ts, yes_bid, yes_ask)
                SELECT d.id, %s, t.ts, t.yes_bid, t.yes_ask
                FROM market_dim d
                JOIN LATERAL (
                    SELECT ts, yes_bid, yes_ask FROM ticks
                    WHERE market_id = d.id AND ts <= d.close_time - make_interval(mins => %s)
                    ORDER BY ts DESC LIMIT 1
                ) t ON TRUE
                WHERE d.close_time < now() AND d.close_time > now() - interval '3 days'
                ON CONFLICT DO NOTHING
                """, (h, h))
            n += cur.rowcount
    conn.commit()
    return n

def rollup_and_drop(conn, keep_days: int, today: date) -> list[str]:
    dropped = []
    for name, d in list_partitions(conn):
        if d > today - timedelta(days=keep_days): continue
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO bars_1h (hour, market_id, open_ask, high_ask, low_ask, close_ask,
                                     close_bid, max_volume, last_oi, n)
                SELECT date_trunc('hour', ts), market_id,
                       (array_agg(yes_ask ORDER BY ts))[1], max(yes_ask), min(yes_ask),
                       (array_agg(yes_ask ORDER BY ts DESC))[1], (array_agg(yes_bid ORDER BY ts DESC))[1],
                       max(volume), (array_agg(oi ORDER BY ts DESC))[1], count(*)
                FROM {name} GROUP BY 1, 2
                ON CONFLICT (hour, market_id) DO NOTHING""")
            cur.execute(f"DROP TABLE {name}")
        conn.commit()
        dropped.append(name)
    return dropped

def maintain(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    mode, keep_days, size = budget_mode(conn)
    anchors = build_anchors(conn)
    dropped = rollup_and_drop(conn, keep_days, now.date())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO meta (k, v) VALUES ('last_maintain', %s) "
                    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (now.isoformat(),))
    conn.commit()
    return {"mode": mode, "keep_days": keep_days, "anchors_added": anchors,
            "partitions_dropped": dropped, "size_mb_before": round(size, 1),
            "size_mb_after": round(db_size_mb(conn), 1)}

def maintain_if_due(conn, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("SELECT v FROM meta WHERE k = 'last_maintain'")
        row = cur.fetchone()
    if row and now - datetime.fromisoformat(row[0]) < timedelta(hours=MAINTAIN_EVERY_HOURS):
        return None
    return maintain(conn, now)
