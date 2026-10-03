#!/usr/bin/env python3
"""
Daily gaming trend & idea bot, with a verification layer.

Posts ONE idea per run to a Discord webhook. Before posting, the idea must pass
checks against official Roblox / Epic sources; otherwise the bot skips the day
(STRICT=1, default) instead of publishing guesses.

Modes (MODE):
    news       anchor the idea to the newest official release; the code fetches the
               latest Roblox and Fortnite versions itself and rejects ideas that cite
               older or invented versions
    evergreen  a timeless design mechanic, grounded in official documentation
    auto       (default) news if the version baseline can be fetched, else evergreen

Lanes (LANE):
    creator    (default) 30-second Shorts/TikTok video concept
    developer  a buildable mechanic/feature idea for Roblox or UEFN developers

Env vars:
    GEMINI_API_KEY        required
    WEBHOOK               Discord webhook URL (optional; prints to stdout if unset)
    NEON_DATABASE_URL     Postgres URL for idea history (optional)
    MODE, LANE            see above
    STRICT                "1" (default): skip the day if verification fails
                          "0": post anyway with a visible warning
    GEMINI_MODEL          comma-separated model fallback list
    USE_SEARCH            "1" (default) enables Google Search grounding
    EVERGREEN_SOURCES     optional comma-separated official URLs for evergreen mode
    REQUIRE_CHANGELIST    "1": a Roblox/Fortnite idea is rejected unless the code could fetch
                          that release's change list to check it against (default "0": the
                          claim check is skipped and the footer says "unavailable")
Flags:
    --dry-run             run everything but do not post to Discord or save history
"""

from __future__ import annotations

import datetime as dt
import html as _html
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trend_bot")


def _env(name: str, default: str = "") -> str:
    """Env var with empty-string treated as unset (GitHub passes '' for missing vars)."""
    return (os.environ.get(name) or default).strip()


DB_URL = _env("NEON_DATABASE_URL")
WEBHOOK = _env("WEBHOOK")
GEMINI_API_KEY = _env("GEMINI_API_KEY")
MODELS = [
    m.strip()
    for m in _env("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite").split(",")
    if m.strip()
]
USE_SEARCH = _env("USE_SEARCH", "1") == "1"
MODE = _env("MODE", "auto").lower()
LANE = _env("LANE", "creator").lower()
STRICT = _env("STRICT", "1") == "1"
REQUIRE_CHANGELIST = _env("REQUIRE_CHANGELIST", "0") == "1"
DRY_RUN = "--dry-run" in sys.argv or _env("DRY_RUN") == "1"
EVERGREEN_SOURCES = [u.strip() for u in _env("EVERGREEN_SOURCES").split(",") if u.strip().startswith("http")]

DISCORD_LIMIT = 1900
HISTORY_SIZE = 15
RETRYABLE_CODES = {429, 500, 503, 504}
# (hostname suffix, platform). Matched against the parsed hostname, never a substring.
OFFICIAL_HOSTS = (("roblox.com", "roblox"), ("epicgames.com", "fortnite"), ("fortnite.com", "fortnite"))
OFFICIAL_HINTS = tuple(h for h, _ in OFFICIAL_HOSTS)
# Search grounding returns redirect URLs whose *title* is the bare source domain.
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)
CLAIM_MIN_OVERLAP = 0.6   # share of significant words the SOURCE CHANGE line must share with a real change

ROBLOX_RELEASES_URL = "https://create.roblox.com/docs/en-us/releases"
ROBLOX_RELEASE_PAGE = "https://create.roblox.com/docs/en-us/release-notes/release-notes-{n}"
ROBLOX_VERSION_ENDPOINTS = (
    "https://clientsettingscdn.roblox.com/v2/client-version/WindowsStudio64",
    "https://clientsettings.roblox.com/v2/client-version/WindowsStudio64",
)
EPIC_INDEX_URL = "https://dev.epicgames.com/documentation/fortnite/whats-new-in-unreal-editor-for-fortnite"

ROBLOX_STALE_WINDOW = 1   # prompt allows the latest release or the one just before it
ROBLOX_AHEAD_WINDOW = 3   # allow slightly-ahead pending versions

CREATOR_FOCUS = (
    "Focus platforms: YouTube Shorts and TikTok. "
    "Niches: Roblox (simulator, tycoon, and co-op horror trends) and Fortnite "
    "(UEFN map mechanics, custom game modes like Reload, and hidden meta tricks)."
)
EVERGREEN_TOPICS = [
    "audio and voice design", "camera and movement feel", "UI and HUD feedback",
    "progression and unlock systems", "lobby and matchmaking flow", "enemy / NPC behavior",
    "physics-based puzzles", "round-based game loops", "co-op player roles",
    "environmental storytelling", "tutorials and onboarding", "risk/reward and tension design",
]


class VerificationError(RuntimeError):
    """Idea could not be verified against official sources."""


# --------------------------------------------------------------------------- #
# Version baseline (fetched by code, not by the model)
# --------------------------------------------------------------------------- #
@dataclass
class Baseline:
    roblox_release: int | None = None
    fortnite_version: tuple[int, int] | None = None
    fortnite_url: str | None = None
    roblox_changes: list[str] = field(default_factory=list)   # fetched by code from the release notes
    fortnite_changes: list[str] = field(default_factory=list)  # same, from Epic's release notes page

    @property
    def ok(self) -> bool:
        return self.roblox_release is not None or self.fortnite_version is not None

    def fortnite_str(self) -> str | None:
        return f"{self.fortnite_version[0]}.{self.fortnite_version[1]:02d}" if self.fortnite_version else None

    def describe(self) -> str:
        parts = []
        if self.roblox_release:
            parts.append(f"Roblox {self.roblox_release}")
        if self.fortnite_version:
            parts.append(f"Fortnite {self.fortnite_str()}")
        return " / ".join(parts) or "no baseline"


def _http_get(url: str) -> requests.Response:
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (trend-bot)"})
    r.raise_for_status()
    return r


def parse_roblox_client_version(version: str | None) -> int | None:
    """Roblox client versions look like 0.<release>.<patch>.<build>."""
    m = re.match(r"\s*0\.(\d{3})\.\d+\.\d+", version or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 300 <= n <= 2000 else None


def fetch_roblox_release() -> int | None:
    for url in ROBLOX_VERSION_ENDPOINTS:
        try:
            rel = parse_roblox_client_version(_http_get(url).json().get("version"))
            if rel:
                return rel
        except Exception as e:
            log.warning("Roblox version endpoint failed (%s): %s", urlparse(url).netloc, e)
    try:  # fallback: numbered release-note links on the docs overview
        nums = [int(n) for n in re.findall(r"release-notes-(\d{3})", _http_get(ROBLOX_RELEASES_URL + ".md").text)]
        return max(nums) if nums else None
    except Exception as e:
        log.warning("Roblox docs fallback failed: %s", e)
        return None


def parse_release_changes(md: str) -> list[str]:
    """Pull individual change lines out of a release-notes page (markdown)."""

    def clean(t: str) -> str:
        t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
        return re.sub(r"[*_`]+", "", t).strip()

    bullets: list[str] = []
    plain: list[str] = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "|", "---", "![")):
            continue
        m = re.match(r"(?:[-*+]|\d+\.)\s+(.*)", line)
        if not m and re.match(r"^[\w.-]+:\s", line):   # front-matter style "key: value"
            continue
        text = clean(m.group(1) if m else line)
        if len(text) < 25 or re.match(r"(?i)(previous|next)\b", text):
            continue
        (bullets if m else plain).append(text)
    return (bullets or [p for p in plain if len(p) >= 40])[:60]


def fetch_roblox_changes(n: int) -> list[str]:
    url = ROBLOX_RELEASE_PAGE.format(n=n) + ".md"
    try:
        items = parse_release_changes(_http_get(url).text)
    except Exception as e:
        log.warning("Roblox change list unavailable for release %s: %s", n, e)
        return []
    log.info("Roblox release %s change list: %d items", n, len(items))
    return items


EPIC_HREF = re.compile(
    r"/documentation/(?:[a-z]{2}(?:-[a-z]{2})?/)?fortnite/"
    r"((\d{2})-(\d{2})-fortnite-ecosystem-updates-and-release-notes[\w-]*)"
)
EPIC_TEXT = re.compile(r"\b(\d{2})\.(\d{2}) Fortnite Ecosystem Updates and Release Notes")


def parse_epic_index(text: str) -> tuple[tuple[int, int] | None, str | None]:
    best: tuple[int, int] | None = None
    url: str | None = None
    for m in EPIC_HREF.finditer(text):
        v = (int(m.group(2)), int(m.group(3)))
        if best is None or v > best:
            best, url = v, f"https://dev.epicgames.com/documentation/fortnite/{m.group(1)}"
    if best is None:
        versions = [(int(a), int(b)) for a, b in EPIC_TEXT.findall(text)]
        best = max(versions) if versions else None
    return best, url


def fetch_fortnite_version() -> tuple[tuple[int, int] | None, str | None]:
    try:
        return parse_epic_index(_http_get(EPIC_INDEX_URL).text)
    except Exception as e:
        log.warning("Epic release index failed: %s", e)
        return None, None


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|nav|footer|svg)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", h)
    h = re.sub(r"(?i)<li[^>]*>", "\n- ", h)
    h = re.sub(r"(?i)</(p|h[1-6]|li|div|tr|ul|ol)>|<br\s*/?>", "\n", h)
    return _html.unescape(re.sub(r"<[^>]+>", " ", h))


EPIC_START = re.compile(r"(?im)^#{1,3}\s*patch notes\s*$")
EPIC_END = ("on this page", "ask questions and help your peers", "write your own tutorials")


def parse_epic_changes(text: str) -> list[str]:
    """Sentences + headings from the body of an Epic release-notes page.

    Works on markdown or raw HTML. Only the text from the 'Patch Notes' heading to the end of the
    article is used, so front matter, the table of contents, images and link URLs never count as
    'changes'. Returns [] if that heading is not found (better 'unavailable' than junk)."""
    if re.search(r"(?i)<(html|body|h[1-6]|p)\b", text):
        text = _html_to_text(text)
    m = EPIC_START.search(text)
    if not m:
        return []
    items: list[str] = []
    for raw in text[m.start():].splitlines():
        line = raw.strip()
        low = line.lower()
        if low in EPIC_END[:1] or low.startswith(EPIC_END[1:]):
            break
        if not line or line.startswith("---"):
            continue
        heading = re.match(r"#{1,6}\s+(.*)", line)
        line = heading.group(1) if heading else re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line)
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", line)            # images
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)            # links -> their text
        line = re.sub(r"https?://\S+", " ", line)                         # bare URLs
        line = re.sub(r"[*`]+", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if heading:
            if len(line) >= 15:
                items.append(line)
            continue
        items += [x.strip() for x in re.split(r"(?<=[.!?])\s+", line) if len(x.strip()) >= 25]
    return items[:150]


def fetch_fortnite_changes(url: str | None) -> list[str]:
    if not url:
        return []
    try:
        items = parse_epic_changes(_http_get(url).text)
    except Exception as e:
        log.warning("Fortnite change list unavailable: %s", e)
        return []
    log.info("Fortnite change list: %d items", len(items))
    return items


def fetch_baseline() -> Baseline:
    fv, furl = fetch_fortnite_version()
    rel = fetch_roblox_release()
    b = Baseline(
        roblox_release=rel,
        fortnite_version=fv,
        fortnite_url=furl,
        roblox_changes=fetch_roblox_changes(rel) if rel else [],
        fortnite_changes=fetch_fortnite_changes(furl),
    )
    log.info("Version baseline: %s", b.describe())
    return b


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _host_platform(host: str) -> str | None:
    host = host.lower().strip(".")
    for suffix, platform in OFFICIAL_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return platform
    return None


@dataclass(frozen=True)
class Source:
    url: str
    title: str = ""

    def platform(self) -> str | None:
        """'roblox' / 'fortnite' for official hosts, else None. Hostname match only."""
        host = (urlparse(self.url).hostname or "").lower()
        if host in REDIRECT_HOSTS:
            host = self.title.strip().lower()
        return _host_platform(host)

    def is_official(self) -> bool:
        return self.platform() is not None

    def label(self) -> str:
        return (self.title or urlparse(self.url).netloc or self.url)[:60].replace("[", "(").replace("]", ")")


ROBLOX_VER = re.compile(r"(?i)\b(?:release|version|v)\.?\s*#?(\d{3})\b")
FORTNITE_VER = re.compile(r"(?i)\b(?:fortnite|uefn|update|release|patch|version|v)\s*(\d{2})\.(\d{2})\b")


def check_versions(text: str, baseline: Baseline) -> list[str]:
    """Reject ideas that cite stale or invented versions; require a current one."""
    problems: list[str] = []
    found_current = False

    if baseline.roblox_release:
        latest = baseline.roblox_release
        for m in ROBLOX_VER.finditer(text):
            n = int(m.group(1))
            if not 300 <= n <= 2000:
                continue
            if latest - n > ROBLOX_STALE_WINDOW:
                problems.append(f"cites Roblox release {n}, but the latest is {latest}")
            elif n - latest > ROBLOX_AHEAD_WINDOW:
                problems.append(f"cites Roblox release {n}, which is newer than the latest known ({latest})")
            else:
                found_current = True

    if baseline.fortnite_version:
        lmaj, lmin = baseline.fortnite_version
        for m in FORTNITE_VER.finditer(text):
            maj, mnr = int(m.group(1)), int(m.group(2))
            if maj < 20:
                continue
            if maj <= lmaj - 2:
                problems.append(f"cites Fortnite {maj}.{mnr:02d}, but the latest is {baseline.fortnite_str()}")
            elif (maj, mnr) > (lmaj, lmin):
                problems.append(f"cites Fortnite {maj}.{mnr:02d}, which is newer than the latest known")
            else:
                found_current = True

    if baseline.ok and not found_current:
        problems.append("does not cite a current release version")
    return problems


SOURCE_CHANGE_RE = re.compile(r"SOURCE CHANGE[\s*]*:[\s*]*(.+)")
FORTNITE_WORDS = re.compile(r"(?i)\bfortnite\b|\buefn\b|\bverse\b")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "now", "are", "was",
    "been", "have", "has", "its", "their", "which", "while", "also", "more", "than", "then",
    "them", "they", "will", "can", "now", "all", "any", "not",
}
_GENERIC = {
    "roblox", "release", "version", "fixed", "fixes", "fix", "adds", "added", "improved",
    "improvements", "introduced", "update", "updated", "studio",
}

STYLE_RULES = (
    (re.compile(r"(?i)stealth[- ]?dropp?ed|finally fixed|game[- ]?chang|\binsane\b|\bmassive\b"),
     "uses hype wording (stealth-dropped / finally fixed / massive)"),
    (re.compile(r"(?i)\bcreator update\b"),
     "calls a release a 'Creator Update'; use 'Roblox release N'"),
    (re.compile(r"(?i)\b(?:every|all|any)\b(?:\s+single)?[^.\n]{0,30}?"
                r"\b(?:simulators?|tycoons?|games?|maps?|experiences?|creators?|developers?)\b"),
     "overclaims scope (every/all/any ...); say which games the change applies to"),
)


def extract_source_change(text: str) -> str:
    m = SOURCE_CHANGE_RE.search(text)
    return m.group(1).strip() if m else ""


def detect_platform(line: str) -> str | None:
    """Which platform a SOURCE CHANGE line is about: 'roblox', 'fortnite' or None (unknown)."""
    if not line:
        return None
    roblox = bool(re.search(r"(?i)roblox", line) or ROBLOX_VER.search(line))
    fortnite = bool(FORTNITE_WORDS.search(line) or FORTNITE_VER.search(line))
    if fortnite and not roblox:
        return "fortnite"
    return "roblox" if roblox else None


def _tokens(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]                       # items/item, weapons/weapon
        if len(w) >= 4 and w not in _STOP and w not in _GENERIC:
            out.add(w)
    return out


def claim_overlap(claim: str, text: str) -> float:
    """Share (0..1) of the claim's significant words that appear in `text`; needs >=3 shared words."""
    a, b = _tokens(claim), _tokens(text)
    shared = len(a & b)
    if not a or shared < min(3, len(a)):
        return 0.0
    return shared / len(a)


def best_support(claim: str, items: list[str], window: int = 3) -> float:
    """Best claim coverage by any run of up to `window` consecutive change lines.

    Release notes explain one change over a heading plus a few sentences, and a good claim
    paraphrases across them, so single lines are too strict."""
    best = 0.0
    for i in range(len(items)):
        for w in range(1, window + 1):
            if i + w <= len(items):
                best = max(best, claim_overlap(claim, " ".join(items[i:i + w])))
    return best


def check_claim(text: str, baseline: Baseline) -> tuple[list[str], str]:
    """Compare the SOURCE CHANGE line with the real change list. Returns (problems, status)."""
    line = extract_source_change(text)
    platform = detect_platform(line)
    if platform == "fortnite":
        changes, known = baseline.fortnite_changes, baseline.fortnite_version is not None
        label, status_na = f"Fortnite {baseline.fortnite_str()}", "unavailable (Fortnite)"
    else:
        changes, known = baseline.roblox_changes, bool(baseline.roblox_release)
        label, status_na = f"Roblox release {baseline.roblox_release}", "unavailable"
    if not changes:
        if REQUIRE_CHANGELIST and known and platform in ("roblox", "fortnite"):
            return [f"could not fetch the {label} change list to verify the claim"], "failed"
        return [], status_na
    if not line:
        return ["no SOURCE CHANGE line to check against the release notes"], "failed"
    if best_support(line, changes) < CLAIM_MIN_OVERLAP:
        return [f"SOURCE CHANGE does not match any change in {label}"], "failed"
    return [], "matched"


def check_style(text: str) -> list[str]:
    return [msg for rx, msg in STYLE_RULES if rx.search(text)]


def verify(text: str, sources: list[Source], mode: str, baseline: Baseline) -> list[str]:
    problems: list[str] = []
    if not any(s.is_official() for s in sources):
        problems.append("no official Roblox/Epic source was actually retrieved")
    if mode == "news":
        platform = detect_platform(extract_source_change(text))
        if platform and any(s.is_official() for s in sources) and not any(s.platform() == platform for s in sources):
            problems.append(f"idea is about {platform} but no official {platform} source was retrieved")
        problems += check_versions(text, baseline)
        problems += check_claim(text, baseline)[0]
    problems += check_style(text)
    return problems


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
                "SELECT title FROM trend_ideas ORDER BY created_at DESC LIMIT %s", (HISTORY_SIZE,)
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.warning("Could not load history: %s", e)
        return []


def save_history(title: str) -> None:
    if not DB_URL or not title or DRY_RUN:
        return
    try:
        import psycopg

        with psycopg.connect(DB_URL, connect_timeout=10) as conn:
            conn.execute("INSERT INTO trend_ideas (title) VALUES (%s)", (title[:500],))
    except Exception as e:
        log.warning("Could not save history: %s", e)


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #
INTRO = {
    "creator": (
        f"{CREATOR_FOCUS}\n\n"
        "Act as an elite short-form gaming content strategist. Produce ONE original, specific "
        "30-second video concept for Roblox or Fortnite. Avoid generic advice; name concrete "
        "games, mechanics, update features, or map types."
    ),
    "developer": (
        "Act as a senior game-design consultant for Roblox (Studio/Luau) and Fortnite (UEFN/Verse) "
        "developers. Produce ONE original, specific mechanic or feature idea that a solo developer "
        "could realistically build, and that makes a game more fun, replayable or shareable."
    ),
}

STRUCTURE = {
    "creator": (
        "🎬 **TITLE / HOOK IDEA**: (catchy, high-CTR title + the first 3-second visual hook)\n"
        "💡 **THE ORIGINAL ANGLE**: (why this stands out from standard creator content)\n"
        "📝 **30-SECOND SCRIPT BREAKDOWN**:\n"
        "• [0-3s] Hook:\n• [3-20s] Core Value / Action:\n• [20-30s] CTA & Loop:\n"
        "⚡ **ESTIMATED EFFORT**: (Low / Medium / High production time, with one line why)\n"
    ),
    "developer": (
        "🛠️ **BUILD IDEA**: (name + one-line pitch of the mechanic or feature)\n"
        "🎯 **WHY PLAYERS CARE**: (retention / replay / share-ability angle)\n"
        "🧩 **IMPLEMENTATION OUTLINE**:\n• Step 1:\n• Step 2:\n• Step 3:\n"
        "(name only classes, services or devices documented on official pages)\n"
        "💰 **MONETIZATION NOTE**: (only rules stated on official pages you read; no revenue "
        "numbers; write 'None' if unsure)\n"
        "⚡ **ESTIMATED EFFORT**: (Low / Medium / High build time, with one line why)\n"
    ),
}

BASIS_LINE = {
    "news": "📌 **SOURCE CHANGE**: (exact version number + the specific change, from the page you read)\n",
    "evergreen": "📚 **DOCS**: (link to the official page that supports each class/device/mechanic named)\n",
}


def sources_to_read(mode: str, baseline: Baseline) -> list[str]:
    if mode == "evergreen":
        return list(EVERGREEN_SOURCES)
    urls: list[str] = []
    if baseline.fortnite_version:
        urls.append(baseline.fortnite_url or EPIC_INDEX_URL)
    if baseline.roblox_release:
        urls += [ROBLOX_RELEASE_PAGE.format(n=baseline.roblox_release), ROBLOX_RELEASES_URL]
    return urls


def build_prompt(mode: str, baseline: Baseline, past_titles: list[str], urls: list[str]) -> str:
    today = dt.date.today()
    parts = [f"Today is {today.strftime('%A, %B %d, %Y')}.", INTRO[LANE]]

    if mode == "news":
        facts = []
        if baseline.roblox_release:
            facts.append(f"- Latest Roblox release: {baseline.roblox_release}")
        if baseline.fortnite_version:
            facts.append(f"- Latest Fortnite ecosystem release: {baseline.fortnite_str()}")
        parts.append(
            "VERIFIED BASELINE (fetched by code today; treat as ground truth):\n"
            + "\n".join(facts)
            + "\nRead the official pages below, then build the idea around ONE specific, real change "
            "introduced in the latest release. Name the version number exactly as above (or the one "
            "just before it). Do not cite any other version number.\nPages to read:\n"
            + "\n".join(f"- {u}" for u in urls)
        )
        if baseline.roblox_changes:
            parts.append(
                f"OFFICIAL ROBLOX RELEASE {baseline.roblox_release} CHANGE LIST (fetched by code; these are "
                "the only Roblox changes you may claim):\n"
                + "\n".join(f"- {c[:300]}" for c in baseline.roblox_changes)
                + "\nIf you build on Roblox, your SOURCE CHANGE line must restate ONE of these items. "
                "Do not describe changes that are not listed."
            )
        if baseline.fortnite_changes:
            parts.append(
                f"OFFICIAL FORTNITE {baseline.fortnite_str()} RELEASE NOTES TEXT (fetched by code; these are "
                "the only Fortnite changes you may claim):\n"
                + "\n".join(f"- {c[:300]}" for c in baseline.fortnite_changes[:80])
                + "\nIf you build on Fortnite, your SOURCE CHANGE line must restate a change described above. "
                "Do not describe changes that are not listed."
            )
    else:
        topic = EVERGREEN_TOPICS[today.toordinal() % len(EVERGREEN_TOPICS)]
        seeds = ("\nStart from these official pages:\n" + "\n".join(f"- {u}" for u in urls)) if urls else ""
        parts.append(
            f"EVERGREEN MODE: do not tie the idea to a release, update or news. Pick a timeless design "
            f"mechanic related to: {topic}.\nUse Google Search and prefer official documentation "
            "(create.roblox.com/docs, dev.epicgames.com/documentation). Only name classes, services, "
            "devices or APIs that you found on an official docs page. Do not mention version numbers."
            + seeds
        )

    if past_titles:
        parts.append(
            "Do NOT repeat or closely resemble any of these previous ideas:\n"
            + "\n".join(f"- {t}" for t in past_titles)
        )

    parts.append(
        "Accuracy rules (important):\n"
        "- Roblox and Fortnite are different platforms: never apply one platform's tools, devices or "
        "monetization rules to the other.\n"
        "- Never invent map/island codes, setting names, device names, class names or input combos. "
        "State a mechanic as fact only if an official page you read supports it.\n"
        "- If something could not be confirmed, say so in the FACT-CHECK line.\n"
        "- Do not state revenue numbers, payout rates or sales figures.\n"
        "- Scope every claim exactly as the official change states it. Never write 'every', 'all' or "
        "'any' game/simulator/map; say 'games that use <the feature>' instead.\n"
        "- Call a release 'Roblox release N' or 'Fortnite N.NN', never 'Creator Update'. "
        "No hype wording (stealth-dropped, finally fixed, massive, insane)."
    )
    parts.append(
        "Use exactly this structure and nothing else (no intro, no outro):\n\n"
        + STRUCTURE[LANE]
        + BASIS_LINE["news" if mode == "news" else "evergreen"]
        + "🔍 **FACT-CHECK**: (what is confirmed by the official pages vs. what must be tested in-game "
        "or in-editor before publishing)\n"
    )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
def extract_sources(response) -> list[Source]:
    """Official pages actually read (URL context) first, then Search grounding links."""
    out: list[Source] = []
    seen: set[str] = set()

    def add(url: str | None, title: str = "") -> None:
        if url and url not in seen:
            seen.add(url)
            out.append(Source(url, title))

    try:
        for item in response.candidates[0].url_context_metadata.url_metadata or []:
            if "SUCCESS" in str(getattr(item, "url_retrieval_status", "")):
                add(item.retrieved_url)
    except (AttributeError, IndexError, TypeError):
        pass
    try:
        for chunk in response.candidates[0].grounding_metadata.grounding_chunks or []:
            web = getattr(chunk, "web", None)
            if web:
                add(web.uri, web.title or "")
    except (AttributeError, IndexError, TypeError):
        pass
    return out[:8]


def build_tools(tool_mode: str, urls: list[str]) -> list[types.Tool] | None:
    tools: list[types.Tool] = []
    if tool_mode in ("full", "urls") and urls:
        tools.append(types.Tool(url_context=types.UrlContext()))
    if tool_mode == "full":
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    return tools or None


def call_model(
    client: genai.Client, model: str, prompt: str, tool_mode: str, urls: list[str]
) -> tuple[str, list[Source]]:
    """One model, one tool config. Retries transient errors with backoff."""
    # temperature/top_p/top_k are deprecated on current Gemini models; not set.
    # The token cap is generous because thinking tokens count against it.
    config = types.GenerateContentConfig(max_output_tokens=8192, tools=build_tools(tool_mode, urls))
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response.")
            finish = ""
            try:
                finish = str(response.candidates[0].finish_reason)
            except (AttributeError, IndexError, TypeError):
                pass
            log.info("%s finished: %s (%d chars)", model, finish or "unknown", len(text))
            if "MAX_TOKENS" in finish or "FACT-CHECK" not in text:
                raise RuntimeError(f"Incomplete response (finish={finish or 'unknown'}).")
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


@dataclass
class Result:
    text: str
    sources: list[Source]
    meta: str
    warnings: list[str] = field(default_factory=list)


def generate(past_titles: list[str], mode: str, baseline: Baseline) -> Result:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    urls = sources_to_read(mode, baseline)
    prompt = build_prompt(mode, baseline, past_titles, urls)
    # full: official pages + Search -> urls: official pages only -> none (non-strict only)
    tool_modes = ["full", "urls"] if USE_SEARCH else ["urls"]
    if not STRICT:
        tool_modes.append("none")

    last_error: Exception | None = None
    last_problems: list[str] = []
    fallback: Result | None = None
    attempt = 0
    total = len(MODELS) * len(tool_modes)

    for model in MODELS:
        for tool_mode in tool_modes:
            attempt += 1
            try:
                log.info("Generating with %s (mode=%s, tools=%s)", model, mode, tool_mode)
                text, sources = call_model(client, model, prompt, tool_mode, urls)
            except (genai_errors.APIError, RuntimeError) as e:
                last_error = e
                log.warning("%s failed (tools=%s): %s", model, tool_mode, str(e)[:200])
                continue

            problems = verify(text, sources, mode, baseline)
            claim = check_claim(text, baseline)[1] if mode == "news" else "n/a"
            meta = (
                f"{model} · mode: {mode} · lane: {LANE} · tools: {tool_mode} · "
                f"baseline: {baseline.describe()} · claim-check: {claim} · try {attempt}/{total}"
            )
            if not problems:
                return Result(text, sources, meta)
            last_problems = problems
            log.warning("Verification failed (%s, tools=%s): %s", model, tool_mode, "; ".join(problems))
            fallback = Result(text, sources, meta, warnings=problems)

    if last_problems:
        if STRICT or fallback is None:
            raise VerificationError("; ".join(last_problems))
        return fallback
    raise RuntimeError(f"All models failed ({', '.join(MODELS)}). Last error: {last_error}")


def extract_title(content: str) -> str:
    m = re.search(r"(?:TITLE\s*/\s*HOOK IDEA|BUILD IDEA)\*{0,2}\s*:?\s*\*{0,2}\s*(.+)", content)
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
        while len(line) > limit:
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
    if not WEBHOOK or DRY_RUN:
        return
    for chunk in split_message(text):
        for _ in range(4):
            r = requests.post(
                WEBHOOK,
                json={"content": chunk, "allowed_mentions": {"parse": []}},
                timeout=15,
            )
            if r.status_code == 429:
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
        if secret and len(secret) >= 8:   # ignore dummy/short values so "x" can't corrupt "Roblox"
            msg = msg.replace(secret, "[redacted]")
    return msg[:1200]


def format_message(result: Result) -> str:
    msg = "📈 **Automated Gaming Trend & Script Intelligence**\n\n" + result.text
    platform = detect_platform(extract_source_change(result.text))
    srcs = [s for s in result.sources if not (platform and s.is_official() and s.platform() != platform)]
    official = [s for s in srcs if s.is_official()]
    shown = official + [s for s in srcs if not s.is_official()]
    if shown:
        msg += "\n\n🔗 **Sources**:\n" + "\n".join(f"• [{s.label()}](<{s.url}>)" for s in shown[:6])
    else:
        msg += "\n\n⚠️ _No sources returned — treat every claim as unverified._"
    if result.warnings:
        msg += "\n⚠️ _Verification warnings: " + "; ".join(result.warnings)[:400] + "_"
    return msg + f"\n-# {result.meta}"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def resolve_mode(baseline: Baseline) -> str:
    if MODE == "evergreen":
        return "evergreen"
    if baseline.ok:
        return "news"
    if MODE == "news":
        raise VerificationError("could not fetch the latest Roblox/Fortnite release versions")
    log.warning("No version baseline available; falling back to evergreen mode.")
    return "evergreen"


def main() -> int:
    try:
        if MODE not in ("auto", "news", "evergreen"):
            raise ValueError(f"MODE must be auto, news or evergreen (got '{MODE}').")
        if LANE not in INTRO:
            raise ValueError(f"LANE must be creator or developer (got '{LANE}').")

        baseline = fetch_baseline() if MODE in ("auto", "news") else Baseline()
        mode = resolve_mode(baseline)
        result = generate(load_history(), mode, baseline)
        message = format_message(result)

        print(message)
        post_discord(message)
        save_history(extract_title(result.text))
        return 0

    except VerificationError as e:
        notice = (
            "⏭️ **Trend Bot skipped today**: the idea could not be verified against official "
            f"sources, so nothing was posted.\nReason: `{sanitize(str(e))}`"
        )
        log.warning(notice)
        try:
            post_discord(notice)
        except Exception as alert_error:
            log.error("Could not send skip notice: %s", sanitize(str(alert_error)))
        return 0  # a skipped day is a safe outcome, not a CI failure

    except Exception as e:
        err = f"⚠️ **Trend Bot Error**: {type(e).__name__}: `{sanitize(str(e))}`"
        log.error(err)
        try:
            post_discord(err)
        except Exception as alert_error:
            log.error("Could not send error alert: %s", sanitize(str(alert_error)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
