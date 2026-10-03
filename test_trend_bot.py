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
assert t.check_versions("Roblox version 737 changed Z", base) == []          # latest or one before
assert any("736" in p for p in t.check_versions("Roblox version 736 changed Z", base))  # 2 back = stale
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

# 10. Hostname-based official check (no substring tricks)
S = t.Source
assert S("https://create.roblox.com/docs/en-us/release-notes/release-notes-741").is_official()
assert S("https://dev.epicgames.com/documentation/fortnite/42-30-x").platform() == "fortnite"
assert not S("https://roblox.com.evil.net/x").is_official()
assert not S("https://notroblox.com/x").is_official()
assert not S("https://example.com/post", "roblox.com").is_official()           # title only counts on redirect hosts
assert S("https://vertexaisearch.cloud.google.com/x", "create.roblox.com").platform() == "roblox"
assert not S("https://vertexaisearch.cloud.google.com/x", "notroblox.com").is_official()

# 11. Release-note parsing
MD = """---
title: Introduced in version 741
---
1. [Learn](https://create.roblox.com/docs)
## Fixes
- Fixed body-dependent sinking in Single Collider swimming by using the collider for buoyancy and water detection and removing the root density increase.
- Added a **new** [Audio Distortion](https://x.y) effect to the Audio API for creators.
- short
[Previous](https://create.roblox.com/docs/release-notes/release-notes-740)
"""
items = t.parse_release_changes(MD)
assert len(items) == 2 and items[1].startswith("Added a new Audio Distortion effect"), items
assert t.parse_release_changes("") == []
assert t.parse_release_changes("## Fixes\nA plain sentence that is clearly long enough to count as a change line.") != []

# 12. Claim check against the real change list
CH = items
base2 = t.Baseline(roblox_release=741, fortnite_version=(42, 30), roblox_changes=CH)
REAL = ("📌 **SOURCE CHANGE**: (Roblox Release version 741: Fixed body-dependent sinking in Single Collider "
        "swimming by using the collider for buoyancy and water detection and removing the root density increase.)")
FAKE = "📌 **SOURCE CHANGE**: (Roblox release 741: Added realistic wave physics and shark AI to terrain water.)"
assert t.extract_source_change(REAL).startswith("(Roblox Release version 741")
assert t.check_claim(REAL, base2) == ([], "matched")
probs, status = t.check_claim(FAKE, base2)
assert status == "failed" and any("does not match" in p for p in probs), (probs, status)
assert t.check_claim("no such line, release 741", base2)[1] == "failed"
assert t.check_claim("📌 **SOURCE CHANGE**: Fortnite 42.30 added a new device", base2) == ([], "unavailable (Fortnite)")
assert t.check_claim(REAL, base) == ([], "unavailable")                          # no change list fetched
t.REQUIRE_CHANGELIST = True
assert t.check_claim(REAL, base)[1] == "failed"                                  # strict: refuse to trust the model
probs, status = t.check_claim("📌 **SOURCE CHANGE**: Fortnite 42.20 added X", base)
assert status == "failed" and "Fortnite 42.20" in probs[0], (probs, status)   # strict now covers Fortnite
t.REQUIRE_CHANGELIST = False
roblox_src = [t.Source("https://create.roblox.com/docs/release-notes/release-notes-741")]
epic_src = [t.Source("https://dev.epicgames.com/documentation/fortnite/42-30-fortnite-ecosystem-updates-and-release-notes")]
assert t.verify("Release 741 " + REAL, roblox_src, "news", base2) == []
assert any("does not match" in p for p in t.verify("Release 741 " + FAKE, roblox_src, "news", base2))

# 13. A Roblox idea backed only by a Fortnite page is rejected (the cross-platform gap)
probs = t.verify(REAL, epic_src, "news", base2)
assert any("no official roblox source" in p for p in probs), probs
assert t.verify(REAL, roblox_src + epic_src, "news", base2) == []

# 14. Style rules: the wording from the real post
bad_post = ("The original angle: it affects every single water-based Roblox simulator. "
            "Update 741 just stealth-dropped a massive fix. Creator Update 741 is here.")
sp = t.check_style(bad_post)
assert len(sp) == 3, sp
assert t.check_style("Applies to games that use Single Collider swimming. Roblox release 741.") == []
assert any("hype" in p for p in t.verify("Roblox release 741 stealth-dropped", roblox_src, "news", base))

# 15. Prompt carries the change list and the scope rules; evergreen does not
news = t.build_prompt("news", base2, [], t.sources_to_read("news", base2))
assert "CHANGE LIST" in news and "Single Collider swimming" in news and "never 'Creator Update'" in news
assert "Never write 'every'" in news
assert "CHANGE LIST" not in t.build_prompt("news", base, [], t.sources_to_read("news", base))
assert "CHANGE LIST" not in t.build_prompt("evergreen", base2, [], [])

# 16. Footer drops sources from the other platform
res = t.Result("🎬 **TITLE / HOOK IDEA**: T\n" + REAL + "\n🔍 **FACT-CHECK**: ok", roblox_src + epic_src, "meta")
msg = t.format_message(res)
assert "release-notes-741" in msg and "epicgames" not in msg, msg

# 17. sanitize() must not mangle text when a secret is a short dummy value
assert t.sanitize("Roblox index SyntaxError") == "Roblox index SyntaxError"
t.GEMINI_API_KEY = "abcdefgh12345678"
assert t.sanitize("key=abcdefgh12345678 ok") == "key=[redacted] ok"
t.GEMINI_API_KEY = "x"

# 18. Fortnite release-note parsing (fixture = real structure of the 42.30 page)
EPIC_MD = """---
base: /documentation/assets/
meta-description: Find out what's new with the 42.30 release of Fortnite on October 1, 2026 in Unreal Editor for Fortnite!
title: 42.30 Fortnite Ecosystem Updates and Release Notes | Fortnite Documentation
---

Table of Contents

1. ![Epic Games](https://edc-cdn.net/assets/images/logo-epic.svg)[Developer](https://dev.epicgames.com/)
2. 42.30 Fortnite Ecosystem Updates and Release Notes

# 42.30 Fortnite Ecosystem Updates and Release Notes

Find out what's new with the 42.30 release of Fortnite on October 1, 2026!

![42.30 Fortnite Ecosystem Updates and Release Notes](https://dev.epicgames.com/community/api/documentation/image/1?resizing_type=fill)

 On this page

## Patch Notes

v42.30 brings a new conversations template for building LLM-powered characters, carryable items that aren't weapons, in-editor Content Pre-Checks, and analytics benchmarks for your island's performance.

### New Conversations Template

The *conversations* template is a playable introduction to the [conversations feature](https://www.fortnite.com/news/bring-npcs-to-life-with-ai-powered-conversations), which you can use to create LLM-powered characters with distinct personalities and voices.

[https://dev.epicgames.com/community/api/cms/videos/V_uUM2Ls/embed.html](https://dev.epicgames.com/community/api/cms/videos/V_uUM2Ls/embed.html)

### Custom Weapons: Held Items

You can now build carryable items that aren’t weapons. The new held_item_template is a ready-to-customize entity prefab (it ships with a torch as the default mesh and icon) that you can reskin into lanterns, tools, banners, or other prop players can equip.

![](https://dev.epicgames.com/community/api/documentation/image/172d80a5?resizing_type=fit)

### Memory Thermometer: Upcoming Changes

The publish memory requirement is moving into the profiling tools. You'll still be able to run the memory calculation manually.

## Release Notes

### Verse Updates and Fixes

Bug Fixes:

- Fixed an issue where equipping an item via Verse caused players to lose the ability to aim or shoot.

- [verse](https://dev.epicgames.com/community/search?query=verse)

---

Ask questions and help your peers [Developer Forums](https://forums.unrealengine.com/categories?tag=fortnite)

On this page

- [Patch Notes](https://dev.epicgames.com/documentation/fortnite/42-30#patchnotes)
- [Custom Weapons: Held Items](https://dev.epicgames.com/documentation/fortnite/42-30#customweapons)
"""
fi = t.parse_epic_changes(EPIC_MD)
joined = " ".join(fi)
assert any("held_item_template is a ready-to-customize entity prefab" in x for x in fi), fi
assert "Custom Weapons: Held Items" in fi                                  # headings are kept
assert not any("http" in x or "meta-description" in x or "Table of Contents" in x for x in fi), fi
assert "creating" not in joined and "Developer Forums" not in joined      # nothing past the article body
assert "patchnotes" not in joined.lower()                                  # bottom table of contents excluded
assert t.parse_epic_changes("just some text without the notes heading") == []
assert t.parse_epic_changes("") == []

# same content as raw HTML (what a plain GET may return)
EPIC_HTML = ("<html><body><nav>menu menu menu</nav><h1>42.30 Fortnite Ecosystem Updates</h1><h2>Patch Notes</h2>"
             "<h3>Custom Weapons: Held Items</h3><p>You can now build carryable items that aren&#8217;t weapons. "
             "The new held_item_template is a ready-to-customize entity prefab (it ships with a torch as the "
             "default mesh and icon) that you can reskin into lanterns, tools, banners, or other prop players "
             "can equip.</p><h2>On this page</h2><ul><li>Patch Notes</li></ul></body></html>")
hi = t.parse_epic_changes(EPIC_HTML)
assert any("held_item_template" in x for x in hi) and not any("menu" in x for x in hi), hi

# 19. Fortnite claim check against that text
FN = t.Baseline(roblox_release=741, fortnite_version=(42, 30), fortnite_changes=fi)
G1 = ("📌 **SOURCE CHANGE**: Fortnite 42.30 introduced the `held_item_template` entity prefab, which provides a "
      "customizable default torch mesh and icon for creating carryable items that are not weapons.")
G2 = ("📌 **SOURCE CHANGE**: Fortnite Ecosystem Release 42.30 — Custom Weapons: Held Items via `held_item_template`, "
      "an entity prefab with a default torch mesh and icon that creators can reskin into non-weapon carryables.")
BAD = "📌 **SOURCE CHANGE**: Fortnite 42.30 added a flying vehicle with jetpack boosters and shark NPC spawners."
assert t.check_claim(G1, FN) == ([], "matched"), t.check_claim(G1, FN)
assert t.check_claim(G2, FN) == ([], "matched"), t.check_claim(G2, FN)
probs, status = t.check_claim(BAD, FN)
assert status == "failed" and "does not match any change in Fortnite 42.30" in probs[0], (probs, status)
assert t.check_claim(G1, t.Baseline(roblox_release=741, fortnite_version=(42, 30))) == ([], "unavailable (Fortnite)")
epic_src = [t.Source("https://dev.epicgames.com/documentation/fortnite/42-30-fortnite-ecosystem-updates-and-release-notes")]
assert t.verify("Fortnite 42.30. " + G1, epic_src, "news", FN) == []
assert any("does not match" in p for p in t.verify("Fortnite 42.30. " + BAD, epic_src, "news", FN))

# 20. Prompt carries the Fortnite text; Roblox-only baselines do not
fp = t.build_prompt("news", FN, [], t.sources_to_read("news", FN))
assert "OFFICIAL FORTNITE 42.30 RELEASE NOTES TEXT" in fp and "held_item_template" in fp
assert "OFFICIAL FORTNITE" not in t.build_prompt("news", base2, [], t.sources_to_read("news", base2))

# 21. fetch_baseline wires it all together (network stubbed)
class _R:  # minimal response stub
    text = EPIC_MD
_saved = (t.fetch_fortnite_version, t.fetch_roblox_release, t.fetch_roblox_changes, t._http_get)
t.fetch_fortnite_version = lambda: ((42, 30), "https://dev.epicgames.com/documentation/fortnite/42-30-x")
t.fetch_roblox_release = lambda: 741
t.fetch_roblox_changes = lambda n: ["a roblox change that is long enough"]
t._http_get = lambda url, **k: _R()
fb = t.fetch_baseline()
assert fb.fortnite_changes and fb.roblox_changes and fb.fortnite_version == (42, 30)
def _boom(url, **k): raise RuntimeError("blocked")
t._http_get = _boom
assert t.fetch_fortnite_changes("https://x.y/z") == [] and t.fetch_fortnite_changes(None) == []   # fails soft
t.fetch_fortnite_version, t.fetch_roblox_release, t.fetch_roblox_changes, t._http_get = _saved

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
