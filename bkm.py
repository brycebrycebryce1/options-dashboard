"""Model-free implied moments (Bakshi, Kapadia and Madan, 2003).

The dashboard already reports implied skew and excess kurtosis, read off the
Breeden-Litzenberger density. Those numbers travel a long road to get there: fit
a spline through the smile, re-price a dense strike grid, differentiate it
twice, clip whatever comes out negative, renormalise, and extrapolate the wings
past the last quoted strike. Every step is defensible. Every one of them can
also go wrong quietly, because the result still integrates to one whatever
happens.

This module computes the same moments by a route that shares none of those
steps. Carr and Madan showed that any smooth payoff can be replicated statically
out of options, so for twice-differentiable H::

    E[H(S_T)] = H(F) + integral of H''(K) * (undiscounted OTM price) dK

Setting H(S) = S^n makes H'' = n(n-1)K^(n-2), and the raw moments of the
settlement price fall straight out of the quoted chain::

    E[S]   = F                       (H'' = 0 -- the martingale property, for free)
    E[S^2] = F^2 + integral of  2      * p(K) dK
    E[S^3] = F^3 + integral of  6K     * p(K) dK
    E[S^4] = F^4 + integral of 12K^2   * p(K) dK

No spline, no differentiation, no density. Just a weighted sum of the mids.
Because these are moments of the *price*, they are directly comparable to
``Density.skew`` and ``Density.excess_kurtosis`` -- the same quantities in the
same units as the headline metrics, arrived at independently.

``mfiv`` is the one genuinely new number: the fair strike of a variance swap,
sqrt(-2 E[ln(S/F)] / T), which is what the VIX approximates. Same replication
argument with H(S) = ln(S/F), so H'' = -1/K^2. It is comparable to the VIX line
on the volatility-premium chart and to the ATM vol quoted everywhere else.

**What this is and is not.** It is a second opinion, not a better answer. Both
routes read the same quotes, so agreement cannot prove either is right. What it
isolates is the difference in *domain*:

* The quote integrals stop at the last listed strike. Everything past that is
  invisible to them, so they systematically understate the tails -- excess
  kurtosis biased down, |skew| biased toward zero.
* The density keeps going, carrying the smile's boundary slope into a damped
  wing. Everything past the last strike is extrapolation, not data.

So the gap between the two measures how much of the reported shape rests on
wings the market never quoted -- worth knowing on every chain, not only broken
ones. It is reported for skew and kurtosis and deliberately kept out of the
pass/fail verdict; see :func:`agreement` for why a fourth moment cannot
adjudicate anything here.

The integrals are evaluated by trapezoid on the strikes the market actually
lists, so there is discretisation error of a fraction of a percent on a dense
chain and more on a sparse one. This is the same error the published VIX
calculation carries, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from prep import ExpirySnapshot

_trapz = getattr(np, "trapezoid", None) or np.trapz

MIN_STRIKES = 6


@dataclass(frozen=True)
class Moments:
    """Standardised moments of the settlement price, however they were obtained."""

    source: str
    mean: float
    stdev: float
    skew: float
    excess_kurtosis: float
    mfiv: float  # annualised model-free implied vol, from the log contract
    forward: float
    n_strikes: int
    strike_range: tuple[float, float]

    @property
    def sigma_span(self) -> tuple[float, float]:
        """Strike coverage in standard deviations either side of the forward."""
        if not np.isfinite(self.stdev) or self.stdev <= 0:
            return (float("nan"), float("nan"))
        lo, hi = self.strike_range
        return ((lo - self.forward) / self.stdev, (hi - self.forward) / self.stdev)

    @property
    def coverage(self) -> str:
        lo, hi = self.sigma_span
        return "unknown" if not np.isfinite(lo) else f"{lo:+.1f}σ to {hi:+.1f}σ"


def _standardise(source, m1, m2, m3, m4, mfiv, forward, n, strike_range) -> Moments:
    """Central and standardised moments from the four raw ones."""
    var = m2 - m1**2
    if not np.isfinite(var) or var <= 0:
        return Moments(source, float(m1), float("nan"), float("nan"), float("nan"),
                       float(mfiv), float(forward), int(n),
                       (float(strike_range[0]), float(strike_range[1])))

    sd = float(np.sqrt(var))
    c3 = m3 - 3 * m1 * m2 + 2 * m1**3
    c4 = m4 - 4 * m1 * m3 + 6 * m1**2 * m2 - 3 * m1**4
    return Moments(source, float(m1), sd, float(c3 / sd**3), float(c4 / sd**4 - 3.0),
                   float(mfiv), float(forward), int(n),
                   (float(strike_range[0]), float(strike_range[1])))


def from_quotes(snap: ExpirySnapshot) -> Moments:
    """Moments replicated directly from the quoted out-of-the-money strikes."""
    otm = snap.otm()
    if len(otm) < MIN_STRIKES:
        raise ValueError(
            f"Only {len(otm)} usable out-of-the-money quotes for {snap.expiry}; "
            "not enough to integrate."
        )

    K = otm.strike.to_numpy(float)
    # Undiscounted out-of-the-money price: puts below the forward, calls above.
    # This single series is exactly what the replication integral wants.
    p = otm.mid.to_numpy(float) / snap.df
    F = snap.forward

    m1 = F  # H'' = 0 for H(S) = S, so the forward comes back exactly
    m2 = F**2 + _trapz(2.0 * p, K)
    m3 = F**3 + _trapz(6.0 * K * p, K)
    m4 = F**4 + _trapz(12.0 * K**2 * p, K)

    # Log contract, for the variance-swap rate: H(S) = ln(S/F) gives H'' = -1/K^2.
    mean_log = -_trapz(p / K**2, K)
    mfiv = float(np.sqrt(-2.0 * mean_log / snap.T)) if (mean_log < 0 and snap.T > 0) \
        else float("nan")

    return _standardise("Quotes (BKM)", m1, m2, m3, m4, mfiv, F, len(K), (K.min(), K.max()))


def from_density(dens) -> Moments:
    """The headline numbers, restated in the same shape for comparison.

    These are read straight off the fitted density rather than recomputed, so
    the table really is comparing what the page reports against what the quotes
    support -- not two fresh calculations that happen to sit near each other.
    """
    x = np.log(dens.price / dens.forward)
    mean_log = float(_trapz(x * dens.pdf, dens.price))
    mfiv = float(np.sqrt(-2.0 * mean_log / dens.T)) if (mean_log < 0 and dens.T > 0) \
        else float("nan")

    return Moments(
        source="Density (BL)",
        mean=dens.mean,
        stdev=dens.stdev,
        skew=dens.skew,
        excess_kurtosis=dens.excess_kurtosis,
        mfiv=mfiv,
        forward=dens.forward,
        n_strikes=dens.n_quotes,
        strike_range=dens.quoted_range,
    )


def compare(quotes: Moments, density: Moments) -> pd.DataFrame:
    """Side-by-side table of the two estimates, with the gap between them."""
    rows = [
        ("Mean / forward", quotes.mean / quotes.forward, density.mean / density.forward),
        ("Std deviation $", quotes.stdev, density.stdev),
        ("Skew", quotes.skew, density.skew),
        ("Excess kurtosis", quotes.excess_kurtosis, density.excess_kurtosis),
    ]
    return pd.DataFrame({
        "Metric": [r[0] for r in rows],
        "From quotes": [r[1] for r in rows],
        "From density": [r[2] for r in rows],
        "Difference": [r[1] - r[2] for r in rows],
    })


# What the two may differ by before something is wrong. Set from measurement,
# not from theory -- both estimators read the same prices, so there is no
# sampling distribution here and no test statistic to appeal to.
#
# Across ten live chains, liquid and thin, the width agrees to 1.2-7.7% and the
# density's mean sits on the forward to within 0.04%. Damaging a single strike
# in the synthetic tests -- a stale quote the quality filters cannot reject --
# moves the width by 46% to 406% and knocks the mean 1.4% to 20% off the
# forward. The thresholds sit in the wide gap between those two regimes.
WIDTH_TOLERANCE = 0.15  # relative, on the standard deviation
MARTINGALE_TOLERANCE = 0.005  # |density mean / forward - 1|

# Below this much strike coverage on either side, the quote-side estimate is
# blind enough that its higher moments say more about the chain than the market.
NARROW_COVERAGE = 1.5  # standard deviations


def agreement(quotes: Moments, density: Moments) -> tuple[bool, str]:
    """Whether the quotes support the reported shape, and a sentence saying so.

    Judged on the two quantities that are *not* dominated by the tails: the
    width, and whether the density's mean is the forward. Skew and especially
    excess kurtosis are reported but deliberately excluded from the verdict.

    The reason is worth stating plainly, because it looks like an omission.
    Kurtosis is a fourth moment, so it lives almost entirely in the tails -- and
    the quote-side integral stops at the last listed strike, around two standard
    deviations out on a typical chain. Measured across ten live chains its
    kurtosis came back between 0.2 and 1.7 whatever the underlying distribution
    actually looked like, while the density's ranged from 1.2 to 14. The
    "difference" between them was therefore just the density's own number
    restated, which is not a check on anything. The width, by contrast, is
    dominated by the middle of the distribution where both estimators can see.

    Nor is this a test of the model-free implied vol, which is built on the log
    contract: its 1/K^2 weighting puts most of its mass in the far left wing,
    which on a skewed chain is entirely extrapolation. The quotes' version of it
    is reported on its own, where it means something.
    """
    d_width = abs(quotes.stdev - density.stdev) / max(density.stdev, 1e-9)
    d_mean = abs(density.mean / density.forward - 1.0)
    d_skew = abs(quotes.skew - density.skew)
    d_kurt = abs(quotes.excess_kurtosis - density.excess_kurtosis)

    if not all(np.isfinite(v) for v in (d_width, d_mean)):
        return False, ("One of the two estimates did not converge, usually because the "
                       "quoted strike range is too narrow to integrate over.")

    ok = d_width <= WIDTH_TOLERANCE and d_mean <= MARTINGALE_TOLERANCE
    tails = (f"Skew differs by {d_skew:.2f} and excess kurtosis by {d_kurt:.2f}; both are tail "
             f"quantities and the quotes only reach {quotes.coverage}, so that gap is the part "
             "of the reported shape resting on extrapolated wings rather than on prices "
             "anyone quoted.")

    if ok:
        return True, (
            f"The quotes support the reported shape: the width agrees to {d_width * 100:.1f}% "
            f"and the density's mean sits on the forward to {d_mean * 100:.3f}%. Replicating "
            f"the moments from {quotes.n_strikes} mids and differentiating a fitted smile twice "
            f"share no arithmetic, so agreement this close is a real check rather than a "
            f"restatement. {tails}"
        )

    lo, hi = quotes.sigma_span
    if min(abs(lo), abs(hi)) < NARROW_COVERAGE and d_mean <= MARTINGALE_TOLERANCE:
        return False, (
            f"The width differs by {d_width * 100:.1f}%, more than truncation usually "
            f"explains — but this chain is quoted only {quotes.coverage} around the forward, "
            "so the quote-side estimate is itself tail-blind. Read this as a thin chain "
            "rather than as evidence of a bad strike."
        )
    return False, (
        f"The two routes disagree: the width differs by {d_width * 100:.1f}% and the density's "
        f"mean is {d_mean * 100:.2f}% off the forward, on a chain quoted {quotes.coverage}. The "
        "mean is the telling one — it has to be the forward, and the quote-side replication "
        "returns it exactly by construction — so a density that misses it has been pulled off "
        "by something in the fit. Treat this expiry's numbers as unreliable and check the "
        "arbitrage panel for a strike that should not be in it."
    )
