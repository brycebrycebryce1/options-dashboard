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


def _retry(fn, attempts: int = 4, base_delay: float = 1.5):
    """Retry ``fn`` through Yahoo's rate limiter with exponential backoff."""
    from yfinance.exceptions import YFRateLimitError

    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except YFRateLimitError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2**i))
    raise RuntimeError(
        "Yahoo Finance is rate limiting this machine. Wait a minute and hit Refresh."
    ) from last


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
