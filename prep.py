"""Turn one raw option chain into a clean, self-consistent expiry snapshot.

Every downstream analytic (density, gamma exposure, surface, expected move)
consumes an :class:`ExpirySnapshot` rather than raw Yahoo columns, so the
cleaning rules and the forward/vol conventions live in exactly one place.

Two deliberate choices:

* **The forward is read off the chain, not assumed.** Put-call parity says
  C - P = df (F - K), so a regression of C - P on K across the liquid strikes
  recovers F without needing a dividend forecast or a borrow rate. Everything
  else keys off that forward, which keeps moneyness, deltas and the density
  mutually consistent.
* **Implied vol is recomputed from mid prices, not taken from Yahoo.** Yahoo's
  ``impliedVolatility`` field is frequently stale, occasionally built off the
  last trade instead of the mid, and unreliable on wide markets. It is kept only
  as a fallback for strikes with no usable two-sided quote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import blackscholes as bs
from data import EXCHANGE_TZ

# How the feeds are named in the line under the title. They are not
# interchangeable overnight, so the reader is told which one is talking.
SOURCE_CBOE = "CBOE"
SOURCE_YAHOO = "yfinance"

# A quote must be positive and not absurdly wide to be trusted.
MAX_RELATIVE_SPREAD = 1.5
MIN_PRICE = 0.01

# What a one-sided quote's price uncertainty is taken to be, as a fraction of
# the quoted side. A strike showing an ask and no bid tells us the value is
# somewhere in (0, ask]; that is a whole-magnitude uncertainty, not the
# penny-wide one a two-sided quote implies.
ONE_SIDED_SPREAD_FRACTION = 1.0
UNQUOTED_IV_ERROR = 0.75  # vol points of noise assumed when there is no live market at all

# What a closing print's price uncertainty is taken to be, as a fraction of the
# print. Outside US trading hours Yahoo returns a bid and ask of zero on every
# strike, so the only price left is the last trade -- which is the previous
# session's closing quote, and worth showing. It is not worth showing at the
# precision of a live market, though, and the reason is asynchrony rather than
# the spread: the strikes in one chain last printed at different moments of the
# session, and the underlying moved between them. A quarter of the premium is
# wide enough to cover that and still narrow enough for the smile fit to prefer
# a well-traded strike to a wing.
PRINT_SPREAD_FRACTION = 0.25

# Wings below one delta are excluded. Not because their prices are wrong, but
# because vol is barely identified there: a deep out-of-the-money option quoted
# at the minimum tick can imply almost any volatility, and on a 9-day NVDA chain
# including those quotes pushes the smile fit's residual from 0.5 vol points to
# 5. They also carry essentially none of the distribution's mass, so dropping
# them costs nothing and buys a far more stable fit.
MIN_ABS_DELTA = 0.01

# The wing filter is applied at the chain's at-the-money vol rather than at each
# strike's own implied vol. Using the strike's own vol makes the test circular:
# a stale print inflates the price, the inflated price implies an absurd vol,
# and the absurd vol inflates the delta enough to clear the floor. AMD on a
# 9-day chain admitted an $810 strike on a $469 spot that way -- eight standard
# deviations out -- which alone pushed the reported implied skew to +3.3.


@dataclass(frozen=True)
class ExpirySnapshot:
    """One expiration, cleaned and enriched.

    ``quotes`` is one row per listed contract with columns:
    ``strike, is_call, bid, ask, mid, spread, volume, open_interest,
    yahoo_iv, iv, vega, delta, gamma, log_moneyness, iv_error, usable,
    price_source, last_trade``.
    """

    symbol: str
    expiry: str
    spot: float
    dte: float
    T: float
    r: float
    df: float
    forward: float
    quotes: pd.DataFrame
    forward_method: str = "parity regression"
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # Whether the chain had a two-sided market, the session its prices came
    # from, and whether the regular session was open when it was read. With the
    # feed's name and the moment it was read, these are what :attr:`price_basis`
    # turns into the line under the title.
    live: bool = True
    session: date | None = None
    session_open: bool = True
    source: str = SOURCE_YAHOO
    # The clock the snapshot was built against, kept so the age of the open
    # interest can be judged later without asking the wall clock again -- which
    # would make the caption disagree with the maturities in a test, or across
    # midnight in production.
    asof: datetime | None = None

    @property
    def open_interest_age(self) -> str:
        """How the sizes behind the quotes stand relative to the quotes.

        Reported rather than assumed. Whether the feed is serving any open
        interest at all is a question for the data -- Yahoo zeroes the column
        outright overnight -- and whether it has caught up is a question for the
        clock. Only in the window between the clearing run and the next open do
        the two agree, and that is the only time gamma exposure is built from a
        single session rather than two.
        """
        if float(self.quotes.open_interest.sum()) <= 0:
            return "no OI reported"
        if self.session is not None and open_interest_caught_up(self.session, self.asof):
            return "OI updated"
        return "OI not yet updated"

    @property
    def price_basis(self) -> str:
        """What the plotted quotes are, in words fit for a caption.

        Six forms, one per feed and state. The feed is named because the two do
        not behave alike once the market shuts: CBOE keeps its closing book up
        until the next open, while Yahoo withholds it in the small hours and
        leaves only each strike's last print. A reader who cannot tell them
        apart cannot tell a two-sided book from a set of prints either.
        """
        if self.live and self.session_open:
            return f"live quotes from {self.source}, delayed ~15 min"
        if self.live:
            # A two-sided book outside the session is the last close's book.
            # Calling it live would mislabel the page for the whole of a
            # Singapore working day, which is New York's evening and night.
            if self.session is None:
                return f"closing quotes from {self.source}, market shut"
            return (f"closing quotes from {self.source} for the "
                    f"{self.session:%Y-%m-%d} session, {self.open_interest_age}")
        if self.session is None:
            return f"last trades from {self.source}, of unknown age"
        return (f"closing prints from {self.source} for the "
                f"{self.session:%Y-%m-%d} session, {self.open_interest_age}")

    @property
    def implied_carry(self) -> float:
        """r - q implied by the chain's own forward, annualised."""
        if self.T <= 0 or self.spot <= 0:
            return float("nan")
        return float(np.log(self.forward / self.spot) / self.T)

    @property
    def implied_div_yield(self) -> float:
        return self.r - self.implied_carry

    @property
    def atm_iv(self) -> float:
        """Vol at the forward, interpolated across the usable quotes.

        The call and the put at one strike share a log-moneyness, so the grid
        arrives with tied abscissae -- on a liquid chain about half the rows are
        tied. ``np.interp`` resolves a tie by whichever row happens to sort
        first, which made this number move with row order rather than with the
        market: on NVDA's 2026-09-18 chain the same quotes gave 33.31%, 33.13%
        or 32.95% depending only on that. Put-call parity says the two sides
        agree at the same strike, so the disagreement is quote noise. Average it
        away first, and the answer is the same whatever order the rows came in.
        """
        use = self.quotes[self.quotes.usable]
        if len(use) < 2:
            return float("nan")
        mid = use.groupby("log_moneyness", as_index=False).iv.mean()
        if len(mid) < 2:
            return float("nan")
        mid = mid.sort_values("log_moneyness")
        return float(np.interp(0.0, mid.log_moneyness, mid.iv))

    def otm(self) -> pd.DataFrame:
        """Out-of-the-money quotes only: puts below the forward, calls above.

        OTM options carry essentially all the information in a chain -- they are
        the liquid side, and their value is pure time value, so a small quote
        error perturbs the implied vol far less than it would for a deep ITM
        contract trading mostly on intrinsic.
        """
        q = self.quotes
        keep = np.where(q.strike < self.forward, ~q.is_call, q.is_call)
        return q[keep & q.usable].sort_values("strike").reset_index(drop=True)


# Months whose monthly expiration is also a quarterly one, when index futures,
# index options, stock options and single-stock futures all expire together.
QUAD_WITCHING_MONTHS = {3, 6, 9, 12}

# The regular US session on the exchange's clock. It runs to 16:15 rather than
# the 16:00 equity close because index options trade the extra quarter hour and
# the quotes Yahoo serves are fifteen minutes behind in any case.
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 15)
# The free feeds run about a quarter-hour behind, which is why the session is
# treated as closing at 16:15 rather than 16:00. The delay applies at the other
# end too: until 09:45 the chain a caller receives is still the last close's
# book, however open the market is.
QUOTES_LIVE_FROM = (9, 45)
# When the overnight clearing run reaches the feed. Open interest is tallied by
# OCC after the close, so the sizes served during a session belong to the
# *previous* one; this is the hour they catch up. Measured on CBOE's SPY file on
# 2026-09-03, where the figures were unchanged at 04:56 and updated by 05:06 --
# one observation, so it is a named constant rather than a magic number.
OI_REFRESH = (5, 0)
_EXCHANGE = ZoneInfo(EXCHANGE_TZ)


def exchange_clock(now: datetime | None = None) -> datetime:
    """``now`` on the exchange's clock.

    Every date here -- days to expiry, whether the session is open, which
    session a closing book belongs to -- is a question about New York, not
    about wherever the page is served from. Run from Singapore, the local date
    is already tomorrow for the whole New York afternoon; on Streamlit Cloud the
    container's clock is UTC, which turns over at 8pm New York. Read naively,
    either one shortens every maturity by a day for part of each day. A naive
    ``now`` is taken to be exchange time already.
    """
    if now is None:
        return datetime.now(_EXCHANGE)
    if now.tzinfo is None:
        return now.replace(tzinfo=_EXCHANGE)
    return now.astimezone(_EXCHANGE)


def in_session(now: datetime | None = None) -> bool:
    """Whether the regular US session is open. Exchange holidays are not modelled."""
    clock = exchange_clock(now)
    if clock.weekday() >= 5:
        return False
    return SESSION_OPEN <= (clock.hour, clock.minute) < SESSION_CLOSE


def quotes_from_this_session(now: datetime | None = None) -> bool:
    """Whether the delayed feed has caught up to the current session's quotes.

    :func:`in_session` answers whether the market is open. This answers the
    narrower question of whether the prices on screen belong to today, and for
    the first quarter-hour after the open they do not -- they are still the
    last close's book, served beside an underlying price that has been moving
    with the pre-market since about 05:00. Anything derived from those quotes
    belongs at the close they were struck at.
    """
    clock = exchange_clock(now)
    if clock.weekday() >= 5:
        return False
    return QUOTES_LIVE_FROM <= (clock.hour, clock.minute) < SESSION_CLOSE


def open_interest_caught_up(session: date, now: datetime | None = None) -> bool:
    """Whether the clearing run for ``session`` has published its open interest.

    The run lands in the small hours of the following calendar day, which is why
    this is asked against the quotes' session rather than the wall clock alone: a
    Friday close is only caught up from Saturday morning, and stays caught up
    across the weekend until Monday's open makes the question moot.
    """
    clock = exchange_clock(now)
    day = clock.date()
    if day <= session:
        return False
    if day > session + timedelta(days=1):
        return True
    return (clock.hour, clock.minute) >= OI_REFRESH


def last_closed_session(now: datetime | None = None) -> date:
    """The most recent weekday whose regular session has already closed."""
    clock = exchange_clock(now)
    day = clock.date()
    if (clock.hour, clock.minute) < SESSION_CLOSE:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def third_friday(year: int, month: int) -> date:
    """The standard monthly options expiration date."""
    first = date(year, month, 1)
    # weekday(): Monday is 0, Friday is 4.
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def is_opex(expiry: str) -> bool:
    """Whether an expiration is the monthly cycle rather than a weekly.

    Monthly expiries carry the overwhelming majority of open interest, are the
    only ones with LEAPS behind them, and are where pinning and dealer hedging
    effects concentrate -- so it is worth being able to see at a glance which
    date in a list is one.
    """
    day = date.fromisoformat(expiry)
    return day == third_friday(day.year, day.month)


def expiry_label(expiry: str, now: datetime | None = None) -> str:
    """Human label for a pick list: date, days out, and the monthly-expiry tag."""
    dte, _ = year_fraction(expiry, now)
    tag = ""
    if is_opex(expiry):
        day = date.fromisoformat(expiry)
        tag = " (OPEX quarterly)" if day.month in QUAD_WITCHING_MONTHS else " (OPEX)"
    return f"{expiry} · {dte:.0f}d{tag}"


def year_fraction(expiry: str, now: datetime | None = None) -> tuple[float, float]:
    """Days and years to expiry. Expiration is taken as midday on the listed date.

    Calendar days over 365 is the market convention for quoting maturities. The
    half-day offset keeps same-day expiries finite instead of dividing by zero.
    Today is the exchange's date, not the caller's: see :func:`exchange_clock`.
    """
    now = exchange_clock(now)
    exp = date.fromisoformat(expiry)
    days = (exp - now.date()).days + 0.5
    days = max(days, 0.25)  # never let T collapse to zero
    return days, days / 365.0


def implied_forward(strikes, call_mid, put_mid, df, spot) -> tuple[float, str]:
    """Recover the forward from put-call parity: C - P = df (F - K).

    Regressing C - P on K over the strikes nearest the money gives slope -df and
    intercept df*F, so F = -intercept/slope. Falls back to the single strike
    where |C - P| is smallest, then to spot, as the chain degrades.
    """
    strikes, call_mid, put_mid = (np.asarray(x, float) for x in (strikes, call_mid, put_mid))
    both = np.isfinite(call_mid) & np.isfinite(put_mid) & (call_mid > 0) & (put_mid > 0)
    if both.sum() < 2:
        return float(spot), "spot (no two-sided strikes)"

    k, c, p = strikes[both], call_mid[both], put_mid[both]
    diff = c - p
    # Parity is most reliable near the money, where both legs are liquid.
    centre = int(np.argmin(np.abs(diff)))
    lo, hi = max(0, centre - 6), min(len(k), centre + 7)
    ks, ds = k[lo:hi], diff[lo:hi]

    if len(ks) >= 3:
        slope, intercept = np.polyfit(ks, ds, 1)
        if slope < -1e-6:
            fwd = -intercept / slope
            if 0.2 * spot < fwd < 5.0 * spot:
                return float(fwd), "parity regression"

    fwd = k[centre] + diff[centre] / df
    if 0.2 * spot < fwd < 5.0 * spot:
        return float(fwd), "parity at nearest-ATM strike"
    return float(spot), "spot (parity rejected)"


def _clean_side(frame: pd.DataFrame, is_call: bool) -> pd.DataFrame:
    cols = ["strike", "bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"]
    out = frame.reindex(columns=cols).copy()
    out.columns = ["strike", "bid", "ask", "last", "volume", "open_interest", "yahoo_iv"]
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    # When a strike last traded is what separates a closing quote from a trade
    # that happened months ago on a strike nobody has touched since, so the
    # timestamp is carried rather than dropped. Parsed after the numeric loop
    # above, which would turn it into NaN.
    out["last_trade"] = pd.to_datetime(
        frame.reindex(columns=["lastTradeDate"])["lastTradeDate"], errors="coerce", utc=True
    )
    out = out.dropna(subset=["strike"])
    # Yahoo omits open interest and volume on illiquid strikes; absent means zero.
    out[["volume", "open_interest"]] = out[["volume", "open_interest"]].fillna(0.0)
    out["is_call"] = is_call

    bid, ask = out.bid.fillna(0.0), out.ask.fillna(0.0)
    two_sided = (bid > 0) & (ask > 0) & (ask >= bid)
    mid = np.where(two_sided, 0.5 * (bid + ask), np.nan)

    # One-sided or unquoted strikes fall back to the last trade: stale, but
    # better than dropping the strike out of the grid entirely. The last trade
    # still has to be consistent with whatever side is live, though. A print
    # above the current offer, or below the current bid, is a stale trade that
    # the market has since moved away from, and taking it at face value quietly
    # corrupts everything built on the smile.
    last = out.last
    coherent = np.isfinite(last) & (last > 0)
    coherent &= np.where(ask > 0, last <= ask, True)
    coherent &= np.where(bid > 0, last >= bid, True)
    out["mid"] = np.where(np.isfinite(mid), mid, np.where(coherent, last, np.nan))
    out["price_source"] = np.where(
        np.isfinite(mid), "quote", np.where(coherent, "print", "none")
    )

    # A one-sided quote bounds the value on one side only, so its uncertainty is
    # its own magnitude rather than a bid-ask width. Recording that here is what
    # stops the smile fit treating a lone penny offer as a precise observation.
    one_sided_width = ONE_SIDED_SPREAD_FRACTION * np.where(ask > 0, ask, bid)
    out["spread"] = np.where(
        two_sided, ask - bid,
        np.where((ask > 0) | (bid > 0), one_sided_width, np.nan),
    )
    out["two_sided"] = two_sided
    return out.groupby(["strike", "is_call"], as_index=False).first()


def _within_wings(q: pd.DataFrame, fwd: float, spot: float, T: float, disc: float) -> pd.Series:
    """Delta filter evaluated at a robust chain-wide vol, not each strike's own.

    A strike's own implied vol is derived from its own price, so using it to
    judge whether that price is trustworthy is circular -- the worse the price,
    the larger the vol it implies, and the more delta it appears to carry. The
    median vol of the strikes nearest the forward is not perfect, but it cannot
    be moved by the strike being tested.
    """
    near = q[np.isfinite(q.iv) & (q.iv > 0.01) & (q.iv < 4.0)]
    if not near.empty:
        near = near.reindex(near.log_moneyness.abs().sort_values().index).head(10)
    ref = float(near.iv.median()) if not near.empty else float("nan")
    if not np.isfinite(ref) or ref <= 0.01:
        # Nothing to anchor on; fall back to each strike's own vol rather than
        # rejecting the whole chain.
        ref_delta = q.delta
    else:
        ref_delta = pd.Series(
            bs.spot_delta(spot, fwd, q.strike.to_numpy(), T, ref, q.is_call.to_numpy(), disc),
            index=q.index,
        )
    return ref_delta.abs().between(MIN_ABS_DELTA, 1.0 - MIN_ABS_DELTA)


def build_snapshot(
    symbol: str,
    expiry: str,
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
    r: float,
    now: datetime | None = None,
    source: str = SOURCE_YAHOO,
) -> ExpirySnapshot:
    """Clean one expiration into an :class:`ExpirySnapshot`."""
    dte, T = year_fraction(expiry, now)
    disc = float(np.exp(-r * T))
    open_now = in_session(now)

    quotes = (
        pd.concat([_clean_side(calls, True), _clean_side(puts, False)], ignore_index=True)
        .sort_values(["strike", "is_call"])
        .reset_index(drop=True)
    )
    if quotes.empty:
        raise ValueError(f"Empty option chain for {symbol} {expiry}.")

    # Forward from parity, using strikes quoted on both sides.
    wide = quotes.pivot_table(index="strike", columns="is_call", values="mid", aggfunc="first")
    empty = pd.Series(index=wide.index, dtype=float)
    fwd, method = implied_forward(
        wide.index.to_numpy(),
        wide[True].to_numpy() if True in wide.columns else empty.to_numpy(),
        wide[False].to_numpy() if False in wide.columns else empty.to_numpy(),
        disc,
        spot,
    )

    q = quotes
    # A print priced off the most recent session in this chain is a closing
    # quote; an older one is a relic. Both arrive in the same column with bid
    # and ask blanked, so the print's own date is the only thing that tells
    # them apart. Give the fresh ones an assumed spread and they flow through
    # the existing weighting, tolerance and usability rules unchanged; leave
    # the stale ones with no spread and those rules keep rejecting them, which
    # is what they already did to the whole chain overnight.
    #
    # Only a strike with no spread at all is eligible. A print is also what
    # prices a strike that has one live side and no other, and that case
    # already has a width -- the offer's own magnitude, which is deliberately
    # pessimistic so the fit cannot mistake a lone penny offer for a precise
    # observation. Overwriting it here narrowed such a strike by four to eight
    # times and handed it that much more pull on the smile, intraday, on
    # exactly the illiquid wings the rule above exists to distrust.
    printed_on = pd.to_datetime(q.last_trade, errors="coerce", utc=True)
    printed_on = printed_on.dt.tz_convert(EXCHANGE_TZ).dt.normalize()
    latest = printed_on.max()
    fresh = (
        q.price_source.eq("print")
        & q.spread.isna()
        & printed_on.notna()
        & printed_on.eq(latest)
    )
    q["spread"] = q.spread.where(~fresh, PRINT_SPREAD_FRACTION * q.mid)

    q["log_moneyness"] = np.log(q.strike / fwd)
    q["iv"] = bs.implied_vol(q.mid, fwd, q.strike, T, q.is_call, disc)
    # Fall back to the feed's own IV only where our inversion has nothing to work with.
    fallback = (
        ~np.isfinite(q.iv) & np.isfinite(q.yahoo_iv) & (q.yahoo_iv > 0.01) & (q.yahoo_iv < 4.0)
    )
    q["iv_source"] = np.where(fallback, "yahoo", np.where(np.isfinite(q.iv), "mid", "none"))
    q["iv"] = np.where(fallback, q.yahoo_iv, q.iv)

    q["vega"] = bs.black_vega(fwd, q.strike, T, q.iv, disc)
    q["delta"] = bs.spot_delta(spot, fwd, q.strike, T, q.iv, q.is_call, disc)
    q["gamma"] = bs.spot_gamma(spot, fwd, q.strike, T, q.iv, disc)

    # Quote noise expressed in vol points: half the bid-ask spread divided by
    # vega. This is what lets the smile fit weight each strike by how precisely
    # the market actually pins its vol, instead of treating a penny-wide ATM
    # quote and a two-dollar-wide wing quote as equally informative.
    with np.errstate(divide="ignore", invalid="ignore"):
        iv_error = 0.5 * q.spread / q.vega
    # An unknown spread means an unknown price, so it earns the worst weight on
    # the scale rather than an average one.
    q["iv_error"] = np.clip(
        np.where(np.isfinite(iv_error), iv_error, UNQUOTED_IV_ERROR), 0.002, 0.75
    )

    rel_spread = q.spread / q.mid.replace(0, np.nan)
    q["usable"] = (
        np.isfinite(q.iv)
        & (q.iv > 0.01)
        & (q.iv < 4.0)
        & (q.mid >= MIN_PRICE)
        # An unmeasurable spread still fails the test rather than passing it:
        # filling with the threshold itself let every unquoted strike through,
        # because the comparison is inclusive. Overnight that rejected the whole
        # chain, which is why a print from the last session is given a spread
        # above and an older one is left without.
        & (rel_spread <= MAX_RELATIVE_SPREAD)
        & _within_wings(q, fwd, spot, T, disc)
    )

    warns = []
    # Whether the page is showing a live market or the last one, judged on the
    # quotes it will actually plot rather than on whether a two-sided quote
    # exists anywhere. A chain can keep five live strikes out of a hundred and
    # seventy overnight, and calling that live would put the wrong label on a
    # page built almost entirely from prints.
    use = q[q.usable]
    live = bool(use.two_sided.sum() > (~use.two_sided).sum()) if len(use) else False
    if not live and pd.notna(latest):
        warns.append(
            f"No live market on this chain: every bid and ask has been blanked, so these "
            f"are the closing prints of the {latest:%Y-%m-%d} session. That is what the "
            "yfinance fallback does in the small hours -- CBOE keeps its own book up until "
            "the next open -- and the prints are the market's last word rather than a "
            "stale quote, but the strikes printed at different moments of that session, "
            "so the smile is less precise than it would be intraday."
        )
    elif not live:
        warns.append(
            "No two-sided quotes on this chain and no dated prints to age them by: every "
            "price here is a last trade of unknown vintage."
        )
    if method.startswith("spot"):
        warns.append("Forward fell back to spot: put-call parity was unusable on this chain.")
    if int(q.usable.sum()) < 8:
        warns.append(f"Only {int(q.usable.sum())} usable quotes; results will be coarse.")
    if dte < 1.5:
        warns.append("Under a day to expiry: greeks and the density get numerically fragile.")

    # Which session the prices belong to. Closing prints carry their own date.
    # A two-sided book read outside the session is the last close's book, and
    # the day it closed is a question for the clock rather than for the prints:
    # on an illiquid chain the newest print can predate the book by days.
    session = latest.date() if pd.notna(latest) else None
    if live and not open_now:
        session = last_closed_session(now)

    return ExpirySnapshot(
        symbol=symbol,
        expiry=expiry,
        spot=float(spot),
        dte=dte,
        T=T,
        r=float(r),
        df=disc,
        forward=fwd,
        quotes=q,
        forward_method=method,
        warnings=tuple(warns),
        live=live,
        session=session,
        session_open=open_now,
        source=source,
        asof=now,
    )
