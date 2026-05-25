"""
price_fetcher.py
Fetches and caches WFM prices for all unique part slugs and relic slugs.

On first run (no cache): fetches live from WFM, ~6 minutes at 2s/request.
On subsequent runs: reads from data/cache/prices.json if < 2 hours old.

Usage:
    from price_fetcher import fetch_prices

    # prices is a dict keyed by slug:
    # {
    #   "nikana_prime_blueprint": {
    #     "sell_price":   float | None,   — list price (lowest active ingame seller)
    #     "buy_price":    float | None,   — top buyer (highest active ingame buy order)
    #     "sell_status":  "ok" | "insufficient_data" | "no_data" | "error",
    #     "buy_status":   "ok" | "no_data" | "error",
    #     "error_message": str | None,
    #   },
    #   ...
    # }
    prices = fetch_prices(parts_index, relics_index, market_client, progress_callback)
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional

CACHE_DIR = Path("data/cache")
PRICES_CACHE_FILE = CACHE_DIR / "prices.json"
PRICES_CACHE_TTL_HOURS = 12  # matches market_client TTL


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
    with open(PRICES_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("prices", {})


def _save_cache(prices: Dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
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


def fetch_prices(
    parts_index: Dict,
    relics_index: Dict,
    market_client,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict:
    """
    Return a price dict for all slugs in parts_index and relics_index.

    Reads from disk cache if fresh (< 2 hours old).
    Otherwise fetches live from WFM, writing results to disk when done.

    Args:
        parts_index:       from relic_data.load()
        relics_index:      from relic_data.load()
        market_client:     initialised MarketClient instance
        progress_callback: optional fn(current, total, slug) called after each fetch
                           use this to update a Streamlit progress bar

    Returns:
        Dict keyed by slug, each value is a price result dict from MarketClient.
    """
    if _is_cache_fresh():
        print(f"Loading prices from cache ({PRICES_CACHE_FILE})...")
        return _load_cache()

    slugs = _collect_slugs(parts_index, relics_index)
    total = len(slugs)
    print(f"Fetching prices for {total} items from warframe.market...")

    prices = {}
    for i, slug in enumerate(slugs, start=1):
        result = market_client.get_prices(slug)
        prices[slug] = result
        if progress_callback:
            progress_callback(i, total, slug)

    _save_cache(prices)
    print(f"  Done. Prices cached to {PRICES_CACHE_FILE}.")
    return prices


def clear_cache():
    """Delete the prices cache file, forcing a fresh fetch on next call."""
    if PRICES_CACHE_FILE.exists():
        PRICES_CACHE_FILE.unlink()
        print("Price cache cleared.")


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

    parts_index, relics_index = load_relics()
    client = MarketClient()

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
    # Low-value parts often have insufficient_data, so we skip ahead to find ones
    # with real numbers we can verify against warframe.market manually.
    print("=== SPOT CHECK (first 3 slugs with prices) ===")
    found = 0
    for slug in slugs:
        if found >= 3:
            break
        result = client.get_prices(slug)
        lp = result.get("sell_price")
        tb = result.get("buy_price")
        ss = result.get("sell_status")
        bs = result.get("buy_status")
        # Skip if neither price is available — nothing to verify
        if lp is None and tb is None:
            print(f"  {slug}  — no data, skipping")
            continue
        found += 1
        print(f"  {slug}")
        print(f"    List Price: {lp if lp is not None else '—'}  ({ss})")
        print(f"    Top Buyer:  {tb if tb is not None else '—'}  ({bs})")
        print(f"    Check: https://warframe.market/items/{slug}")