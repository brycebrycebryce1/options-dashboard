"""Gamma exposure (GEX) and the zero-gamma flip level.

The idea: option dealers sit on the other side of customer flow and hedge with
the underlying. If their net book is *long gamma* they must sell into rallies
and buy into dips to stay delta neutral, which damps realised volatility. If it
is *short gamma* they must do the opposite, which amplifies moves. The price at
which net gamma crosses zero -- the **flip level** -- is the boundary between
those two regimes.

Per strike, in dollars of dealer delta bought per 1% move in spot:

    GEX(K) = gamma(K) * OI(K) * 100 * S^2 * 0.01

signed positive for calls and negative for puts. That sign is the standard
convention (SqueezeMetrics, SpotGamma), and it encodes an *assumption*: that
customers are net sellers of calls (covered-call and buy-write overwriting)
and net buyers of puts (portfolio protection), which puts the dealer on the
long side of the calls and the short side of the puts. Open interest carries no
sign, so there is no way to verify this from the chain -- on tickers where the
flow runs the other way, customers buying calls outright to chase upside or
selling puts for premium, the sign is simply wrong and every conclusion drawn
from it inverts. Treat GEX as a positioning hypothesis, not a measurement.

The profile is recomputed at each candidate spot with every strike's implied
vol held fixed (sticky-strike), which is the standard assumption and the one
that keeps the flip level well defined.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import blackscholes as bs
from prep import ExpirySnapshot

CONTRACT_MULTIPLIER = 100
PROFILE_POINTS = 241


@dataclass(frozen=True)
class GexResult:
    """Gamma exposure across strikes and across candidate spot prices."""

    by_strike: pd.DataFrame  # strike, call_gex, put_gex, net_gex, call_oi, put_oi
    profile: pd.DataFrame  # spot, net_gex
    total_gex: float  # net GEX at the current spot, $ per 1% move
    flip: float  # spot where net GEX crosses zero, NaN if it never does
    spot: float
    call_oi: float
    put_oi: float
    weight: str = "open_interest"

    @property
    def regime(self) -> str:
        if not np.isfinite(self.flip):
            return "positive gamma" if self.total_gex >= 0 else "negative gamma"
        return "above flip (positive gamma)" if self.spot > self.flip else "below flip (negative gamma)"

    @property
    def put_call_oi_ratio(self) -> float:
        return self.put_oi / self.call_oi if self.call_oi else float("nan")


def has_open_interest(snaps: list[ExpirySnapshot]) -> bool:
    """Whether the source actually reported any open interest.

    Yahoo zeroes the whole ``openInterest`` column for stretches at a time,
    especially outside US market hours. That is indistinguishable from a chain
    with genuinely no positions, so it has to be checked before the caller can
    trust a gamma number.
    """
    return any(float(s.quotes.open_interest.sum()) > 0 for s in snaps)


def _per_strike_inputs(snaps: list[ExpirySnapshot], weight: str = "open_interest") -> pd.DataFrame:
    """Collapse one or more expiries to strike x side, carrying size, vol and T.

    Expiries are kept as separate rows rather than summed, because gamma depends
    on time to expiry -- a 30-delta strike one week out and the same strike three
    months out contribute completely different gamma per contract.
    """
    if weight not in {"open_interest", "volume"}:
        raise ValueError(f"weight must be 'open_interest' or 'volume', got {weight!r}")

    frames = []
    for snap in snaps:
        q = snap.quotes.copy()
        # A strike's own implied vol is used only where the quote it came from
        # passed the quality filters; everywhere else the expiry's at-the-money
        # vol is borrowed, so the strike still contributes its (small) gamma
        # rather than vanishing.
        #
        # Trusting the vol of a *rejected* quote is the trap here, and it is not
        # a small one. Reading only `isfinite(iv)` let several hundred strikes
        # per ticker -- most of them carrying real open interest -- price their
        # gamma at implied vols of 120% to 390% against chain at-the-money vols
        # near 40%, because a stale wing print inverts to an absurd vol that is
        # still a finite number. Measured on live chains that moved total gamma
        # exposure by -20% to +19% and the flip level by up to a third of a
        # dollar. The filter already knew those quotes were untrustworthy; this
        # is just honouring it.
        atm = snap.atm_iv
        trusted = q.usable & np.isfinite(q.iv) & (q.iv > 0.01)
        q["iv"] = q.iv.where(trusted, atm)
        q["size"] = q[weight]
        q = q[np.isfinite(q.iv) & (q["size"] > 0)]
        if q.empty:
            continue
        q["T"] = snap.T
        q["df"] = snap.df
        # Carry ratio F/S rather than the forward itself: it is what stays fixed
        # when we shift the spot to trace the profile.
        q["carry"] = snap.forward / snap.spot
        q["expiry"] = snap.expiry
        frames.append(q[["strike", "is_call", "iv", "size", "T", "df", "carry", "expiry"]])

    if not frames:
        label = "open interest" if weight == "open_interest" else "volume"
        raise ValueError(
            f"No strike on the selected expiries has both {label} and a usable implied "
            "vol. Yahoo blanks open interest for stretches at a time, especially outside "
            "US market hours."
        )
    return pd.concat(frames, ignore_index=True)


def _gex_at(spot, rows: pd.DataFrame) -> np.ndarray:
    """Dollar gamma per 1% spot move, per row, signed +calls / -puts.

    ``spot`` may be a scalar or a vector of candidate prices; the return has
    shape (len(spot), len(rows)) in the vector case.
    """
    spot = np.atleast_1d(np.asarray(spot, float))[:, None]
    # Sticky-strike: each strike keeps its implied vol as the spot moves, and the
    # forward slides with it at a constant carry ratio.
    forward = spot * rows.carry.to_numpy()[None, :]
    gamma = bs.spot_gamma(
        spot,
        forward,
        rows.strike.to_numpy()[None, :],
        rows["T"].to_numpy()[None, :],
        rows.iv.to_numpy()[None, :],
        rows.df.to_numpy()[None, :],
    )
    sign = np.where(rows.is_call.to_numpy()[None, :], 1.0, -1.0)
    size = rows["size"].to_numpy()[None, :]
    return sign * gamma * size * CONTRACT_MULTIPLIER * spot**2 * 0.01


def gamma_exposure(
    snaps: list[ExpirySnapshot],
    spot: float,
    window: float = 0.25,
    points: int = PROFILE_POINTS,
    weight: str = "open_interest",
) -> GexResult:
    """Net dealer gamma by strike, plus the profile across spot and its flip level.

    ``weight`` selects what each strike's gamma is multiplied by. Open interest
    is the real measure -- it is the position that has to be hedged. Volume is a
    fallback for when the source has blanked open interest, and it answers a
    different question: where gamma was *traded* today, not where it sits.
    """
    rows = _per_strike_inputs(snaps, weight)

    here = _gex_at(spot, rows)[0]
    rows = rows.assign(gex=here)
    by_strike = (
        rows.assign(
            call_gex=np.where(rows.is_call, rows.gex, 0.0),
            put_gex=np.where(rows.is_call, 0.0, rows.gex),
            call_oi=np.where(rows.is_call, rows["size"], 0.0),
            put_oi=np.where(rows.is_call, 0.0, rows["size"]),
        )
        .groupby("strike", as_index=False)[["call_gex", "put_gex", "call_oi", "put_oi"]]
        .sum()
        .sort_values("strike")
        .reset_index(drop=True)
    )
    by_strike["net_gex"] = by_strike.call_gex + by_strike.put_gex

    grid = np.linspace(spot * (1 - window), spot * (1 + window), points)
    profile_vals = _gex_at(grid, rows).sum(axis=1)
    profile = pd.DataFrame({"spot": grid, "net_gex": profile_vals})

    return GexResult(
        by_strike=by_strike,
        profile=profile,
        total_gex=float(here.sum()),
        flip=find_flip(grid, profile_vals, spot),
        spot=float(spot),
        call_oi=float(by_strike.call_oi.sum()),
        put_oi=float(by_strike.put_oi.sum()),
        weight=weight,
    )


def find_flip(grid: np.ndarray, values: np.ndarray, spot: float) -> float:
    """Zero crossing of the gamma profile nearest the spot, linearly interpolated."""
    sign_change = np.signbit(values[:-1]) != np.signbit(values[1:])
    idx = np.flatnonzero(sign_change)
    if idx.size == 0:
        return float("nan")

    crossings = []
    for i in idx:
        y0, y1 = values[i], values[i + 1]
        x0, x1 = grid[i], grid[i + 1]
        crossings.append(x0 if y1 == y0 else x0 - y0 * (x1 - x0) / (y1 - y0))
    crossings = np.array(crossings)
    return float(crossings[int(np.argmin(np.abs(crossings - spot)))])
