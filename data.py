"""Market data retrieval from Yahoo Finance.

All network I/O lives here. Quotes are delayed roughly 15 minutes and open
interest settles overnight, so nothing in this dashboard is real time -- the
refresh button re-pulls whatever Yahoo currently has.

``yfinance`` defaults to ``curl_cffi``, whose certificate verification is broken
on some Windows installs (``CertificateVerifyError``). :func:`make_session`
hands it a plain ``requests`` session instead, which uses ``certifi`` and works
everywhere.
"""

from __future__ import annotations

import threading
import time

import pandas as pd

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 13-week T-bill yield, quoted by Yahoo in percent. The best free proxy for the
# short risk-free rate that discounts a listed option chain.
RISK_FREE_SYMBOL = "^IRX"
RISK_FREE_FALLBACK = 0.04

EXCHANGE_TZ = "America/New_York"

VIX_SYMBOL = "^VIX"
SPX_SYMBOL = "^GSPC"


def make_session():
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "application/json,text/html,*/*"})
    return session


# Yahoo publishes no rate limit, but it does enforce one per source IP, and a
# shared host is the case that matters: on Streamlit Community Cloud this app's
# requests leave from an address it shares with every other app on that pool,
# most of a page load's budget having already been spent by strangers. So the
# rate is paced here rather than being left to the size of the thread pool. The
# lock is held across the sleep on purpose, exactly as the SEC one in edgar is:
# that spaces request *starts* for the process as a whole, which lets several
# workers overlap the latency of their replies without ever raising the rate
# Yahoo sees. A cold page load is about sixteen requests, so the interval below
# costs a few seconds and holds the peak to four a second.
YAHOO_INTERVAL = 0.25

# How long to stop asking after Yahoo has said no, lengthening each time it says
# it again. Refreshing the page re-runs the script, and a failed load caches
# nothing, so every refresh fires the whole burst again -- which keeps the
# limiter's window saturated and makes the block outlast itself. One minute was
# the first guess and it was too short: a block on a datacentre address outlasts
# it, so the app came back every minute, was refused again, and looked to the
# reader like a page frozen on the same sentence. The ladder backs off to half
# an hour; any success anywhere resets it to the beginning.
COOLDOWN_LADDER = (60.0, 300.0, 900.0, 1800.0)

_pace_lock = threading.Lock()
_last_request = 0.0
_blocked_until = 0.0
_refusals = 0  # consecutive times Yahoo has refused a whole retry ladder


def _pace() -> None:
    """Block until this thread may start another Yahoo request."""
    global _last_request
    with _pace_lock:
        wait = YAHOO_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _cooldown_remaining() -> float:
    return max(0.0, _blocked_until - time.monotonic())


def _begin_cooldown() -> float:
    """Start the next step of the backoff, and say how long it is."""
    global _blocked_until, _refusals
    _refusals += 1
    wait = COOLDOWN_LADDER[min(_refusals, len(COOLDOWN_LADDER)) - 1]
    _blocked_until = time.monotonic() + wait
    return wait


def _clear_cooldown() -> None:
    """A reply came back, so the address is not blocked. Start the ladder over."""
    global _blocked_until, _refusals
    _refusals = 0
    _blocked_until = 0.0


def _spell(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    return f"{seconds / 60:.0f} minutes"


# The two situations below look identical to a reader and are not the same
# thing, which the first version of this message got wrong: it reported the
# full cooldown either way, so a restarted process always said "60s" and the
# page looked frozen on one sentence. Saying which of the two it is also
# answers the question the reader asks next, because rebooting the app is the
# obvious thing to try and it cannot work -- the refusal is Yahoo's, and it
# is remembered at their end against the address, not at ours.
def _refused_now(attempts: int, wait: float) -> RuntimeError:
    return RuntimeError(
        f"Yahoo Finance refused {attempts} requests in a row from this address just "
        f"now, so the page has nothing to draw. Not asking again for {_spell(wait)}. "
        "Rebooting the app will not help: the refusal is recorded at Yahoo's end "
        "against the address, not held here. On Streamlit Cloud that address is "
        "shared with every other app on the pool, so the limit can already be spent "
        "before this app asks for anything."
    )


def _still_blocked(remaining: float) -> RuntimeError:
    return RuntimeError(
        f"Yahoo Finance refused this address a moment ago. Waiting {_spell(remaining)} "
        "more before trying again -- asking sooner only restarts its clock. Reload the "
        "page after that; a reboot does not shorten it, because the refusal is "
        "remembered at Yahoo's end rather than here."
    )


def _drop_stale_crumb() -> None:
    """Forget the crumb yfinance minted, so the next attempt asks for a new one.

    yfinance 1.5.1 assigns the response body to its cached crumb *before* it
    checks the status code, so a 429 leaves the literal text "Too Many Requests"
    sitting there as the crumb. It reuses that on the next call without going
    near the network, and only re-mints once the bad crumb has cost another
    rejected request. Clearing it here means a retry actually retries.
    """
    try:
        from yfinance.data import SingletonMeta, YfData

        instance = SingletonMeta._instances.get(YfData)
        if instance is not None:
            instance._crumb = None
    except Exception:  # noqa: BLE001 - a private detail of a pinned version
        pass


def _retry(fn, attempts: int = 4, base_delay: float = 1.5):
    """Run ``fn`` against Yahoo, paced, with backoff through its rate limiter."""
    from yfinance.exceptions import YFRateLimitError

    remaining = _cooldown_remaining()
    if remaining > 0:
        raise _still_blocked(remaining)

    last: Exception | None = None
    for i in range(attempts):
        _pace()
        try:
            result = fn()
        except YFRateLimitError as exc:
            last = exc
            _drop_stale_crumb()
            if i < attempts - 1:
                time.sleep(base_delay * (2**i))
        else:
            _clear_cooldown()
            return result
    raise _refused_now(attempts, _begin_cooldown()) from last


def get_ticker(symbol: str, session=None):
    import yfinance as yf

    return yf.Ticker(symbol.strip().upper(), session=session or make_session())


def fetch_expirations(symbol: str, session=None) -> list[str]:
    ticker = get_ticker(symbol, session)
    expirations = list(_retry(lambda: ticker.options))
    if not expirations:
        raise ValueError(f"No listed options found for '{symbol.upper()}'.")
    return expirations


def fetch_spot(symbol: str, session=None) -> float:
    ticker = get_ticker(symbol, session)

    def _read() -> float:
        info = ticker.fast_info
        for key in ("lastPrice", "last_price", "regularMarketPrice", "previousClose"):
            try:
                value = info[key]
            except (KeyError, TypeError):
                continue
            if value:
                return float(value)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"No price data for '{symbol.upper()}'.")
        return float(hist["Close"].iloc[-1])

    return _retry(_read)


def fetch_raw_chain(symbol: str, expiration: str, session=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Raw calls/puts frames for one expiration, straight from Yahoo."""
    ticker = get_ticker(symbol, session)
    chain = _retry(lambda: ticker.option_chain(expiration))
    return chain.calls.copy(), chain.puts.copy()


def fetch_cusip(symbol: str, session=None) -> str | None:
    """The security's CUSIP, from its ISIN, or None if there isn't a clean one.

    A US ISIN is the country code, the nine-character CUSIP and a check digit,
    so the CUSIP falls straight out of it. Anything else is refused rather than
    guessed at: the lookup behind this is a name search and does return the
    wrong company sometimes (it answers a Canadian ISIN for GOOGL), and only the
    US form is unambiguous enough to slice. Callers treat None as "no extra
    information", so a miss costs nothing.
    """
    ticker = get_ticker(symbol, session)
    try:
        _pace()
        isin = ticker.isin
    except Exception:  # noqa: BLE001 - a missing ISIN must not fail the page
        return None
    if not isinstance(isin, str) or len(isin) != 12 or not isin.startswith("US"):
        return None
    cusip = isin[2:11].upper()
    return cusip if cusip.isalnum() else None


def fetch_history(symbol: str, period: str = "3y", session=None) -> pd.DataFrame:
    """Daily OHLCV, split/dividend adjusted, indexed by date."""
    ticker = get_ticker(symbol, session)
    hist = _retry(lambda: ticker.history(period=period, auto_adjust=True))
    if hist.empty:
        raise ValueError(f"No price history for '{symbol.upper()}'.")
    hist = hist.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    return hist.dropna(subset=["close"])


def fetch_closes(symbols: list[str], period: str = "2y", session=None) -> pd.DataFrame:
    """Aligned close-price panel for several tickers, used by the pairs screen."""
    import yfinance as yf

    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if len(symbols) < 2:
        raise ValueError("Need at least two tickers.")

    raw = _retry(
        lambda: yf.download(
            symbols,
            period=period,
            auto_adjust=True,
            progress=False,
            group_by="column",
            session=session or make_session(),
        )
    )
    if raw is None or raw.empty:
        raise ValueError("Yahoo returned no history for those tickers.")

    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(symbols[0])
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    # Drop tickers Yahoo could not resolve rather than poisoning every pair with NaN.
    closes = closes.dropna(axis=1, how="all").dropna()
    if closes.shape[1] < 2:
        raise ValueError("Fewer than two tickers had usable overlapping history.")
    return closes


def fetch_earnings_dates(symbol: str, session=None) -> list[pd.Timestamp]:
    """Announcement timestamps in exchange-local time, oldest first.

    Never raises. Earnings dates are the flakiest thing Yahoo serves: the primary
    endpoint is a scrape that intermittently returns a frame with no date column
    at all (observed failing and then succeeding for the same ticker minutes
    apart), and ETFs legitimately have none. Every caller treats an empty list as
    "no earnings information", so a failure here quietly removes the earnings
    annotations rather than taking the page down.

    ``ticker.calendar`` is deliberately not used as a fallback even though it
    looks like the obvious one. For an after-close reporter it returns the *next*
    calendar day -- NVDA's 2026-11-17 16:00 ET announcement comes back as
    2026-11-18 -- so it would silently shift every event by a day. The unix
    timestamp on ``info`` carries the actual instant and is used instead.
    """
    ticker = get_ticker(symbol, session)
    stamps: list[pd.Timestamp] = []

    try:
        frame = ticker.get_earnings_dates(limit=12)
        if frame is not None and len(frame):
            stamps = [pd.Timestamp(i) for i in frame.index]
    except Exception:  # noqa: BLE001 - see docstring; absence is a valid answer
        pass

    if not stamps:
        try:
            epoch = (ticker.info or {}).get("earningsTimestamp")
            if epoch:
                stamps = [pd.Timestamp(int(epoch), unit="s", tz="UTC")]
        except Exception:  # noqa: BLE001
            pass

    out = []
    for stamp in stamps:
        local = (stamp.tz_localize(EXCHANGE_TZ) if stamp.tzinfo is None
                 else stamp.tz_convert(EXCHANGE_TZ))
        out.append(local)
    return sorted(out)


def fetch_risk_free_rate(session=None) -> tuple[float, str]:
    """Short risk-free rate as a decimal, and where it came from.

    Re-read live on every cache miss, so the rate tracks the T-bill on its own
    without anyone editing anything. The source string names the instrument only
    -- the number itself is formatted by the caller, which stops it appearing
    twice in the same sentence.
    """
    try:
        pct = fetch_spot(RISK_FREE_SYMBOL, session)
        if 0.0 <= pct < 25.0:
            return pct / 100.0, f"the {RISK_FREE_SYMBOL} 13-week T-bill"
    except Exception:  # noqa: BLE001 - the rate is a nicety, never a blocker
        pass
    return RISK_FREE_FALLBACK, "a fallback constant (^IRX unavailable)"


def as_of() -> str:
    """Now, on the exchange's clock rather than this machine's.

    The dashboard can be run from anywhere; a US option chain is only ever
    meaningful on New York time. Stamped with the local clock, a run at five in
    the morning in New York read as a late afternoon one -- precisely when the
    reader most needs to know the market has been shut for hours.
    """
    return pd.Timestamp.now(tz=EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M %Z")
