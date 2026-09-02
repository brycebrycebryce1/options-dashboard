"""Options and market analytics dashboard.

Option chains come from CBOE's free delayed feed, price history and the rates
from Yahoo, filings from SEC EDGAR. Delayed by roughly 15 minutes (quotes) and
by a night (open interest); the Refresh button in the sidebar drops the market
caches and re-pulls. SEC filings are cached for six hours: they change quarterly.

Sections, in order down the page:

    1. Expected move          -- what the chain charges for the move to expiry
    2. Implied distribution   -- Breeden-Litzenberger risk-neutral density
    3. Gamma exposure         -- dealer positioning and the zero-gamma flip
    4. Volatility surface     -- term structure, skew, earnings, index, arbitrage
    5. Volatility premium     -- implied vs subsequently realised
    6. Insider trades         -- SEC Form 4
    7. Institutional activity -- SEC 13F, filtered to this ticker
    8. Fundamentals           -- SEC XBRL company facts
    9. Cointegration screen   -- pairs, at the bottom because it is not per-ticker

Colour convention, used everywhere: green is calls and buying, red is puts and
selling, periwinkle is the lead series where there is no direction to encode and
lilac is the second one.

Two of the sections exist to check the others rather than to add information:
the model-free moment cross-check under the density, and the static arbitrage
checks under the surface. Both read the quotes by a route that shares no code
with the pipeline they are checking, which is the only way a silent error in it
would ever surface.

Everything drawn here is also handed to a `report.Report`, so the sidebar can
rebuild the whole page as a PDF from the source figures rather than from a
screenshot.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

import benchmark
import bkm
import cboe
import data
import earnings as earn
import edgar
import expiries
import expmove
import gex
import mdreport
import noarb
import pairs as pairs_mod
import prep
import report as report_mod
import rnd
import surface as surf
import vol

# The browser tab carries the ticker. set_page_config has to be the first
# Streamlit call and can only run once, so the symbol is read straight out of
# session state -- the text input below writes it there under the same key, and
# typing a new ticker triggers a rerun that lands back here with the new value.
TAB_SYMBOL = str(st.session_state.get("ticker", "NVDA")).strip().upper() or "NVDA"
st.set_page_config(page_title=f"Market Analytics · {TAB_SYMBOL}", layout="wide")

# Directional colours carry meaning; the neutral ones deliberately do not.
GREEN = "#22c55e"  # calls, buys, added positions
RED = "#ef4444"  # puts, sells, trimmed positions
BLUE = "#A9C7EE"  # lead series where there is no direction to encode
LILAC = "#EAC5FF"  # second neutral series
TEAL = "#5EEAD4"  # third neutral series, and this ticker's implied-vol reference
AMBER = "#f59e0b"  # the market-wide (VIX) reference line
NEUTRAL = "#94a3b8"  # spot lines, zero lines, gridwork

BLUE_FILL = "rgba(169, 199, 238, {alpha})"

MARKET_TTL = 900  # 15 minutes, matching the quote delay
FILING_TTL = 6 * 3600  # filings change quarterly; no point re-pulling them

# Fixed because they have sensible answers and do not belong in a sidebar.
SMOOTHING = 1.0  # smile fit residuals sized to the quoted bid-ask spread
GEX_WINDOW = 0.20  # gamma profile spans +/- 20% of spot
HISTORY_PERIOD = "3y"  # long enough for a realised-vol percentile, short enough to be current
INSIDER_MONTHS = 12

# Room above the plotting area for a left-aligned title on one line and the
# horizontal legend on another. Without it plotly stacks the two on top of each
# other and the modebar lands across both.
TITLE_PAD = 86
MODEBAR_PAD = 12  # a little breathing room at the right, where the modebar sits


def why(exc: BaseException) -> str:
    """An exception rendered as something a reader can act on.

    Not every exception carries a message. A bare ``assert`` inside a library
    raises an AssertionError whose ``str`` is the empty string, and interpolating
    that produces a warning box that says "Could not load X:" and then stops --
    which reads as a rendering bug rather than as the failure it is. Falling back
    to the class name always leaves something to search for.
    """
    text = str(exc).strip()
    return text or type(exc).__name__


def layout(fig: go.Figure, height: int = 420, **kwargs) -> go.Figure:
    title = kwargs.pop("title", None)
    fig.update_layout(
        height=height,
        margin=dict(t=TITLE_PAD, b=45, l=10, r=MODEBAR_PAD),
        # Left-aligned and pinned to the top of the container, clear of both the
        # legend below it and the modebar at the top right.
        title=dict(text=title, x=0, xanchor="left", yref="container", y=0.97, yanchor="top",
                   pad=dict(l=8)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode=kwargs.pop("hovermode", "x unified"),
        **kwargs,
    )
    return fig


def money(value: float, digits: int = 2) -> str:
    """Compact dollar formatting: $1.2bn, $340m, $12k."""
    if not np.isfinite(value):
        return "n/a"
    if value == 0:
        return "$0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for cut, suffix in ((1e12, "tn"), (1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if value >= cut:
            return f"{sign}${value / cut:,.{digits}f}{suffix}"
    return f"{sign}${value:,.{digits}f}"


# Streamlit renders markdown with LaTeX enabled, so two unescaped "$" on one
# line are read as math delimiters and the text between them disappears. Use
# these inside st.markdown / st.caption; st.metric does not parse markdown and
# takes the plain versions.
def money_md(value: float, digits: int = 2) -> str:
    return money(value, digits).replace("$", "\\$")


def price_md(value: float) -> str:
    return rf"\${value:,.2f}"


# --------------------------------------------------------------------------
# Cached data access
# --------------------------------------------------------------------------


@st.cache_resource
def _session():
    return data.make_session()


@st.cache_resource
def _cboe_session():
    return cboe.make_session()


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_book(symbol: str) -> tuple[cboe.Book | None, str]:
    """CBOE's whole option book for one underlying, and why not if it is absent.

    One request carries every expiration, so this is fetched once per ticker and
    sliced below, rather than asked for per expiry the way Yahoo has to be. It is
    warmed before the parallel batch runs, deliberately: several chain tasks would
    otherwise reach a cold cache at the same moment and each start its own download
    of the same five megabytes.

    The reason for a failure is carried back rather than raised, because a failure
    here is not fatal -- Yahoo can still serve the chains, and every loader below
    falls back to it. What the reader must not get is that swap happening silently,
    so the caller says so on the page.
    """
    try:
        return cboe.fetch_book(symbol, _cboe_session()), ""
    except Exception as exc:  # noqa: BLE001 - reported, then Yahoo is asked
        return None, why(exc)


def _book(symbol: str) -> cboe.Book | None:
    return load_book(symbol)[0]


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_expirations(symbol: str) -> list[str]:
    book = _book(symbol)
    if book is not None:
        return book.expirations
    return data.fetch_expirations(symbol, _session())


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_spot(symbol: str) -> float:
    # Taken from the same payload as the chains when there is one, so the spot and
    # the strikes are read at one instant rather than a few seconds apart.
    book = _book(symbol)
    if book is not None:
        return book.spot
    return data.fetch_spot(symbol, _session())


@st.cache_data(ttl=FILING_TTL, show_spinner=False)
def load_earnings(symbol: str) -> list:
    """Announcement timestamps. Cached long: a reporting date moves rarely.

    Held for the SEC cache's lifetime rather than the market one because the
    endpoint behind it is a scrape that fails intermittently, and re-asking it
    every fifteen minutes turns an occasional failure into a flickering one.
    """
    return data.fetch_earnings_dates(symbol, _session())


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_rate() -> tuple[float, str]:
    return data.fetch_risk_free_rate(_session())


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_chain(symbol: str, expiration: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    book = _book(symbol)
    if book is not None:
        return book.chain(expiration)
    return data.fetch_raw_chain(symbol, expiration, _session())


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_history(symbol: str, period: str) -> pd.DataFrame:
    return data.fetch_history(symbol, period, _session())


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_closes(symbols: tuple[str, ...], period: str) -> pd.DataFrame:
    return data.fetch_closes(list(symbols), period, _session())


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def run_screen(symbols: tuple[str, ...], period: str, max_half_life: float):
    """Cointegration results for a universe, cached on the inputs.

    The screen is re-run on every rerun so that its output survives clicking
    something else -- caching is what keeps that from costing anything.
    """
    closes = load_closes(symbols, period)
    return closes, pairs_mod.screen_pairs(closes, max_half_life=max_half_life)


@st.cache_data(ttl=FILING_TTL, show_spinner=False)
def load_company(symbol: str) -> tuple[str, str]:
    return edgar.ticker_to_cik(symbol, edgar.default_contact())


@st.cache_data(ttl=FILING_TTL, show_spinner=False)
def load_cusip(symbol: str) -> str | None:
    """The ticker's CUSIP, used only to corroborate 13F holdings. None is fine.

    The shared session is passed here for the same reason as everywhere else,
    and it matters more here than the others. yfinance keeps one process-wide
    object holding the cookie and the crumb Yahoo issues together, and handing
    it a session swaps that cookie jar out while leaving the crumb behind. This
    lookup runs in the same batch as a dozen chain downloads, so omitting the
    session invalidated the pair mid-burst and forced a re-mint -- an extra
    round trip against the one endpoint most likely to be rate limited.
    """
    try:
        return data.fetch_cusip(symbol, _session())
    except Exception:  # noqa: BLE001 - purely an enrichment; never fail the page
        return None


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def load_insiders(symbol: str, months: int) -> pd.DataFrame:
    return edgar.insider_transactions(symbol, edgar.default_contact(), months)


@st.cache_data(ttl=FILING_TTL, show_spinner=False)
def load_fundamentals(symbol: str) -> tuple[pd.DataFrame, str]:
    return edgar.fundamentals(symbol, edgar.default_contact())


@st.cache_data(ttl=FILING_TTL, show_spinner=False)
def load_manager_13f(cik: str) -> list:
    """One manager's last two 13F filings.

    Cached per manager rather than per ticker: the roster scan is the same
    download whatever symbol is on screen, so switching tickers costs nothing
    after the first load.
    """
    return edgar.load_13f(cik, edgar.default_contact(), count=2)


MARKET_CACHES = (
    load_book, load_expirations, load_spot, load_rate, load_chain, load_history, load_closes,
    load_insiders, run_screen,
)

# How many downloads may be in flight at once. Everything this app fetches is
# I/O, so the wall clock was almost entirely time spent waiting on one reply
# before asking for the next. The pool no longer sets the rate either side sees:
# edgar holds the SEC to its published limit with a lock, and data now paces
# Yahoo the same way, so both are governed by an interval between request starts
# rather than by this number. What it still buys is overlap -- a task parked on
# one of those locks would otherwise hold a worker the other could be using.
# Twenty 13F managers are queued in the same batch, which is what makes it worth
# having at all; the twelve Yahoo requests beside them measured about three
# seconds in total whether they ran together or one at a time.
FETCH_WORKERS = 6


def in_parallel(tasks: dict, on_done=None) -> dict:
    """Run independent downloads together, keyed by whatever the caller passed.

    Returns the result for each key, or the exception it raised, so that every
    caller keeps deciding for itself which failures are fatal -- a missing
    option chain stops the page, a missing price history only narrows it.

    Streamlit reads the script run context off the calling thread, so workers
    are seeded with the main thread's. Without it every cached call made from a
    worker misses the cache and warns about a missing context.
    """
    ctx = get_script_run_ctx()

    def seed():
        add_script_run_ctx(threading.current_thread(), ctx)

    out: dict = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, initializer=seed) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for i, fut in enumerate(as_completed(futures), start=1):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                out[key] = exc
            if on_done is not None:
                on_done(i, len(futures), key)
    return out


def snapshot(symbol: str, expiry: str, spot: float, rate: float) -> prep.ExpirySnapshot:
    calls, puts = load_chain(symbol, expiry)
    return prep.build_snapshot(symbol, expiry, calls, puts, spot, rate)


SEEDED_FOR = "_expiries_seeded_for"  # the ticker the pickers currently describe


def remember(key: str, valid: list[str], fallback):
    """Reconcile a remembered widget selection with the options now available.

    Streamlit keeps keyed widget state across reruns, which is what stops the
    expiry pickers resetting themselves on every refresh. The cost is that a
    value left over from a different ticker has to be reconciled by hand, or the
    widget raises on options it no longer contains.

    The seeding is done here rather than through the widget's own ``default=``
    or ``index=``. Streamlit ignores those whenever the key already holds a
    value, and warns that it is doing so -- passing both is the documented way
    to get "created with a default value but also had its value set via the
    Session State API" in the log on every rerun. Owning the state in one place
    removes the ambiguity instead of silencing it.
    """
    current = st.session_state.get(key)
    if isinstance(current, list):
        pruned = [v for v in current if v in valid]
        if pruned != current:
            # The options changed underneath it, so this is a new ticker rather
            # than a deliberate choice. Falling back only when nothing survived
            # leaves a selection the user emptied on purpose alone.
            st.session_state[key] = pruned if pruned else list(fallback)
    elif current not in valid:
        st.session_state[key] = fallback


def seed_expiry_pickers(symbol: str, valid: list[str], chosen: list[str], primary: str):
    """Point the two expiry pickers at a ticker.

    Pruning alone is not enough on a ticker change. Standard listings mean two
    unrelated names share most of their expiration dates, so a selection carried
    over from the last ticker survives the prune intact and the new defaults
    never apply -- which is wrong, because the defaults are specific to the name:
    where its monthlies fall, and whether its earnings sit inside the front of
    the curve. So the pickers are re-seeded whenever the symbol changes, and only
    reconciled against the option list on reruns of the same one.

    This does discard a hand-picked selection when the ticker changes and comes
    back. That is the intended trade: the selection describes a ticker, and
    silently keeping one ticker's dates on another's chain is the failure that
    actually misleads.

    Returns whether the ticker changed, which is the one moment the rest of
    the page needs to know about too.
    """
    if st.session_state.get(SEEDED_FOR) != symbol:
        st.session_state[SEEDED_FOR] = symbol
        st.session_state["surface_expiries"] = list(chosen)
        st.session_state["primary_expiry"] = primary
        return True
    remember("surface_expiries", valid, chosen)
    remember("primary_expiry", valid, primary)
    return False


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------

st.sidebar.header("Settings")
symbol = st.sidebar.text_input("Ticker", value="NVDA", key="ticker").strip().upper()

if not symbol:
    st.info("Enter a ticker in the sidebar to begin.")
    st.stop()

try:
    # Warmed here, on one thread, before anything else asks for it. Both calls below
    # read it, and so does every chain task in the parallel batch further down;
    # letting those race for a cold cache would start the same download several
    # times over.
    _, chain_source_note = load_book(symbol)
    expirations = load_expirations(symbol)
    spot = load_spot(symbol)
except Exception as exc:  # noqa: BLE001 - surface any data problem to the user
    st.error(f"Could not load data for **{symbol}**: {why(exc)}")
    st.stop()

if chain_source_note:
    # The fallback is meant to be invisible in the sense that the page still works,
    # not in the sense that nobody is told. Which feed priced the smile belongs on
    # the page beside everything else this dashboard says about where its numbers
    # came from.
    st.warning(
        f"Option chains are coming from Yahoo rather than CBOE on this load: "
        f"{chain_source_note} Yahoo answers one expiration per request and rate "
        "limits by address, so the page may be slower here and may fail outright "
        "on the hosted copy.",
        icon="⚠️",
    )

auto_rate, rate_source = load_rate()

# Earnings is resolved before the pickers rather than with the other per-expiry
# work below, because it changes what the defaults should be: an announcement
# inside the front of the curve moves the primary onto the expiry that captures
# it. It is cached like the rest of the filing data, so reading it here costs
# nothing on a rerun. Four sections further down use the same value -- the cone
# and the term structure mark it, the density describes a mixture rather than a
# diffusion when it is inside the window, and the volatility premium is
# comparing unlike things when implied carries an event premium that trailing
# realised does not.
earnings_event = earn.next_event(load_earnings(symbol))

default_chosen, default_primary = expiries.default_expiries(expirations, earnings_event)
if seed_expiry_pickers(symbol, expirations, default_chosen, default_primary):
    # A built PDF belongs to the ticker it was built from. Left in session
    # state it outlives the switch, so the new ticker's sidebar offers a
    # finished report on the old one, under a button that says only
    # "Download PDF". Drop it and let the button be rebuilt.
    for stale in ("pdf_bytes", "pdf_name", "pdf_stamp"):
        st.session_state.pop(stale, None)

chosen = st.sidebar.multiselect(
    "Expirations",
    expirations,
    format_func=prep.expiry_label,
    key="surface_expiries",
    help="Drives the volatility surface, the skew metrics and gamma exposure.",
)

# The density's expiry is chosen from the full list, not from the multiselect
# above: tying them together meant adding a date to one picker to make it
# selectable in the other, and a refresh could silently move the selection.
primary = st.sidebar.selectbox(
    "Primary expiration",
    expirations,
    format_func=prep.expiry_label,
    key="primary_expiry",
    help="The expiry the implied distribution and the expected-move headline are built from.",
)

use_auto_rate = st.sidebar.checkbox(
    f"Risk-free {auto_rate:.2%} from {rate_source}",
    value=True,
    help="Re-read from Yahoo on every cache miss, so it follows the bill on its own. "
         "Untick to pin a rate by hand.",
)
rate = auto_rate if use_auto_rate else st.sidebar.number_input(
    "Risk-free rate", 0.0, 0.25, float(auto_rate), 0.0025, format="%.4f"
)

# The action buttons sit here, at the foot of the settings they act on. Refresh
# has to restart the run: the expirations, the spot and the rate above were read
# from cache before this line, so clearing the caches without a rerun would
# leave the top of the page stale while the chains below it were fresh.
if st.sidebar.button("Refresh data", type="primary", width="stretch"):
    for cache in MARKET_CACHES:
        cache.clear()
    # Safe to rerun from here, unlike from the top of the sidebar: the expiry
    # widgets above have already been instantiated this run, so their keyed
    # state is registered and survives the restart. (Streamlit forbids
    # reassigning those keys once the widgets exist, so the state cannot be
    # pinned explicitly -- it has to be preserved by ordering.)
    st.rerun()

build_pdf = st.sidebar.button("Build PDF report", width="stretch")
# Both downloads are filled in at the very end of the script, once every figure
# and table has been registered; the slots reserve their place in the sidebar.
pdf_slot = st.sidebar.empty()
md_slot = st.sidebar.empty()

st.sidebar.caption(
    "Option chains come from CBOE's free delayed feed; price history, the T-bill and "
    "the index series come from Yahoo. During US trading hours quotes are delayed ~15 "
    "minutes. After the close the closing bids and offers keep being served for some "
    "hours, then are blanked overnight, and the page falls back to each strike's "
    "closing print. The line "
    "under the title says which of the three it is showing. Open interest settles "
    "overnight; SEC filings and earnings dates are cached for six hours."
)

# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

wanted = sorted(set(chosen) | {primary})

# The chains and the price history are independent downloads that used to run
# one after another, which on a name with eight expirations was most of the
# page's load time spent waiting. Asked for together they cost about as long as
# the slowest one.
_tasks = {("chain", exp): (lambda e=exp: snapshot(symbol, e, spot, rate)) for exp in wanted}
_tasks["history"] = lambda: load_history(symbol, HISTORY_PERIOD)

# The SEC panels sit at the bottom of the page but their downloads do not have
# to wait for the reader to get there. Warming their caches in this same batch
# costs nothing here -- the workers would otherwise be idle waiting on Yahoo --
# and by the time those sections render they are reading memory. Failures are
# deliberately ignored: each section still calls its own loader and will retry
# and report in its own way.
_tasks["insiders"] = lambda: load_insiders(symbol, INSIDER_MONTHS)
if symbol != benchmark.BENCHMARK_SYMBOL:
    # The index comparison needs the same two lookups for SPY. Its own chain
    # still waits, because which expiry to ask for is not known until the
    # primary one is.
    # One task rather than two: both of these read the same CBOE book, and as
    # separate tasks they reached a cold cache together and each fetched it.
    _tasks["bench"] = lambda: (
        load_book(benchmark.BENCHMARK_SYMBOL),
        load_expirations(benchmark.BENCHMARK_SYMBOL),
        load_spot(benchmark.BENCHMARK_SYMBOL),
    )
_tasks["fundamentals"] = lambda: load_fundamentals(symbol)
_tasks["cusip"] = lambda: load_cusip(symbol)
for _name, _cik in edgar.KNOWN_MANAGERS.items():
    _tasks[("13f", _name)] = lambda c=_cik: load_manager_13f(c)

with st.spinner(f"Fetching {len(wanted)} option chain(s) for {symbol}..."):
    _loaded = in_parallel(_tasks)

snaps = {}
for exp in wanted:
    result = _loaded[("chain", exp)]
    if isinstance(result, Exception):
        st.error(f"Could not load option chains: {why(result)}")
        st.stop()
    snaps[exp] = result

front = snaps[primary]
surface_snaps = [snaps[e] for e in chosen] if chosen else [front]

earnings_note = earn.describe(earnings_event, primary)
# The decomposition needs expiries either side of the event, so it is fed every
# snapshot loaded this run rather than only the selected ones.
earnings_jump = earn.decompose(list(snaps.values()), earnings_event)

history = _loaded["history"]
if isinstance(history, Exception):
    st.warning(f"Price history unavailable, volatility panels will be limited: {why(history)}")
    history = pd.DataFrame()

REPORT = report_mod.Report(
    title=f"{symbol} · market analytics",
    subtitle=(
        f"Spot ${spot:,.2f} · {front.price_basis} · generated {data.as_of()} · "
        f"density on {primary} ({front.dte:.0f}d) · risk-free {rate:.2%} from {rate_source}"
    ),
)


def chart(fig: go.Figure) -> None:
    """Draw a figure on the page and register it for the PDF."""
    st.plotly_chart(fig, width="stretch")
    REPORT.figure(fig)


st.title(f"{symbol} · market analytics")
st.caption(
    f"Spot ${spot:,.2f} · {front.price_basis} · as of {data.as_of()} · "
    f"density on {primary} ({front.dte:.0f}d) · risk-free {rate:.2%} from {rate_source}"
)
if earnings_event is not None:
    (st.warning if earn.spans(primary, earnings_event) else st.info)(earnings_note, icon="📅")
REPORT.note(earnings_note)

front_move = expmove.expected_move(front)
rv21 = float(vol.yang_zhang_vol(history, 21).iloc[-1] * 100) if len(history) > 25 else float("nan")

headline = [
    ("Spot", f"${spot:,.2f}"),
    (f"Forward ({primary})", f"${front.forward:,.2f}"),
    ("ATM implied vol", f"{front.atm_iv * 100:,.1f}%" if np.isfinite(front.atm_iv) else "n/a"),
    ("Realised vol (21d)", f"{rv21:,.1f}%" if np.isfinite(rv21) else "n/a"),
    ("Vol premium",
     f"{front.atm_iv * 100 - rv21:+,.1f} pts" if np.isfinite(rv21 + front.atm_iv) else "n/a"),
    ("Days to expiry", f"{front.dte:,.1f}"),
]

cols = st.columns(6)
cols[0].metric("Spot", f"${spot:,.2f}")
cols[1].metric(f"Forward ({primary})", f"${front.forward:,.2f}",
               f"{(front.forward / spot - 1) * 100:+.2f}% carry")
cols[2].metric("ATM implied vol", headline[2][1])
cols[3].metric("Realised vol (21d)", headline[3][1])
cols[4].metric("Vol premium", headline[4][1],
               help="Primary-expiry ATM implied minus trailing 21-day Yang-Zhang realised.")
cols[5].metric("Days to expiry", headline[5][1])
REPORT.metrics(headline)

for warning in front.warnings:
    st.warning(warning, icon="⚠️")
    REPORT.note(warning)

st.divider()

# --------------------------------------------------------------------------
# 1. Expected move
# --------------------------------------------------------------------------

st.header("Expected move")
EXPECTED_MOVE_BLURB = (
    "The at-the-money straddle is worth the discounted expected absolute move, "
    "E|S−F|. One standard deviation is F·σ·√T, which is about 25% larger — the two "
    "get conflated constantly, so both are shown."
)
st.caption(EXPECTED_MOVE_BLURB)
REPORT.heading("1. Expected move")
REPORT.text(EXPECTED_MOVE_BLURB)

moves = [expmove.expected_move(s) for s in snaps.values()]
term = expmove.move_term_structure(moves)

lo1, hi1 = front_move.band(1.0)
lo2, hi2 = front_move.band(2.0)
REPORT.metrics([
    (f"1σ move to {primary}", f"±${front_move.one_sigma:,.2f} ({front_move.one_sigma_pct:.2f}%)"),
    ("Expected absolute move", f"${front_move.expected_abs_move:,.2f}"),
    ("ATM straddle", f"${front_move.straddle:,.2f}"),
    ("68% band", f"${lo1:,.2f} – ${hi1:,.2f}"),
    ("95% band", f"${lo2:,.2f} – ${hi2:,.2f}"),
])

em1, em2 = st.columns([2, 3])

with em1:
    st.metric(f"1σ move to {primary}", f"±${front_move.one_sigma:,.2f}",
              f"±{front_move.one_sigma_pct:.2f}%")
    st.metric("Expected absolute move", f"${front_move.expected_abs_move:,.2f}",
              f"{front_move.expected_abs_move_pct:.2f}% · straddle ${front_move.straddle:,.2f}")
    st.markdown(
        f"**68% band** {price_md(lo1)} – {price_md(hi1)}  \n"
        f"**95% band** {price_md(lo2)} – {price_md(hi2)}  \n"
        f"<span style='color:{NEUTRAL}'>Straddle source: {front_move.source}</span>",
        unsafe_allow_html=True,
    )

    if len(history) > 60:
        trading_days = max(int(round(front.dte * 252 / 365)), 1)
        actual = expmove.historical_moves(history.close, trading_days)
        if actual:
            body = (
                f"Over the last {HISTORY_PERIOD} the {trading_days}-trading-day move "
                f"averaged **±{actual['mean_abs_pct']:.2f}%**, with 68% of moves inside "
                f"**±{actual['p68_abs_pct']:.2f}%** "
                f"({actual['up_share'] * 100:.0f}% of them up, n={actual['n']} overlapping windows). "
                f"The chain is currently charging **±{front_move.one_sigma_pct:.2f}%**."
            )
            gap = front_move.one_sigma_pct - actual["p68_abs_pct"]
            caveat = (
                f"Implied is {'above' if gap > 0 else 'below'} the historical 68% band by "
                f"{abs(gap):.2f} points. Overlapping windows are autocorrelated, so treat "
                "that comparison as indicative rather than a test."
            )
            st.markdown("**What actually happened, historically**")
            st.markdown(body)
            st.caption(caveat)
            REPORT.text("<b>What actually happened, historically.</b> " + body.replace("**", ""))
            REPORT.note(caveat)

with em2:
    cone = go.Figure()

    # Each shaded band needs two boundary traces, because a fill is drawn between
    # a trace and the one before it. Only the second of each pair carries the fill
    # and so only it earns a legend entry -- but both show up in the hover box, so
    # both need a label there. `name` is what the legend prints; the <extra> block
    # of the hover template is what the unified hover box prints, which lets the
    # band be called "±2σ (95%)" in one place and "95% high"/"95% low" in the
    # other. Without it the unnamed edges hovered as "trace 0" and "trace 2".
    def edge(values, label, *, name=None, fill=None, alpha=0.0):
        cone.add_trace(go.Scatter(
            x=term.dte, y=values, mode="lines", name=name or label,
            showlegend=name is not None,
            line=dict(color=BLUE, width=0), fill=fill,
            fillcolor=BLUE_FILL.format(alpha=alpha) if fill else None,
            hovertemplate="$%{y:,.2f}<extra>" + label + "</extra>",
        ))

    edge(term.upper_2sd, "95% high")
    edge(term.lower_2sd, "95% low", name="±2σ (95%)", fill="tonexty", alpha=0.12)
    edge(term.upper_1sd, "68% high")
    edge(term.lower_1sd, "68% low", name="±1σ (68%)", fill="tonexty", alpha=0.28)
    cone.add_trace(go.Scatter(x=term.dte, y=term.forward, name="Forward", mode="lines+markers",
                              line=dict(color=BLUE, width=2),
                              hovertemplate="$%{y:,.2f}<extra>Forward</extra>"))
    cone.add_hline(y=spot, line=dict(color=NEUTRAL, width=1.5, dash="dot"),
                   annotation_text=f"Spot ${spot:,.2f}")
    if earnings_event is not None:
        # The cone widens smoothly with time; the event does not. Marking it
        # shows which part of the widening is a scheduled jump rather than drift.
        away = earnings_event.days_away()
        if 0 <= away <= float(term.dte.max()):
            cone.add_vline(x=away, line=dict(color=TEAL, width=2, dash="dot"),
                           annotation_text="Earnings", annotation_position="top left")
    layout(cone, 400, title="Implied cone by expiry",
           xaxis_title="Days to expiry", yaxis_title="Price ($)")
    chart(cone)

move_table = term[["expiry", "dte", "atm_iv", "straddle", "expected_abs_move",
                   "one_sigma", "one_sigma_pct", "lower_1sd", "upper_1sd"]].copy()
move_table.columns = ["Expiry", "DTE", "ATM IV", "Straddle", "E|move| $",
                      "1σ $", "1σ %", "68% low", "68% high"]
with st.expander("Expected move by expiration"):
    st.dataframe(
        move_table.style.format({
            "DTE": "{:,.1f}", "ATM IV": "{:.1%}", "Straddle": "${:,.2f}",
            "E|move| $": "${:,.2f}", "1σ $": "${:,.2f}", "1σ %": "{:,.2f}%",
            "68% low": "${:,.2f}", "68% high": "${:,.2f}",
        }),
        width="stretch", hide_index=True,
    )
REPORT.table(move_table.round(2))

st.divider()

# --------------------------------------------------------------------------
# 2. Implied distribution (Breeden-Litzenberger)
# --------------------------------------------------------------------------

st.header("Implied probability distribution")
DENSITY_BLURB = (
    f"The market's own view of where {symbol} settles on {primary}, extracted from the "
    "chain: pdf(K) = e^(rT) ∂²C/∂K². Prices are differentiated after smoothing in "
    "implied-vol space, which is what keeps the density positive."
)
st.caption(DENSITY_BLURB)
REPORT.heading("2. Implied probability distribution")
REPORT.text(DENSITY_BLURB)

if earn.spans(primary, earnings_event):
    MIXTURE_NOTE = (
        "This expiry spans earnings, so the distribution below is a **mixture**, not a "
        "diffusion: most of its width is one scheduled announcement rather than the "
        "accumulation of ordinary days. Expect it to be wider and flatter through the middle "
        "than a non-earnings expiry, and read the excess kurtosis with that in mind — the fat "
        "tails here are an event, and the market knows the date."
    )
    st.info(MIXTURE_NOTE.replace("**", ""), icon="📅")
    REPORT.note(MIXTURE_NOTE.replace("**", ""))

try:
    dens = rnd.risk_neutral_density(front, smoothing=SMOOTHING)
except Exception as exc:  # noqa: BLE001
    dens = None
    st.warning(f"Could not build the density for {primary}: {why(exc)}")
    REPORT.note(f"Density unavailable for {primary}: {why(exc)}")

if dens is not None:
    REPORT.metrics([
        ("P(above spot)", f"{dens.prob_above(spot) * 100:,.1f}%"),
        ("Median", f"${dens.median:,.2f}"),
        ("Implied skew", f"{dens.skew:+.2f}"),
        ("Excess kurtosis", f"{dens.excess_kurtosis:+.2f}"),
        ("Quotes fitted", f"{dens.n_quotes}"),
        ("RMS fit residual", f"{dens.rms_fit_error * 100:.2f} vol pts"),
    ])

    d1, d2 = st.columns([3, 2])

    with d1:
        p10, p90 = dens.quantile(0.10), dens.quantile(0.90)
        mask = (dens.price >= dens.quantile(0.005)) & (dens.price <= dens.quantile(0.995))
        px, py = dens.price[mask], dens.pdf[mask]

        fig = go.Figure()
        inner = (px >= p10) & (px <= p90)
        fig.add_trace(go.Scatter(
            x=px[inner], y=py[inner], name="80% of the probability", mode="lines",
            line=dict(color=BLUE, width=0), fill="tozeroy",
            fillcolor=BLUE_FILL.format(alpha=0.26), hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=px, y=py, name="Market-implied density", mode="lines",
            line=dict(color=BLUE, width=3),
            hovertemplate="$%{x:,.2f}<br>density %{y:.5f}<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=px, y=dens.lognormal_pdf()[mask], name="Lognormal at the same ATM vol",
            mode="lines", line=dict(color=NEUTRAL, width=2, dash="dash"),
        ))
        fig.add_vline(x=spot, line=dict(color=NEUTRAL, width=2, dash="dot"),
                      annotation_text=f"Spot ${spot:,.2f}", annotation_position="top left")
        fig.add_vline(x=dens.median, line=dict(color=LILAC, width=2, dash="dash"),
                      annotation_text=f"Median ${dens.median:,.2f}", annotation_position="top right")
        layout(fig, 460, hovermode="x",
               title=f"Where the market prices {symbol} on {primary}",
               xaxis_title="Settlement price ($)", yaxis_title="Probability density")
        chart(fig)

        st.caption(
            "The dashed grey line is the plain Black-Scholes lognormal at the same ATM vol. "
            "The gap between the two *is* the smile: the left tail is fatter because "
            "puts are bid, and the peak sits higher because the middle is correspondingly "
            "cheaper."
        )

    with d2:
        st.metric("P(above spot)", f"{dens.prob_above(spot) * 100:,.1f}%")
        c1, c2 = st.columns(2)
        c1.metric("Implied skew", f"{dens.skew:+.2f}",
                  help="Third standardised moment. Negative = fatter left tail.")
        c2.metric("Excess kurtosis", f"{dens.excess_kurtosis:+.2f}",
                  help="0 would be a normal. Positive = fat tails on both sides.")

        table = rnd.summary_table(dens)
        st.dataframe(
            table.style.format({"price": "${:,.2f}", "vs spot %": "{:+,.2f}%"}),
            width="stretch", hide_index=True, height=290,
        )

        touch = rnd.touch_table(dens, spot)
        st.markdown("**Reaching a level before expiry**")
        st.dataframe(
            touch.style.format({
                "level": "${:,.2f}", "move %": "{:+,.0f}%",
                "finishes beyond": "{:,.1f}%", "touches before expiry": "{:,.1f}%",
            }),
            width="stretch", hide_index=True,
        )
        touch_note = (
            "The density says where the price *settles*; the right-hand column says whether "
            "it ever gets there. They are different questions and the second is usually the "
            "one being asked — of a stop, a level, a decision to act. First passage of a "
            "driftless process, with the vol read off the smile at each level, so the "
            "downside uses the higher vol the market actually charges there."
        )
        st.caption(touch_note)
        REPORT.table(touch.round(2))
        REPORT.note(touch_note)

        fit_note = (
            f"Fitted {dens.n_quotes} out-of-the-money quotes, RMS residual "
            f"{dens.rms_fit_error * 100:.2f} vol points. Quoted strikes span "
            f"${dens.quoted_range[0]:,.2f}–${dens.quoted_range[1]:,.2f}; outside that the "
            f"smile's boundary slope is carried into a damped wing, so the extreme tails are "
            f"extrapolation. {dens.negative_mass_clipped * 100:.2f}% of the raw curvature came "
            f"out negative and was clipped, and {dens.tail_mass_missing * 100:.3f}% of the "
            "probability falls outside the strike grid."
        )
        st.caption(fit_note.replace("$", "\\$"))

    REPORT.table(table.round(2))
    REPORT.note(
        "The dashed grey line is the plain Black-Scholes lognormal at the same ATM vol; the "
        "gap between the two is the smile. " + fit_note
    )

    # --- cross-check: the same moments, replicated straight from the quotes ---
    # Nothing above this point would notice if the smoothing step had distorted
    # the shape, because a wrong density still integrates to one and still has
    # its mean at the forward. This is the independent second opinion.
    try:
        mom_quotes = bkm.from_quotes(front)
        mom_density = bkm.from_density(dens)
    except Exception as exc:  # noqa: BLE001
        mom_quotes = mom_density = None
        st.caption(f"Model-free moment cross-check unavailable: {why(exc)}")
        REPORT.note(f"Model-free moment cross-check unavailable: {why(exc)}")

    if mom_quotes is not None:
        supported, verdict = bkm.agreement(mom_quotes, mom_density)
        compare = bkm.compare(mom_quotes, mom_density)

        mfiv_note = (
            f"Model-free implied vol is **{mom_quotes.mfiv * 100:.2f}%** against the fitted "
            f"smile's ATM vol of {dens.atm_iv * 100:.2f}%. That second figure is the smoothed "
            "curve read at the forward, not the raw interpolation quoted at the top of the "
            "page, so the two differ by whatever the smoothing took out. Model-free vol is the "
            "fair strike of a variance swap — the quantity the VIX approximates — so it is the "
            "number to compare against the VIX line further down. It sits above ATM whenever "
            "the smile has curvature, because it prices every strike rather than just the one "
            "at the money."
        )

        st.markdown("**Cross-check: the same moments, replicated from the quotes**")
        (st.success if supported else st.warning)(verdict, icon="✅" if supported else "⚠️")
        st.markdown(mfiv_note)

        REPORT.metrics([
            ("Model-free IV", f"{mom_quotes.mfiv * 100:.2f}%"),
            ("Skew from quotes", f"{mom_quotes.skew:+.2f}"),
            ("Kurtosis from quotes", f"{mom_quotes.excess_kurtosis:+.2f}"),
            ("Quotes support the shape", "yes" if supported else "no"),
            ("Strikes integrated", f"{mom_quotes.n_strikes}"),
            ("Strike coverage", mom_quotes.coverage),
        ])
        REPORT.text("<b>Cross-check: the same moments, replicated from the quotes.</b> " + verdict)
        REPORT.table(compare.round(3))
        REPORT.note(mfiv_note.replace("**", ""))

        with st.expander("How the two estimates differ, and why that is the point"):
            st.dataframe(
                compare.style.format({
                    "From quotes": "{:,.3f}", "From density": "{:,.3f}",
                    "Difference": "{:+,.3f}",
                }),
                width="stretch", hide_index=True,
            )
            st.markdown(
                "Bakshi, Kapadia and Madan showed that the moments of the settlement price "
                "are a **weighted sum of option prices** — no smoothing, no differentiation, "
                "no density at all. The left column is that sum over the quoted mids; the "
                "right column is what the fitted density reports, and what the metrics at the "
                "top of this section show.\n\n"
                "The two are not expected to match exactly, and the reason is the useful part. "
                f"The quote integrals stop at the last listed strike (here "
                f"{mom_quotes.coverage} around the forward), so they are blind to the tail "
                "beyond it. The density keeps going into an extrapolated wing. **The gap "
                "between the columns is therefore a measure of how much of the reported skew "
                "and kurtosis rests on strikes the market never quoted** rather than on "
                "prices anyone can trade.\n\n"
                "Which is exactly why the verdict above is judged on the **width** and the "
                "**mean**, not on those two. Excess kurtosis is a fourth moment, so it lives "
                "almost entirely in the tails — measured across ten live chains the quote-side "
                "figure came back between 0.2 and 1.7 whatever the distribution actually "
                "looked like, so the 'difference' would just restate the density's own number. "
                "The width is dominated by the middle, where both sides can see.\n\n"
                "The mean is the sharpest line in the table. From the quotes it is the forward "
                "*exactly* — the replicating weight for the first moment is identically zero, "
                "so no integration error can touch it. From the density it is an integral that "
                "has to come out right. Damaging a single strike in testing knocks it 1.4% to "
                "20% off; an honest chain lands within 0.05%."
            )

    with st.expander("How the density is built, and what the smoothing does"):
        st.markdown(
            "Differentiating quoted prices twice does not work: strikes are spaced coarse, "
            "quotes are pinned to a penny, and the second difference of that is noise that "
            "goes negative everywhere. So the fit happens in **volatility space** instead — "
            "a smoothing spline through implied vol against log-moneyness, which is a gentle "
            "near-quadratic curve and therefore easy to fit. Re-pricing a dense strike grid "
            "off that smoothed smile gives a call curve whose curvature is well behaved by "
            "construction.\n\n"
            f"**Smoothing is fixed at {SMOOTHING:.1f}**, which means the spline is asked to fit "
            "the quotes to within their own bid-ask spreads — each strike is weighted by how "
            "precisely its spread pins its vol (half-spread ÷ vega), so a penny-wide "
            "at-the-money quote counts for far more than a two-dollar-wide wing. A lower "
            "number chases individual quotes and puts ripples in the density; a higher one "
            "stiffens the smile toward a parabola. There is no reason to move it day to day, "
            "which is why it is no longer a slider."
        )
        s1, s2 = st.columns(2)
        smile = go.Figure()
        smile.add_trace(go.Scatter(
            x=np.exp(dens.quote_k) * dens.forward, y=dens.quote_iv * 100,
            name="Quoted", mode="markers", marker=dict(color=NEUTRAL, size=7, opacity=0.8),
        ))
        smile.add_trace(go.Scatter(
            x=dens.price, y=dens.smile_iv * 100, name="Smoothing spline",
            mode="lines", line=dict(color=BLUE, width=3),
        ))
        smile.add_vline(x=dens.forward, line=dict(color=NEUTRAL, width=1.5, dash="dot"),
                        annotation_text="Forward")
        layout(smile, 380, title="Implied vol vs strike",
               xaxis_title="Strike ($)", yaxis_title="Implied vol (%)")
        with s1:
            chart(smile)

        calls = go.Figure()
        calls.add_trace(go.Scatter(
            x=dens.price, y=dens.call_curve, name="Smoothed call price",
            mode="lines", line=dict(color=LILAC, width=3),
        ))
        layout(calls, 380, title="Call price vs strike",
               xaxis_title="Strike ($)", yaxis_title="Call value ($)")
        with s2:
            chart(calls)
        st.caption("Curvature of the right-hand curve is the density on the left.")

st.divider()

# --------------------------------------------------------------------------
# 3. Gamma exposure
# --------------------------------------------------------------------------

st.header("Gamma exposure")
GEX_BLURB = (
    "Dollar gamma dealers are assumed to hold, per 1% move in spot, signed positive "
    "for calls and negative for puts. Above the flip level dealer hedging damps moves; "
    "below it, hedging amplifies them. Aggregated across the expirations selected in "
    "the sidebar."
)
st.caption(GEX_BLURB)
REPORT.heading("3. Gamma exposure")
REPORT.text(GEX_BLURB)

oi_available = gex.has_open_interest(surface_snaps)
if not oi_available:
    oi_warning = (
        "The feed is reporting zero open interest across these chains right now. That "
        "column is blanked for stretches at a time, particularly outside US market "
        "hours — hit "
        "Refresh later, or fall back to volume. Volume answers a different question: "
        "where gamma was traded today, not where the position sits."
    )
    st.warning(oi_warning.replace("zero open interest", "**zero open interest**"), icon="⚠️")
    REPORT.note(oi_warning)

GEX_WEIGHTS = ["Open interest (positioning)", "Volume (today's flow)"]

# ``index`` only decides the first render: once the key exists, session state
# wins and the argument is ignored. That quietly disabled the fallback this
# panel is built around -- open the page while the market is up, come back
# after the close, and open interest is blanked but the radio is still sitting
# on it, so the panel errors instead of showing the volume view the warning
# above just recommended. Re-seed whenever availability actually flips, which
# leaves a deliberate choice alone in between.
if st.session_state.get("gex_oi_available") != oi_available:
    st.session_state["gex_oi_available"] = oi_available
    st.session_state["gex_weight"] = GEX_WEIGHTS[0 if oi_available else 1]

weight_label = st.radio(
    "Weight each strike by",
    GEX_WEIGHTS,
    horizontal=True,
    key="gex_weight",
)
weight = "volume" if weight_label.startswith("Volume") else "open_interest"

try:
    gx = gex.gamma_exposure(surface_snaps, spot, window=GEX_WINDOW, weight=weight)
except Exception as exc:  # noqa: BLE001
    gx = None
    st.warning(f"Could not compute gamma exposure: {why(exc)}")
    REPORT.note(f"Gamma exposure unavailable: {why(exc)}")

if gx is not None:
    size_label = "OI" if gx.weight == "open_interest" else "volume"
    REPORT.metrics([
        ("Net gamma at spot", money(gx.total_gex, 1)),
        ("Flip level", f"${gx.flip:,.2f}" if np.isfinite(gx.flip) else "none in range"),
        ("Regime", gx.regime.split(" (")[0].title()),
        (f"Put/call {size_label}", f"{gx.put_call_oi_ratio:,.2f}"),
        ("Weighted by", weight_label),
    ])

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Net gamma at spot", money(gx.total_gex, 1), help="Dollars per 1% move in spot.")
    g2.metric("Flip level", f"${gx.flip:,.2f}" if np.isfinite(gx.flip) else "none in range",
              f"{(gx.flip / spot - 1) * 100:+.2f}% from spot" if np.isfinite(gx.flip) else None)
    g3.metric("Regime", gx.regime.split(" (")[0].title())
    g4.metric(f"Put/call {size_label}", f"{gx.put_call_oi_ratio:,.2f}")

    gg1, gg2 = st.columns(2)

    with gg1:
        view = gx.by_strike[gx.by_strike.strike.between(spot * 0.7, spot * 1.3)]
        view = view if not view.empty else gx.by_strike
        bars = go.Figure()
        bars.add_bar(x=view.strike, y=view.call_gex, name="Call gamma", marker_color=GREEN)
        bars.add_bar(x=view.strike, y=view.put_gex, name="Put gamma", marker_color=RED)
        bars.add_vline(x=spot, line=dict(color=NEUTRAL, width=2, dash="dot"),
                       annotation_text="Spot", annotation_position="top right")
        if np.isfinite(gx.flip):
            bars.add_vline(x=gx.flip, line=dict(color=BLUE, width=2, dash="dash"),
                           annotation_text="Flip", annotation_position="top left")
        layout(bars, 420, barmode="relative", title="Gamma exposure by strike",
               xaxis_title=f"Strike ($) · weighted by {size_label}",
               yaxis_title="$ gamma per 1% move")
        chart(bars)

    with gg2:
        prof = gx.profile
        line = go.Figure()
        line.add_trace(go.Scatter(
            x=prof.spot, y=prof.net_gex, name="Net gamma", mode="lines",
            line=dict(color=BLUE, width=3), fill="tozeroy",
            fillcolor=BLUE_FILL.format(alpha=0.18),
        ))
        line.add_hline(y=0, line=dict(color=NEUTRAL, width=1))
        line.add_vline(x=spot, line=dict(color=NEUTRAL, width=2, dash="dot"),
                       annotation_text=f"Spot ${spot:,.2f}", annotation_position="top right")
        if np.isfinite(gx.flip):
            line.add_vline(x=gx.flip, line=dict(color=LILAC, width=2, dash="dash"),
                           annotation_text=f"Flip ${gx.flip:,.2f}", annotation_position="bottom left")
        layout(line, 420, title="Net gamma across spot prices",
               xaxis_title="Underlying price ($)", yaxis_title="$ gamma per 1% move")
        chart(line)

    GEX_CAVEAT = (
        "The sign is an assumption, not a measurement. Open interest is unsigned, so "
        "this uses the standard convention that dealers are long call gamma and short put "
        "gamma. On tickers where customers overwrite calls or buy puts for protection, the "
        "true sign is the opposite and every conclusion here inverts. Read it as a "
        "positioning hypothesis."
    )
    st.info(GEX_CAVEAT.replace("The sign is an assumption, not a measurement.",
                               "**The sign is an assumption, not a measurement.**"), icon="ℹ️")
    REPORT.note(GEX_CAVEAT)

st.divider()

# --------------------------------------------------------------------------
# 4. Volatility surface and skew
# --------------------------------------------------------------------------

st.header("Volatility surface and skew")
REPORT.heading("4. Volatility surface and skew")

if len(surface_snaps) < 2:
    st.info("Select at least two expirations in the sidebar to build a surface.")
    REPORT.note("Fewer than two expirations selected; no surface was built.")
else:
    try:
        table = surf.surface_table(surface_snaps)
        skew = surf.skew_metrics(surface_snaps)
        term_iv = surf.term_structure(surface_snaps)
    except Exception as exc:  # noqa: BLE001
        table = skew = term_iv = None
        st.warning(f"Could not build the surface: {why(exc)}")
        REPORT.note(f"Surface unavailable: {why(exc)}")

    if table is not None:
        v1, v2 = st.columns([3, 2])

        with v1:
            try:
                dte_axis, k_axis, grid = surf.surface_grid(table)
                srf = go.Figure(go.Surface(
                    x=dte_axis, y=(np.exp(k_axis) - 1) * 100, z=grid * 100,
                    colorscale="Viridis", colorbar=dict(title="IV %"),
                    hovertemplate="DTE %{x:.0f}<br>Moneyness %{y:.1f}%"
                                  "<br>IV %{z:.1f}%<extra></extra>",
                ))
                srf.update_layout(
                    height=490, margin=dict(t=60, b=10, l=10, r=10),
                    title=dict(text="Implied vol surface", x=0, xanchor="left",
                               yref="container", y=0.97, yanchor="top", pad=dict(l=8)),
                    scene=dict(
                        xaxis_title="Days to expiry",
                        yaxis_title="Strike vs forward (%)",
                        zaxis_title="Implied vol (%)",
                        camera=dict(eye=dict(x=1.7, y=-1.5, z=0.9)),
                    ),
                )
                chart(srf)
                st.caption(
                    "Linear interpolation between quotes, nearest-neighbour fill in the "
                    "corners the quotes do not reach — those corners are decoration."
                )
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Not enough coverage for a 3D surface: {why(exc)}")

        with v2:
            sk = go.Figure()
            sk.add_trace(go.Scatter(
                x=skew.dte, y=skew.rr_25d * 100, name="25Δ risk reversal",
                mode="lines+markers", line=dict(color=BLUE, width=3),
            ))
            sk.add_trace(go.Scatter(
                x=skew.dte, y=skew.bf_25d * 100, name="25Δ butterfly",
                mode="lines+markers", line=dict(color=LILAC, width=3),
            ))
            sk.add_hline(y=0, line=dict(color=NEUTRAL, width=1))
            layout(sk, 300, title="Skew by expiry",
                   xaxis_title="Days to expiry", yaxis_title="Vol points")
            chart(sk)

            ts = go.Figure()
            ts.add_trace(go.Scatter(
                x=term_iv.dte, y=term_iv.atm_iv * 100, name="ATM implied",
                mode="lines+markers", line=dict(color=BLUE, width=3),
            ))
            ts.add_trace(go.Scatter(
                x=term_iv.dte, y=term_iv.forward_iv * 100, name="Forward vol between expiries",
                mode="lines+markers", line=dict(color=NEUTRAL, width=2, dash="dash"),
            ))
            if earnings_event is not None:
                away = earnings_event.days_away()
                if 0 <= away <= float(term_iv.dte.max()):
                    # The kink in a term structure is almost always this date.
                    ts.add_vline(x=away, line=dict(color=TEAL, width=2, dash="dot"),
                                 annotation_text="Earnings", annotation_position="top left")
            layout(ts, 300, title="Term structure",
                   xaxis_title="Days to expiry", yaxis_title="Implied vol (%)")
            chart(ts)

        front_skew = skew[skew.expiry == primary]
        if not front_skew.empty and np.isfinite(front_skew.rr_25d.iloc[0]):
            rr = float(front_skew.rr_25d.iloc[0]) * 100
            bf = float(front_skew.bf_25d.iloc[0]) * 100
            direction = "puts bid over calls" if rr < 0 else "calls bid over puts"
            skew_note = (
                f"At {primary} the 25-delta risk reversal is {rr:+.2f} vol points "
                f"({direction}) and the butterfly is {bf:+.2f}. Equity skew is normally "
                "negative; the question is always whether it is more negative than usual, "
                "not whether it is negative."
            )
            st.markdown(skew_note)
            REPORT.text(skew_note)
        skew_table = skew.copy()
        for col in ("atm_iv", "iv_25d_call", "iv_25d_put", "rr_25d", "bf_25d"):
            skew_table[col] = skew_table[col] * 100
        skew_table.columns = ["Expiry", "DTE", "ATM IV %", "25Δ call IV %", "25Δ put IV %",
                              "RR25 (pts)", "BF25 (pts)", "Quotes"]
        with st.expander("Skew detail"):
            st.dataframe(
                skew_table.style.format({
                    "DTE": "{:,.1f}", "ATM IV %": "{:,.2f}", "25Δ call IV %": "{:,.2f}",
                    "25Δ put IV %": "{:,.2f}", "RR25 (pts)": "{:+,.2f}", "BF25 (pts)": "{:+,.2f}",
                }),
                width="stretch", hide_index=True,
            )
            st.caption(
                "Blank wings mean the chain does not quote out to 25 delta on that side; "
                "the vol is left undefined rather than extrapolated."
            )
        REPORT.table(skew_table.round(2))

# --- the earnings jump priced into the term structure -----------------------
if earnings_jump is not None:
    st.subheader("What the chain charges for earnings")
    EARNINGS_BLURB = (
        "Total implied variance is additive in time, so an expiry that spans the "
        "announcement prices diffusion *plus* a jump, and one that settles before it prices "
        "diffusion alone. Holding diffusive vol at the level of the last expiry before the "
        "event, the difference between the two is the event: σ²ₚₒₛₜ·T = σ²ₚᵣₑ·T + J²."
    )
    st.caption(EARNINGS_BLURB)
    REPORT.heading("4b. What the chain charges for earnings")
    REPORT.text(EARNINGS_BLURB)

    if earnings_jump.priced:
        e1, e2, e3 = st.columns(3)
        e1.metric("Implied earnings move", f"±{earnings_jump.jump * 100:,.2f}%",
                  help="The one-session move the chain is charging for, as a fraction of spot.")
        e2.metric("Share of the expiry's variance", f"{earnings_jump.jump_share * 100:,.0f}%",
                  help=f"Of everything {earnings_jump.post_expiry} prices, this much is the event.")
        e3.metric("Vol across the event",
                  f"{earnings_jump.pre_iv * 100:,.1f}% → {earnings_jump.post_iv * 100:,.1f}%",
                  help=f"{earnings_jump.pre_expiry} → {earnings_jump.post_expiry}")
        REPORT.metrics([
            ("Implied earnings move", f"±{earnings_jump.jump * 100:.2f}%"),
            ("Share of variance", f"{earnings_jump.jump_share * 100:.0f}%"),
            ("Anchor expiry", f"{earnings_jump.pre_expiry} ({earnings_jump.pre_iv * 100:.1f}%)"),
            ("First spanning expiry",
             f"{earnings_jump.post_expiry} ({earnings_jump.post_iv * 100:.1f}%)"),
        ])

    (st.warning if not earnings_jump.reliable else st.markdown)(earnings_jump.note)
    REPORT.text(earnings_jump.note)
    caveat = (
        "The assumption doing the work is that diffusive volatility is unchanged across the "
        "two expiries — that the event is the only difference between them. It never is "
        "exactly, so treat this as the size of the premium rather than a forecast of the move."
    )
    st.caption(caveat)
    REPORT.note(caveat)
elif earnings_event is not None:
    no_bracket = (
        f"No earnings decomposition: every expiry loaded sits on the same side of the "
        f"{earnings_event.move_date:%d %b} announcement. Select an expiry either side of it "
        "to price the event."
    )
    st.caption(no_bracket)
    REPORT.note(no_bracket)

# --- the same numbers, against the index ------------------------------------
if symbol != benchmark.BENCHMARK_SYMBOL:
    st.subheader(f"Against the index ({benchmark.BENCHMARK_SYMBOL})")
    BENCH_BLURB = (
        "A risk reversal of −3 vol points means nothing on its own. Equity skew is always "
        f"negative, so the only useful question is whether it is steep *for what it is* — "
        f"which is what the same numbers for {benchmark.BENCHMARK_SYMBOL}, on the same "
        "afternoon and at a matched maturity, answer. Both readings move with market-wide "
        "risk appetite, so comparing them nets most of that out."
    )
    st.caption(BENCH_BLURB)
    REPORT.heading(f"4c. Against the index ({benchmark.BENCHMARK_SYMBOL})")
    REPORT.text(BENCH_BLURB)

    try:
        bench_expirations = load_expirations(benchmark.BENCHMARK_SYMBOL)
        bench_expiry = benchmark.nearest_expiry(
            bench_expirations, front.dte, lambda e: prep.year_fraction(e)[0]
        )
        bench_snap = snapshot(benchmark.BENCHMARK_SYMBOL, bench_expiry,
                              load_spot(benchmark.BENCHMARK_SYMBOL), rate)
        versus = benchmark.compare(front, bench_snap)
    except Exception as exc:  # noqa: BLE001 - a benchmark is a nicety, never a blocker
        versus = None
        st.caption(f"Could not load {benchmark.BENCHMARK_SYMBOL} for comparison: {why(exc)}")
        REPORT.note(f"Benchmark comparison unavailable: {why(exc)}")

    if versus is not None:
        st.markdown(versus.note())
        b1, b2 = st.columns([2, 3])
        with b1:
            st.dataframe(
                versus.table().style.format({
                    versus.symbol: "{:+,.2f}", versus.benchmark: "{:+,.2f}",
                    "Difference": "{:+,.2f}",
                }),
                width="stretch", hide_index=True,
            )
        with b2:
            bfig = go.Figure()
            metrics = ["ATM IV", "25Δ risk reversal", "25Δ butterfly"]
            bfig.add_trace(go.Bar(
                x=metrics, name=versus.symbol, marker_color=BLUE,
                y=[versus.atm_iv * 100, versus.rr25 * 100, versus.bf25 * 100],
            ))
            bfig.add_trace(go.Bar(
                x=metrics, name=versus.benchmark, marker_color=LILAC,
                y=[versus.benchmark_atm_iv * 100, versus.benchmark_rr25 * 100,
                   versus.benchmark_bf25 * 100],
            ))
            bfig.add_hline(y=0, line=dict(color=NEUTRAL, width=1))
            layout(bfig, 300, barmode="group", hovermode="x",
                   title=f"{versus.symbol} {versus.expiry} vs "
                         f"{versus.benchmark} {versus.benchmark_expiry}",
                   yaxis_title="Vol points")
            chart(bfig)
        REPORT.text(versus.note())
        REPORT.table(versus.table().round(2))
        st.caption(
            f"Matched on maturity, not on date: {versus.expiry} is {versus.dte:.1f} days out "
            f"against {versus.benchmark_expiry} at {versus.benchmark_dte:.1f}. The vol ratio "
            "is not a beta — implied vol carries idiosyncratic risk the index has diversified "
            f"away, so it is above 1 for almost every single name. And "
            f"{benchmark.BENCHMARK_SYMBOL} is the market, not the sector; for a name whose "
            "sector is having its own day, a peer would be the better comparison."
        )

# The arbitrage checks sit outside the two-expiry guard above, because the
# vertical and butterfly conditions are per-expiry and worth running even when
# there is no surface to build. `surface_snaps` is never empty -- it falls back
# to the primary expiry -- so there is always something to check.
st.subheader("Static arbitrage checks")
ARB_BLURB = (
    "Three conditions a set of option prices has to satisfy for no model at all: call "
    "value must fall with strike but not faster than one-for-one (vertical), must be "
    "convex in strike (butterfly), and total variance must not fall as expiry lengthens "
    "at any moneyness (calendar). A violation is only counted when it is larger than the "
    "bid-ask spreads of the strikes involved — otherwise every penny-wide chain in the "
    "market would light up."
)
st.caption(ARB_BLURB)
REPORT.heading("4d. Static arbitrage checks")
REPORT.text(ARB_BLURB)

try:
    arb = noarb.check(surface_snaps)
except Exception as exc:  # noqa: BLE001
    arb = None
    st.warning(f"Could not run the arbitrage checks: {why(exc)}")
    REPORT.note(f"Arbitrage checks unavailable: {why(exc)}")

if arb is not None:
    arb_note = noarb.summary(arb)
    (st.success if arb.clean else st.warning)(arb_note, icon="✅" if arb.clean else "⚠️")
    REPORT.text(arb_note)

    if not arb.clean:
        violations = arb.all_violations()
        show = violations.rename(columns={
            "check": "Check", "expiry": "Expiry", "where": "Where",
            "excess": "Beyond spread", "detail": "What is wrong",
        })
        st.dataframe(
            show.style.format({"Beyond spread": "{:,.3f}"}),
            width="stretch", hide_index=True,
        )
        st.caption(
            "“Beyond spread” is how far past the quote uncertainty the violation goes — in "
            "dollars for the vertical and butterfly rows, in vol points for the calendar "
            "ones. These are stale prints, not opportunities: by the time a 15-minute "
            "delayed quote shows an arbitrage, it is gone. What they are useful for is "
            "knowing which strike is dragging the density around."
        )
        REPORT.table(show.round(3))

st.divider()

# --------------------------------------------------------------------------
# 5. Volatility risk premium
# --------------------------------------------------------------------------

st.header("Volatility risk premium")
VRP_BLURB = (
    "What sellers of volatility get paid: the vol the market charged, minus the vol "
    "that actually showed up afterwards."
)
st.caption(VRP_BLURB)
REPORT.heading("5. Volatility risk premium")
REPORT.text(VRP_BLURB)

if len(history) > 60:
    ticker_vrp = vol.implied_vs_trailing(front.atm_iv, history, 21)
    p1, p2 = st.columns([2, 3])

    with p1:
        st.subheader(f"{symbol} today")
        if ticker_vrp:
            st.metric("Implied − trailing realised", f"{ticker_vrp['spread_pts']:+,.1f} pts",
                      f"ratio {ticker_vrp['ratio']:,.2f}×")
            vrp_body = (
                f"Primary-expiry ATM implied {ticker_vrp['implied_pct']:.1f}% against "
                f"21-day Yang-Zhang realised {ticker_vrp['yang_zhang_pct']:.1f}% "
                f"(close-to-close {ticker_vrp['close_to_close_pct']:.1f}%). "
                f"Realised sits at the {ticker_vrp['rv_percentile']:.0f}th percentile "
                f"of its own {HISTORY_PERIOD} history."
            )
            st.markdown(vrp_body)
            if earn.spans(primary, earnings_event):
                # Without this the panel reads a scheduled event premium as a
                # volatility premium, which is a different thing entirely.
                vrp_earnings = (
                    f"**The primary expiry spans earnings, so this comparison is not "
                    f"like-for-like.** Implied vol here contains a premium for one scheduled "
                    f"announcement; the trailing 21 days it is measured against contain no "
                    f"such event. Some of the "
                    f"{ticker_vrp['spread_pts']:+.1f} points is the earnings date, not a "
                    "premium for bearing volatility risk"
                )
                if earnings_jump is not None and earnings_jump.priced:
                    vrp_earnings += (
                        f" — the panel above puts {earnings_jump.jump_share * 100:.0f}% of "
                        f"the expiry's variance on the event"
                    )
                vrp_earnings += (
                    ". Compare an expiry that settles before the announcement to see the "
                    "diffusive premium on its own."
                )
                st.warning(vrp_earnings.replace("**", ""), icon="📅")
                REPORT.note(vrp_earnings.replace("**", ""))
            st.caption(
                "This compares a forecast with the recent past, which is the weaker "
                "comparison. The true premium — implied against what follows — needs an "
                "implied-vol history no free source provides per ticker, so the panel on "
                "the right computes it market-wide instead."
            )
            REPORT.metrics([
                ("Implied − trailing realised", f"{ticker_vrp['spread_pts']:+,.1f} pts"),
                ("Implied / realised", f"{ticker_vrp['ratio']:,.2f}×"),
                ("Yang-Zhang 21d", f"{ticker_vrp['yang_zhang_pct']:.1f}%"),
                ("Realised percentile", f"{ticker_vrp['rv_percentile']:.0f}th"),
            ])
            REPORT.text(vrp_body)
        rv_table = vol.realized_vol_table(history)
        st.dataframe(
            rv_table.style.format("{:,.1f}", subset=[
                "close-to-close %", "Parkinson %", "Garman-Klass %", "Yang-Zhang %"]),
            width="stretch", hide_index=True,
        )
        REPORT.table(rv_table.round(1))

    with p2:
        rvfig = go.Figure()
        for window, color in ((21, BLUE), (63, LILAC)):
            series = vol.yang_zhang_vol(history, window).dropna() * 100
            rvfig.add_trace(go.Scatter(
                x=series.index, y=series, name=f"{window}-day realised",
                mode="lines", line=dict(color=color, width=2),
            ))
        if np.isfinite(front.atm_iv):
            # Carried as a trace rather than an annotated hline: the annotation
            # sits inside the plotting area, where three years of realised vol
            # runs straight through it. In the legend it is always readable.
            span = [history.index.min(), history.index.max()]
            rvfig.add_trace(go.Scatter(
                x=span, y=[front.atm_iv * 100] * 2,
                name=f"Implied {front.atm_iv * 100:.1f}% ({primary})",
                mode="lines", line=dict(color=TEAL, width=2, dash="dash"),
            ))
        layout(rvfig, 400, title=f"{symbol} realised volatility (Yang-Zhang)",
               xaxis_title=None, yaxis_title="Annualised vol (%)")
        chart(rvfig)
else:
    st.info("Not enough price history for the realised-volatility panels.")

st.subheader("Market-wide premium: VIX against subsequent realised")
try:
    vix = load_history(data.VIX_SYMBOL, "5y")
    spx = load_history(data.SPX_SYMBOL, "5y")
    series = vol.vrp_series(vix.close, spx.close, vol.VRP_HORIZON)
    stats = series.stats()
except Exception as exc:  # noqa: BLE001
    series, stats = None, {}
    st.warning(f"Could not load VIX/S&P history: {why(exc)}")

if series is not None and stats:
    q1, q2, q3, q4 = st.columns(4)
    q1.metric("VIX now", f"{series.latest_implied:,.1f}")
    q2.metric("Mean premium", f"{stats['mean']:+,.1f} pts",
              help=f"Average over {stats['n']} overlapping observations.")
    q3.metric("Positive share", f"{stats['positive_share'] * 100:,.0f}%",
              help="How often implied exceeded what followed.")
    q4.metric("Last complete reading", f"{stats['current']:+,.1f} pts",
              f"{stats['current_percentile']:,.0f}th percentile")
    REPORT.metrics([
        ("VIX now", f"{series.latest_implied:,.1f}"),
        ("Mean premium", f"{stats['mean']:+,.1f} pts"),
        ("Positive share", f"{stats['positive_share'] * 100:,.0f}%"),
        ("Last complete reading", f"{stats['current']:+,.1f} pts"),
    ])

    frame = series.frame
    vfig = go.Figure()
    vfig.add_trace(go.Scatter(x=frame.index, y=frame.implied, name="VIX (implied)",
                              mode="lines", line=dict(color=AMBER, width=2)))
    vfig.add_trace(go.Scatter(x=frame.index, y=frame.forward_rv,
                              name=f"Realised over the next {series.horizon} days",
                              mode="lines", line=dict(color=BLUE, width=2)))
    layout(vfig, 360, title="Implied vs subsequently realised volatility",
           xaxis_title=None, yaxis_title="Annualised vol (%)")
    chart(vfig)

    pfig = go.Figure()
    pfig.add_trace(go.Scatter(
        x=frame.index, y=frame.vrp, name="Premium", mode="lines",
        line=dict(color=BLUE, width=1.5), fill="tozeroy",
        fillcolor=BLUE_FILL.format(alpha=0.28),
    ))
    pfig.add_hline(y=0, line=dict(color=NEUTRAL, width=1))
    pfig.add_hline(y=stats["mean"], line=dict(color=LILAC, width=1.5, dash="dash"),
                   annotation_text=f"mean {stats['mean']:+.1f}")
    layout(pfig, 340, title="Volatility risk premium (VIX − subsequent realised)",
           xaxis_title=None, yaxis_title="Vol points")
    chart(pfig)

    vrp_note = (
        f"The premium is positive about {stats['positive_share'] * 100:.0f}% of the time — "
        "that persistence is why selling volatility makes money most months. The negative "
        "episodes are the ones that matter: they cluster, and they are much larger than the "
        f"positive ones. The last {series.horizon} trading days are blank because that "
        "realised vol has not happened yet."
    )
    st.caption(vrp_note)
    REPORT.note(vrp_note)

st.divider()

# --------------------------------------------------------------------------
# 6. Insider trades (SEC Form 4)
# --------------------------------------------------------------------------

st.header("Insider trades")
INSIDER_BLURB = (
    f"SEC Form 4 filings for {symbol} over the last {INSIDER_MONTHS} months, parsed from "
    "the raw XML. Officers, directors and 10% holders must file within two business days."
)
st.caption(INSIDER_BLURB)
REPORT.page_break()
REPORT.heading("6. Insider trades")
REPORT.text(INSIDER_BLURB)

try:
    with st.spinner("Pulling Form 4 filings..."):
        txns = load_insiders(symbol, INSIDER_MONTHS)
except Exception as exc:  # noqa: BLE001
    txns = pd.DataFrame()
    st.warning(f"Could not load insider filings: {why(exc)}")
    REPORT.note(f"Insider filings unavailable: {why(exc)}")

if txns.empty:
    st.info(f"No Form 4 filings for {symbol} in the last {INSIDER_MONTHS} months.")
else:
    summary = edgar.insider_summary(txns)
    if summary:
        i1, i2, i3, i4 = st.columns(4)
        i1.metric("Open-market buys", f"{summary['n_buys']}", f"{summary['unique_buyers']} insiders")
        i2.metric("Open-market sells", f"{summary['n_sells']}", f"{summary['unique_sellers']} insiders")
        i3.metric("Bought", money(summary["buy_value"], 1))
        i4.metric("Net", money(summary["net_value"], 1), "sold " + money(summary["sell_value"], 1))
        REPORT.metrics([
            ("Open-market buys", f"{summary['n_buys']} ({summary['unique_buyers']} insiders)"),
            ("Open-market sells", f"{summary['n_sells']} ({summary['unique_sellers']} insiders)"),
            ("Bought", money(summary["buy_value"], 1)),
            ("Sold", money(summary["sell_value"], 1)),
            ("Net", money(summary["net_value"], 1)),
        ])
    else:
        st.info("Filings exist, but none were open-market buys or sells.")

    clusters = edgar.cluster_buys(txns)
    if not clusters.empty:
        cluster_note = (
            f"Cluster buying detected. {int(clusters.insiders.iloc[0])} different "
            f"insiders bought on the open market within 30 days "
            f"({money(clusters.total_value.iloc[0], 1)} total): {clusters.names.iloc[0]}."
        )
        st.success(cluster_note.replace("Cluster buying detected.", "**Cluster buying detected.**")
                   .replace("$", "\\$"), icon="🔎")
        REPORT.text("<b>" + cluster_note + "</b>")

    trades = edgar.open_market(txns)
    if not trades.empty:
        flow = go.Figure()
        for code, color, label in (("P", GREEN, "Purchases"), ("S", RED, "Sales")):
            side = trades[trades.code == code]
            if side.empty:
                continue
            flow.add_bar(
                x=side.transaction_date, y=side.value.abs(), name=label,
                marker_color=color, customdata=side[["owner", "role"]],
                hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>$%{y:,.0f}<extra></extra>",
            )
        layout(flow, 340, barmode="relative", hovermode="closest",
               title="Open-market insider transactions",
               xaxis_title=None, yaxis_title="Transaction value ($)")
        chart(flow)

    insider_table = txns[["transaction_date", "owner", "role", "code", "code_meaning",
                          "shares", "price", "value"]].copy()
    insider_table.columns = ["Date", "Insider", "Role", "Code", "Meaning", "Shares", "Price", "Value"]
    with st.expander(f"All {len(txns)} reported transactions"):
        show = txns[["transaction_date", "owner", "role", "code", "code_meaning",
                     "shares", "price", "value", "shares_after", "derivative"]].copy()
        show.columns = ["Date", "Insider", "Role", "Code", "Meaning", "Shares",
                        "Price", "Value", "Holding after", "Derivative"]
        st.dataframe(
            show.style.format({
                "Date": lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "",
                "Shares": "{:,.0f}", "Price": "${:,.2f}", "Value": "${:,.0f}",
                "Holding after": "{:,.0f}",
            }),
            width="stretch", hide_index=True, height=340,
        )
        st.download_button("Download transactions CSV", txns.to_csv(index=False).encode(),
                           file_name=f"{symbol}_insiders.csv", mime="text/csv")

    CODE_NOTE = (
        "Only codes P and S are discretionary open-market trades and feed the totals "
        "above. A is a grant, M an option exercise, F shares withheld to pay tax on "
        "vesting — counting those as buying or selling is the usual way this data gets "
        "misread."
    )
    st.caption(CODE_NOTE.replace("codes P and S", "codes **P** and **S**"))
    insider_table = insider_table.copy()
    insider_table["Date"] = insider_table["Date"].dt.strftime("%Y-%m-%d")
    REPORT.table(insider_table.round(2), max_rows=14)
    REPORT.note(CODE_NOTE)

st.divider()

# --------------------------------------------------------------------------
# 7. Institutional activity in this ticker (SEC 13F)
# --------------------------------------------------------------------------

st.header("Institutional activity")
INST_BLURB = (
    f"Which managers bought and sold {symbol} last quarter, from their 13F filings. "
    "Managers over $100m must report long US equity positions 45 days after quarter end."
)
st.caption(INST_BLURB.replace(symbol, f"**{symbol}**", 1))
REPORT.heading("7. Institutional activity")
REPORT.text(INST_BLURB)

try:
    _, issuer_name = load_company(symbol)
except Exception as exc:  # noqa: BLE001
    issuer_name = ""
    st.warning(f"Could not resolve {symbol} on EDGAR: {why(exc)}")

if issuer_name:
    filings_by_manager: dict[str, list] = {}
    failures: list[str] = []
    progress = st.progress(0.0, text="Scanning 13F filings...")

    def _scanned(done: int, total: int, manager: str) -> None:
        # Runs on the main thread: in_parallel consumes completions itself.
        progress.progress(done / total, text=f"Scanning 13F filings… {manager}")

    # Each manager is five requests, and the roster is the same download for
    # every ticker. Fetching them together overlaps the waiting; the rate the
    # SEC actually sees is unchanged, held by the lock in edgar._throttle.
    scanned = in_parallel(
        {name: (lambda c=cik: load_manager_13f(c))
         for name, cik in edgar.KNOWN_MANAGERS.items()},
        on_done=_scanned,
    )
    for manager, result in scanned.items():
        if isinstance(result, Exception):
            failures.append(manager)  # one unparseable filer must not kill the scan
        else:
            filings_by_manager[manager] = result
    progress.empty()

    # The ticker's own CUSIP, where Yahoo gives a clean one. It is what lets a
    # fund every filer names after its sponsor ("STATE STR SPDR S&P 500 ETF T"
    # against EDGAR's "SPDR S&P 500 ETF TRUST") be recognised at all. edgar
    # corroborates it against the filed names before trusting it.
    seed = load_cusip(symbol)
    activity, matched_cusips = edgar.scan_managers(
        filings_by_manager, [issuer_name, symbol], {seed} if seed else None
    )

    if activity.empty:
        none_found = (
            f"None of the {len(edgar.KNOWN_MANAGERS)} managers scanned reported a position "
            f"in {issuer_name} in either of their last two filings."
        )
        st.info(none_found)
        REPORT.text(none_found)
    else:
        periods = activity.period.dropna()
        prior_periods = activity.prior_period.dropna()
        header = f"{len(activity)} of {len(edgar.KNOWN_MANAGERS)} managers hold or held it"
        if len(periods) and len(prior_periods):
            header += f" · {periods.max():%Y-%m-%d} vs {prior_periods.max():%Y-%m-%d}"
        st.markdown(f"**{header}**")
        REPORT.text(f"<b>{header}</b>")

        inst_metrics = [
            ("Buying", f"{int(activity.action.isin(['New', 'Added']).sum())} "
                       f"({int((activity.action == 'New').sum())} new)"),
            ("Selling", f"{int(activity.action.isin(['Trimmed', 'Exited']).sum())} "
                        f"({int((activity.action == 'Exited').sum())} exited)"),
            ("Shares held", f"{activity.shares_now.sum():,.0f}"),
            ("Change on the quarter", f"{activity.share_change.sum():+,.0f}"),
            ("Reported value", money(activity.value_now.sum(), 1)),
        ]
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("Buying", f"{int(activity.action.isin(['New', 'Added']).sum())}",
                  f"{int((activity.action == 'New').sum())} new")
        t2.metric("Selling", f"{int(activity.action.isin(['Trimmed', 'Exited']).sum())}",
                  f"{int((activity.action == 'Exited').sum())} exited")
        t3.metric("Shares held", f"{activity.shares_now.sum():,.0f}",
                  f"{activity.share_change.sum():+,.0f} on the quarter")
        t4.metric("Reported value", money(activity.value_now.sum(), 1),
                  money(activity.value_change.sum(), 1) + " change")
        REPORT.metrics(inst_metrics)

        # A fund that buys to change the company is saying something different
        # from one whose model happened to select it, so the activists carry a
        # label. It rides on the name rather than in a column of its own, which
        # keeps it attached to the manager in the chart as well as the tables,
        # and therefore in the PDF and markdown exports too.
        activity["label"] = activity.manager + activity.tag.map(
            lambda t: f" ({t})" if t else ""
        )

        # A row is only as current as that manager's own filing. Most report the
        # same quarter; one that has moved filing entity, or simply not filed
        # yet, sits further back -- and the header above names the newest
        # quarter on the page, not theirs. So the vintage goes on the row.
        newest_period = activity.period.max()
        behind = activity[activity.period.notna() & (activity.period < newest_period)]
        if len(behind):
            lagging = ", ".join(
                f"{r.label} ({r.period:%Y-%m-%d})" for _, r in behind.iterrows()
            )
            stale_note = (
                f"{len(behind)} of these report an older quarter than the rest: {lagging}. "
                "Their rows compare that manager's own two filings, so the changes are "
                "real, but they describe a different period from the others."
            )
            st.caption(stale_note)
            REPORT.note(stale_note)

        ordered = activity.sort_values("share_change")
        inst_chart = go.Figure()
        inst_chart.add_bar(
            x=ordered.share_change, y=ordered.label, orientation="h",
            marker_color=np.where(ordered.share_change >= 0, GREEN, RED),
            customdata=ordered[["action", "shares_now", "value_now"]],
            hovertemplate="%{y}<br>%{customdata[0]}<br>%{x:+,.0f} shares"
                          "<br>now holds %{customdata[1]:,.0f}<extra></extra>",
        )
        inst_chart.add_vline(x=0, line=dict(color=NEUTRAL, width=1))
        layout(inst_chart, max(340, 34 * len(ordered) + 130), hovermode="closest",
               title=f"Change in {symbol} position, by manager",
               xaxis_title="Change in shares held", yaxis_title=None)
        chart(inst_chart)

        inst_table = activity[["label", "action", "period", "shares_now", "share_change",
                               "share_change_pct", "value_now", "value_change",
                               "weight_now_pct"]].copy()
        inst_table.columns = ["Manager", "Action", "Quarter", "Shares", "Δ shares",
                              "Δ shares %", "Value", "Δ value", "% of their book"]
        inst_table["Quarter"] = inst_table["Quarter"].dt.strftime("%Y-%m-%d")
        st.dataframe(
            inst_table.style.format({
                "Shares": "{:,.0f}", "Δ shares": "{:+,.0f}", "Δ shares %": "{:+,.1f}%",
                "Value": "${:,.0f}", "Δ value": "${:+,.0f}", "% of their book": "{:,.2f}%",
            }),
            width="stretch", hide_index=True,
        )
        REPORT.table(inst_table.round(2))

    INST_CAVEAT = (
        "No free API answers who holds ticker X, so this pulls each manager's filings and "
        "looks for the issuer — the answer is only ever as broad as the roster. A 13F is "
        "filed up to 45 days after quarter end and is older still by the time you read it. "
        "It covers long US equity and listed options only: no shorts, no bonds, no foreign "
        "listings, no cash. Share classes are aggregated, so GOOGL and GOOG cannot be "
        "separated here. Managers tagged (activist) buy in order to change the company "
        "rather than to express a view on it, so a new position from one of them usually "
        "precedes something else."
    )
    with st.expander("Which managers are scanned, and the caveats"):
        st.markdown(
            "No free API answers *who holds ticker X*, so the only way to build this view "
            "is to pull each manager's filings and look for the issuer — which means the "
            "answer is only ever as broad as this roster. Add a CIK to "
            "`edgar.KNOWN_MANAGERS` to widen it.\n\n"
            + ", ".join(edgar.KNOWN_MANAGERS)
            + (f"\n\nCould not parse: {', '.join(failures)}." if failures else "")
            + (f"\n\nMatched CUSIP(s): {', '.join(sorted(matched_cusips))}." if matched_cusips else "")
        )
        st.markdown(
            "**Caveats.** A 13F is filed up to 45 days after quarter end and is older still "
            "by the time you read it. It covers long US equity and listed options only: no "
            "shorts, no bonds, no foreign listings, no cash. Share classes are aggregated — "
            "without a free ticker-to-CUSIP mapping there is no way to separate GOOGL from "
            "GOOG here. Filings are cached for six hours."
        )
    REPORT.note(INST_CAVEAT + " Roster: " + ", ".join(edgar.KNOWN_MANAGERS) + ".")

st.divider()

# --------------------------------------------------------------------------
# 8. Fundamentals (SEC XBRL)
# --------------------------------------------------------------------------

st.header("Fundamentals")
REPORT.page_break()
REPORT.heading("8. Fundamentals")

try:
    with st.spinner("Pulling XBRL company facts..."):
        facts, legal_name = load_fundamentals(symbol)
except Exception as exc:  # noqa: BLE001
    facts, legal_name = None, ""
    st.warning(f"Could not load XBRL facts: {why(exc)}")
    REPORT.note(f"XBRL facts unavailable: {why(exc)}")

if facts is not None and not facts.empty:
    st.caption(f"**{legal_name}** — quarterly, as reported to the SEC.")
    REPORT.text(f"<b>{legal_name}</b> — quarterly, as reported to the SEC.")

    money_cols = [c for c in facts.columns if "%" not in c]
    bars = go.Figure()
    for col, color in (("Revenue", BLUE), ("Net income", LILAC)):
        if col in facts:
            bars.add_bar(x=facts.index, y=facts[col], name=col, marker_color=color)
    layout(bars, 360, barmode="group", hovermode="x",
           title="Revenue and net income by quarter", xaxis_title=None, yaxis_title="USD")
    chart(bars)

    margin_cols = [c for c in facts.columns if c.endswith("margin %")]
    if margin_cols:
        mfig = go.Figure()
        for col, color in zip(margin_cols, (BLUE, LILAC, TEAL)):
            mfig.add_trace(go.Scatter(x=facts.index, y=facts[col], name=col,
                                      mode="lines+markers", line=dict(color=color, width=2)))
        layout(mfig, 340, title="Margins", xaxis_title=None, yaxis_title="%")
        chart(mfig)

    display = facts.copy()
    display.index = display.index.strftime("%Y-%m-%d")
    with st.expander("Quarterly figures"):
        st.dataframe(
            display.style.format(
                {c: "{:,.0f}" for c in money_cols}
                | {c: "{:+,.1f}%" for c in facts.columns if "%" in c}
            ),
            width="stretch", height=340,
        )

    XBRL_NOTE = (
        "Cash-flow lines are filed year-to-date, so they are un-cumulated into discrete "
        "quarters here — without that step Q4 comes through as the whole year. Where a "
        "company has migrated between XBRL tags (NVIDIA moved revenue from "
        "RevenueFromContractWithCustomerExcludingAssessedTax to Revenues in fiscal 2022) "
        "the alternatives are merged. Restatements are not back-propagated: these are the "
        "figures as filed at the time."
    )
    st.caption(XBRL_NOTE)

    # Percent columns and dollar columns are told apart by name, the way the
    # on-screen formatter above does it. Deciding it on magnitude gets it wrong
    # in both directions: a revenue that recovered twelvefold reports a YoY of
    # 1,150%, which clears the threshold and comes out as "0.00bn", and a
    # loss-making name's net margin of -2,400% goes the same way.
    pdf_facts = display.tail(8).reset_index()
    for col in pdf_facts.columns:
        if col == "period_end":
            continue
        as_pct = "%" in col
        pdf_facts[col] = pd.to_numeric(pdf_facts[col], errors="coerce").map(
            lambda v: "" if pd.isna(v) else (f"{v:,.1f}%" if as_pct else f"{v / 1e9:,.2f}bn")
        )
    REPORT.table(pdf_facts, max_rows=8)
    REPORT.note(XBRL_NOTE)

st.divider()

# --------------------------------------------------------------------------
# 9. Cointegration screen
# --------------------------------------------------------------------------

st.header("Cointegration screen")
st.caption(
    "Not a per-ticker panel: cointegration is a property of a *pair*, so this takes its "
    "own universe. Every pair is tested in both directions, refitted out of sample, and "
    "shown against the threshold that survives correcting for how many tests were run."
)

default_universe = f"{symbol}, AMD, AVGO, INTC, MU, QCOM, TSM, ASML"
with st.form("pairs_form"):
    c1, c2, c3 = st.columns([4, 1, 1])
    universe = c1.text_input("Tickers (comma separated)", value=default_universe)
    lookback = c2.selectbox("History", ["1y", "2y", "3y", "5y"], index=1)
    max_hl = c3.number_input("Max half-life (days)", 1, 500, 60, 5)
    run = st.form_submit_button("Run screen", type="primary")

# The submitted parameters are remembered rather than the button press. A form
# submit is true for exactly one run, so keying the section off `run` made the
# whole screen vanish the moment anything else on the page caused a rerun --
# building the PDF, for one, which then exported a report with no screen in it.
if run:
    st.session_state["screen_inputs"] = (universe, lookback, float(max_hl))

screen_inputs = st.session_state.get("screen_inputs")

if screen_inputs:
    universe_text, screen_lookback, screen_max_hl = screen_inputs
    tickers = tuple(sorted({t.strip().upper() for t in universe_text.split(",") if t.strip()}))
    if len(tickers) < 2:
        st.warning("Enter at least two tickers.")
    else:
        try:
            with st.spinner(f"Testing {len(tickers) * (len(tickers) - 1) // 2} pairs..."):
                closes, results = run_screen(tickers, screen_lookback, screen_max_hl)
        except Exception as exc:  # noqa: BLE001
            closes, results = None, None
            st.error(f"Screen failed: {why(exc)}")

        if results is not None and not results.empty:
            n_tests, threshold = pairs_mod.bonferroni_threshold(closes.shape[1])
            survivors = results[results.pvalue < threshold]
            stable = results[
                (results.pvalue_in_sample < 0.05) & (results.pvalue_out_of_sample < 0.05)
            ]

            s1, s2, s3 = st.columns(3)
            s1.metric("Pairs tested", f"{n_tests:,}",
                      f"{closes.shape[1]} tickers, {len(closes):,} days")
            s2.metric("Below p < 0.05", f"{int((results.pvalue < 0.05).sum()):,}",
                      f"{n_tests * 0.05:.1f} expected by chance")
            s3.metric("Survive Bonferroni", f"{len(survivors):,}", f"p < {threshold:.4f}")

            REPORT.heading("9. Cointegration screen")
            REPORT.text(
                f"Universe: {', '.join(tickers)} over {screen_lookback}, "
                f"maximum half-life {screen_max_hl:,.0f} days."
            )
            REPORT.metrics([
                ("Pairs tested", f"{n_tests:,}"),
                ("Below p < 0.05", f"{int((results.pvalue < 0.05).sum()):,}"),
                ("Survive Bonferroni", f"{len(survivors):,}"),
                ("Held out of sample", f"{len(stable):,}"),
            ])

            show = results[["pair", "pvalue", "pvalue_in_sample", "pvalue_out_of_sample",
                            "beta", "half_life", "zscore", "correlation"]].copy()
            show.columns = ["Pair", "p (full)", "p (in-sample)", "p (out-of-sample)",
                            "Hedge ratio", "Half-life (d)", "Z-score", "Correlation"]
            st.dataframe(
                show.style.format({
                    "p (full)": "{:.4f}", "p (in-sample)": "{:.4f}",
                    "p (out-of-sample)": "{:.4f}", "Hedge ratio": "{:,.3f}",
                    "Half-life (d)": "{:,.1f}", "Z-score": "{:+,.2f}",
                    "Correlation": "{:,.2f}",
                }),
                width="stretch", hide_index=True, height=320,
            )
            REPORT.table(show.round(4))

            if stable.empty:
                st.warning(
                    "No pair cointegrated on both the fitted and the held-out window. "
                    "That is the common outcome, and it is the useful one: it says the "
                    "relationships in this universe are not stable enough to trade.",
                    icon="⚠️",
                )
            else:
                st.success(
                    f"{len(stable)} pair(s) held up out of sample: "
                    f"{', '.join(stable.pair.head(5))}.", icon="✅",
                )

            picked = st.selectbox("Inspect a pair", results.pair.tolist())
            row = results[results.pair == picked].iloc[0]
            pair = pairs_mod.pair_from_row(row)
            frame = pairs_mod.spread_frame(closes, pair)

            z1, z2, z3, z4 = st.columns(4)
            z1.metric("Z-score now", f"{pair.zscore:+,.2f}")
            z2.metric("Half-life", f"{pair.half_life:,.1f} days"
                      if np.isfinite(pair.half_life) else "not mean-reverting")
            z3.metric("p out of sample", f"{pair.pvalue_out_of_sample:.4f}")
            z4.metric("Hedge ratio", f"{pair.beta:,.3f}",
                      help=f"log({pair.y}) − {pair.beta:.3f}·log({pair.x})")

            zfig = go.Figure()
            zfig.add_trace(go.Scatter(x=frame.index, y=frame.zscore, name="Spread z-score",
                                      mode="lines", line=dict(color=BLUE, width=2)))
            for level, dash in ((2, "dash"), (-2, "dash"), (0, "dot")):
                zfig.add_hline(y=level, line=dict(color=NEUTRAL, width=1, dash=dash))
            layout(zfig, 360, title=f"{picked} — spread z-score",
                   xaxis_title=None, yaxis_title="Standard deviations")
            chart(zfig)

            screen_note = (
                f"The hedge ratio is fitted on the first 70% of the window only, so the "
                f"right-hand portion of this chart is out of sample. p (full) = "
                f"{pair.pvalue:.4f} against a Bonferroni threshold of {threshold:.4f} for "
                f"{n_tests} tests — a pair that clears 0.05 but not the threshold is not "
                "evidence of anything."
            )
            st.caption(screen_note)
            REPORT.note(screen_note)

st.divider()
DISCLAIMER = (
    "Educational tool, not investment advice. Everything here is a descriptive statistic "
    "about prices, quotes and filings — none of it is a forecast. Quotes are delayed ~15 "
    "minutes while US markets are open and are the last session's closing book, then its "
    "closing prints, once they shut; open interest settles by a session, and 13F holdings "
    "lag by up to 45 days."
)
st.caption(DISCLAIMER)
REPORT.note(DISCLAIMER)

# --------------------------------------------------------------------------
# PDF export
#
# Built at the very end, once every figure has been registered. The sidebar slot
# was reserved before any of them existed, so the download button still lands up
# there next to the button that triggered the build.
# --------------------------------------------------------------------------

if build_pdf:
    try:
        with st.spinner(f"Rendering {REPORT.figure_count} charts into a PDF..."):
            st.session_state["pdf_bytes"] = REPORT.build()
            st.session_state["pdf_name"] = report_mod.filename(symbol)
            st.session_state["pdf_stamp"] = data.as_of()
    except Exception as exc:  # noqa: BLE001
        st.session_state.pop("pdf_bytes", None)
        st.sidebar.error(f"PDF build failed: {why(exc)}")

if st.session_state.get("pdf_bytes"):
    with pdf_slot.container():
        st.download_button(
            "Download PDF",
            st.session_state["pdf_bytes"],
            file_name=st.session_state.get("pdf_name", "dashboard.pdf"),
            mime="application/pdf",
            width="stretch",
        )
        st.caption(f"Built {st.session_state.get('pdf_stamp', '')}")

# The markdown export needs no build step -- it is text assembled from blocks
# that already exist, so it is always current and always ready to download.
with md_slot.container():
    st.download_button(
        "Download markdown (for AI)",
        mdreport.to_markdown(REPORT),
        file_name=mdreport.filename(symbol),
        mime="text/markdown",
        width="stretch",
        help="The same content as the PDF with every chart written out as numbers, "
             "sized to paste into a chat with a model.",
    )
