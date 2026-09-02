"""Realised volatility estimators and the volatility risk premium.

Realised vol
------------
Close-to-close throws away the intraday range and needs about four times the
sample of a range estimator for the same precision. Yang-Zhang (2000) is the
efficient choice: it combines overnight gaps, the open-to-close move and the
Rogers-Satchell range term, so it is unbiased in the presence of both opening
jumps and drift, which Parkinson and Garman-Klass are not.

    V = V_overnight + k V_open_to_close + (1 - k) V_rogers_satchell
    k = 0.34 / (1.34 + (n + 1)/(n - 1))

Volatility risk premium
-----------------------
VRP is what option sellers get paid for bearing volatility risk: the vol the
market charged, minus the vol that subsequently showed up.

    VRP_t = IV_t - RV_{t -> t+h}

Note the alignment. The realised leg is *forward* looking from t, which is the
only comparison that answers "was the option fairly priced". Comparing today's
implied to the *trailing* realised -- the version usually plotted -- measures
something different and much weaker. The consequence is that the last h trading
days of any VRP series are unknowable, and they are returned as NaN rather than
quietly truncated.

For a single stock there is no free source of historical implied vol, so the
premium's *history* is computed market-wide from VIX against S&P 500 realised.
The ticker-specific panel reports today's implied against trailing realised,
clearly labelled as the weaker like-for-not-quite-like comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252
DEFAULT_WINDOWS = (10, 21, 63, 252)
VRP_HORIZON = 21  # trading days, ~ the 30 calendar days VIX prices


def log_returns(closes: pd.Series) -> pd.Series:
    return np.log(pd.Series(closes).astype(float)).diff()


def close_to_close_vol(closes: pd.Series, window: int) -> pd.Series:
    """Annualised rolling standard deviation of log returns."""
    return log_returns(closes).rolling(window).std(ddof=1) * np.sqrt(TRADING_DAYS)


def parkinson_vol(ohlc: pd.DataFrame, window: int) -> pd.Series:
    hl = np.log(ohlc.high / ohlc.low) ** 2
    return np.sqrt(hl.rolling(window).mean() / (4 * np.log(2)) * TRADING_DAYS)


def garman_klass_vol(ohlc: pd.DataFrame, window: int) -> pd.Series:
    hl = 0.5 * np.log(ohlc.high / ohlc.low) ** 2
    co = (2 * np.log(2) - 1) * np.log(ohlc.close / ohlc.open) ** 2
    return np.sqrt((hl - co).rolling(window).mean().clip(lower=0) * TRADING_DAYS)


def rogers_satchell_vol(ohlc: pd.DataFrame, window: int) -> pd.Series:
    rs = np.log(ohlc.high / ohlc.close) * np.log(ohlc.high / ohlc.open) + np.log(
        ohlc.low / ohlc.close
    ) * np.log(ohlc.low / ohlc.open)
    return np.sqrt(rs.rolling(window).mean().clip(lower=0) * TRADING_DAYS)


def yang_zhang_vol(ohlc: pd.DataFrame, window: int) -> pd.Series:
    """Yang-Zhang annualised volatility over a rolling window."""
    if window < 3:
        raise ValueError("Yang-Zhang needs a window of at least 3 days.")

    overnight = np.log(ohlc.open / ohlc.close.shift(1))
    open_to_close = np.log(ohlc.close / ohlc.open)
    rs = np.log(ohlc.high / ohlc.close) * np.log(ohlc.high / ohlc.open) + np.log(
        ohlc.low / ohlc.close
    ) * np.log(ohlc.low / ohlc.open)

    v_overnight = overnight.rolling(window).var(ddof=1)
    v_open_close = open_to_close.rolling(window).var(ddof=1)
    v_rs = rs.rolling(window).mean()

    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    total = v_overnight + k * v_open_close + (1 - k) * v_rs
    return np.sqrt(total.clip(lower=0) * TRADING_DAYS)


def realized_vol_table(ohlc: pd.DataFrame, windows=DEFAULT_WINDOWS) -> pd.DataFrame:
    """Latest realised vol by estimator and window, in percent."""
    rows = []
    for w in windows:
        if len(ohlc) < w + 2:
            continue
        rows.append(
            {
                "window (days)": w,
                "close-to-close %": float(close_to_close_vol(ohlc.close, w).iloc[-1] * 100),
                "Parkinson %": float(parkinson_vol(ohlc, w).iloc[-1] * 100),
                "Garman-Klass %": float(garman_klass_vol(ohlc, w).iloc[-1] * 100),
                "Yang-Zhang %": float(yang_zhang_vol(ohlc, w).iloc[-1] * 100),
            }
        )
    return pd.DataFrame(rows)


def forward_realized_vol(closes: pd.Series, horizon: int = VRP_HORIZON) -> pd.Series:
    """Annualised realised vol over the ``horizon`` days *after* each date.

    The final ``horizon`` entries are NaN by construction: that future has not
    happened yet.
    """
    rets = log_returns(closes)
    # rolling() looks backwards, so compute trailing vol then shift it back to
    # the date the window starts from.
    trailing = rets.rolling(horizon).std(ddof=1) * np.sqrt(TRADING_DAYS)
    return trailing.shift(-horizon)


def percentile_rank(series: pd.Series, value: float) -> float:
    """Share of the history at or below ``value``, in percent."""
    clean = pd.Series(series).dropna()
    if clean.empty or not np.isfinite(value):
        return float("nan")
    return float((clean <= value).mean() * 100)


@dataclass(frozen=True)
class VrpSeries:
    frame: pd.DataFrame  # implied, forward_rv, vrp, all in percent
    horizon: int

    @property
    def latest_implied(self) -> float:
        return float(self.frame.implied.dropna().iloc[-1])

    @property
    def latest_complete(self) -> pd.Series:
        """Most recent row whose forward realised leg is actually known."""
        done = self.frame.dropna(subset=["vrp"])
        return done.iloc[-1] if len(done) else pd.Series(dtype=float)

    def stats(self) -> dict:
        vrp = self.frame.vrp.dropna()
        if vrp.empty:
            return {}
        return {
            "mean": float(vrp.mean()),
            "median": float(vrp.median()),
            "positive_share": float((vrp > 0).mean()),
            "current": float(vrp.iloc[-1]),
            "current_percentile": percentile_rank(vrp, float(vrp.iloc[-1])),
            "n": int(len(vrp)),
        }


def vrp_series(implied_close: pd.Series, index_closes: pd.Series, horizon: int = VRP_HORIZON) -> VrpSeries:
    """Align an implied-vol index (VIX, in percent) against subsequent realised vol."""
    implied = pd.Series(implied_close).astype(float).dropna()
    realized = forward_realized_vol(index_closes, horizon) * 100.0

    frame = pd.DataFrame({"implied": implied, "forward_rv": realized}).dropna(subset=["implied"])
    frame["vrp"] = frame.implied - frame.forward_rv
    return VrpSeries(frame=frame, horizon=horizon)


def implied_vs_trailing(atm_iv: float, ohlc: pd.DataFrame, window: int = VRP_HORIZON) -> dict:
    """Today's implied vol against trailing realised for a single ticker.

    Weaker than a true forward-looking VRP -- it compares a forecast with the
    recent past -- but it is the only per-ticker read available without a paid
    implied-vol history.
    """
    if len(ohlc) < window + 2:
        return {}
    yz = yang_zhang_vol(ohlc, window)
    cc = close_to_close_vol(ohlc.close, window)
    current_rv = float(yz.iloc[-1] * 100)
    implied_pct = float(atm_iv * 100) if np.isfinite(atm_iv) else float("nan")
    return {
        "implied_pct": implied_pct,
        "yang_zhang_pct": current_rv,
        "close_to_close_pct": float(cc.iloc[-1] * 100),
        "spread_pts": implied_pct - current_rv,
        "ratio": implied_pct / current_rv if current_rv > 0 else float("nan"),
        "rv_percentile": percentile_rank(yz.dropna() * 100, current_rv),
        "rv_series": yz * 100,
    }
