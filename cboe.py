"""Option chains from CBOE's free delayed-quote feed.

This is the primary source for the option book. Yahoo still supplies price
history, the T-bill and the index quotes, and remains the fallback here, but the
chains come from CBOE, for one reason: Yahoo enforces its rate limit per source
address, and the hosted copy of this dashboard runs on a Streamlit Community
Cloud address shared with every other app on that pool. The limit could be spent
by strangers before this app asked for anything, and the failure that produced
was total -- the page has nothing to draw without a chain. CBOE serves the same
data from a public CDN, which does not police datacentre addresses that way.

It is also fewer requests by an order of magnitude. Yahoo answers one expiration
per request, so a page needed about ten; CBOE returns the entire book for an
underlying in a single reply, and the app slices the expirations it wants out of
that. The reply is large -- five megabytes for SPY -- which is why it is parsed
once into a frame and cached, rather than re-read per expiration.

Quotes are delayed by roughly fifteen minutes, the same as Yahoo's, so nothing
about the freshness of the page changes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

EXCHANGE_TZ = "America/New_York"

_UA = "market-dashboard/1.0 (options research)"

# The columns prep._clean_side reads. Named as Yahoo names them because that is
# the shape the whole pipeline was built against; CBOE is translated into it
# rather than the pipeline being taught two vocabularies.
COLUMNS = [
    "strike", "bid", "ask", "lastPrice", "volume", "openInterest",
    "impliedVolatility", "lastTradeDate",
]


class CboeError(RuntimeError):
    """Raised when CBOE cannot be reached or does not list the symbol."""


def make_session():
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "application/json"})
    return session


def cboe_symbol(symbol: str) -> str:
    """Yahoo's spelling of a ticker, in CBOE's.

    Two differences, both silent failures if missed, because an unlisted symbol
    comes back as a 403 rather than as a 404. Share classes are separated by a
    dot rather than a hyphen -- BRK-B is BRK.B, and BRK-B, BRKB and BRK/B are
    all refused -- and an index carries a leading underscore rather than a
    caret, so ^VIX is _VIX.
    """
    s = symbol.strip().upper()
    if s.startswith("^"):
        return "_" + s[1:]
    return s.replace("-", ".")


def parse_occ(name: str) -> tuple[str, bool, float]:
    """Expiration, side and strike out of an OCC contract symbol.

    Read from the right-hand end rather than matched from the left. The fixed
    part of the symbol is the last fifteen characters -- six of date, one of
    C or P, eight of strike in thousandths -- and everything before it is the
    root. Anchoring on the root instead means guessing what may appear in it,
    which is exactly where a dotted share class would break.
    """
    if len(name) < 16 or name[-9] not in "CP":
        raise ValueError(f"Not an OCC option symbol: {name!r}")
    yy, mm, dd = name[-15:-13], name[-13:-11], name[-11:-9]
    return f"20{yy}-{mm}-{dd}", name[-9] == "C", int(name[-8:]) / 1000.0


@dataclass(frozen=True)
class Book:
    """Every listed contract on one underlying, as of one moment."""

    symbol: str
    spot: float
    contracts: pd.DataFrame  # COLUMNS, plus expiry and is_call

    @property
    def expirations(self) -> list[str]:
        return sorted(self.contracts.expiry.unique())

    def chain(self, expiry: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """One expiration split into calls and puts, shaped as Yahoo shapes it."""
        side = self.contracts[self.contracts.expiry == expiry]
        if side.empty:
            raise CboeError(f"CBOE lists no {expiry} contracts for {self.symbol}.")
        calls = side[side.is_call].reindex(columns=COLUMNS).reset_index(drop=True)
        puts = side[~side.is_call].reindex(columns=COLUMNS).reset_index(drop=True)
        return calls, puts


def _payload(symbol: str, session=None) -> dict:
    session = session or make_session()
    url = QUOTE_URL.format(symbol=cboe_symbol(symbol))
    try:
        response = session.get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001 - reported as one kind of failure
        raise CboeError(f"Could not reach CBOE: {exc}") from exc

    # An unlisted symbol is refused rather than missing: CBOE answers 403 for a
    # ticker it does not carry, so 403 has to be read as "no such chain" and not
    # as a blocked client, or a typo would look like an outage.
    if response.status_code == 403:
        raise CboeError(f"CBOE does not list options for '{symbol.upper()}'.")
    if response.status_code != 200:
        raise CboeError(f"CBOE returned HTTP {response.status_code} for '{symbol.upper()}'.")
    try:
        return response.json()["data"]
    except Exception as exc:  # noqa: BLE001
        raise CboeError(f"CBOE sent something unreadable for '{symbol.upper()}'.") from exc


def fetch_book(symbol: str, session=None) -> Book:
    """The whole option book for one underlying, in one request."""
    data = _payload(symbol, session)
    options = data.get("options") or []
    if not options:
        raise CboeError(f"CBOE listed no contracts for '{symbol.upper()}'.")

    frame = pd.DataFrame(options)
    parsed = [parse_occ(name) for name in frame["option"]]
    frame["expiry"] = [p[0] for p in parsed]
    frame["is_call"] = [p[1] for p in parsed]
    frame["strike"] = [p[2] for p in parsed]

    # CBOE stamps the print in exchange-local time with no offset on it. Read as
    # UTC -- which is what the caller does with Yahoo's, because Yahoo's carries
    # one -- an afternoon trade would land four hours early and, near enough to
    # midnight, on the wrong date. The date is what separates a closing print
    # from a relic, so it is localised here rather than downstream.
    stamped = pd.to_datetime(frame["last_trade_time"], errors="coerce")
    frame["lastTradeDate"] = (
        stamped.dt.tz_localize(EXCHANGE_TZ, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert("UTC")
    )

    frame = frame.rename(columns={
        "last_trade_price": "lastPrice",
        "open_interest": "openInterest",
        "iv": "impliedVolatility",
    })
    for col in ("bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"):
        frame[col] = pd.to_numeric(frame.get(col), errors="coerce")

    spot = pd.to_numeric(pd.Series([data.get("current_price")]), errors="coerce").iloc[0]
    if not pd.notna(spot) or spot <= 0:
        raise CboeError(f"CBOE quoted no price for '{symbol.upper()}'.")

    keep = COLUMNS + ["expiry", "is_call"]
    return Book(symbol.strip().upper(), float(spot), frame.reindex(columns=keep))
