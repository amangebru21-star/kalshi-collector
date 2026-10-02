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
    OFFICIAL_SOURCES      comma-separated official URLs to read first ("none" to disable)
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

# Official pages Gemini reads first (via the URL context tool). Override with a
# comma-separated OFFICIAL_SOURCES env var; set it to "none" to disable.
DEFAULT_SOURCES = (
    "https://dev.epicgames.com/documentation/fortnite/whats-new-in-unreal-editor-for-fortnite,"
    "https://create.roblox.com/docs/en-us/releases"
)
_raw_sources = os.environ.get("OFFICIAL_SOURCES", DEFAULT_SOURCES)
OFFICIAL_SOURCES = (
    []
    if _raw_sources.strip().lower() == "none"
    else [u.strip() for u in _raw_sources.split(",") if u.strip().startswith("http")]
)

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
    official = ""
    if OFFICIAL_SOURCES:
        official = (
            "\nFirst, read these official pages and find the NEWEST release/update listed:\n"
            + "\n".join(f"- {u}" for u in OFFICIAL_SOURCES)
            + "\nBuild the concept around a specific, real change from the latest release "
            "notes (name the version number). Players and viewers care about what is new.\n"
        )
    return f"""Today is {today}.
{ACTIVE_FOCUS}

Act as an elite short-form gaming content strategist. Produce ONE original,
specific video concept for Roblox or Fortnite, based on what is currently new and
trending in these niches (official updates, plus what is performing on YouTube
Shorts and TikTok). Avoid generic advice; name concrete games, mechanics, update
features, or map types.
{official}{avoid}
Accuracy rules (important):
- Never invent map/island codes, setting names, device names, or input combos.
  Only state a mechanic as fact if a source you found supports it.
- If the idea depends on something you could not confirm, say so in the fact-check line.
- If you cannot find a real, current trend, pitch an evergreen idea and say it is evergreen.

Use exactly this structure and nothing else (no intro, no outro):

🎬 **TITLE / HOOK IDEA**: (catchy, high-CTR title + the first 3-second visual hook)
💡 **THE ORIGINAL ANGLE**: (why this stands out from standard creator content)
📝 **30-SECOND SCRIPT BREAKDOWN**:
• [0-3s] Hook:
• [3-20s] Core Value / Action:
• [20-30s] CTA & Loop:
⚡ **ESTIMATED EFFORT**: (Low / Medium / High production time, with one line why)
🔍 **FACT-CHECK**: (what is confirmed by sources vs. what the creator must test in-game before posting)
"""


def extract_sources(response) -> list[str]:
    """Official pages actually read (URL context) first, then Search grounding links."""
    urls: list[str] = []
    try:
        ucm = response.candidates[0].url_context_metadata
        for item in (ucm.url_metadata or []):
            status = str(getattr(item, "url_retrieval_status", ""))
            if item.retrieved_url and "SUCCESS" in status and item.retrieved_url not in urls:
                urls.append(item.retrieved_url)
    except (AttributeError, IndexError, TypeError):
        pass
    try:
        meta = response.candidates[0].grounding_metadata
        for chunk in (meta.grounding_chunks or []):
            web = getattr(chunk, "web", None)
            if web and web.uri and web.uri not in urls:
                urls.append(web.uri)
    except (AttributeError, IndexError, TypeError):
        pass
    return urls[:6]


def build_tools(mode: str) -> list[types.Tool] | None:
    tools: list[types.Tool] = []
    if mode in ("full", "urls") and OFFICIAL_SOURCES:
        tools.append(types.Tool(url_context=types.UrlContext()))
    if mode == "full":
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    return tools or None


def call_model(
    client: genai.Client, model: str, prompt: str, mode: str
) -> tuple[str, list[str]]:
    """One model, one tool config. Retries transient errors with backoff."""
    # Note: temperature/top_p/top_k are deprecated on current Gemini models, so
    # they are deliberately not set. The token cap is generous because thinking
    # tokens count against it.
    config = types.GenerateContentConfig(
        max_output_tokens=2048,
        tools=build_tools(mode),
    )
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response.")
            return text, extract_sources(response)
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


def generate_trend_intelligence(past_titles: list[str]) -> tuple[str, list[str]]:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = build_prompt(past_titles)
    # Degrade gracefully: official pages + Search -> official pages only -> no tools.
    modes = ["full", "urls", "none"] if USE_SEARCH else ["urls", "none"]
    last_error: Exception | None = None

    for model in MODELS:
        for mode in modes:
            try:
                log.info("Generating with %s (tools=%s)", model, mode)
                return call_model(client, model, prompt, mode)
            except (genai_errors.APIError, RuntimeError) as e:
                last_error = e
                log.warning("%s failed (tools=%s): %s", model, mode, str(e)[:200])

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
        content, sources = generate_trend_intelligence(history)
        message = "📈 **Automated Gaming Trend & Script Intelligence**\n\n" + content
        if sources:
            message += "\n\n🔗 **Sources**:\n" + "\n".join(f"<{u}>" for u in sources)
        else:
            message += "\n\n⚠️ _No sources returned — treat every claim as unverified._"

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
