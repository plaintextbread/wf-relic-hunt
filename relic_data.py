"""
relic_data.py
Loads WFCD's Relics.json, filters to unvaulted standard-tier relics,
and builds two lookup structures used by the rest of the app:

  parts_index  — one entry per unique Prime part, with source relics
  relics_index — one entry per unique relic, with all 4 refinement states

Data is cached to data/cache/relics.json after the first fetch.
Call load() to get both structures. Call refresh() to re-fetch from WFCD.
"""

import json
import urllib.request
from pathlib import Path
from typing import Dict

# ==============================================================================
# CONFIGURATION
# ==============================================================================
WFCD_URL = "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Relics.json"
CACHE_DIR = Path("data/cache")
CACHE_FILE = CACHE_DIR / "relics.json"

STANDARD_TIERS = {"Lith", "Meso", "Neo", "Axi"}
REFINEMENT_STATES = ("Intact", "Exceptional", "Flawless", "Radiant")

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
#     "relic_wfm_slug": str   — WFM slug for the relic itself (for unopened price)
#     "locations":      List[dict]  — farm locations from WFCD
#                                     each: {"location": str, "rarity": str, "chance": float}
#     "states": {
#       "Intact":      List[dict],   — rewards at this refinement
#       "Exceptional": List[dict],   — each reward: {
#       "Flawless":    List[dict],   —   "name": str,
#       "Radiant":     List[dict],   —   "slug": str,   (WFM urlName)
#     }                              —   "chance": float,
#   }                                —   "rarity": str
#                                    — }
#   }
# ==============================================================================


def _fetch_raw() -> list:
    """Download Relics.json from WFCD. Returns the raw list of all relic entries."""
    print("Fetching Relics.json from WFCD...")
    req = urllib.request.Request(WFCD_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    print(f"  Fetched {len(data)} total relic entries.")
    return data


def _save_cache(data: list):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"  Saved to {CACHE_FILE}.")


def _load_cache() -> list:
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_relic_name(full_name: str):
    """
    Split "Axi A20 Intact" into ("Axi A20", "Intact").
    Returns (base_name, state) or (full_name, None) if no state suffix found.
    """
    for state in REFINEMENT_STATES:
        if full_name.endswith(f" {state}"):
            base = full_name[: -(len(state) + 1)]
            return base, state
    return full_name, None


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


def _build_indexes(raw: list):
    """
    Filter raw WFCD data and build parts_index and relics_index.
    Only processes unvaulted standard-tier relics.
    """
    parts_index: Dict[str, dict] = {}
    relics_index: Dict[str, dict] = {}

    for entry in raw:
        name = entry.get("name", "")
        vaulted = entry.get("vaulted", True)

        # Skip vaulted relics
        if vaulted:
            continue

        # Parse name to get tier, base name, and refinement state
        base_name, state = _parse_relic_name(name)
        if state is None:
            continue  # malformed name, skip

        tier = base_name.split()[0]
        if tier not in STANDARD_TIERS:
            continue  # skip Requiem, Vanguard, etc.

        rewards = entry.get("rewards", [])
        relic_slug = entry.get("marketInfo", {}).get("urlName", "")

        # Filter out locations on removed nodes.
        # Location strings start with "Node, Planet (...)" so we extract the
        # node name as the text before the first comma or slash.
        raw_locations = entry.get("locations", [])
        locations = [
            loc for loc in raw_locations
            if not _is_removed_node(loc.get("location", ""))
        ]

        # ── Build relics_index entry ──────────────────────────────────────────
        if base_name not in relics_index:
            relics_index[base_name] = {
                "relic_wfm_slug": relic_slug,
                "locations": locations,
                "states": {s: [] for s in REFINEMENT_STATES},
            }

        state_rewards = []
        for r in rewards:
            item     = r.get("item", {})
            wfm      = item.get("warframeMarket", {})
            slug     = wfm.get("urlName", "") if wfm else ""
            item_name = item.get("name", "")
            if not item_name:
                continue
            # Keep non-tradeable rewards (e.g. Forma Blueprint) with slug=None
            # so their drop chance is included in EV calculations (price = 0).
            state_rewards.append({
                "name":   item_name,
                "slug":   slug or None,
                "chance": r.get("chance", 0.0),
                "rarity": r.get("rarity", "Unknown"),
            })

        # Assign correct rarities from Intact state (WFCD has no Common tier).
        # Intact drop chances reflect the true slot structure: rank by chance desc
        # → top 3 = Common, next 2 = Uncommon, bottom 1 = Rare.
        # Non-Intact states keep the same labels — a Common stays Common even
        # though its drop rate shifts with refinement.
        if state == "Intact":
            state_rewards.sort(key=lambda x: (-x["chance"], x["name"]))
            rarity_by_rank = ["Common", "Common", "Common", "Uncommon", "Uncommon", "Rare"]
            for i, rw in enumerate(state_rewards):
                rw["rarity"] = rarity_by_rank[i] if i < len(rarity_by_rank) else "Common"

        relics_index[base_name]["states"][state] = state_rewards

        # ── Build parts_index (from Intact state only for rarity) ─────────────
        # Only include tradeable parts (slug is not None)
        if state == "Intact":
            for r in state_rewards:
                slug = r["slug"]
                if not slug:
                    continue  # skip non-tradeable rewards like Forma
                if slug not in parts_index:
                    parts_index[slug] = {
                        "name":          r["name"],
                        "rarity":        r["rarity"],
                        "source_relics": [],
                    }
                if base_name not in parts_index[slug]["source_relics"]:
                    parts_index[slug]["source_relics"].append(base_name)

    # Stamp Intact rarities onto all other states — refinement shifts drop chances
    # but doesn't change what tier a slot belongs to.
    for relic in relics_index.values():
        intact_rarity = {rw["name"]: rw["rarity"] for rw in relic["states"]["Intact"]}
        for state in ("Exceptional", "Flawless", "Radiant"):
            for rw in relic["states"][state]:
                rw["rarity"] = intact_rarity.get(rw["name"], rw["rarity"])

    return parts_index, relics_index


def load():
    """
    Load relic data. Uses disk cache if available, otherwise fetches from WFCD.

    Returns:
        (parts_index, relics_index) — see module docstring for structure.
    """
    if CACHE_FILE.exists():
        print(f"Loading Relics.json from cache ({CACHE_FILE})...")
        raw = _load_cache()
        print(f"  Loaded {len(raw)} entries from cache.")
    else:
        raw = _fetch_raw()
        _save_cache(raw)

    parts_index, relics_index = _build_indexes(raw)

    print(f"  {len(relics_index)} unvaulted standard relics.")
    print(f"  {len(parts_index)} unique Prime parts.")

    return parts_index, relics_index


def refresh():
    """
    Force re-fetch Relics.json from WFCD, overwrite cache, and return fresh indexes.
    Call this from the UI's Refresh Relic Data button.

    Returns:
        (parts_index, relics_index)
    """
    raw = _fetch_raw()
    _save_cache(raw)
    parts_index, relics_index = _build_indexes(raw)

    print(f"  Refreshed: {len(relics_index)} relics, {len(parts_index)} parts.")
    return parts_index, relics_index


# ==============================================================================
# QUICK VERIFICATION (run this file directly to check output)
# ==============================================================================
if __name__ == "__main__":
    parts, relics = load()

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