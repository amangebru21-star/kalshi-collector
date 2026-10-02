#!/usr/bin/env python3
"""
Daily gaming trend & script bot.

Generates one original Roblox/Fortnite short-form video concept with Gemini
(grounded in Google Search when available), posts it to a Discord webhook,
and remembers past ideas in Neon/Postgres so it doesn't repeat itself.

Env vars:
    GEMINI_API_KEY        required
    WEBHOOK               Discord webhook URL (optional; prints to stdout if unset)
    NEON_DATABASE_URL     Postgres URL for idea history (optional)
    GEMINI_MODEL          comma-separated model fallback list
                          (default: gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite)
    USE_SEARCH            "1" (default) to ground in Google Search, "0" to disable
"""

import datetime as dt
import logging
import os
import re
import sys
import time

import requests
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trend_bot")

DB_URL = os.environ.get("NEON_DATABASE_URL", "")
WEBHOOK = os.environ.get("WEBHOOK", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Tried in order; if one is retired or unavailable to your key, the next is used.
# (gemini-2.5-* is being wound down, so it is intentionally not in the list.)
MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite"
    ).split(",")
    if m.strip()
]
USE_SEARCH = os.environ.get("USE_SEARCH", "1") == "1"

DISCORD_LIMIT = 1900          # hard limit is 2000; leave headroom
HISTORY_SIZE = 15             # past titles fed back into the prompt
RETRYABLE_CODES = {429, 500, 503, 504}

ACTIVE_FOCUS = (
    "Focus platforms: YouTube Shorts and TikTok. "
    "Niches: Roblox (simulator, tycoon, and co-op horror trends) and Fortnite "
    "(UEFN map mechanics, custom game modes like Reload, and hidden meta tricks)."
)


# --------------------------------------------------------------------------- #
# History (optional, Neon/Postgres)
# --------------------------------------------------------------------------- #
def load_history() -> list[str]:
    if not DB_URL:
        return []
    try:
        import psycopg

        with psycopg.connect(DB_URL, connect_timeout=10) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trend_ideas ("
                "id SERIAL PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "title TEXT NOT NULL)"
            )
            rows = conn.execute(
                "SELECT title FROM trend_ideas ORDER BY created_at DESC LIMIT %s",
                (HISTORY_SIZE,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:  # history is a nice-to-have, never fatal
        log.warning("Could not load history: %s", e)
        return []


def save_history(title: str) -> None:
    if not DB_URL or not title:
        return
    try:
        import psycopg

        with psycopg.connect(DB_URL, connect_timeout=10) as conn:
            conn.execute("INSERT INTO trend_ideas (title) VALUES (%s)", (title[:500],))
    except Exception as e:
        log.warning("Could not save history: %s", e)


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
def build_prompt(past_titles: list[str]) -> str:
    today = dt.date.today().strftime("%A, %B %d, %Y")
    avoid = ""
    if past_titles:
        avoid = (
            "\nDo NOT repeat or closely resemble any of these previous ideas:\n"
            + "\n".join(f"- {t}" for t in past_titles)
            + "\n"
        )
    return f"""Today is {today}.
{ACTIVE_FOCUS}

Act as an elite short-form gaming content strategist. Check what is currently
trending in these niches: recent official updates (Roblox developer announcements,
Fortnite / UEFN release notes), plus what is performing on YouTube Shorts and TikTok.
Then produce ONE original, specific video concept for Roblox or Fortnite. Avoid
generic advice; name concrete games, mechanics, update features, or map types.
{avoid}
Use exactly this structure and nothing else (no intro, no outro):

🎬 **TITLE / HOOK IDEA**: (catchy, high-CTR title + the first 3-second visual hook)
💡 **THE ORIGINAL ANGLE**: (why this stands out from standard creator content)
📝 **30-SECOND SCRIPT BREAKDOWN**:
• [0-3s] Hook:
• [3-20s] Core Value / Action:
• [20-30s] CTA & Loop:
⚡ **ESTIMATED EFFORT**: (Low / Medium / High production time, with one line why)
"""


def call_model(client: genai.Client, model: str, prompt: str, use_search: bool) -> str:
    """One model, one tool config. Retries transient errors with backoff."""
    # Note: temperature/top_p/top_k are deprecated on current Gemini models, so
    # they are deliberately not set. The token cap is generous because thinking
    # tokens count against it.
    config = types.GenerateContentConfig(
        max_output_tokens=2048,
        tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None,
    )
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response.")
            return text
        except genai_errors.APIError as e:
            if e.code in RETRYABLE_CODES and attempt < 3:
                delay = 2 ** attempt
                log.warning("%s: HTTP %s (attempt %d); retrying in %ds", model, e.code, attempt, delay)
                time.sleep(delay)
                continue
            raise
        except RuntimeError:
            if attempt < 3:
                time.sleep(2)
                continue
            raise
    raise RuntimeError("unreachable")


def generate_trend_intelligence(past_titles: list[str]) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = build_prompt(past_titles)
    search_modes = [True, False] if USE_SEARCH else [False]
    last_error: Exception | None = None

    for model in MODELS:
        for use_search in search_modes:
            try:
                log.info("Generating with %s (search=%s)", model, use_search)
                return call_model(client, model, prompt, use_search)
            except (genai_errors.APIError, RuntimeError) as e:
                last_error = e
                log.warning("%s failed (search=%s): %s", model, use_search, str(e)[:200])

    raise RuntimeError(f"All models failed ({', '.join(MODELS)}). Last error: {last_error}")


def extract_title(content: str) -> str:
    m = re.search(r"TITLE\s*/\s*HOOK IDEA\*{0,2}\s*:?\s*\*{0,2}\s*(.+)", content)
    title = m.group(1) if m else content.splitlines()[0]
    return title.strip(" *")[:300]


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries so no chunk exceeds `limit` or breaks mid-line."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # pathological single long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


def post_discord(text: str) -> None:
    if not WEBHOOK:
        return
    for chunk in split_message(text):
        for _ in range(4):
            r = requests.post(
                WEBHOOK,
                json={"content": chunk, "allowed_mentions": {"parse": []}},
                timeout=15,
            )
            if r.status_code == 429:  # Discord rate limit
                try:
                    wait = float(r.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(wait + 0.5)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Discord kept rate-limiting the webhook.")
        time.sleep(0.4)


def sanitize(msg: str) -> str:
    for secret in (GEMINI_API_KEY, WEBHOOK, DB_URL):
        if secret:
            msg = msg.replace(secret, "[redacted]")
    return msg[:1200]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        history = load_history()
        content = generate_trend_intelligence(history)
        message = "📈 **Automated Gaming Trend & Script Intelligence**\n\n" + content

        print(message)
        post_discord(message)
        save_history(extract_title(content))
        return 0

    except Exception as e:
        err = f"⚠️ **Trend Bot Error**: {type(e).__name__}: `{sanitize(str(e))}`"
        log.error(err)
        try:
            post_discord(err)
        except Exception as alert_error:
            log.error("Could not send error alert: %s", sanitize(str(alert_error)))
        return 1  # non-zero so the GitHub Actions run shows as failed


if __name__ == "__main__":
    sys.exit(main())
