"""
relic_data.py
Builds the set of currently-droppable ("unvaulted") standard-tier relics and
two lookup structures used by the rest of the app:

  parts_index  — one entry per unique Prime part, with source relics
  relics_index — one entry per unique relic, with all 4 refinement states

Everything comes from the DE drop table (drops.warframestat.us), which mirrors
DE's official drop tables and reflects the live game:

  - which relics are active: a relic is active iff it appears as a reward
    somewhere in the drop table
  - where each relic drops (farm locations and chances)
  - what each relic contains at each refinement (6 rewards per state)

The WFM item manifest (MarketClient) turns reward names into WFM slugs. A relic's
own slug follows the pattern "<tier>_<code>_relic".

WFCD's Relics.json is NOT used any more. Its `vaulted` flag lagged the drop table
by days after a vault rotation (Update 44), and in Oct 2026 the file was emptied
upstream (no rewards, locations, slugs or flags).

The parsed drop table is cached in data/cache/relic_drops.json and expires (see
DROPS_TTL_HOURS), so a stale snapshot never lingers silently.

Call load(client) for both structures (cache-first). Call refresh(client) to force
a re-check. With no data at all they return ({}, {}) and write nothing.
"""

import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DROPS_URL = "https://drops.warframestat.us/data/all.slim.json"
DROPS_INFO_URL = "https://drops.warframestat.us/data/info.json"
CACHE_DIR = Path("data/cache")
DROPS_CACHE_FILE = CACHE_DIR / "relic_drops.json"

# A relic_drops.json with any other "schema" value is treated as absent. Bumping
# this forces one re-download everywhere, including on the Railway volume, which
# shadows the committed snapshot. 1 = locations only (WFCD supplied contents).
CACHE_SCHEMA = 2

DROPS_TTL_HOURS = 12   # how long before load() re-checks the drop table's version stamp

# Sanity floor: the drop table has ~35 active standard relics. A result far below
# that means a bad/partial download, not a real vault — never replace good data with it.
MIN_ACTIVE_RELICS = 15

# Re-download the WFM manifest for unresolved reward names at most this often.
MANIFEST_REFRESH_AFTER_HOURS = 1

# Rewards that have no WFM listing; they stay in the relic (their drop chance
# counts toward EV at price 0) but are not parts and are not "unresolved".
NON_TRADEABLE = {"Forma Blueprint", "2X Forma Blueprint"}

STANDARD_TIERS = {"Lith", "Meso", "Neo", "Axi"}
REFINEMENT_STATES = ("Intact", "Exceptional", "Flawless", "Radiant")

# Shape every relic-state must have in the drop table to be shown.
REWARDS_PER_STATE = 6
CHANCE_TOTAL_TOLERANCE = 1.0   # chances must sum to 100 +/- this

# Nodes removed from the Star Chart over the years.
# Matched against the start of each location string, e.g.
# "Hapke, Ceres" matches "Hapke, Ceres (Spy), Rotation B".
# Sources: Specters of the Rail 0.0 (2016-07-08), Update 29.10 (2021-03-19)
REMOVED_NODES = {
    # Mercury
    "Caduceus", "Neruda",
    # Venus
    "Vesper",
    # Mars
    "Arcadia", "Quirinus",
    # Saturn
    "Mimas", "Iapetus", "Phoebe", "Pallene", "Aegaeon",
    # Uranus
    "Miranda", "Portia", "Cupid", "Bianca", "Prospero",
    "Mab", "Setebos", "Trinculo",
    # Neptune
    "Thalassa", "Halimede",
    # Pluto
    "Charon", "Corb",
    # Ceres
    "Olla", "Varro", "Hapke", "Egeria",
    # Eris
    "Cyath", "Cosis", "Candiru", "Lepis", "Hymeno", "Gnathos",
    "Sporid", "Ixodes", "Phalan", "Ranova", "Psoro", "Sparga", "Viver",
    # Sedna
    "Camenae", "Undine", "Jengu", "Tikoloshe", "Phithale",
    "Scylla", "Yemaja", "Veles",
    # Europa
    "Gamygyn", "Eligor", "Lillith", "Shax", "Zagan", "Beleth", "Limtoc",
    # Phobos
    "Grildrig", "Drunlo", "Wendell", "Todd", "Flimnap", "Opik",
    # Earth Proxima (Empyrean, Update 29.10)
    "Posit Cluster", "Minhast Station", "Phanghoul Satellites",
    "Jex Lanes", "Rian Belt",
    # Saturn Proxima
    "Vila Gap", "Spiro Gap",
    # Veil Proxima
    "Rya", "Gian Point", "Ruse War Field", "Ganalen's Grave",
}

# ==============================================================================
# DATA STRUCTURES (what load() returns)
# ==============================================================================
#
# parts_index: Dict[str, dict]
#   Key: WFM urlName slug (e.g. "nikana_prime_blueprint")
#   Value: {
#     "name":         str   — display name ("Nikana Prime Blueprint")
#     "rarity":       str   — "Common" | "Uncommon" | "Rare"  (from Intact state)
#     "source_relics": List[str]  — relic base names containing this part
#                                   (e.g. ["Axi A1", "Neo N7"])
#   }
#
# relics_index: Dict[str, dict]
#   Key: relic base name (e.g. "Axi A20")
#   Value: {
#     "relic_wfm_slug": str   — WFM slug for the relic itself (for unopened price),
#                               "<tier>_<code>_relic" e.g. "axi_a20_relic"
#     "locations":      List[dict]  — farm locations from the drop table
#                                     each: {"location": str, "rarity": str, "chance": float}
#     "states": {
#       "Intact":      List[dict],   — rewards at this refinement
#       "Exceptional": List[dict],   — each reward: {
#       "Flawless":    List[dict],   —   "name": str,
#       "Radiant":     List[dict],   —   "slug": str | None,  (WFM urlName; None = not tradeable)
#     }                              —   "chance": float,
#   }                                —   "rarity": str
#                                    — }
#   }
#
# Cache file (data/cache/relic_drops.json):
#   {"schema": 2, "checked_at": epoch seconds, "info": {drops info.json},
#    "relics":   {relic: [location dicts]},
#    "contents": {relic: {state: [{"name", "chance", "rarity"}]}}}
#   "relics" and "contents" hold only relics that passed validation.
# ==============================================================================


def _http_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── DE drop table (which relics are active, where they drop, what they hold) ──

_RELIC_REWARD_RE = re.compile(r"^((?:Lith|Meso|Neo|Axi) \w+) Relic")
# A relic's own contents rows: place is "Axi A1 Relic" (Intact has no suffix),
# "Axi A1 Relic (Exceptional)", "... (Flawless)" or "... (Radiant)".
_RELIC_PLACE_RE = re.compile(
    r"^((?:Lith|Meso|Neo|Axi) \w+) Relic(?: \((Exceptional|Flawless|Radiant)\))?$"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_relic_drops(rows: list) -> Dict[str, list]:
    """
    From the drop table's flat [{place, item, rarity, chance}, ...] rows, keep
    those where a standard relic is the REWARD. Returns {base name: [location dicts]}
    as {"location", "rarity", "chance"}.

    Radiant-only rows ("Axi A21 Relic (Radiant)") are kept under the same relic.
    """
    drops: Dict[str, list] = {}
    for row in rows:
        m = _RELIC_REWARD_RE.match(row.get("item", ""))
        if not m:
            continue
        # (A relic's own contents rows have place="Axi A1 Relic" and a part as
        # the item, so they never match the regex above.)
        drops.setdefault(m.group(1), []).append({
            "location": _HTML_TAG_RE.sub("", row.get("place", "")),
            "rarity":   row.get("rarity", ""),
            "chance":   row.get("chance", 0.0),
        })
    return drops


def _extract_contents(rows: list, active) -> Dict[str, Dict[str, list]]:
    """
    From the same rows, keep those where the PLACE is an active relic's contents
    list. Returns {base name: {state: [{"name", "chance", "rarity"}]}}. The drop
    table has only Uncommon/Rare labels, so rarity is relabelled in _build_indexes.
    """
    contents: Dict[str, Dict[str, list]] = {}
    for row in rows:
        m = _RELIC_PLACE_RE.match(row.get("place", ""))
        if not m or m.group(1) not in active:
            continue
        state = m.group(2) or "Intact"
        contents.setdefault(m.group(1), {}).setdefault(state, []).append({
            "name":   row.get("item", ""),
            "chance": row.get("chance", 0.0),
            "rarity": row.get("rarity", ""),
        })
    return contents


def _contents_problem(states: Optional[dict]) -> Optional[str]:
    """Why a relic's contents can't be shown, or None if they are well-formed."""
    if not states:
        return "no contents rows in the drop table"
    for state in REFINEMENT_STATES:
        rewards = states.get(state)
        if not rewards:
            return f"no {state} rows"
        if len(rewards) != REWARDS_PER_STATE:
            return f"{state} has {len(rewards)} rows, expected {REWARDS_PER_STATE}"
        for r in rewards:
            chance = r.get("chance")
            if not r.get("name") or isinstance(chance, bool) or not isinstance(chance, (int, float)):
                return f"{state} has a malformed row ({r})"
        total = sum(r["chance"] for r in rewards)
        if abs(total - 100) > CHANCE_TOTAL_TOLERANCE:
            return f"{state} chances sum to {total:.2f}, expected 100"
    return None


def _validated(locations: Dict[str, list], contents: Dict[str, dict]) -> Tuple[dict, dict]:
    """
    Keep only active relics whose contents are well-formed. A malformed relic is
    skipped with a WARNING instead of poisoning the whole table.
    """
    good_locations: Dict[str, list] = {}
    good_contents: Dict[str, dict] = {}
    for relic in sorted(locations):
        problem = _contents_problem(contents.get(relic))
        if problem:
            print(f"  WARNING: skipping active relic {relic}: {problem}")
            continue
        good_locations[relic] = locations[relic]
        good_contents[relic] = contents[relic]
    return good_locations, good_contents


def _load_drops_cache() -> Optional[dict]:
    """The cached drop table, or None if absent, unreadable or not CACHE_SCHEMA."""
    if not DROPS_CACHE_FILE.exists():
        return None
    try:
        with open(DROPS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict)
            or data.get("schema") != CACHE_SCHEMA
            or not isinstance(data.get("relics"), dict)
            or not isinstance(data.get("contents"), dict)):
        return None
    return data


def _save_drops_cache(payload: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(DROPS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _get_drops(force: bool = False) -> Optional[Tuple[Dict[str, list], Dict[str, dict]]]:
    """
    Active relics from the DE drop table, as (locations, contents).

    Cheap path: info.json (a few bytes) carries a content hash. If it matches the
    cached hash, the ~4.5MB table is not re-downloaded — only the cache's
    `checked_at` stamp is renewed. `force` skips the TTL, not the hash check.

    The TTL runs off `checked_at` stored INSIDE the cache, not the file's mtime:
    a git checkout/deploy stamps every file with the clone time, which would make a
    weeks-old committed snapshot look fresh. A cache whose schema isn't
    CACHE_SCHEMA counts as absent, bypassing both the TTL and the hash check.

    A new download is validated per relic (_validated) and rejected whole if fewer
    than MIN_ACTIVE_RELICS valid relics remain; a rejected or failed download keeps
    the cache. Returns None only if there is no usable data at all (fetch failed
    or rejected AND no cache); nothing is written then.
    """
    cached = _load_drops_cache()
    if not force and cached:
        age = (time.time() - cached.get("checked_at", 0)) / 3600
        if age < DROPS_TTL_HOURS:
            return cached["relics"], cached["contents"]

    try:
        info = _http_json(DROPS_INFO_URL, timeout=15)
        if cached and cached.get("info", {}).get("hash") == info.get("hash"):
            cached["checked_at"] = time.time()
            _save_drops_cache(cached)
            return cached["relics"], cached["contents"]
        print("Fetching drop table from drops.warframestat.us...")
        rows = _http_json(DROPS_URL, timeout=90)
        locations = _extract_relic_drops(rows)
        contents = _extract_contents(rows, set(locations))
        locations, contents = _validated(locations, contents)
        if len(locations) < MIN_ACTIVE_RELICS:
            raise ValueError(f"only {len(locations)} valid active relics found (expected ~35)")
    except Exception as e:
        if cached:
            print(f"  Drop table fetch failed ({e}); keeping cached rotation.")
            return cached["relics"], cached["contents"]
        print(f"  Drop table fetch failed ({e}); no cache — no relic data available.")
        return None

    _save_drops_cache({
        "schema": CACHE_SCHEMA,
        "checked_at": time.time(),
        "info": info,
        "relics": locations,
        "contents": contents,
    })
    print(f"  {len(locations)} active relics in drop table (modified {info.get('modified')}).")
    return locations, contents


def drops_status() -> Optional[dict]:
    """Version stamp of the cached drop table ({'hash','timestamp','modified'}), or None."""
    cached = _load_drops_cache()
    return cached.get("info") if cached else None


def _is_removed_node(location_str: str) -> bool:
    """
    Return True if a location string refers to a removed node.
    Location strings look like "Hapke, Ceres (Spy), Rotation B" or
    "Veil/Gian Point (Skirmish)" — we extract the node name as the
    text before the first comma or slash, then check against REMOVED_NODES.
    """
    # Strip planet-prefix format: "Veil/Gian Point (...)" → "Gian Point"
    loc = location_str
    if "/" in loc:
        loc = loc.split("/", 1)[1]
    # Node name is before the first comma or opening parenthesis
    node = loc.split("(")[0].split(",")[0].strip()
    return node in REMOVED_NODES


def _build_indexes(locations: Dict[str, list], contents: Dict[str, dict], client):
    """
    Build parts_index and relics_index from the drop table's active relics.

    `client` (MarketClient) resolves reward names to WFM slugs.

    Returns (parts_index, relics_index, unresolved) where `unresolved` is the sorted
    list of reward names with no WFM slug that aren't known non-tradeables (a part
    too new for the manifest, or a naming change).
    """
    parts_index: Dict[str, dict] = {}
    relics_index: Dict[str, dict] = {}
    unresolved = set()

    for base_name in sorted(locations):
        tier, code = base_name.split()

        # Filter out locations on removed nodes.
        relic_locations = [
            loc for loc in locations[base_name]
            if not _is_removed_node(loc.get("location", ""))
        ]

        states: Dict[str, list] = {}
        for state in REFINEMENT_STATES:
            state_rewards = []
            for r in contents[base_name][state]:
                slug = client.get_slug(r["name"])
                if slug is None and r["name"] not in NON_TRADEABLE:
                    unresolved.add(r["name"])
                # Keep non-tradeable rewards (e.g. Forma Blueprint) with slug=None
                # so their drop chance is included in EV calculations (price = 0).
                state_rewards.append({
                    "name":   r["name"],
                    "slug":   slug,
                    "chance": r["chance"],
                    "rarity": r.get("rarity") or "Unknown",
                })
            states[state] = state_rewards

        # Assign correct rarities from Intact state (the drop table has no Common tier).
        # Intact drop chances reflect the true slot structure: rank by chance desc
        # → top 3 = Common, next 2 = Uncommon, bottom 1 = Rare.
        # Non-Intact states keep the same labels — a Common stays Common even
        # though its drop rate shifts with refinement.
        states["Intact"].sort(key=lambda x: (-x["chance"], x["name"]))
        rarity_by_rank = ["Common", "Common", "Common", "Uncommon", "Uncommon", "Rare"]
        for i, rw in enumerate(states["Intact"]):
            rw["rarity"] = rarity_by_rank[i] if i < len(rarity_by_rank) else "Common"
        intact_rarity = {rw["name"]: rw["rarity"] for rw in states["Intact"]}
        for state in ("Exceptional", "Flawless", "Radiant"):
            for rw in states[state]:
                rw["rarity"] = intact_rarity.get(rw["name"], rw["rarity"])

        relics_index[base_name] = {
            "relic_wfm_slug": f"{tier.lower()}_{code.lower()}_relic",
            "locations": relic_locations,
            "states": states,
        }

        # ── parts_index (from Intact state; only tradeable parts) ─────────────
        for rw in states["Intact"]:
            slug = rw["slug"]
            if not slug:
                continue  # skip non-tradeable rewards like Forma
            if slug not in parts_index:
                parts_index[slug] = {
                    "name":          rw["name"],
                    "rarity":        rw["rarity"],
                    "source_relics": [],
                }
            if base_name not in parts_index[slug]["source_relics"]:
                parts_index[slug]["source_relics"].append(base_name)

    return parts_index, relics_index, sorted(unresolved)


def _build(drops, client):
    """(locations, contents) or None -> (parts_index, relics_index)."""
    if drops is None:
        print("  No relic data available (drop table unreachable and nothing cached).")
        return {}, {}

    locations, contents = drops
    parts_index, relics_index, unresolved = _build_indexes(locations, contents, client)

    # A reward name the manifest can't resolve is usually a part newer than the
    # manifest, which has no TTL of its own. Re-download it (at most hourly) and retry.
    if unresolved and client.manifest_age_hours() > MANIFEST_REFRESH_AFTER_HOURS:
        print(f"  {len(unresolved)} reward names not in the WFM manifest; refreshing it...")
        try:
            client.update_item_manifest()
        except Exception as e:
            print(f"  Manifest refresh failed ({e}).")
        else:
            parts_index, relics_index, unresolved = _build_indexes(locations, contents, client)

    if unresolved:
        print(f"  WARNING: {len(unresolved)} reward names have no WFM slug yet: {', '.join(unresolved)}")

    print(f"  {len(relics_index)} unvaulted standard relics.")
    print(f"  {len(parts_index)} unique Prime parts.")
    return parts_index, relics_index


def load(client):
    """
    Load relic data. Cache-first, but the cache expires (DROPS_TTL_HOURS), so a
    stale snapshot is re-checked here rather than trusted.

    Args:
        client: MarketClient, used to resolve reward names to WFM slugs.

    Returns:
        (parts_index, relics_index) — see structure comments above. Both are {}
        when there is no data at all (drop table down and nothing usable cached).
    """
    return _build(_get_drops(), client)


def refresh(client):
    """
    Force a re-check and return fresh indexes. Call from the UI's Refresh Relics
    button and from the scheduler. The drop table is re-downloaded only if its
    version stamp changed.

    Returns:
        (parts_index, relics_index)
    """
    return _build(_get_drops(force=True), client)


# ==============================================================================
# QUICK VERIFICATION (run this file directly to check output)
# ==============================================================================
if __name__ == "__main__":
    from market_client import MarketClient

    parts, relics = load(MarketClient())
    if not relics:
        raise SystemExit("No relic data.")

    print()
    print("=== RELICS INDEX SAMPLE ===")
    sample_relic = next(iter(relics))
    r = relics[sample_relic]
    print(f"Relic: {sample_relic}")
    print(f"  WFM slug:  {r['relic_wfm_slug']}")
    print(f"  Locations: {len(r['locations'])} entries")
    for state in REFINEMENT_STATES:
        rewards = r["states"][state]
        print(f"  {state}: {len(rewards)} rewards")
        for rw in rewards:
            print(f"    {rw['chance']:5.2f}%  [{rw['rarity'][:2]}]  {rw['name']}")

    print()
    print("=== PARTS INDEX SAMPLE (first 10 by name) ===")
    for slug, p in sorted(parts.items(), key=lambda x: x[1]["name"])[:10]:
        sources = ", ".join(p["source_relics"])
        print(f"  {p['name']:<45}  [{p['rarity'][:2]}]  {sources}")

    print()
    print("=== COUNTS BY TIER ===")
    from collections import Counter
    tier_counts = Counter(name.split()[0] for name in relics)
    for tier, count in sorted(tier_counts.items()):
        print(f"  {tier}: {count} relics")
