"""Black-76 / Black-Scholes pricing, greeks and implied-volatility inversion.

Everything here is pure numpy + scipy, fully vectorised, with no network and no
Streamlit imports, so the maths can be tested offline.

Pricing is done in *forward* space (Black-76):

    call = df * (F N(d1) - K N(d2))
    put  = df * (K N(-d2) - F N(-d1))
    d1   = (ln(F/K) + sigma^2 T / 2) / (sigma sqrt(T)),   d2 = d1 - sigma sqrt(T)

where ``F`` is the forward price of the underlying to expiry and ``df`` the
discount factor exp(-rT). Working in forward space means the dividend yield
never has to be guessed: the forward is read straight off the option chain via
put-call parity (see :func:`rnd.implied_forward`).
"""

from __future__ import annotations

import numpy as np
from scipy.special import ndtr

SQRT_2PI = float(np.sqrt(2.0 * np.pi))

# Vol bracket used by the implied-vol solver. 400% is well beyond anything a
# listed equity option quotes at; hitting either edge means the quote is junk.
VOL_MIN = 1e-4
VOL_MAX = 4.0


def norm_pdf(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: np.ndarray) -> np.ndarray:
    return ndtr(np.asarray(x, dtype=float))


def forward_price(spot, T, r, q=0.0):
    """Carry the spot to expiry: F = S exp((r - q) T)."""
    return np.asarray(spot, float) * np.exp((np.asarray(r, float) - np.asarray(q, float)) * np.asarray(T, float))


def d1_d2(F, K, T, sigma):
    """Black-76 d1/d2. Returns NaN where the diffusion term is degenerate."""
    F, K, T, sigma = (np.asarray(x, dtype=float) for x in (F, K, T, sigma))
    v = sigma * np.sqrt(np.maximum(T, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * v * v) / v
        d2 = d1 - v
    degenerate = ~(v > 0) | ~(F > 0) | ~(K > 0)
    return np.where(degenerate, np.nan, d1), np.where(degenerate, np.nan, d2)


def black_price(F, K, T, sigma, is_call=True, df=1.0):
    """Black-76 option value. Falls back to discounted intrinsic when T or sigma is 0."""
    F, K, T, sigma, df = (np.asarray(x, dtype=float) for x in (F, K, T, sigma, df))
    is_call = np.asarray(is_call, dtype=bool)

    intrinsic = np.where(is_call, np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))
    d1, d2 = d1_d2(F, K, T, sigma)
    with np.errstate(invalid="ignore"):
        call = F * norm_cdf(d1) - K * norm_cdf(d2)
        put = K * norm_cdf(-d2) - F * norm_cdf(-d1)
    value = np.where(is_call, call, put)
    return df * np.where(np.isfinite(value), value, intrinsic)


def black_vega(F, K, T, sigma, df=1.0):
    """dPrice/dSigma for one full unit of vol (divide by 100 for "per vol point")."""
    F, K, T, sigma, df = (np.asarray(x, dtype=float) for x in (F, K, T, sigma, df))
    d1, _ = d1_d2(F, K, T, sigma)
    with np.errstate(invalid="ignore"):
        vega = df * F * norm_pdf(d1) * np.sqrt(np.maximum(T, 0.0))
    return np.where(np.isfinite(vega), vega, 0.0)


def spot_delta(spot, F, K, T, sigma, is_call=True, df=1.0):
    """dPrice/dSpot. exp(-qT) N(d1) for calls, exp(-qT)(N(d1) - 1) for puts.

    exp(-qT) is recovered as df * F / S, which keeps the greek consistent with
    whatever forward the chain actually implies.
    """
    spot, F, df = (np.asarray(x, dtype=float) for x in (spot, F, df))
    is_call = np.asarray(is_call, dtype=bool)
    carry = df * F / spot  # == exp(-qT)
    d1, _ = d1_d2(F, K, T, sigma)
    with np.errstate(invalid="ignore"):
        delta = np.where(is_call, carry * norm_cdf(d1), carry * (norm_cdf(d1) - 1.0))
    return np.where(np.isfinite(delta), delta, np.nan)


def spot_gamma(spot, F, K, T, sigma, df=1.0):
    """d2Price/dSpot2 = exp(-qT) phi(d1) / (S sigma sqrt(T)). Same for calls and puts."""
    spot, F, K, T, sigma, df = (np.asarray(x, dtype=float) for x in (spot, F, K, T, sigma, df))
    carry = df * F / spot
    d1, _ = d1_d2(F, K, T, sigma)
    with np.errstate(invalid="ignore", divide="ignore"):
        gamma = carry * norm_pdf(d1) / (spot * sigma * np.sqrt(np.maximum(T, 0.0)))
    return np.where(np.isfinite(gamma), gamma, 0.0)


def implied_vol(price, F, K, T, is_call=True, df=1.0, iters=80):
    """Invert Black-76 for sigma by vectorised bisection.

    Bisection rather than Newton: option value is monotonic in sigma, so 80
    halvings of [1e-4, 4.0] converge to machine precision unconditionally, with
    no dependence on a starting guess and no divergence on near-zero vega
    quotes. Returns NaN where the price violates the no-arbitrage bounds or
    lands outside the bracket -- both mean the quote is unusable, not that the
    solver failed.
    """
    price, F, K, T, df = (np.asarray(x, dtype=float) for x in (price, F, K, T, df))
    is_call = np.asarray(is_call, dtype=bool)
    price, F, K, T, df, is_call = np.broadcast_arrays(price, F, K, T, df, is_call)

    intrinsic = df * np.where(is_call, np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))
    ceiling = df * np.where(is_call, F, K)  # value as sigma -> infinity

    lo = np.full(price.shape, VOL_MIN, dtype=float)
    hi = np.full(price.shape, VOL_MAX, dtype=float)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        cheap = black_price(F, K, T, mid, is_call, df) < price
        lo = np.where(cheap, mid, lo)
        hi = np.where(cheap, hi, mid)
    sigma = 0.5 * (lo + hi)

    usable = (
        np.isfinite(price)
        & (T > 0)
        & (price > intrinsic * (1 + 1e-9) + 1e-10)
        & (price < ceiling)
        & (sigma > VOL_MIN * 1.01)
        & (sigma < VOL_MAX * 0.99)
    )
    return np.where(usable, sigma, np.nan)
