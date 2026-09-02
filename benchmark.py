"""The ticker's skew and vol, against the index on the same day.

A 25-delta risk reversal of -3.2 vol points means nothing on its own. Equity
skew is always negative, so the only question worth asking is whether it is more
negative than usual -- and answering that needs a benchmark.

The obvious benchmark is the ticker's own history, and it is not available. A
history of implied skew has to be accumulated day by day, and Streamlit Cloud
gives a container no persistent filesystem to accumulate it in. Storing it would
mean taking on a database for the sake of one number.

So the comparison here is cross-sectional instead of longitudinal: the same
numbers, on the same afternoon, for SPY. That sidesteps persistence entirely and
answers a slightly different but equally useful question -- not "is this ticker's
skew steep for this ticker" but "is it steep for what it is". Both readings move
with the same market-wide risk appetite, so comparing them nets most of that out
and leaves what is specific to the name.

Two things this is not. The vol ratio is not a beta: it compares implied vols,
which contain idiosyncratic risk the index has diversified away, so it is above
one for essentially every single name and that is not a signal. And SPY is not a
sector benchmark -- for a name whose sector is having its own day, the index is
the wrong comparison and a peer would be better.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import surface as surf
from prep import ExpirySnapshot

BENCHMARK_SYMBOL = "SPY"


def nearest_expiry(expirations: list[str], target_dte: float, dte_of) -> str | None:
    """The listed expiry closest in days to ``target_dte``."""
    if not expirations:
        return None
    return min(expirations, key=lambda e: abs(dte_of(e) - target_dte))


@dataclass(frozen=True)
class Comparison:
    """One expiry of the ticker against the matched expiry of the benchmark."""

    symbol: str
    benchmark: str
    expiry: str
    benchmark_expiry: str
    dte: float
    benchmark_dte: float
    atm_iv: float
    benchmark_atm_iv: float
    rr25: float
    benchmark_rr25: float
    bf25: float
    benchmark_bf25: float

    @property
    def vol_ratio(self) -> float:
        if not np.isfinite(self.benchmark_atm_iv) or self.benchmark_atm_iv <= 0:
            return float("nan")
        return self.atm_iv / self.benchmark_atm_iv

    def table(self) -> pd.DataFrame:
        rows = [
            ("ATM implied vol %", self.atm_iv * 100, self.benchmark_atm_iv * 100),
            ("25Δ risk reversal (pts)", self.rr25 * 100, self.benchmark_rr25 * 100),
            ("25Δ butterfly (pts)", self.bf25 * 100, self.benchmark_bf25 * 100),
        ]
        return pd.DataFrame({
            "Metric": [r[0] for r in rows],
            self.symbol: [r[1] for r in rows],
            self.benchmark: [r[2] for r in rows],
            "Difference": [r[1] - r[2] for r in rows],
        })

    def note(self) -> str:
        parts = []
        if np.isfinite(self.vol_ratio):
            parts.append(
                f"{self.symbol} is priced at {self.atm_iv * 100:.1f}% vol against "
                f"{self.benchmark}'s {self.benchmark_atm_iv * 100:.1f}%, a ratio of "
                f"{self.vol_ratio:.2f}×"
            )
        if np.isfinite(self.rr25) and np.isfinite(self.benchmark_rr25):
            gap = (self.rr25 - self.benchmark_rr25) * 100
            if gap < -0.25:
                verdict = (f"downside is bid {abs(gap):.2f} points *harder* here than in the "
                           "index — the market is paying up for protection on this name "
                           "specifically, not on equities generally")
            elif gap > 0.25:
                verdict = (f"downside is bid {gap:.2f} points *less* hard here than in the "
                           "index, which for a single name is unusual and normally means "
                           "calls are being chased")
            else:
                verdict = ("its skew is in line with the index, so whatever is priced into it "
                           "is market-wide rather than specific to the name")
            parts.append(
                f"its 25Δ risk reversal is {self.rr25 * 100:+.2f} points against "
                f"{self.benchmark_rr25 * 100:+.2f} for {self.benchmark}, so {verdict}"
            )
        if not parts:
            return f"No comparable {self.benchmark} quotes at this maturity."
        return ". ".join(p[0].upper() + p[1:] if i == 0 else p
                         for i, p in enumerate(parts)) + "."


def _metrics(snap: ExpirySnapshot) -> tuple[float, float, float]:
    row = surf.skew_metrics([snap]).iloc[0]
    return float(row.atm_iv), float(row.rr_25d), float(row.bf_25d)


def compare(snap: ExpirySnapshot, benchmark_snap: ExpirySnapshot) -> Comparison:
    """Match one expiry of the ticker against one expiry of the benchmark."""
    atm, rr, bf = _metrics(snap)
    b_atm, b_rr, b_bf = _metrics(benchmark_snap)
    return Comparison(
        symbol=snap.symbol,
        benchmark=benchmark_snap.symbol,
        expiry=snap.expiry,
        benchmark_expiry=benchmark_snap.expiry,
        dte=snap.dte,
        benchmark_dte=benchmark_snap.dte,
        atm_iv=atm,
        benchmark_atm_iv=b_atm,
        rr25=rr,
        benchmark_rr25=b_rr,
        bf25=bf,
        benchmark_bf25=b_bf,
    )
