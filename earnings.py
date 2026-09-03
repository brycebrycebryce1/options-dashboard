"""Earnings dates, and the jump they put into the volatility surface.

An earnings announcement is the one scheduled event big enough to dominate
everything else on this page. An expiry that spans one and an expiry that does
not are not comparable objects: the first prices a diffusion plus a jump, the
second prices a diffusion. Reading them off the same term-structure chart
without saying which is which is how a perfectly ordinary event premium gets
mistaken for a mispricing.

Two things are computed here.

**Which expiries span the event.** Not as simple as comparing dates. A company
reporting after the close on Tuesday moves the stock on Wednesday, so a Tuesday
expiry does *not* capture that earnings move even though it falls on the
announcement date. The rule below maps the announcement to the session its move
lands in, then an expiry spans the event if it settles on or after that session.

**How much of the priced variance is the event.** Total implied variance is
additive in time, so if the last expiry before the announcement prices a purely
diffusive variance and the first one after prices diffusion plus a jump::

    sigma_post^2 * T_post  =  sigma_pre^2 * T_post  +  J^2

which solves for the jump directly::

    J = sqrt(sigma_post^2 * T_post - sigma_pre^2 * T_post)

J is the one-session move the chain is charging for, as a fraction of spot.

The assumption doing the work is that the diffusive volatility is the same over
both windows -- that the only difference between the two expiries is the event.
That is never exactly true and is worst when the pre-earnings anchor is a very
short expiry, where quoted vol is inflated by weekend effects and by the
discreteness of a chain with three strikes near the money. The estimate carries
a warning when its anchor is under two days out, and the share of variance
attributable to the jump is reported alongside the jump itself, because a large
jump on a small share is a different statement from a large jump on a large one.

Nothing here is a forecast. It is a reading of what the chain charges.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from prep import QUOTES_LIVE_FROM, ExpirySnapshot, exchange_clock

# Announcements land either side of the session, essentially never mid-session.
# A stamp at or after this hour is read as "after today's close", which puts the
# move in the next session. Yahoo's times are approximate -- a large cap that
# reports after the close is sometimes stamped 15:00 -- so the cut sits well
# before the close rather than on it, which errs toward the next session.
AFTER_CLOSE_HOUR = 12

# Below this, the pre-earnings anchor is too short-dated to be a clean read on
# diffusive volatility.
MIN_ANCHOR_DTE = 2.0


@dataclass(frozen=True)
class Event:
    """One scheduled announcement, and the session its move lands in."""

    announced: pd.Timestamp  # exchange-local
    move_date: date
    after_close: bool

    @property
    def label(self) -> str:
        when = "after the close" if self.after_close else "before the open"
        return f"{self.announced:%Y-%m-%d} {when}"

    def days_away(self, now: datetime | None = None) -> float:
        # Counted on the exchange's date, like every other maturity here.
        return (self.move_date - exchange_clock(now).date()).days + 0.5


def _next_session(day: date) -> date:
    """The next weekday. Exchange holidays are not modelled."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def to_event(stamp: pd.Timestamp) -> Event:
    """Map an announcement timestamp to the session its move lands in."""
    after_close = stamp.hour >= AFTER_CLOSE_HOUR
    day = stamp.date()
    move = _next_session(day) if after_close else day
    # A before-the-open announcement on a weekend day still moves Monday.
    while move.weekday() >= 5:
        move += timedelta(days=1)
    return Event(announced=stamp, move_date=move, after_close=after_close)


def next_event(stamps: list[pd.Timestamp], now: datetime | None = None) -> Event | None:
    """The soonest announcement whose move the chain on screen has not yet seen.

    The move date itself is the awkward one. Testing ``move_date >= today`` kept
    an announcement pending for the whole of the session it had already moved,
    which is not a cosmetic error: the picker reads this to choose the primary
    expiry, so a stock that reported last night spent the next day anchored to
    whichever weekly captured the jump instead of to its monthly, and the page
    warned about an event that was hours in the past.

    The moment it flips is the feed's, not the exchange's. The jump lands at the
    open, but until the delayed quotes catch up at 09:45 the chain being plotted
    is still the previous close's, and that chain genuinely does still price the
    event. Calling it past any earlier would move the primary off the only
    expiry that knows about it.
    """
    clock = exchange_clock(now)
    today = clock.date()
    seen = (clock.hour, clock.minute) >= QUOTES_LIVE_FROM
    future = [e for e in (to_event(s) for s in stamps)
              if e.move_date > today or (e.move_date == today and not seen)]
    return min(future, key=lambda e: e.move_date) if future else None


def spans(expiry: str, event: Event | None) -> bool:
    """Whether an expiry settles on or after the session the move lands in."""
    if event is None:
        return False
    return date.fromisoformat(expiry) >= event.move_date


@dataclass(frozen=True)
class JumpEstimate:
    """The event premium implied by a pre/post pair of expiries."""

    pre_expiry: str
    post_expiry: str
    pre_iv: float
    post_iv: float
    pre_dte: float
    post_dte: float
    jump: float  # implied one-session move, as a fraction of spot
    jump_share: float  # fraction of the post expiry's total variance
    floor: float  # smallest jump distinguishable from quote noise
    reliable: bool
    note: str

    @property
    def priced(self) -> bool:
        return np.isfinite(self.jump) and self.jump > 0


def _atm_iv_error(snap: ExpirySnapshot) -> float:
    """Vol uncertainty near the money, in vol points.

    Each quote already carries how precisely its own bid-ask pins its implied
    vol (half-spread divided by vega). The median of the strikes nearest the
    forward is the natural read on how well the chain pins its at-the-money vol.
    """
    otm = snap.otm()
    if otm.empty:
        return float("nan")
    near = otm.reindex(otm.log_moneyness.abs().sort_values().index).head(8)
    return float(np.nanmedian(near.iv_error.to_numpy(float)))


def decompose(snaps: list[ExpirySnapshot], event: Event | None) -> JumpEstimate | None:
    """Split the first post-earnings expiry's variance into diffusion and jump.

    Returns ``None`` when the selected expiries do not bracket the event, which
    is the common case -- most of the time every expiry on screen sits on the
    same side of it.
    """
    if event is None:
        return None

    usable = [s for s in snaps if np.isfinite(s.atm_iv) and s.atm_iv > 0]
    ordered = sorted(usable, key=lambda s: s.T)
    before = [s for s in ordered if not spans(s.expiry, event)]
    after = [s for s in ordered if spans(s.expiry, event)]
    if not before or not after:
        return None

    # The last expiry before the event is the cleanest read on diffusive vol:
    # closest in time, so least exposed to genuine term structure.
    pre, post = before[-1], after[0]

    total_var = post.atm_iv**2 * post.T
    diffusive_var = pre.atm_iv**2 * post.T
    jump_var = total_var - diffusive_var

    # The jump is a difference of two measured variances, so it inherits the
    # uncertainty of both. Without this floor the estimate reports a jump
    # whenever quote noise happens to fall the right way: two chains built from
    # the *same* volatility recover at-the-money vols a hundredth of a point
    # apart, which is enough to manufacture a spurious 0.05% earnings move and
    # announce it as a finding. d(jump_var) = 2T sqrt((s_post d_post)^2 +
    # (s_pre d_pre)^2), from the derivative of sigma^2 T.
    var_error = 2.0 * post.T * float(np.hypot(
        post.atm_iv * _atm_iv_error(post), pre.atm_iv * _atm_iv_error(pre)
    ))
    if not np.isfinite(var_error):
        var_error = 0.0
    floor = float(np.sqrt(var_error))

    reliable = pre.dte >= MIN_ANCHOR_DTE
    if jump_var <= var_error:
        raw = float(np.sqrt(jump_var)) if jump_var > 0 else 0.0
        if jump_var <= 0:
            movement = (f"implied vol *falls* from {pre.atm_iv * 100:.1f}% at {pre.expiry} to "
                        f"{post.atm_iv * 100:.1f}% at {post.expiry}, across the event")
        else:
            movement = (f"implied vol rises only from {pre.atm_iv * 100:.1f}% to "
                        f"{post.atm_iv * 100:.1f}%, which works out at a ±{raw * 100:.2f}% "
                        f"move — inside the ±{floor * 100:.2f}% these quotes can resolve")
        note = (
            f"No earnings premium here can be told apart from quote noise: {movement}. The "
            f"floor comes from the bid-ask widths of the strikes near the money on both "
            "expiries, so on a chain quoted this loosely an event of this size is simply not "
            "measurable. That is a statement about the quotes, not about the event."
        )
        return JumpEstimate(pre.expiry, post.expiry, pre.atm_iv, post.atm_iv,
                            pre.dte, post.dte, 0.0, 0.0, floor, reliable, note)

    jump = float(np.sqrt(jump_var))
    share = float(jump_var / total_var) if total_var > 0 else float("nan")
    note = (
        f"Implied vol goes from {pre.atm_iv * 100:.1f}% at {pre.expiry} to "
        f"{post.atm_iv * 100:.1f}% at {post.expiry}, the first expiry that captures the "
        f"move. Holding diffusive vol at the near expiry's level, the extra variance is a "
        f"one-session move of ±{jump * 100:.2f}%, which is {share * 100:.0f}% of everything "
        f"{post.expiry} prices. The quote-noise floor on that estimate is "
        f"±{floor * 100:.2f}%."
    )
    if not reliable:
        note += (
            f" The anchor expiry is only {pre.dte:.1f} days out, where quoted vol is "
            "distorted by weekend decay and by how few strikes sit near the money, so "
            "read the size loosely."
        )
    return JumpEstimate(pre.expiry, post.expiry, pre.atm_iv, post.atm_iv,
                        pre.dte, post.dte, jump, share, floor, reliable, note)


def describe(event: Event | None, expiry: str, now: datetime | None = None) -> str:
    """One sentence on where an expiry sits relative to the next announcement."""
    if event is None:
        return ("No upcoming earnings date is available for this ticker, so nothing on this "
                "page is adjusted for one.")
    away = event.days_away(now)
    if spans(expiry, event):
        return (f"{expiry} spans earnings — announced {event.label}, moving the "
                f"{event.move_date:%d %b} session, {away:.0f} days out. The vol quoted for it "
                "prices a scheduled jump on top of ordinary day-to-day movement.")
    return (f"{expiry} settles before earnings — announced {event.label}, moving the "
            f"{event.move_date:%d %b} session, {away:.0f} days out. The vol quoted for it is "
            "diffusive only, with no event premium in it.")
