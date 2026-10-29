"""
Warframe.Market API Client
Handles all communication with the warframe.market API: the item manifest
(v2 /items) and per-item closed-trade statistics (v1 /statistics).

RATE LIMITING:
    REQUEST_INTERVAL_SECONDS controls the delay between API calls.
    Currently set to 2.0 (one request every 2 seconds).
    Before running, confirm this value at the top of the file.

USER-AGENT:
    warframe.market's Cloudflare answers python-requests' default User-Agent with
    a 403 challenge, so every request sends the honest HEADERS["User-Agent"] below.
"""

import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple

# ==============================================================================
# RATE LIMIT CONFIGURATION
# Confirm this value before running any live tests.
# 2.0 = one request every 2 seconds (0.5 req/sec)
# ==============================================================================
REQUEST_INTERVAL_SECONDS = 2.0

# ==============================================================================
# API CONFIGURATION
# ==============================================================================
BASE_URL    = "https://api.warframe.market/v2"   # Used for the item manifest
V1_BASE_URL = "https://api.warframe.market/v1"   # Used for the statistics endpoint
HEADERS = {
    "Content-Type": "application/json",
    "Platform": "pc",
    "Language": "en",
    "Crossplay": "false",
    "User-Agent": "wf-relic-hunt/1.0 (+https://relics.ananth.link)",
}

# Cache settings
CACHE_DIR = Path("data/cache")
WFM_ITEMS_CACHE_FILE = CACHE_DIR / "wfm_items.json"

# A manifest with fewer items than this is a bad download (the real one has ~3900);
# keep the old manifest instead of replacing it.
MIN_MANIFEST_ITEMS = 1000

# Only used by _calculate_sell_price_from_orders (listing-price fallback, currently unused)
MIN_SELL_LISTINGS = 4   # Need at least 4 to skip lowest and avg #2-4


class MarketClient:
    """
    Client for the warframe.market API.

    Responsibilities:
    - Fetching and caching the WFM item manifest (slug + ducat lookup)
    - Fetching closed-trade statistics for items via /v1/items/{slug}/statistics
    - Rate limiting (REQUEST_INTERVAL_SECONDS between requests)

    There is deliberately no in-memory price cache: the disk cache owned by
    price_fetcher is the only cache, so every get_prices() call is a live request.
    """

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Timestamp of last API request (for rate limiting)
        self._last_request_time: float = 0.0

        # Item manifest: built from WFM's /v2/items endpoint
        # slug_by_name: {lowercase_display_name: slug}
        # ducats_by_slug: {slug: int}
        self.slug_by_name: Dict[str, str] = {}
        self.ducats_by_slug: Dict[str, int] = {}
        # ISO UTC timestamp of the last manifest download, None if unknown
        self.manifest_fetched_at: Optional[str] = None

        self._load_item_manifest()

    # --------------------------------------------------------------------------
    # Item manifest
    # --------------------------------------------------------------------------

    def _load_item_manifest(self):
        """
        Load the WFM item manifest from disk cache, or fetch fresh if missing.
        The manifest maps display names to slugs and stores ducat values.
        """
        if WFM_ITEMS_CACHE_FILE.exists():
            print("Loading WFM item manifest from disk cache...")
            with open(WFM_ITEMS_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            self.slug_by_name = cached.get("slug_by_name", {})
            self.ducats_by_slug = cached.get("ducats_by_slug", {})
            self.manifest_fetched_at = cached.get("fetched_at")
            print(f"  Loaded {len(self.slug_by_name)} items from cache.")
        else:
            print("WFM item manifest not found. Fetching from API...")
            self.update_item_manifest()

    def manifest_age_hours(self) -> float:
        """Hours since the manifest was downloaded; infinite if there is no usable stamp."""
        if not self.manifest_fetched_at:
            return float("inf")
        try:
            fetched_at = datetime.fromisoformat(self.manifest_fetched_at)
        except ValueError:
            return float("inf")
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600

    def update_item_manifest(self):
        """
        Fetch the full item list from GET /v2/items and rebuild the manifest.
        Saves result to disk for future startups.

        Raises (and leaves the existing manifest untouched) if the request fails
        or returns fewer than MIN_MANIFEST_ITEMS items.
        """
        print("Fetching item manifest from warframe.market...")
        self._rate_limit_wait()

        try:
            response = requests.get(
                f"{BASE_URL}/items",
                headers=HEADERS,
                timeout=30
            )
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise ConnectionError("warframe.market API timed out fetching item manifest.")
        except requests.exceptions.HTTPError as e:
            raise ConnectionError(f"warframe.market API error fetching manifest: {e}")
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Cannot connect to warframe.market. Check your internet connection.")
        finally:
            self._last_request_time = time.time()

        data = response.json()
        items = data.get("data", [])

        slug_by_name = {}
        ducats_by_slug = {}

        for item in items:
            slug = item.get("slug")
            ducats = item.get("ducats", 0)
            i18n = item.get("i18n", {})
            en = i18n.get("en", {})
            name = en.get("name", "")

            if slug and name:
                slug_by_name[name.lower()] = slug
                ducats_by_slug[slug] = ducats or 0

        if len(slug_by_name) < MIN_MANIFEST_ITEMS:
            raise ValueError(
                f"item manifest has only {len(slug_by_name)} items "
                f"(expected thousands); keeping the old manifest."
            )

        fetched_at = datetime.now(timezone.utc).isoformat()
        self.slug_by_name = slug_by_name
        self.ducats_by_slug = ducats_by_slug
        self.manifest_fetched_at = fetched_at

        # Save to disk
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(WFM_ITEMS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "fetched_at": fetched_at,
                    "slug_by_name": slug_by_name,
                    "ducats_by_slug": ducats_by_slug,
                },
                f,
                indent=2,
                ensure_ascii=False
            )

        print(f"  Fetched and cached {len(slug_by_name)} items.")

    def get_slug(self, display_name: str) -> Optional[str]:
        """
        Look up the WFM slug for a display name (case-insensitive).

        The drop table names components as "Rhino Prime Chassis" but WFM lists
        them as "Rhino Prime Chassis Blueprint". If the direct lookup fails and
        the name doesn't already end in "Blueprint", we try appending it.

        Args:
            display_name: Item display name, e.g. "Rhino Prime Chassis"

        Returns:
            Slug string or None if not found
        """
        key = display_name.lower()
        slug = self.slug_by_name.get(key)
        if slug:
            return slug

        # Try appending "Blueprint" for component names
        if not key.endswith("blueprint"):
            slug = self.slug_by_name.get(key + " blueprint")
            if slug:
                return slug

        return None

    def get_ducats(self, slug: str) -> int:
        """
        Get the ducat value for a slug from the WFM manifest.

        Args:
            slug: WFM item slug

        Returns:
            Ducat value (0 if not found or not applicable)
        """
        return self.ducats_by_slug.get(slug, 0)

    # --------------------------------------------------------------------------
    # Price fetching
    # --------------------------------------------------------------------------

    def get_prices(self, slug: str) -> Dict:
        """
        Fetch the sell price for an item (one rate-limited request, never cached
        in memory; price_fetcher owns the disk cache).

        The sell price is the volume-weighted average of closed trades from
        /v1/items/{slug}/statistics (48h window, 90 days if the 48h window is empty).

        Args:
            slug: WFM item slug

        Returns:
            Dict with keys:
                sell_price: float or None
                ducat_plat_ratio: float or None
                sell_status: "ok" | "no_data" | "error"
                    "no_data": the item has no closed trades (or no statistics page, 404)
                    "error":   the request failed (HTTP error, timeout, bad response)
                error_message: str or None (only set on "error")
        """
        self._rate_limit_wait()

        sell_price, sell_status, error_message = self._fetch_avg_trade(slug)

        # Ducat/plat ratio uses sell price as the denominator
        ducat_plat_ratio = None
        ducats = self.get_ducats(slug)
        if sell_price and sell_price > 0 and ducats and ducats > 0:
            ducat_plat_ratio = round(ducats / sell_price, 2)

        return {
            "sell_price": sell_price,
            "ducat_plat_ratio": ducat_plat_ratio,
            "sell_status": sell_status,
            "error_message": error_message,
        }

    def _fetch_avg_trade(self, slug: str) -> Tuple[Optional[float], str, Optional[str]]:
        """
        Fetch volume-weighted average trade price from /v1/items/{slug}/statistics.

        Uses 48h data. Falls back to 90days if 48h has no entries.
        Returns (price, status, error_message):
          - 200 with trades          -> (price, "ok", None)
          - 200 without trades, 404  -> (None, "no_data", None)
          - any other HTTP status, timeout or unreadable response
                                     -> (None, "error", message with the cause)
        """
        try:
            response = requests.get(
                f"{V1_BASE_URL}/items/{slug}/statistics",
                headers=HEADERS,
                timeout=15,
            )
        except requests.exceptions.Timeout:
            return None, "error", "Request timed out"
        except requests.exceptions.ConnectionError:
            return None, "error", "Cannot connect to warframe.market"
        except Exception as e:
            return None, "error", f"{type(e).__name__}: {e}"
        finally:
            self._last_request_time = time.time()

        if response.status_code == 404:
            return None, "no_data", None
        if response.status_code != 200:
            message = f"HTTP {response.status_code}"
            if response.headers.get("Cf-Mitigated"):
                message += " (Cloudflare challenge)"
            return None, "error", message

        try:
            buckets = (
                response.json()
                .get("payload", {})
                .get("statistics_closed", {})
            )
            for window in ("48hours", "90days"):
                entries = buckets.get(window, [])
                if not entries:
                    continue
                # Volume-weighted average across all buckets in the window
                total_volume = sum(e.get("volume", 0) for e in entries)
                if total_volume == 0:
                    continue
                wa_price = sum(
                    e.get("wa_price", 0) * e.get("volume", 0)
                    for e in entries
                ) / total_volume
                return round(wa_price, 1), "ok", None
        except Exception as e:
            return None, "error", f"Unreadable response ({type(e).__name__}: {e})"

        return None, "no_data", None

    def _calculate_sell_price_from_orders(self, sell_orders: list) -> Tuple[Optional[float], str]:
        """
        Fallback sell price from listing orders. NOT CALLED: it is kept for the day
        the v1 statistics endpoint goes away. To use it, get_prices() would have to
        request /v2/orders/item/{slug}/top again (the "sell" list of its payload
        feeds this function) and use the result in place of _fetch_avg_trade.

        Preference order:
          1. 4+ ingame listings: skip lowest, average #2-4.
          2. 1-3 ingame listings: use the single lowest.
          3. No ingame listings: use the single lowest from any online seller.

        Returns:
            (price, status)
        """
        ingame_prices = sorted([
            o["platinum"] for o in sell_orders
            if "platinum" in o and o.get("user", {}).get("status") == "ingame"
        ])

        if len(ingame_prices) >= MIN_SELL_LISTINGS:
            avg = sum(ingame_prices[1:4]) / 3
            return round(avg, 1), "ok"

        if ingame_prices:
            return float(ingame_prices[0]), "ok"

        # Fallback: any online seller
        all_prices = sorted([
            o["platinum"] for o in sell_orders
            if "platinum" in o
        ])
        if all_prices:
            return float(all_prices[0]), "ok"

        return None, "no_data"

    # --------------------------------------------------------------------------
    # Rate limiting
    # --------------------------------------------------------------------------

    def _rate_limit_wait(self):
        """
        Block until enough time has passed since the last request.
        Enforces REQUEST_INTERVAL_SECONDS between all API calls.
        """
        elapsed = time.time() - self._last_request_time
        wait_needed = REQUEST_INTERVAL_SECONDS - elapsed
        if wait_needed > 0:
            time.sleep(wait_needed)
