"""Offline checks for trend_bot.py (no network, no API keys). Run: python test_trend_bot.py"""
import os
os.environ.update({"WEBHOOK": "", "GEMINI_API_KEY": "x", "NEON_DATABASE_URL": ""})
import trend_bot as t

EPIC_SAMPLE = """
- [42.20 Fortnite Ecosystem Updates and Release Notes 42.20 Fortnite Ecosystem Updates and Release Notes Find out what's new ...](https://dev.epicgames.com/documentation/fortnite/42-20-fortnite-ecosystem-updates-and-release-notes)
- [42.10 ...](https://dev.epicgames.com/documentation/fortnite/42-10-fortnite-ecosystem-updates-and-release-notes)
- [41.20 ...](https://dev.epicgames.com/documentation/fortnite/41-20-fortnite-ecosystem-updates-and-release-notes-in-fortnite)
- [39.50 ...](https://dev.epicgames.com/documentation/fortnite/39-50-fortnite-ecosystem-updates-and-release-notes)
"""
# 1. Epic parsing (links, relative links, and text-only fallback)
v, url = t.parse_epic_index(EPIC_SAMPLE)
assert v == (42, 20) and url.endswith("42-20-fortnite-ecosystem-updates-and-release-notes"), (v, url)
v, _ = t.parse_epic_index('<a href="/documentation/en-us/fortnite/42-10-fortnite-ecosystem-updates-and-release-notes">')
assert v == (42, 10)
v, url = t.parse_epic_index("42.20 Fortnite Ecosystem Updates and Release Notes\n41.30 Fortnite Ecosystem Updates and Release Notes")
assert v == (42, 20) and url is None
assert t.parse_epic_index("nothing here") == (None, None)

# 2. Roblox client version parsing
assert t.parse_roblox_client_version("0.738.0.7380123") == 738
assert t.parse_roblox_client_version("garbage") is None
assert t.parse_roblox_client_version(None) is None

base = t.Baseline(roblox_release=738, fortnite_version=(42, 20))

# 3. The exact failure we saw: stale "Release 646" must be rejected
bad = "Roblox officially rolled out Release 646, expanding the Audio API with AudioDistortion."
probs = t.check_versions(bad, base)
assert any("646" in p for p in probs), probs

# 4. Current versions pass
good = "Roblox release 738 adds X. Fortnite 42.20 adds Y."
assert t.check_versions(good, base) == []
assert t.check_versions("Roblox version 736 changed Z", base) == []          # within window
assert any("newer" in p for p in t.check_versions("Roblox release 790", base))
assert any("42.20" in p or "latest" in p for p in t.check_versions("Fortnite 39.10 added Q", base))
assert any("current release" in p for p in t.check_versions("No versions mentioned at all.", base))
# map-code style numbers and timestamps must not trigger false positives
assert t.check_versions("Map Code 4092-1823-9910 [0-3s] Hook; release 738", base) == []

# 5. verify(): needs an official source
srcs_bad = [t.Source("https://example.com/post", "example.com")]
srcs_ok = [t.Source("https://vertexaisearch.cloud.google.com/x", "roblox.com")]
assert any("official" in p for p in t.verify(good, srcs_bad, "news", base))
assert t.verify(good, srcs_ok, "news", base) == []
assert t.verify("evergreen idea, no versions", srcs_ok, "evergreen", base) == []
assert any("official" in p for p in t.verify("x", [], "evergreen", base))

# 6. Prompt content per mode / lane
for lane in ("creator", "developer"):
    t.LANE = lane
    news = t.build_prompt("news", base, ["old idea"], t.sources_to_read("news", base))
    assert "Latest Roblox release: 738" in news and "42.20" in news and "old idea" in news
    assert "FACT-CHECK" in news and "SOURCE CHANGE" in news
    ever = t.build_prompt("evergreen", base, [], [])
    assert "EVERGREEN MODE" in ever and "DOCS" in ever and "Latest Roblox release" not in ever
t.LANE = "developer"
assert "BUILD IDEA" in t.build_prompt("evergreen", base, [], []) and "MONETIZATION NOTE" in t.build_prompt("evergreen", base, [], [])
t.LANE = "creator"

# 7. Mode resolution
t.MODE = "auto"
assert t.resolve_mode(base) == "news"
assert t.resolve_mode(t.Baseline()) == "evergreen"
t.MODE = "news"
try:
    t.resolve_mode(t.Baseline()); raise SystemExit("expected VerificationError")
except t.VerificationError:
    pass
t.MODE = "evergreen"
assert t.resolve_mode(base) == "evergreen"
t.MODE = "auto"

# 8. Misc helpers
assert t.extract_title("🎬 **TITLE / HOOK IDEA**: \n\"Cool title\"\nmore") == '"Cool title"'
assert t.extract_title("🛠️ **BUILD IDEA**: Voice relay\nmore") == "Voice relay"
chunks = t.split_message("\n".join("line %d %s" % (i, "x" * 80) for i in range(60)))
assert all(len(c) <= t.DISCORD_LIMIT for c in chunks)

# 9. End-to-end flow with stubs
posted = []
t.post_discord = lambda m: posted.append(m)
t.save_history = lambda title: None
t.load_history = lambda: []
t.fetch_baseline = lambda: base

ok = t.Result("🎬 **TITLE / HOOK IDEA**: T\n🔍 **FACT-CHECK**: ok", srcs_ok, "meta")
t.generate = lambda *a, **k: ok
assert t.main() == 0 and "Sources" in posted[-1] and "-# meta" in posted[-1]

def reject(*a, **k): raise t.VerificationError("cites Roblox release 646, but the latest is 738")
t.generate = reject
assert t.main() == 0 and "skipped today" in posted[-1] and "646" in posted[-1]

def boom(*a, **k): raise RuntimeError("API down")
t.generate = boom
assert t.main() == 1 and "Trend Bot Error" in posted[-1]
print("ALL TESTS PASSED")
