"""Cointegration screening for pairs trading.

Two prices are cointegrated when some linear combination of them is stationary
even though each is individually a random walk. The Engle-Granger test regresses
one on the other and asks whether the residual is stationary; a low p-value says
the spread mean-reverts, which is the whole premise of a pairs trade.

Three things are done here that a naive screen leaves out, because without them
the output is a list of coincidences:

* **Out-of-sample testing.** The hedge ratio is fitted on the first 70% of the
  history and the test is then run again on the held-out 30%. A pair that only
  cointegrates in-sample has told you about the past, not the relationship.
* **A multiple-testing threshold.** Screening n tickers runs n(n-1)/2 tests. At
  p < 0.05, 50 tickers means 1225 tests and roughly 61 false positives by pure
  chance. The Bonferroni threshold 0.05/n_tests is reported alongside the raw
  p-values so the noise floor is visible.
* **Half-life.** A statistically stationary spread that reverts over three years
  is untradeable. Half-life comes from an Ornstein-Uhlenbeck fit on the spread:
  regress ds on the lagged level, half-life = -ln 2 / ln(1 + b).

Direction matters to Engle-Granger -- regressing A on B is not the same test as
B on A -- so both orientations are run and the stronger is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint

MIN_OBSERVATIONS = 120
DEFAULT_SPLIT = 0.7


@dataclass(frozen=True)
class Pair:
    """One fitted spread: y - beta*x - alpha."""

    y: str
    x: str
    beta: float
    alpha: float
    pvalue: float
    pvalue_in_sample: float
    pvalue_out_of_sample: float
    half_life: float  # trading days
    zscore: float  # latest spread z-score
    correlation: float
    n_obs: int

    @property
    def stable(self) -> bool:
        """Cointegrates on both the fitted and the held-out window."""
        return self.pvalue_in_sample < 0.05 and self.pvalue_out_of_sample < 0.05


def hedge_ratio(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """OLS slope and intercept of y on x."""
    beta, alpha = np.polyfit(x, y, 1)
    return float(beta), float(alpha)


def spread_series(y: pd.Series, x: pd.Series, beta: float, alpha: float) -> pd.Series:
    return y - beta * x - alpha


def half_life(spread: pd.Series) -> float:
    """Ornstein-Uhlenbeck mean-reversion half-life in trading days.

    Returns NaN when the fitted process is not mean-reverting (b >= 0), which is
    the honest answer -- there is no half-life for a divergent spread.
    """
    s = pd.Series(spread).dropna()
    if len(s) < 20:
        return float("nan")
    lagged = s.shift(1).dropna()
    delta = s.diff().dropna()
    lagged, delta = lagged.align(delta, join="inner")
    if len(lagged) < 10 or lagged.std() == 0:
        return float("nan")

    b, _ = np.polyfit(lagged.to_numpy(), delta.to_numpy(), 1)
    if b >= 0 or (1 + b) <= 0:
        return float("nan")
    return float(-np.log(2) / np.log(1 + b))


def _directed_test(y: pd.Series, x: pd.Series, split: float) -> dict | None:
    n = len(y)
    cut = int(n * split)
    if cut < MIN_OBSERVATIONS // 2 or n - cut < 30:
        return None

    try:
        pvalue = float(coint(y, x)[1])
        p_in = float(coint(y.iloc[:cut], x.iloc[:cut])[1])
        p_out = float(coint(y.iloc[cut:], x.iloc[cut:])[1])
    except (ValueError, np.linalg.LinAlgError):
        return None

    # Hedge ratio from the in-sample window only, then applied to everything --
    # using the full sample here would leak the held-out data into the spread.
    beta, alpha = hedge_ratio(y.iloc[:cut].to_numpy(), x.iloc[:cut].to_numpy())
    spread = spread_series(y, x, beta, alpha)
    sd = spread.std(ddof=1)

    return {
        "beta": beta,
        "alpha": alpha,
        "pvalue": pvalue,
        "pvalue_in_sample": p_in,
        "pvalue_out_of_sample": p_out,
        "half_life": half_life(spread),
        "zscore": float((spread.iloc[-1] - spread.mean()) / sd) if sd > 0 else float("nan"),
        "correlation": float(y.corr(x)),
        "n_obs": n,
    }


def screen_pairs(
    closes: pd.DataFrame,
    split: float = DEFAULT_SPLIT,
    max_half_life: float | None = None,
) -> pd.DataFrame:
    """Test every pair in both directions and rank by full-sample p-value."""
    closes = closes.dropna()
    if len(closes) < MIN_OBSERVATIONS:
        raise ValueError(
            f"Need at least {MIN_OBSERVATIONS} overlapping observations, got {len(closes)}."
        )
    if closes.shape[1] < 2:
        raise ValueError("Need at least two tickers.")

    # Log prices: cointegration on levels is scale-dependent, and a spread in log
    # space corresponds to a constant-dollar-ratio position rather than a
    # constant-share one.
    logs = np.log(closes)

    rows = []
    for a, b in combinations(logs.columns, 2):
        best = None
        for y_name, x_name in ((a, b), (b, a)):
            res = _directed_test(logs[y_name], logs[x_name], split)
            if res is None:
                continue
            res |= {"y": y_name, "x": x_name}
            if best is None or res["pvalue"] < best["pvalue"]:
                best = res
        if best is not None:
            rows.append(best)

    if not rows:
        raise ValueError("No pair had enough usable history to test.")

    out = pd.DataFrame(rows)
    out["pair"] = out.y + " ~ " + out.x
    if max_half_life is not None:
        out = out[out.half_life.between(0, max_half_life, inclusive="both") | out.half_life.isna()]
    cols = [
        "pair", "y", "x", "beta", "alpha", "pvalue", "pvalue_in_sample",
        "pvalue_out_of_sample", "half_life", "zscore", "correlation", "n_obs",
    ]
    return out[cols].sort_values("pvalue").reset_index(drop=True)


def bonferroni_threshold(n_tickers: int, alpha: float = 0.05) -> tuple[int, float]:
    """Number of pair tests run, and the p-value that survives correcting for them."""
    tests = n_tickers * (n_tickers - 1) // 2
    return tests, alpha / max(tests, 1)


def pair_from_row(row: pd.Series) -> Pair:
    return Pair(
        y=row.y, x=row.x, beta=float(row.beta), alpha=float(row.alpha),
        pvalue=float(row.pvalue), pvalue_in_sample=float(row.pvalue_in_sample),
        pvalue_out_of_sample=float(row.pvalue_out_of_sample),
        half_life=float(row.half_life), zscore=float(row.zscore),
        correlation=float(row.correlation), n_obs=int(row.n_obs),
    )


def spread_frame(closes: pd.DataFrame, pair: Pair) -> pd.DataFrame:
    """Spread level and rolling z-score for one pair, ready to chart."""
    logs = np.log(closes[[pair.y, pair.x]].dropna())
    spread = spread_series(logs[pair.y], logs[pair.x], pair.beta, pair.alpha)
    mean, sd = spread.mean(), spread.std(ddof=1)
    return pd.DataFrame(
        {
            "spread": spread,
            "mean": mean,
            "upper_2sd": mean + 2 * sd,
            "lower_2sd": mean - 2 * sd,
            "zscore": (spread - mean) / sd if sd > 0 else np.nan,
        }
    )
