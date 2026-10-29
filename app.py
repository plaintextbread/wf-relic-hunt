"""
app.py
Warframe Relic Farming Tool — Streamlit UI
"""

import time
from datetime import datetime, timezone
import streamlit as st
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import relic_data
import price_fetcher
from market_client import MarketClient

# ==============================================================================
# PAGE CONFIG
# ==============================================================================
st.set_page_config(
    page_title="Warframe Relic Farming Tool",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ==============================================================================
# CSS
# ==============================================================================
st.markdown("""
<style>
.stApp { background-color: #0E0E0E; color: #E0E0E0; }
h1, h2, h3 { color: #D8A11B !important; letter-spacing: 0.04em; }
hr { border-color: #2E2E2E !important; }

.stButton > button {
    background-color: #1A1A1A; color: #D8A11B;
    border: 1px solid #D8A11B; border-radius: 4px; font-size: 0.8rem;
}
.stButton > button:hover { background-color: #D8A11B; color: #0E0E0E; }

.stDataFrame { border: 1px solid #2E2E2E; border-radius: 4px; }
.stSelectbox label, .stMultiSelect label, .stToggle label {
    color: #D0D0D0 !important; font-size: 0.8rem;
}
[data-testid="stToggle"],
[data-testid="stToggle"] > div,
[data-testid="stWidgetLabel"] {
    background-color: transparent !important;
}
[data-testid="stToggle"] p,
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label {
    color: #D0D0D0 !important;
}
.stCaption, [data-testid="stCaptionContainer"] { color: #D0D0D0 !important; }
[data-testid="stMetricLabel"] { color: #D0D0D0 !important; }
[data-testid="stMetricValue"] { color: #D8A11B !important; }
.stProgress > div > div { background-color: #D8A11B !important; }
/* Remove text from inside the progress bar */
.stProgress p { display: none; }
.stAlert { background-color: #1A1400 !important; border-left: 4px solid #D8A11B !important; }

.fetch-status { color: #E0E0E0; font-size: 0.85rem; font-family: monospace; margin: 2px 0; }
.fetch-timer  { color: #4EC9C9; font-size: 0.85rem; margin: 2px 0; }
.wf-teal  { color: #4EC9C9; font-weight: 600; }
.wf-gold  { color: #D8A11B; font-weight: 600; }
.wf-muted { color: #D0D0D0; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)

# ==============================================================================
# SESSION STATE
# ==============================================================================
for key, default in [
    ("initialized",   False),
    ("parts",         {}),
    ("relics",        {}),
    ("prices",        {}),
    ("jump_to_relic", None),
    ("fetch_state",                  None),   # dict while a live fetch is in progress, else None
    ("fetch_completed_this_session", False),  # True once a live fetch finishes this session
    ("seen_intro",                   False),  # True after the help dialog has been dismissed once
    ("notice",                       None),   # warning text from a failed fetch/refresh, shown under the header
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ==============================================================================
# HELPERS
# ==============================================================================
@st.cache_resource
def get_market_client():
    return MarketClient()


def _scheduled_price_refresh():
    """Background job: re-check the relic rotation, then force a full price fetch.

    Uses relic_data.refresh() (not .load()) so vault rotations propagate — otherwise
    the job would re-price a frozen slug list from the stale disk cache. refresh()
    re-checks the DE drop table (which decides the active relic set and holds every
    relic's contents), downloading it only when its version stamp changed. It also
    re-downloads the WFM manifest if a reward name can't be resolved to a slug.

    fetch_prices(force=True) merges into the existing price cache rather than
    replacing it, so a run that fails (warframe.market unreachable) leaves the last
    good prices in place.
    """
    client = get_market_client()
    parts, relics = relic_data.refresh(client)
    if not parts:
        print("Scheduled refresh: no relic data; skipping the price fetch.")
        return
    price_fetcher.fetch_prices(parts, relics, client, force=True)


@st.cache_resource
def _start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _scheduled_price_refresh,
        CronTrigger(hour="0,12", minute=0, timezone="UTC"),
        id="price_refresh",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    return scheduler


_start_scheduler()


def _slug_to_name_map(parts: dict, relics: dict) -> dict:
    """Build a slug → display name lookup for the fetch status display."""
    m = {slug: p["name"] for slug, p in parts.items()}
    for relic_name, r in relics.items():
        slug = r.get("relic_wfm_slug", "")
        if slug:
            m[slug] = relic_name
    return m


def wiki_url(display_name: str) -> str:
    """
    Convert a display name to a wiki.warframe.com URL.
    Parts:  "Nikana Prime Blueprint" -> /w/Nikana_Prime
    Relics: "Axi A20"               -> /w/Axi_A20
    """
    if any(display_name.startswith(t) for t in ("Lith ", "Meso ", "Neo ", "Axi ")):
        return "https://wiki.warframe.com/w/" + display_name.replace(" ", "_")
    suffixes = [
        " Neuroptics Blueprint", " Chassis Blueprint", " Systems Blueprint",
        " Neuroptics", " Chassis", " Systems", " Blueprint",
        " Barrel", " Stock", " Receiver", " Grip", " Handle", " Blade",
        " Head", " Guard", " Upper Limb", " Lower Limb", " String",
        " Link", " Carapace", " Cerebrum", " Disc",
    ]
    name = display_name
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return "https://wiki.warframe.com/w/" + name.replace(" ", "_")


def mission_wiki_url(location_str: str) -> str:
    """
    Build a wiki URL for a mission node.
    "Sedna/Hydron (Defense), Rotation B"    -> /w/Hydron
    "Elite Sanctuary Onslaught, Rotation C" -> /w/Elite_Sanctuary_Onslaught
    Apostrophes etc. are percent-encoded.
    """
    import urllib.parse
    loc = location_str
    if "/" in loc:
        loc = loc.split("/", 1)[1]
    node = loc.split("(")[0].split(",")[0].strip()
    wiki_slug = urllib.parse.quote(node.replace(" ", "_"), safe="_")
    return "https://wiki.warframe.com/w/" + wiki_slug


def wfm_url(slug: str) -> str:
    return f"https://warframe.market/items/{slug}"


def render_footer():
    st.divider()
    st.markdown(
        "<span class='wf-muted'>Relic data: DE drop tables via drops.warframestat.us · "
        "Prices and ducats: warframe.market · "
        "Not affiliated with Digital Extremes.</span>",
        unsafe_allow_html=True,
    )


def load_relic_data():
    with st.spinner("Loading relic data..."):
        parts, relics = relic_data.load(get_market_client())
    st.session_state.parts  = parts
    st.session_state.relics = relics


# Slugs to fetch per Streamlit rerun. Equal to the fetch's abort probe, so a fetch
# whose first batch is all errors is aborted here exactly as in the scheduler.
FETCH_BATCH_SIZE = price_fetcher.ABORT_PROBE_SIZE

# Seconds per item for the time estimate: one request per item, spaced
# market_client.REQUEST_INTERVAL_SECONDS apart, plus response time (measured
# 2026-10-29: 2.4-2.6s per item over 3 live calls; it was ~2.8s with the /top request).
SECONDS_PER_ITEM = 2.5


def load_prices(force_refresh=False):
    """
    Start a progressive price fetch. Sets fetch_state in session state and
    returns immediately — the actual fetching happens in advance_fetch() which
    is called each rerun until the fetch is complete.

    The cache file is never deleted: advance_fetch() merges what it fetches into
    the cache loaded here, so a failed refresh leaves the last good prices in place.
    """
    st.session_state.notice = None
    slugs = price_fetcher._collect_slugs(st.session_state.parts, st.session_state.relics)
    if not slugs:
        st.session_state.fetch_state = None
        return

    cached = price_fetcher._load_cache()
    name_map = _slug_to_name_map(st.session_state.parts, st.session_state.relics)
    if cached:
        st.session_state.prices = dict(cached)  # shown while the fetch runs

    if not force_refresh and price_fetcher._is_cache_fresh():
        # Fresh by age isn't enough: a cache built before a relic rotation changed
        # lacks the new parts, which the default "hide parts with no price" toggle
        # then silently drops. Fetch just the missing slugs (failed ones count as
        # missing: an error entry is not data).
        missing = price_fetcher.missing_slugs(slugs, cached)
        if not missing:
            st.session_state.fetch_state = None
            return
        st.session_state.fetch_state = {
            "slugs":     missing,
            "done":      0,
            "start":     time.time(),
            "name_map":  name_map,
            "topup":     True,
            "cache_fetched_at": price_fetcher.cache_fetched_at(),
            "old":       cached,
            "new":       {},
        }
        return

    st.session_state.fetch_state = {
        "slugs":     slugs,
        "done":      0,
        "start":     time.time(),
        "name_map":  name_map,
        "old":       cached,
        "new":       {},
    }


def advance_fetch():
    """
    Fetch the next batch of slugs and render the progress banner.
    Does NOT call st.rerun() — the caller at the bottom of the script does that
    so the views below can render with partial data first.

    Results are merged, never overwritten: a failed request keeps the slug's
    previous price. If the first batch is all errors the fetch is aborted and
    nothing is saved; likewise if it ends with nothing but errors.
    """
    fs     = st.session_state.fetch_state
    slugs  = fs["slugs"]
    done   = fs["done"]
    total  = len(slugs)
    client = get_market_client()
    if total == 0:
        st.session_state.fetch_state = None
        return

    # Fetch the next batch before rendering so the count shown is up to date
    batch = slugs[done : done + FETCH_BATCH_SIZE]
    for slug in batch:
        result = client.get_prices(slug)
        fs["new"][slug] = result
        st.session_state.prices = price_fetcher.merge(st.session_state.prices, {slug: result})
    fs["done"] += len(batch)

    if done == 0 and price_fetcher.first_batch_failed(list(fs["new"].values())):
        cause = price_fetcher.describe_failure(fs["new"].values())
        print(f"Price fetch aborted: warframe.market unreachable ({cause}); price cache left untouched.")
        st.session_state.prices = fs["old"]
        st.session_state.fetch_state = None
        st.session_state.notice = (
            f"warframe.market unreachable ({cause}). Prices were not updated; "
            "showing the last saved prices."
        )
        return

    if fs["done"] >= total:
        print(f"Price fetch finished: {price_fetcher.summarize(fs['new'])}.")
        if price_fetcher.has_usable_results(fs["new"]):
            merged = price_fetcher.merge(fs["old"], fs["new"])
            price_fetcher._save_cache(merged, fetched_at=fs.get("cache_fetched_at"))
            st.session_state.prices = merged
            # A top-up only fills gaps; the rest of the cache isn't "just updated".
            st.session_state.fetch_completed_this_session = not fs.get("topup")
        else:
            st.session_state.prices = fs["old"]
            st.session_state.notice = "No prices could be fetched from warframe.market; prices were not updated."
        st.session_state.fetch_state = None

    # Render progress banner (uses updated done count)
    new_done  = fs["done"]
    est_secs  = int(total * SECONDS_PER_ITEM)
    elapsed   = int(time.time() - fs["start"])
    remaining = max(0, est_secs - elapsed)

    if fs.get("topup"):
        st.warning(
            f"⏳ Fetching prices for **{total} new items** missing from the cache. "
            f"Estimated time: **{est_secs // 60}m {est_secs % 60}s**.",
            icon="⚠️",
        )
    else:
        st.warning(
            f"⏳ Fetching live prices for **{total} items** from warframe.market. "
            f"Estimated time: **{est_secs // 60}m {est_secs % 60}s**. "
            f"Prices are then cached for 12 hours.",
            icon="⚠️",
        )
    last_name = fs["name_map"].get(slugs[new_done - 1], slugs[new_done - 1]) if new_done > 0 else "—"
    st.markdown(
        f"<div class='fetch-status'>Fetching: {new_done} / {total} — {last_name}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='fetch-timer'>"
        f"⏱ Elapsed {elapsed // 60}m {elapsed % 60}s"
        f" · Remaining ~{remaining // 60}m {remaining % 60}s"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.progress(min(new_done / total, 1.0))


# ==============================================================================
# FIRST LOAD
# ==============================================================================
if not st.session_state.initialized:
    load_relic_data()
    if st.session_state.parts:  # no relic data -> nothing to price (error banner below)
        load_prices()
    st.session_state.initialized = True

# ==============================================================================
# HELP DIALOG
# ==============================================================================
@st.dialog("How to use this tool")
def show_intro():
    st.session_state.seen_intro = True
    st.markdown("""
This tool helps you find prime parts to farm and figure out what relics to crack.

The **Prime Parts** table lists all currently unvaulted prime parts. A few ways to use it:

- Search for a specific part to see its sell price and which relic it comes from, then use the second and third tables to learn more about that relic.
- Filter by relic tier to see what's available from the relics you already have.
- Sort by Ducat/Plat to find the most efficient parts to farm for ducats. A higher ratio means the part pays out more ducats relative to what it sells for on the market.

The **Relic EV** table shows the expected value of cracking a relic at each refinement tier, so you can decide how many void traces to invest in it or whether you want to just sell it unopened.

**Relic Detail** (select a relic from the dropdown in the EV table) shows the parts inside a relic, and which missions drop that relic.
""")

# ==============================================================================
# HEADER
# ==============================================================================
st.markdown(
    "<h1 style='display:flex;align-items:center;gap:0.4em;'>"
    "<img src='app/static/relic.png' style='height:1.1em;vertical-align:middle;'>"
    " Warframe Relic Farming Tool</h1>",
    unsafe_allow_html=True,
)
st.caption(
    "Unvaulted Prime parts with live warframe.market prices. "
    "Find what's worth farming, and whether to sell or crack your relics."
)

col_age, col_rp, col_rr, col_help = st.columns([5, 1, 1, 1])
with col_age:
    age = price_fetcher.cache_age_minutes()
    if st.session_state.get("fetch_completed_this_session"):
        st.caption("Prices last updated just now · Cache expires after 12 hours")
    elif age is not None:
        age_h = int(age) // 60
        age_m = int(age) % 60
        age_str = f"{age_h}h {age_m}m" if age_h > 0 else f"{age_m}m"
        st.caption(f"Prices last updated {age_str} ago · Cache expires after 12 hours")
    elif st.session_state.fetch_state is not None:
        st.caption("Prices fetching...")
    drops_info = relic_data.drops_status()
    if drops_info and drops_info.get("modified"):
        rotation_date = datetime.fromtimestamp(drops_info["modified"] / 1000, tz=timezone.utc)
        st.caption(
            f"Relic rotation from DE drop table dated {rotation_date:%Y-%m-%d} "
            f"· {len(st.session_state.relics)} active relics"
        )
with col_rp:
    if st.button("🔄 Refresh Prices", use_container_width=True):
        load_prices(force_refresh=True)
        st.rerun()
with col_rr:
    if st.button("🔄 Refresh Relics", use_container_width=True):
        with st.spinner("Re-checking the drop table..."):
            parts, relics = relic_data.refresh(get_market_client())
        if parts:
            st.session_state.parts  = parts
            st.session_state.relics = relics
            load_prices(force_refresh=True)
        else:
            # Never replace data we already have with nothing.
            st.session_state.notice = "Couldn't re-check the drop table; keeping the relic data already loaded."
        st.rerun()
with col_help:
    if st.button("? How to use", use_container_width=True):
        show_intro()

if not st.session_state.get("seen_intro") and st.runtime.exists():
    show_intro()

if st.session_state.fetch_state is not None:
    advance_fetch()

if st.session_state.notice:
    st.warning(st.session_state.notice, icon="⚠️")

st.divider()

if not st.session_state.parts:
    st.error(
        "Relic data is unavailable right now: the DE drop table (drops.warframestat.us) "
        "couldn't be reached and there is no saved copy. Try **Refresh Relics** in a few minutes.",
        icon="⚠️",
    )
    render_footer()
    st.stop()

# ==============================================================================
# VIEW 1 — PRIME PARTS TABLE
# ==============================================================================
st.subheader("Unvaulted Prime Parts")
st.caption(
    "Ranked by sell price. WFM and Wiki links open in a new tab."
)

client = get_market_client()
rows = []
for slug, part in st.session_state.parts.items():
    pd_   = st.session_state.prices.get(slug, {})
    sell  = pd_.get("sell_price")
    ducats = client.get_ducats(slug)
    ducat_ratio = round(ducats / sell, 1) if sell and sell > 0 and ducats > 0 else None

    rows.append({
        "Part":          part["name"],
        "Sell Price":    sell,
        "Ducats":        ducats if ducats > 0 else None,
        "Ducat/Plat":    ducat_ratio,
        "Rarity":        part["rarity"],
        "Source Relics": ", ".join(sorted(part["source_relics"])),
        "WFM":           wfm_url(slug),
        "Wiki":          wiki_url(part["name"]),
        "_slug":         slug,
    })

df_parts = pd.DataFrame(rows)

hide_no_price = st.toggle("Hide parts with no price data", value=True)

fc1, fc2, fc3, fc4 = st.columns(4)
with fc1:
    search_query = st.text_input("Search parts", placeholder="Search by part name...")
with fc2:
    rarity_filter = st.multiselect(
        "Filter by rarity",
        options=["Common", "Uncommon", "Rare"],
        default=["Common", "Uncommon", "Rare"],
    )
with fc3:
    tier_filter = st.multiselect(
        "Filter by relic tier",
        options=["Lith", "Meso", "Neo", "Axi"],
        default=["Lith", "Meso", "Neo", "Axi"],
    )
with fc4:
    sort_by = st.selectbox(
        "Sort by",
        options=["Sell Price", "Ducats", "Ducat/Plat", "Part"],
        index=0,
    )

df_show = df_parts.copy()
if hide_no_price:
    df_show = df_show[df_show["Sell Price"].notna()]
if search_query:
    df_show = df_show[df_show["Part"].str.contains(search_query, case=False, na=False)]
if rarity_filter:
    df_show = df_show[df_show["Rarity"].isin(rarity_filter)]
if tier_filter:
    # .astype(bool): on an empty frame (e.g. no prices yet, all hidden) apply() returns an
    # empty object-dtype Series, which pandas would treat as a column selection and drop every column.
    df_show = df_show[df_show["Source Relics"].apply(
        lambda s: any(s.startswith(t) or f", {t}" in s for t in tier_filter)
    ).astype(bool)]
df_show = df_show.sort_values(sort_by, ascending=(sort_by == "Part"), na_position="last")
df_show = df_show.drop(columns=["_slug"])

st.dataframe(
    df_show,
    use_container_width=True,
    hide_index=True,
    height=500,
    column_config={
        "Part":          st.column_config.TextColumn("Part", width="large"),
        "Sell Price":    st.column_config.NumberColumn("Sell Price ⬡", format="%.1f"),
        "Ducats":        st.column_config.NumberColumn("Ducats"),
        "Ducat/Plat":    st.column_config.NumberColumn("Ducat/Plat", format="%.1f"),
        "Rarity":        st.column_config.TextColumn("Rarity", width="small"),
        "Source Relics": st.column_config.TextColumn("Source Relics", width="medium"),
        "WFM":           st.column_config.LinkColumn("WFM", display_text="WFM ↗", width="small"),
        "Wiki":          st.column_config.LinkColumn("Wiki", display_text="Wiki ↗", width="small"),
    },
)
st.caption(
    f"Showing {len(df_show)} of {len(df_parts)} parts. "
    "Sell Price: volume-weighted average of closed trades on warframe.market (48h window, 90d fallback). Not necessarily a listing price. "
)

st.divider()

# ==============================================================================
# VIEW 2 — RELIC EV PANEL
# ==============================================================================
st.subheader("Relic EV Panel")
st.caption(
    "Expected plat per crack at each refinement tier vs. selling unopened. "
    "EV excludes reward items with no price data (lower-bound estimate)."
)

STATES = ["Intact", "Exceptional", "Flawless", "Radiant"]
relic_names = sorted(st.session_state.relics.keys())

if "relic_selector" not in st.session_state:
    st.session_state["relic_selector"] = None

ev_fc1, ev_fc2, ev_fc3 = st.columns(3)
with ev_fc1:
    selected_relic = st.selectbox(
        "Highlighted relic",
        options=relic_names,
        index=None,
        placeholder="Select a relic...",
        key="relic_selector",
    )
with ev_fc2:
    ev_tier_filter = st.multiselect(
        "Filter by relic tier",
        options=["Lith", "Meso", "Neo", "Axi"],
        default=["Lith", "Meso", "Neo", "Axi"],
        key="ev_tier_filter",
    )
with ev_fc3:
    ev_verdict_filter = st.multiselect(
        "Filter by verdict",
        options=["Sell unopened", "Crack Intact", "Crack Exceptional", "Crack Flawless", "Crack Radiant"],
        default=[],
        placeholder="All verdicts",
        key="ev_verdict_filter",
    )

# Build EV rows
ev_rows = []
for relic_name, relic in sorted(st.session_state.relics.items()):
    relic_slug    = relic.get("relic_wfm_slug", "")
    relic_pd      = st.session_state.prices.get(relic_slug, {})
    unopened_sell = relic_pd.get("sell_price")

    ev_by_state = {}
    for state in STATES:
        ev = sum(
            st.session_state.prices.get(r["slug"], {}).get("sell_price", 0)
            * (r["chance"] / 100)
            for r in relic["states"].get(state, [])
            if st.session_state.prices.get(r["slug"], {}).get("sell_price")
        )
        ev_by_state[state] = round(ev, 1) if ev > 0 else None

    candidates = {}
    if unopened_sell:
        candidates["Sell unopened"] = unopened_sell
    for state in STATES:
        if ev_by_state[state]:
            candidates[f"Crack {state}"] = ev_by_state[state]
    verdict = max(candidates, key=candidates.get) if candidates else "—"

    ev_rows.append({
        "Relic":          relic_name,
        "Sell Unopened":  unopened_sell,
        "EV Intact":      ev_by_state["Intact"],
        "EV Exceptional": ev_by_state["Exceptional"],
        "EV Flawless":    ev_by_state["Flawless"],
        "EV Radiant":     ev_by_state["Radiant"],
        "Verdict":        verdict,
        "WFM":            wfm_url(relic_slug) if relic_slug else None,
        "_selected":      relic_name == selected_relic,
    })

df_ev = pd.DataFrame(ev_rows)

# Apply filters (never hide the highlighted relic)
if ev_tier_filter:
    df_ev = df_ev[
        df_ev["_selected"] |
        df_ev["Relic"].apply(lambda r: any(r.startswith(t + " ") for t in ev_tier_filter))
    ]
if ev_verdict_filter:
    df_ev = df_ev[df_ev["_selected"] | df_ev["Verdict"].isin(ev_verdict_filter)]

# Selected relic always sorted to top
df_ev["_sort_key"] = (~df_ev["_selected"]).astype(int)
df_ev = df_ev.sort_values(
    ["_sort_key", "Relic"], ascending=[True, True], na_position="last"
).drop(columns=["_sort_key"])

# Drop _selected for display
df_ev_display = df_ev.drop(columns=["_selected"])


st.dataframe(
    df_ev_display,
    use_container_width=True,
    hide_index=True,
    height=400,
    column_config={
        "Relic":          st.column_config.TextColumn("Relic", width="small"),
        "Sell Unopened":  st.column_config.NumberColumn("Sell Unopened ⬡", format="%.0f"),
        "EV Intact":      st.column_config.NumberColumn("EV Intact",        format="%.1f ⬡"),
        "EV Exceptional": st.column_config.NumberColumn("EV Exceptional",   format="%.1f ⬡"),
        "EV Flawless":    st.column_config.NumberColumn("EV Flawless",      format="%.1f ⬡"),
        "EV Radiant":     st.column_config.NumberColumn("EV Radiant",       format="%.1f ⬡"),
        "Verdict":        st.column_config.TextColumn("Verdict", width="medium"),
        "WFM":            st.column_config.LinkColumn("WFM", display_text="WFM ↗", width="small"),
    },
)

st.divider()

# ==============================================================================
# VIEW 3 — RELIC DETAIL
# ==============================================================================
st.subheader("Relic Detail")
st.caption("Reward breakdown and farm locations for the selected relic.")

if not selected_relic:
    st.caption("Select a relic in the EV panel above to see its reward breakdown and farm locations.")
elif selected_relic in st.session_state.relics:
    relic      = st.session_state.relics[selected_relic]
    relic_slug = relic.get("relic_wfm_slug", "")

    title_col, wiki_col = st.columns([6, 1])
    with title_col:
        st.markdown(
            f"### <span class='wf-gold'>{selected_relic}</span>",
            unsafe_allow_html=True,
        )
    with wiki_col:
        st.link_button("Wiki ↗", wiki_url(selected_relic), use_container_width=True)

    # Look up the verdict for this relic from the already-computed ev_rows
    relic_verdict = next(
        (row["Verdict"] for row in ev_rows if row["Relic"] == selected_relic), "—"
    )
    # Extract the recommended crack state if verdict is "Crack X"
    recommended_state = (
        relic_verdict.replace("Crack ", "") if relic_verdict.startswith("Crack ") else None
    )

    col_rewards, col_locations = st.columns(2)

    with col_rewards:
        if recommended_state:
            verdict_col, toggle_col = st.columns([3, 2])
            with verdict_col:
                st.markdown(
                    f"**Rewards** · Verdict: <span class='wf-gold'>{relic_verdict}</span>",
                    unsafe_allow_html=True,
                )
            with toggle_col:
                show_recommended = st.toggle(
                    f"Show {recommended_state} drop chances",
                    value=False,
                    key=f"detail_toggle_{selected_relic}",
                )
            display_state = recommended_state if show_recommended else "Intact"
        else:
            st.markdown("**Rewards** (Intact drop chances)")
            display_state = "Intact"

        state_label = display_state if display_state == "Intact" else f"{display_state} (recommended)"
        st.caption(f"Drop chances shown: {state_label}")

        reward_rows = []
        for r in sorted(relic["states"].get(display_state, []), key=lambda x: x["chance"]):
            pd_   = st.session_state.prices.get(r["slug"], {})
            sell  = pd_.get("sell_price")
            ducats = client.get_ducats(r["slug"])
            reward_rows.append({
                "Part":          r["name"],
                "Rarity":        r["rarity"],
                "Drop %":        r["chance"],
                "Sell Price":    sell,
                "Ducats":        ducats if ducats > 0 else None,
                "WFM":           wfm_url(r["slug"]) if r["slug"] else None,
                "Wiki":          wiki_url(r["name"]),
            })

        st.dataframe(
            pd.DataFrame(reward_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Part":          st.column_config.TextColumn("Part", width="large"),
                "Rarity":        st.column_config.TextColumn("Rarity", width="small"),
                "Drop %":        st.column_config.NumberColumn("Drop %",      format="%.2f%%"),
                "Sell Price":    st.column_config.NumberColumn("Sell ⬡", format="%.1f"),
                "Ducats":        st.column_config.NumberColumn("Ducats"),
                "WFM":           st.column_config.LinkColumn("WFM",  display_text="WFM ↗",  width="small"),
                "Wiki":          st.column_config.LinkColumn("Wiki", display_text="Wiki ↗", width="small"),
            },
        )

    with col_locations:
        locations = relic.get("locations", [])
        st.markdown(f"**Farm locations** ({len(locations)} sources)")
        if not locations:
            st.info("No location data available.")
        else:
            loc_rows = []
            for loc in locations:
                mission = loc.get("location", "")
                loc_rows.append({
                    "Mission":  mission,
                    "Rarity":   loc.get("rarity", ""),
                    "Chance %": round(loc.get("chance", 0), 2),
                    "Wiki":     mission_wiki_url(mission),
                })
            df_loc = (pd.DataFrame(loc_rows)
                        .sort_values("Chance %", ascending=False))
            st.dataframe(
                df_loc,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Mission":  st.column_config.TextColumn("Mission", width="large"),
                    "Rarity":   st.column_config.TextColumn("Rarity",  width="small"),
                    "Chance %": st.column_config.NumberColumn("Chance %", format="%.2f%%"),
                    "Wiki":     st.column_config.LinkColumn("Wiki", display_text="Wiki ↗", width="small"),
                },
            )

# ==============================================================================
# FOOTER
# ==============================================================================
render_footer()

# Trigger next batch rerun after full page renders
if st.session_state.fetch_state is not None:
    st.rerun()