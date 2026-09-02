"""Expected move: what the option market is charging for the move to expiry.

Two numbers get conflated constantly, so this module reports both separately.

**Expected absolute move.** The at-the-money straddle is worth exactly the
discounted expected size of the move:

    straddle = df * E|S_T - F|

so E|S_T - F| = straddle / df. This is a *mean absolute deviation*, not a
standard deviation.

**One standard deviation.** The move that brackets roughly 68% of outcomes:

    1 sigma = F * sigma_ATM * sqrt(T)

For a lognormal these differ by a fixed factor: E|S_T - F| = sqrt(2/pi) * sigma
= 0.798 sigma. The widespread habit of calling the straddle price "the expected
move" therefore understates the 1-sigma band by about 20%, and the widespread
fix of multiplying the straddle by 0.85 is a fudge for the same thing.

The forward, not the spot, is the centre of the distribution. Over a week that
distinction is noise; over a LEAP on a dividend payer it is not.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from prep import ExpirySnapshot

# E|X| / sigma for a zero-mean normal.
MAD_TO_SIGMA = float(np.sqrt(2.0 / np.pi))


@dataclass(frozen=True)
class ExpectedMove:
    expiry: str
    dte: float
    T: float
    spot: float
    forward: float
    atm_iv: float
    straddle: float  # ATM straddle mid, in dollars
    expected_abs_move: float  # E|S_T - F|, dollars
    one_sigma: float  # dollars
    source: str

    @property
    def one_sigma_pct(self) -> float:
        return self.one_sigma / self.spot * 100.0

    @property
    def expected_abs_move_pct(self) -> float:
        return self.expected_abs_move / self.spot * 100.0

    def band(self, sigmas: float = 1.0) -> tuple[float, float]:
        return self.forward - sigmas * self.one_sigma, self.forward + sigmas * self.one_sigma


def atm_straddle(snap: ExpirySnapshot) -> tuple[float, str]:
    """Mid price of the straddle struck at the forward.

    Call + put is smooth and convex in strike, so linear interpolation between
    the two strikes bracketing the forward is accurate. Only strikes quoted on
    both sides are used.
    """
    wide = snap.quotes.pivot_table(index="strike", columns="is_call", values="mid", aggfunc="first")
    if True not in wide.columns or False not in wide.columns:
        return float("nan"), "no two-sided strikes"

    pairs = wide.dropna()
    pairs = pairs[(pairs[True] > 0) & (pairs[False] > 0)]
    if pairs.empty:
        return float("nan"), "no two-sided strikes"

    strikes = pairs.index.to_numpy(float)
    straddle = (pairs[True] + pairs[False]).to_numpy(float)
    if snap.forward < strikes.min() or snap.forward > strikes.max():
        nearest = int(np.argmin(np.abs(strikes - snap.forward)))
        return float(straddle[nearest]), f"nearest strike {strikes[nearest]:g} (forward outside chain)"
    return float(np.interp(snap.forward, strikes, straddle)), "interpolated at the forward"


def expected_move(snap: ExpirySnapshot) -> ExpectedMove:
    straddle, source = atm_straddle(snap)
    atm_iv = snap.atm_iv
    one_sigma = snap.forward * atm_iv * np.sqrt(snap.T) if np.isfinite(atm_iv) else float("nan")

    if np.isfinite(straddle):
        expected_abs = straddle / snap.df
    elif np.isfinite(one_sigma):
        expected_abs = MAD_TO_SIGMA * one_sigma
        source = "implied from ATM vol (no straddle quote)"
    else:
        expected_abs = float("nan")

    # If the vol interpolation failed but a straddle exists, back the sigma out
    # of the straddle instead of leaving the whole row empty.
    if not np.isfinite(one_sigma) and np.isfinite(expected_abs):
        one_sigma = expected_abs / MAD_TO_SIGMA

    return ExpectedMove(
        expiry=snap.expiry,
        dte=snap.dte,
        T=snap.T,
        spot=snap.spot,
        forward=snap.forward,
        atm_iv=atm_iv,
        straddle=straddle,
        expected_abs_move=expected_abs,
        one_sigma=one_sigma,
        source=source,
    )


def move_term_structure(moves: list[ExpectedMove]) -> pd.DataFrame:
    """One row per expiry: the implied cone, ready to chart."""
    rows = []
    for m in moves:
        lo1, hi1 = m.band(1.0)
        lo2, hi2 = m.band(2.0)
        rows.append(
            {
                "expiry": m.expiry,
                "dte": m.dte,
                "atm_iv": m.atm_iv,
                "straddle": m.straddle,
                "expected_abs_move": m.expected_abs_move,
                "expected_abs_move_pct": m.expected_abs_move_pct,
                "one_sigma": m.one_sigma,
                "one_sigma_pct": m.one_sigma_pct,
                "forward": m.forward,
                "lower_1sd": lo1,
                "upper_1sd": hi1,
                "lower_2sd": lo2,
                "upper_2sd": hi2,
            }
        )
    return pd.DataFrame(rows).sort_values("dte").reset_index(drop=True)


def historical_moves(closes: pd.Series, horizon_days: int) -> dict:
    """What the stock actually did over the same horizon, historically.

    Overlapping windows: uses every start date, not just non-overlapping blocks.
    That maximises the sample but means the observations are autocorrelated, so
    the percentiles are far less precise than the sample count suggests.
    """
    closes = pd.Series(closes).dropna()
    horizon = max(int(round(horizon_days)), 1)
    if len(closes) <= horizon + 20:
        return {}

    ratio = closes.shift(-horizon) / closes - 1.0
    ratio = ratio.dropna()
    if ratio.empty:
        return {}

    absolute = ratio.abs()
    return {
        "n": int(len(ratio)),
        "horizon_trading_days": horizon,
        "mean_abs_pct": float(absolute.mean() * 100),
        "median_abs_pct": float(absolute.median() * 100),
        "p68_abs_pct": float(absolute.quantile(0.68) * 100),
        "p95_abs_pct": float(absolute.quantile(0.95) * 100),
        "q16_pct": float(ratio.quantile(0.16) * 100),
        "q84_pct": float(ratio.quantile(0.84) * 100),
        "up_share": float((ratio > 0).mean()),
    }
