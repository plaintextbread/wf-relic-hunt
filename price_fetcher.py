"""
price_fetcher.py
Fetches and caches WFM prices for all unique part slugs and relic slugs.

On first run (no cache): fetches live from WFM, one request per slug at
market_client.REQUEST_INTERVAL_SECONDS apart (~2.4s per item, ~6-7 minutes for ~160 slugs).
On subsequent runs: reads from data/cache/prices.json if < PRICES_CACHE_TTL_HOURS old.

The cache is only ever MERGED into, never overwritten blind: a failed request
("error") never replaces an existing good entry, a fetch that produced nothing but
errors is not saved, and a fetch whose first batch is all errors is aborted. The
price cache is therefore the last good data we have, not the last attempt.

Usage:
    from price_fetcher import fetch_prices

    # prices is a dict keyed by slug:
    # {
    #   "nikana_prime_blueprint": {
    #     "sell_price":   float | None,   — volume-weighted avg of closed trades (48h, 90d fallback)
    #     "ducat_plat_ratio": float | None,
    #     "sell_status":  "ok" | "no_data" | "error",
    #     "error_message": str | None,    — set on "error", e.g. "HTTP 403 (Cloudflare challenge)"
    #   },
    #   ...
    # }
    prices = fetch_prices(parts_index, relics_index, market_client, progress_callback)
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

CACHE_DIR = Path("data/cache")
PRICES_CACHE_FILE = CACHE_DIR / "prices.json"
PRICES_CACHE_TTL_HOURS = 12

# If every result in the first batch of a fetch is an error, warframe.market is
# unreachable (or blocking us): stop instead of working through the whole list.
# Keep equal to app.FETCH_BATCH_SIZE so the app and the scheduler abort alike.
ABORT_PROBE_SIZE = 5


def _is_cache_fresh() -> bool:
    """Return True if prices.json exists and is less than TTL hours old."""
    if not PRICES_CACHE_FILE.exists():
        return False
    with open(PRICES_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    fetched_at_str = data.get("fetched_at")
    if not fetched_at_str:
        return False
    fetched_at = datetime.fromisoformat(fetched_at_str)
    # Handle both offset-aware and offset-naive datetimes
    now = datetime.now(timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age = now - fetched_at
    return age < timedelta(hours=PRICES_CACHE_TTL_HOURS)


def _load_cache() -> Dict:
    """The cached prices, or {} if there is no cache file."""
    if not PRICES_CACHE_FILE.exists():
        return {}
    with open(PRICES_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("prices", {})


def cache_fetched_at() -> Optional[str]:
    """ISO timestamp stored in the price cache, or None if there is no cache."""
    if not PRICES_CACHE_FILE.exists():
        return None
    with open(PRICES_CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("fetched_at")


def _save_cache(prices: Dict, fetched_at: Optional[str] = None):
    """
    Write the price cache. `fetched_at` is only passed when topping up a fresh cache
    with a few missing slugs: the older entries keep their original timestamp instead
    of being relabeled as new.

    After a partial failure the file holds entries of mixed ages under one
    `fetched_at` (entries whose refetch failed keep their old value; see merge()).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "prices": prices,
    }
    with open(PRICES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _collect_slugs(parts_index: Dict, relics_index: Dict) -> list:
    """
    Build a deduplicated list of all slugs to price:
    - one per unique part (from parts_index)
    - one per unique relic (from relics_index, for unopened sell price)
    """
    slugs = set()
    for slug in parts_index:
        slugs.add(slug)
    for relic_name, relic in relics_index.items():
        relic_slug = relic.get("relic_wfm_slug", "")
        if relic_slug:
            slugs.add(relic_slug)
    return sorted(slugs)


def is_error(result: Optional[Dict]) -> bool:
    return bool(result) and result.get("sell_status") == "error"


def missing_slugs(slugs: Iterable[str], cached: Dict) -> List[str]:
    """
    Slugs a top-up should fetch: absent from the cache, or cached as an "error"
    (a failed attempt isn't data, so it is retried).
    """
    return sorted(s for s in slugs if s not in cached or is_error(cached[s]))


def merge(old: Dict, new: Dict) -> Dict:
    """
    Overlay freshly fetched results `new` onto the cached `old`, returning a new dict.
    A fresh "error" never replaces an existing entry that isn't itself an error
    (a good price, or a legitimate "no_data"); everything else in `new` wins.
    """
    merged = dict(old)
    for slug, result in new.items():
        if is_error(result) and slug in merged and not is_error(merged[slug]):
            continue
        merged[slug] = result
    return merged


def first_batch_failed(results: List[Dict]) -> bool:
    """True if a fetch's first batch is complete and every result in it is an error."""
    return bool(results) and all(is_error(r) for r in results)


def describe_failure(results: Iterable[Dict]) -> str:
    """Short cause for a failed batch, e.g. 'HTTP 403 (Cloudflare challenge)'."""
    for r in results:
        if is_error(r) and r.get("error_message"):
            return r["error_message"]
    return "unknown error"


def summarize(prices: Dict) -> str:
    """'priced X / no_data Y / error Z' counts over a price dict."""
    counts = {"ok": 0, "no_data": 0, "error": 0}
    for r in prices.values():
        status = r.get("sell_status")
        if status in counts:
            counts[status] += 1
    return f"priced {counts['ok']} / no_data {counts['no_data']} / error {counts['error']}"


def has_usable_results(results: Dict) -> bool:
    """True if at least one result isn't an error (so saving it can't lose anything)."""
    return any(not is_error(r) for r in results.values())


def fetch_prices(
    parts_index: Dict,
    relics_index: Dict,
    market_client,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    force: bool = False,
) -> Dict:
    """
    Return a price dict for all slugs in parts_index and relics_index.

    Reads from disk cache if fresh (< PRICES_CACHE_TTL_HOURS old), unless `force`.
    Otherwise fetches live from WFM and merges the results into the cached ones.
    Nothing is written when there are no slugs, when the first batch is all errors
    (the fetch is aborted), or when the fetch produced nothing but errors; the
    existing cache is returned in those cases.

    Args:
        parts_index:       from relic_data.load()
        relics_index:      from relic_data.load()
        market_client:     initialised MarketClient instance
        progress_callback: optional fn(current, total, slug) called after each fetch
                           use this to update a Streamlit progress bar
        force:             fetch even if the cache is fresh (the scheduler does this)

    Returns:
        Dict keyed by slug, each value is a price result dict from MarketClient.
    """
    slugs = _collect_slugs(parts_index, relics_index)
    if not slugs:
        print("No slugs to price; leaving the price cache untouched.")
        return _load_cache()

    if not force and _is_cache_fresh():
        print(f"Loading prices from cache ({PRICES_CACHE_FILE})...")
        return _load_cache()

    old = _load_cache()
    total = len(slugs)
    print(f"Fetching prices for {total} items from warframe.market...")

    new = {}
    for i, slug in enumerate(slugs, start=1):
        new[slug] = market_client.get_prices(slug)
        if progress_callback:
            progress_callback(i, total, slug)
        if i == min(ABORT_PROBE_SIZE, total) and first_batch_failed(list(new.values())):
            print(f"  Aborted: warframe.market unreachable ({describe_failure(new.values())}); "
                  f"price cache left untouched.")
            return old

    print(f"  Fetched: {summarize(new)}.")
    if not has_usable_results(new):
        print("  Nothing usable fetched; price cache left untouched.")
        return old

    merged = merge(old, new)
    _save_cache(merged)
    print(f"  Done. Prices cached to {PRICES_CACHE_FILE} ({summarize(merged)}).")
    return merged


def cache_age_minutes() -> Optional[float]:
    """
    Return the age of the price cache in minutes, or None if no cache exists.
    Useful for showing "last updated X minutes ago" in the UI.
    """
    if not PRICES_CACHE_FILE.exists():
        return None
    with open(PRICES_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    fetched_at_str = data.get("fetched_at")
    if not fetched_at_str:
        return None
    fetched_at = datetime.fromisoformat(fetched_at_str)
    now = datetime.now(timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (now - fetched_at).total_seconds() / 60


# ==============================================================================
# QUICK VERIFICATION (run this file directly)
# ==============================================================================
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from relic_data import load as load_relics
    from market_client import MarketClient

    client = MarketClient()
    parts_index, relics_index = load_relics(client)

    slugs = _collect_slugs(parts_index, relics_index)
    print(f"\nTotal slugs to fetch: {len(slugs)}")
    print(f"  Parts: {len(parts_index)}")
    print(f"  Relics: {len(relics_index)}")
    print(f"  Estimated time (cold): ~{len(slugs) * 2 // 60}m {len(slugs) * 2 % 60}s")
    print()

    age = cache_age_minutes()
    if age is not None:
        print(f"Cache exists — {age:.1f} minutes old (TTL: {PRICES_CACHE_TTL_HOURS * 60} minutes)")
        if _is_cache_fresh():
            print("Cache is fresh. Would use cached prices.")
        else:
            print("Cache is stale. Would fetch live.")
    else:
        print("No cache found. Would fetch live on first run.")
    print()

    # Spot-check: fetch up to 10 slugs, print the first 3 that have actual prices.
    # Low-value parts often have no_data, so we skip ahead to find ones
    # with real numbers we can verify against warframe.market manually.
    print("=== SPOT CHECK (first 3 slugs with prices) ===")
    found = 0
    for slug in slugs:
        if found >= 3:
            break
        result = client.get_prices(slug)
        sp = result.get("sell_price")
        ss = result.get("sell_status")
        # Skip if no price is available — nothing to verify
        if sp is None:
            print(f"  {slug}  — {ss}, skipping")
            continue
        found += 1
        print(f"  {slug}")
        print(f"    Sell Price: {sp}  ({ss})")
        print(f"    Check: https://warframe.market/items/{slug}")
