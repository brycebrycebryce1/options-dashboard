"""Risk-neutral density from the option chain (Breeden-Litzenberger).

Breeden and Litzenberger (1978) showed that the second derivative of the call
price with respect to strike *is* the market's implied probability density of
the settlement price:

    pdf(K) = exp(rT) * d2C/dK2

Differentiating quoted prices directly does not work: strikes are spaced coarse,
quotes are pinned to a penny, and the second difference of that is pure noise
that goes negative everywhere. The fix, and the only real subtlety in this
module, is to **smooth in implied-vol space rather than price space**:

    1. Convert every usable OTM quote to an implied vol.
    2. Fit a smoothing spline to vol against log-moneyness, weighting each quote
       by how precisely its bid-ask spread pins its vol (spread/2 / vega).
    3. Extend beyond the quoted strikes by continuing total variance linearly
       in log-moneyness, matching the fitted slope at the boundary. Holding vol
       flat instead leaves a kink there, and a kink in the vol curve becomes a
       spike of negative density in the second derivative -- see
       :func:`wing_extended_vol`.
    4. Re-price a dense strike grid off the smoothed smile.
    5. Take the second difference of *that*, which is smooth by construction.

The smile is a gentle, near-quadratic function of log-moneyness, so a spline
fits it with very few effective degrees of freedom; the price curve inherits
that smoothness and its curvature stays positive almost everywhere. Any residual
negative density is clipped and the result renormalised to integrate to one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.interpolate import UnivariateSpline
from scipy.special import erf

import blackscholes as bs
from prep import ExpirySnapshot

# numpy 2 renamed trapz; keep working on both.
_trapz = getattr(np, "trapezoid", None) or np.trapz

GRID_POINTS = 1200
MAX_GRID_POINTS = 6000
# Probability allowed to fall outside the strike grid before it is widened.
TAIL_TOLERANCE = 1e-5
MAX_WING_EXTENSION = 8.0
# How far past the quoted strikes to extend the (flat-vol) wings, in units of
# the quoted log-moneyness range. The tails are extrapolation, not data.
WING_EXTENSION = 0.6


@dataclass(frozen=True)
class Density:
    """Risk-neutral density over settlement price for one expiration."""

    price: np.ndarray  # dense strike grid
    pdf: np.ndarray  # risk-neutral density, integrates to 1
    cdf: np.ndarray
    call_curve: np.ndarray  # smoothed call prices the density came from
    smile_k: np.ndarray  # log-moneyness of the dense grid
    smile_iv: np.ndarray  # fitted vol on the dense grid
    quote_k: np.ndarray  # log-moneyness of the quotes actually fitted
    quote_iv: np.ndarray
    forward: float
    spot: float
    T: float
    df: float
    atm_iv: float
    rms_fit_error: float  # RMS smile residual, in vol points
    n_quotes: int
    quoted_range: tuple[float, float]  # strike range actually quoted
    negative_mass_clipped: float  # share of raw density that came out negative
    tail_mass_missing: float  # probability that fell outside the strike grid

    def quantile(self, p: float | np.ndarray):
        """Settlement price at a given cumulative probability."""
        return np.interp(p, self.cdf, self.price)

    def prob_above(self, level: float) -> float:
        return float(1.0 - np.interp(level, self.price, self.cdf))

    def prob_between(self, lo: float, hi: float) -> float:
        c = np.interp([lo, hi], self.price, self.cdf)
        return float(c[1] - c[0])

    @property
    def mean(self) -> float:
        return float(_trapz(self.price * self.pdf, self.price))

    @property
    def mode(self) -> float:
        return float(self.price[int(np.argmax(self.pdf))])

    @property
    def median(self) -> float:
        return float(self.quantile(0.5))

    @property
    def stdev(self) -> float:
        var = _trapz((self.price - self.mean) ** 2 * self.pdf, self.price)
        return float(np.sqrt(max(var, 0.0)))

    @property
    def skew(self) -> float:
        """Third standardised moment: negative means a fatter left tail."""
        sd = self.stdev
        if sd <= 0:
            return float("nan")
        return float(_trapz(((self.price - self.mean) / sd) ** 3 * self.pdf, self.price))

    @property
    def excess_kurtosis(self) -> float:
        sd = self.stdev
        if sd <= 0:
            return float("nan")
        return float(_trapz(((self.price - self.mean) / sd) ** 4 * self.pdf, self.price) - 3.0)

    def iv_at(self, level: float) -> float:
        """Fitted implied vol at a price level, read off the smoothed smile."""
        if not np.isfinite(level) or level <= 0:
            return float("nan")
        return float(np.interp(np.log(level / self.forward), self.smile_k, self.smile_iv))

    def prob_touch(self, level: float) -> float:
        """Probability of trading at ``level`` at any point before expiry.

        The density answers where the price *settles*; this answers whether it
        ever gets there, which for anything path-dependent -- a stop, a barrier,
        a decision to act at a level -- is the question that matters. They are
        very different numbers: a level with a 20% chance of being the closing
        price on expiry day has roughly a 40% chance of trading at some point
        before it.

        First passage of a driftless price process, which the forward measure
        gives for free since the forward is a martingale. In log space that
        leaves a drift of -sigma^2/2, so the reflection principle carries a
        correction term::

            P(hit a) = N((-a + mu T)/(sigma sqrt(T))) + e^(2 mu a / sigma^2) N((-a - mu T)/(sigma sqrt(T)))

        with ``a = ln(level/F)`` and ``mu = -sigma^2/2``. The cruder rule of
        doubling the terminal probability drops that correction; measured
        against this formula it is out by up to 7 points of probability for a
        50% vol name three months out, so it is not used.

        ``sigma`` is read off the fitted smile *at the level being tested*, not
        at the money, so a downside barrier is priced with the higher put vol
        the market actually charges there. That is the one place the skew enters.

        The result is floored at the terminal probability, which is a hard
        logical constraint rather than a numerical guard: the price cannot
        finish beyond a level without having touched it. The floor binds only
        where the constant-vol assumption here disagrees with the full density,
        and when it binds it is the density that wins.
        """
        if not np.isfinite(level) or level <= 0 or self.T <= 0:
            return float("nan")

        upward = level > self.forward
        terminal = self.prob_above(level) if upward else 1.0 - self.prob_above(level)
        if np.isclose(level, self.forward):
            return 1.0

        sigma = self.iv_at(level)
        if not np.isfinite(sigma) or sigma <= 0:
            return float(min(2.0 * terminal, 1.0))

        a = np.log(level / self.forward)
        mu = -0.5 * sigma * sigma
        s = sigma * np.sqrt(self.T)
        # e^(2 mu a / sigma^2) reduces to e^-a = F/level for this drift.
        reflect = self.forward / level

        if upward:
            hit = _norm_cdf((-a + mu * self.T) / s) + reflect * _norm_cdf((-a - mu * self.T) / s)
        else:
            hit = _norm_cdf((a - mu * self.T) / s) + reflect * _norm_cdf((a + mu * self.T) / s)

        return float(np.clip(max(hit, terminal), 0.0, 1.0))

    def lognormal_pdf(self) -> np.ndarray:
        """Black-Scholes density at the ATM vol, for an apples-to-apples overlay.

        The gap between this and :attr:`pdf` is exactly what the smile is
        pricing: skew (crash premium) and fat tails.
        """
        return lognormal_pdf(self.price, self.forward, self.atm_iv, self.T)


def _norm_cdf(x):
    """Standard normal CDF via erf, matching blackscholes.norm_cdf."""
    return 0.5 * (1.0 + erf(np.asarray(x, float) / np.sqrt(2.0)))


def lognormal_pdf(price, forward, sigma, T):
    """Density of a driftless lognormal with median forward*exp(-sigma^2 T/2)."""
    price = np.asarray(price, float)
    v = sigma * np.sqrt(T)
    if not np.isfinite(v) or v <= 0:
        return np.zeros_like(price)
    mu = np.log(forward) - 0.5 * v * v
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (np.log(price) - mu) / v
        out = np.exp(-0.5 * z * z) / (price * v * np.sqrt(2 * np.pi))
    return np.where(np.isfinite(out) & (price > 0), out, 0.0)


def fit_smile(k, iv, iv_error, strength: float = 1.0):
    """Smoothing spline of implied vol against log-moneyness.

    ``iv_error`` is each quote's vol uncertainty in vol points (half the
    bid-ask spread divided by vega). ``scipy`` weights points by 1/sigma and
    compares the weighted residual sum to ``s``, so setting ``s = strength * n``
    asks for a fit whose residuals are, on average, exactly the size of the
    quoted spreads. ``strength`` below 1 tracks the quotes more tightly (and
    risks fitting quote noise); above 1 imposes a smoother, stiffer smile.
    """
    k, iv, iv_error = (np.asarray(x, float) for x in (k, iv, iv_error))
    order = np.argsort(k)
    k, iv, iv_error = k[order], iv[order], iv_error[order]

    # UnivariateSpline needs strictly increasing x; average any duplicate strikes.
    uniq, inverse = np.unique(k, return_inverse=True)
    if len(uniq) < len(k):
        iv = np.bincount(inverse, weights=iv) / np.bincount(inverse)
        iv_error = np.bincount(inverse, weights=iv_error) / np.bincount(inverse)
        k = uniq

    if len(k) < 4:
        raise ValueError(f"Need at least 4 usable strikes to fit a smile, got {len(k)}.")

    weights = 1.0 / np.clip(iv_error, 1e-3, None)
    degree = 3 if len(k) >= 5 else min(3, len(k) - 1)
    spline = UnivariateSpline(k, iv, w=weights, s=strength * len(k), k=degree, ext="const")
    rms = float(np.sqrt(np.mean((spline(k) - iv) ** 2)))
    return spline, k, iv, rms


# Largest slope allowed for total variance in the extrapolated wings, d w / d k.
# Lee's moment formula caps the asymptotic slope of total variance at 2 for any
# process with a finite moment, so this is the arbitrage-free ceiling rather
# than a tuning knob.
MAX_WING_SLOPE = 2.0

# How far the boundary slope is carried into the wing before total variance
# flattens, as a fraction of the quoted log-moneyness width. Chosen by the
# martingale property rather than by taste: on synthetic chains built from a
# known smile, the mean of the extracted density only lands on the forward once
# this reaches about 0.5. Cutting the wing shorter than that -- which is what
# holding vol flat from the boundary does -- truncates real risk-neutral mass and
# put the mean 0.53% below the forward on a steep smile. Longer adds nothing.
WING_DAMPING = 0.5


def wing_extended_vol(spline, grid_k, k_lo, k_hi, T):
    """Fitted vol inside the quoted strikes, C1 wing extension outside them.

    The wings have to be extrapolated somehow -- the grid deliberately runs past
    the last quoted strike -- and *how* matters far more than it looks. Holding
    vol flat past the boundary (``ext="const"`` on the spline) is the obvious
    choice and it is subtly wrong: it is continuous but its slope is not, and a
    kink in sigma(k) puts a delta-like spike in the second derivative of the call
    price, which is the density. Measured on live chains that single kink threw
    off 3-7% of the total mass as *negative* density at exactly two grid points,
    both sitting within a few cents of the quoted boundaries. Clipping that and
    renormalising then dragged the mean off the forward by up to 0.65%, breaking
    the one identity the whole construction has to satisfy.

    Extending *total variance* w = sigma^2 T linearly in k, matching both the
    level and the slope at the boundary, removes the kink by construction. It is
    also the right asymptotic shape: linear total variance in the wings is
    Gatheral's arbitrage-free wing condition, and the slope is capped at Lee's
    bound of 2. A flat smile has zero boundary slope and so still gets flat-vol
    wings, exactly as before.
    """
    inside = np.clip(spline(grid_k), 0.01, 4.0)
    if T <= 0:
        return inside

    slope_fn = spline.derivative()
    length = max((k_hi - k_lo) * WING_DAMPING, 1e-6)
    out = inside.copy()

    for k_edge, wing in ((k_hi, grid_k > k_hi), (k_lo, grid_k < k_lo)):
        if not wing.any():
            continue
        sigma_edge = float(np.clip(spline(k_edge), 0.01, 4.0))
        w_edge = sigma_edge**2 * T
        # d w / d k = 2 sigma sigma' T
        dw = float(np.clip(2.0 * sigma_edge * float(slope_fn(k_edge)) * T,
                           -MAX_WING_SLOPE, MAX_WING_SLOPE))

        delta = grid_k[wing] - k_edge
        # Continue the boundary slope, then let it decay so total variance
        # saturates within a fraction of a quoted width. Near the join this is the
        # straight line w_edge + dw*delta, so the fit and the wing meet with
        # matching slopes and no kink. Far out it is flat vol -- a plain
        # Black-Scholes tail.
        #
        # The decay matters as much as the slope. Continuing the boundary slope
        # forever is arbitrage-free but not conservative: the smile is cut at the
        # 0.01-delta wing, where it is steepest, so an undamped extension
        # manufactures enormous tails out of the most extreme thing it ever saw
        # -- on live chains that put GME's excess kurtosis at 184. Flattening it
        # immediately is the opposite error, and the martingale property is what
        # decides between them; see WING_DAMPING.
        ramp = length * np.sign(delta) * (1.0 - np.exp(-np.abs(delta) / length))
        w = w_edge + dw * ramp
        out[wing] = np.clip(np.sqrt(np.maximum(w, 1e-8) / T), 0.01, 4.0)

    return out


def _price_grid(snap, spline, k_lo, k_hi, extension, points):
    """Reprice a uniform strike grid off the fitted smile, and differentiate it.

    The grid is uniform *in strike*, because that is the variable being
    differentiated; spacing it evenly in log-moneyness would make dK vary across
    the grid and bias the second difference.
    """
    span = k_hi - k_lo
    strikes = np.linspace(
        snap.forward * np.exp(k_lo - extension * span),
        snap.forward * np.exp(k_hi + extension * span),
        points,
    )
    grid_k = np.log(strikes / snap.forward)
    grid_iv = wing_extended_vol(spline, grid_k, k_lo, k_hi, snap.T)
    calls = bs.black_price(snap.forward, strikes, snap.T, grid_iv, True, snap.df)

    dk = float(strikes[1] - strikes[0])
    raw_pdf = np.gradient(np.gradient(calls, dk), dk) / snap.df
    return strikes, grid_k, grid_iv, calls, raw_pdf


def risk_neutral_density(
    snap: ExpirySnapshot,
    smoothing: float = 1.0,
    grid_points: int = GRID_POINTS,
    wing_extension: float = WING_EXTENSION,
) -> Density:
    """Extract the market-implied distribution of the settlement price."""
    otm = snap.otm()
    if len(otm) < 6:
        raise ValueError(
            f"Only {len(otm)} usable out-of-the-money quotes for {snap.expiry}; "
            "not enough to fit a density."
        )

    spline, quote_k, quote_iv, rms = fit_smile(
        otm.log_moneyness.to_numpy(), otm.iv.to_numpy(), otm.iv_error.to_numpy(), smoothing
    )

    k_lo, k_hi = float(quote_k.min()), float(quote_k.max())

    # The grid is widened until the density has actually died out at its edges.
    # A fixed multiple of the quoted span is not enough on its own: how far the
    # tail reaches depends on the smile, not on how many strikes happen to be
    # listed. Whatever mass falls outside the grid is mass the renormalisation
    # redistributes over what is left, which drags the mean off the forward --
    # a steep smile truncated at the old fixed 0.6 span lost 0.8% of its
    # probability and broke the martingale identity by 0.4%.
    #
    # `raw_mass` measures exactly that: the second derivative of a call curve
    # integrates to one over all strikes, so over a truncated range it
    # integrates to the fraction of probability the range captures.
    extension = wing_extension
    points = grid_points
    while True:
        strikes, grid_k, grid_iv, calls, raw_pdf = _price_grid(
            snap, spline, k_lo, k_hi, extension, points
        )
        raw_mass = float(_trapz(np.clip(raw_pdf, 0.0, None), strikes))
        if raw_mass >= 1.0 - TAIL_TOLERANCE or extension >= MAX_WING_EXTENSION:
            break
        # Widening coarsens dK, which is what the second difference is taken
        # over, so the point count grows with the range to hold resolution.
        extension = min(extension * 2.0, MAX_WING_EXTENSION)
        points = min(int(points * 1.5), MAX_GRID_POINTS)

    negative_mass = max(0.0, float(
        -_trapz(np.minimum(raw_pdf, 0.0), strikes) / max(_trapz(np.abs(raw_pdf), strikes), 1e-12)
    ))
    pdf = np.clip(raw_pdf, 0.0, None)
    total = float(_trapz(pdf, strikes))
    if not np.isfinite(total) or total <= 0:
        raise ValueError("Density came out empty; try a nearer expiry or more smoothing.")
    pdf = pdf / total

    cdf = np.concatenate([[0.0], np.cumsum(0.5 * (pdf[1:] + pdf[:-1]) * np.diff(strikes))])
    cdf = np.clip(cdf / cdf[-1], 0.0, 1.0)

    return Density(
        price=strikes,
        pdf=pdf,
        cdf=cdf,
        call_curve=calls,
        smile_k=grid_k,
        smile_iv=grid_iv,
        quote_k=quote_k,
        quote_iv=quote_iv,
        forward=snap.forward,
        spot=snap.spot,
        T=snap.T,
        df=snap.df,
        atm_iv=float(np.clip(spline(0.0), 0.01, 4.0)),
        rms_fit_error=rms,
        n_quotes=len(quote_k),
        quoted_range=(float(snap.forward * np.exp(k_lo)), float(snap.forward * np.exp(k_hi))),
        negative_mass_clipped=negative_mass,
        tail_mass_missing=max(0.0, 1.0 - raw_mass),
    )


def touch_table(dens: Density, spot: float, moves=(0.05, 0.10, 0.20)) -> pd.DataFrame:
    """Terminal versus touch probability for a ladder of moves from spot.

    Both columns are shown because the gap between them is the point. Reading
    only the terminal column understates how often a level comes into play, and
    reading only the touch column overstates where the price is likely to end up.
    """
    rows = []
    for move in moves:
        for direction in (1, -1):
            level = spot * (1.0 + direction * move)
            if level <= 0:
                continue
            above = direction > 0
            terminal = dens.prob_above(level) if above else 1.0 - dens.prob_above(level)
            rows.append({
                "level": level,
                "move %": direction * move * 100,
                "finishes beyond": terminal * 100,
                "touches before expiry": dens.prob_touch(level) * 100,
            })
    return pd.DataFrame(rows).sort_values("level", ascending=False).reset_index(drop=True)


def summary_table(dens: Density) -> pd.DataFrame:
    """Percentiles of the implied distribution, next to the spot for reference."""
    levels = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
    prices = dens.quantile(np.array(levels))
    return pd.DataFrame(
        {
            "percentile": [f"{int(p * 100)}th" for p in levels],
            "price": prices,
            "vs spot %": (prices / dens.spot - 1.0) * 100.0,
        }
    )
