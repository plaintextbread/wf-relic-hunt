"""
Warframe.Market API Client (v2)
Handles all communication with the warframe.market API.

RATE LIMITING:
    REQUEST_INTERVAL_SECONDS controls the delay between API calls.
    Currently set to 2.0 (one request every 2 seconds).
    Before running, confirm this value at the top of the file.
"""

import json
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta
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
BASE_URL    = "https://api.warframe.market/v2"
V1_BASE_URL = "https://api.warframe.market/v1"  # Used for statistics endpoint
HEADERS = {
    "Content-Type": "application/json",
    "Platform": "pc",
    "Language": "en",
    "Crossplay": "false",
}

# Cache settings
CACHE_DIR = Path("data/cache")
WFM_ITEMS_CACHE_FILE = CACHE_DIR / "wfm_items.json"
PRICE_CACHE_TTL_MINUTES = 720  # 12 hours — avg trade prices move slowly

# Pricing logic thresholds (from design doc)
MIN_SELL_LISTINGS = 4   # Need at least 4 to skip lowest and avg #2-4


class MarketClient:
    """
    Client for the warframe.market v2 API.

    Responsibilities:
    - Fetching and caching the WFM item manifest (slug + ducat lookup)
    - Fetching top orders for items via /v2/orders/item/{slug}/top
    - Rate limiting (REQUEST_INTERVAL_SECONDS between requests)
    - In-memory price cache with TTL
    """

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # In-memory price cache: {slug: {"timestamp": datetime, "result": dict}}
        self._price_cache: Dict[str, dict] = {}

        # Timestamp of last API request (for rate limiting)
        self._last_request_time: float = 0.0

        # Item manifest: built from WFM's /v2/items endpoint
        # slug_by_name: {lowercase_display_name: slug}
        # ducats_by_slug: {slug: int}
        self.slug_by_name: Dict[str, str] = {}
        self.ducats_by_slug: Dict[str, int] = {}

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
            print(f"  Loaded {len(self.slug_by_name)} items from cache.")
        else:
            print("WFM item manifest not found. Fetching from API...")
            self.update_item_manifest()

    def update_item_manifest(self):
        """
        Fetch the full item list from GET /v2/items and rebuild the manifest.
        Saves result to disk for future startups.
        """
        print("Fetching item manifest from warframe.market...")
        self._rate_limit_wait()

        try:
            response = requests.get(
                f"{BASE_URL}/items",
                headers=HEADERS,
                timeout=30
            )
            self._last_request_time = time.time()
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise ConnectionError("warframe.market API timed out fetching item manifest.")
        except requests.exceptions.HTTPError as e:
            raise ConnectionError(f"warframe.market API error fetching manifest: {e}")
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Cannot connect to warframe.market. Check your internet connection.")

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

        self.slug_by_name = slug_by_name
        self.ducats_by_slug = ducats_by_slug

        # Save to disk
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(WFM_ITEMS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"slug_by_name": slug_by_name, "ducats_by_slug": ducats_by_slug},
                f,
                indent=2,
                ensure_ascii=False
            )

        print(f"  Fetched and cached {len(slug_by_name)} items.")

    def get_slug(self, display_name: str) -> Optional[str]:
        """
        Look up the WFM slug for a display name (case-insensitive).

        WFCD names components as "Rhino Prime Chassis" but WFM lists them as
        "Rhino Prime Chassis Blueprint". If the direct lookup fails and the name
        doesn't already end in "Blueprint", we try appending it.

        Args:
            display_name: Item display name, e.g. "Rhino Prime Chassis"

        Returns:
            Slug string or None if not found
        """
        key = display_name.lower()
        slug = self.slug_by_name.get(key)
        if slug:
            return slug

        # Try appending "Blueprint" for WFCD component names
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
        Get sell and buy prices for an item using /v2/orders/item/{slug}/top.

        Pricing logic (from design doc):
        - Sell: skip lowest listing, average listings #2, #3, #4 (need >= 4)
        - Buy:  average top 3 online buy orders (need >= 3)

        The /top endpoint already filters to online-only users and sorts by price
        (sell: ascending, buy: descending).

        Args:
            slug: WFM item slug

        Returns:
            Dict with keys:
                sell_price: float or None
                buy_price: float or None
                ducat_plat_ratio: float or None
                sell_status: "ok" | "insufficient_data" | "no_data" | "error"
                buy_status:  "ok" | "insufficient_data" | "no_data" | "error"
                error_message: str or None (only set on "error")
        """
        # Check in-memory cache
        cached = self._price_cache.get(slug)
        if cached:
            age = datetime.now() - cached["timestamp"]
            if age < timedelta(minutes=PRICE_CACHE_TTL_MINUTES):
                return cached["result"]

        result = self._fetch_and_calculate_prices(slug)

        # Store in cache regardless of outcome (avoids hammering on error)
        self._price_cache[slug] = {
            "timestamp": datetime.now(),
            "result": result
        }

        return result

    def _fetch_and_calculate_prices(self, slug: str) -> Dict:
        """
        Fetch prices for an item.

        Primary sell price: volume-weighted average from /v1/statistics (48h,
        fallback to 90days). This reflects actual completed trades rather than
        listing prices, which is more accurate especially for thin markets.

        Buy price: highest active ingame buy order from /v2/orders/item/{slug}/top.

        If the statistics endpoint is unavailable, falls back to listing-based
        sell price (see _calculate_sell_price_from_orders, commented below).
        """
        self._rate_limit_wait()

        # ── Primary: avg trade price from statistics endpoint ─────────────────
        avg_trade, avg_status = self._fetch_avg_trade(slug)

        # ── Buy price from /top orders ────────────────────────────────────────
        try:
            response = requests.get(
                f"{BASE_URL}/orders/item/{slug}/top",
                headers=HEADERS,
                timeout=15
            )
            self._last_request_time = time.time()

            if response.status_code == 404:
                return self._no_data_result("Item not found on warframe.market")
            if response.status_code == 429:
                return self._error_result("Rate limit hit (429). Wait a moment and try again.")
            if response.status_code >= 500:
                return self._error_result(f"warframe.market server error ({response.status_code})")
            response.raise_for_status()

        except requests.exceptions.Timeout:
            return self._error_result("Request timed out")
        except requests.exceptions.ConnectionError:
            return self._error_result("Cannot connect to warframe.market")
        except requests.exceptions.HTTPError as e:
            return self._error_result(f"HTTP error: {e}")

        data = response.json()
        payload = data.get("data", {})

        if not payload:
            # Still return avg trade if we have it
            return {
                "sell_price": avg_trade,
                "buy_price": None,
                "ducat_plat_ratio": None,
                "sell_status": avg_status,
                "buy_status": "no_data",
                "error_message": None,
            }

        buy_orders = payload.get("buy", [])
        buy_price, buy_status = self._calculate_buy_price(buy_orders)

        # ── ACTIVE: avg trade from statistics endpoint ───────────────────────
        # To switch to listing-price fallback if v1 goes down:
        #   1. Comment out these two lines
        #   2. Uncomment the FALLBACK block below
        sell_price  = avg_trade
        sell_status = avg_status

        # ── FALLBACK: listing-price sell (uncomment if v1 statistics goes down)
        #   1. Comment out the two lines in the ACTIVE block above
        #   2. Uncomment these two lines
        # sell_orders = payload.get("sell", [])
        # sell_price, sell_status = self._calculate_sell_price_from_orders(sell_orders)

        # Ducat/plat ratio uses sell price as the denominator
        ducat_plat_ratio = None
        ducats = self.get_ducats(slug)
        if sell_price and sell_price > 0 and ducats and ducats > 0:
            ducat_plat_ratio = round(ducats / sell_price, 2)

        return {
            "sell_price": sell_price,
            "buy_price": buy_price,
            "ducat_plat_ratio": ducat_plat_ratio,
            "sell_status": sell_status,
            "buy_status": buy_status,
            "error_message": None,
        }

    def _fetch_avg_trade(self, slug: str) -> Tuple[Optional[float], str]:
        """
        Fetch volume-weighted average trade price from /v1/items/{slug}/statistics.

        Uses 48h data. Falls back to 90days if 48h has no entries.
        Returns (price, status).
        """
        try:
            response = requests.get(
                f"{V1_BASE_URL}/items/{slug}/statistics",
                headers=HEADERS,
                timeout=15,
            )
            self._last_request_time = time.time()

            if response.status_code != 200:
                return None, "no_data"

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
                return round(wa_price, 1), "ok"

            return None, "no_data"

        except Exception:
            return None, "no_data"

    def _calculate_sell_price_from_orders(self, sell_orders: list) -> Tuple[Optional[float], str]:
        """
        Fallback sell price from /top listing orders.

        Used only if the v1 statistics endpoint is unavailable.
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

    def _calculate_buy_price(self, buy_orders: list) -> Tuple[Optional[float], str]:
        """
        Calculate buy price from top orders.

        Filter to ingame users only (matching WFM's "In Game" filter), then
        take the highest offer. This answers "what's the most plat I can get
        right now from someone actively in the game."

        Returns:
            (price, status)
        """
        prices = [
            o["platinum"] for o in buy_orders
            if "platinum" in o and o.get("user", {}).get("status") == "ingame"
        ]

        if not prices:
            return None, "no_data"

        return float(max(prices)), "ok"

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

    # --------------------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------------------

    def _no_data_result(self, message: str = "No market data available") -> Dict:
        return {
            "sell_price": None,
            "buy_price": None,
            "ducat_plat_ratio": None,
            "sell_status": "no_data",
            "buy_status": "no_data",
            "error_message": message,
        }

    def _error_result(self, message: str) -> Dict:
        return {
            "sell_price": None,
            "buy_price": None,
            "ducat_plat_ratio": None,
            "sell_status": "error",
            "buy_status": "error",
            "error_message": message,
        }

    def clear_price_cache(self):
        """Clear the in-memory price cache (e.g. for manual refresh)."""
        self._price_cache.clear()
        print("Price cache cleared.")

    def get_cache_stats(self) -> Dict:
        """Return stats about the current price cache state."""
        total = len(self._price_cache)
        now = datetime.now()
        fresh = sum(
            1 for v in self._price_cache.values()
            if (now - v["timestamp"]) < timedelta(minutes=PRICE_CACHE_TTL_MINUTES)
        )
        return {
            "total_cached": total,
            "fresh_entries": fresh,
            "stale_entries": total - fresh,
            "ttl_minutes": PRICE_CACHE_TTL_MINUTES,
        }