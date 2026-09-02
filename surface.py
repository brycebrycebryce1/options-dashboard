"""Implied volatility surface, term structure and skew.

The surface is built over (days to expiry, log-moneyness) using out-of-the-money
quotes only, because OTM options are the liquid side and their implied vols are
what the market actually trades. Log-moneyness ln(K/F) rather than raw strike
keeps expiries comparable when the forward drifts.

Skew is summarised per expiry with the two numbers an FX desk would quote, both
expressed in vol points:

* **25-delta risk reversal** = IV(25-delta call) - IV(25-delta put). Negative
  means puts are bid over calls -- the normal state for equity indices, where
  crash protection carries a premium. A sharply more negative reading than usual
  is the market paying up for downside.
* **25-delta butterfly** = average of the two wings minus the ATM vol. This is
  the smile's curvature: how much more than ATM the market charges for tails on
  *both* sides.

Deltas come from the chain's own forward, so they are consistent with the
density and gamma modules rather than assuming a dividend yield.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.interpolate import griddata

from prep import ExpirySnapshot

TARGET_DELTA = 0.25


def surface_table(snaps: list[ExpirySnapshot]) -> pd.DataFrame:
    """Tidy long-format surface: one row per usable OTM quote."""
    frames = []
    for snap in snaps:
        otm = snap.otm()
        if otm.empty:
            continue
        frames.append(
            pd.DataFrame(
                {
                    "expiry": snap.expiry,
                    "dte": snap.dte,
                    "T": snap.T,
                    "strike": otm.strike.to_numpy(),
                    "log_moneyness": otm.log_moneyness.to_numpy(),
                    "moneyness_pct": (otm.strike.to_numpy() / snap.forward - 1.0) * 100.0,
                    "iv": otm.iv.to_numpy(),
                    "delta": otm.delta.to_numpy(),
                    "is_call": otm.is_call.to_numpy(),
                    "open_interest": otm.open_interest.to_numpy(),
                    "volume": otm.volume.to_numpy(),
                    "forward": snap.forward,
                }
            )
        )
    if not frames:
        raise ValueError("No usable out-of-the-money quotes across the selected expiries.")
    return pd.concat(frames, ignore_index=True)


def surface_grid(
    table: pd.DataFrame,
    n_dte: int = 40,
    n_k: int = 60,
    k_clip: float = 0.35,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Interpolate the scattered quotes onto a regular grid for a 3D plot.

    Linear interpolation inside the convex hull of the quotes, with a
    nearest-neighbour pass to fill the corners the hull does not cover. The
    filled corners are extrapolation and should be read as decoration, not data.
    """
    table = table[np.abs(table.log_moneyness) <= k_clip]
    if table.expiry.nunique() < 2 or len(table) < 12:
        raise ValueError("Need at least two expiries with quotes to build a surface.")

    dte_axis = np.linspace(table.dte.min(), table.dte.max(), n_dte)
    k_axis = np.linspace(table.log_moneyness.min(), table.log_moneyness.max(), n_k)
    grid_dte, grid_k = np.meshgrid(dte_axis, k_axis)

    points = table[["dte", "log_moneyness"]].to_numpy()
    values = table.iv.to_numpy()
    surface = griddata(points, values, (grid_dte, grid_k), method="linear")
    holes = ~np.isfinite(surface)
    if holes.any():
        surface[holes] = griddata(points, values, (grid_dte, grid_k), method="nearest")[holes]
    return dte_axis, k_axis, surface


def _iv_at_delta(rows: pd.DataFrame, target: float) -> float:
    """Interpolate IV against delta. ``target`` is signed (+0.25 call, -0.25 put)."""
    rows = rows.dropna(subset=["delta", "iv"])
    if len(rows) < 2:
        return float("nan")
    rows = rows.sort_values("delta")
    deltas, ivs = rows.delta.to_numpy(), rows.iv.to_numpy()
    # Refuse to extrapolate: a wing that stops at 35 delta cannot tell us the
    # 25-delta vol, and pretending otherwise silently invents skew.
    if target < deltas.min() or target > deltas.max():
        return float("nan")
    return float(np.interp(target, deltas, ivs))


def skew_metrics(snaps: list[ExpirySnapshot]) -> pd.DataFrame:
    """Per-expiry ATM vol, 25-delta risk reversal and 25-delta butterfly."""
    rows = []
    for snap in snaps:
        otm = snap.otm()
        calls = otm[otm.is_call & otm.delta.between(0.02, 0.5)]
        puts = otm[~otm.is_call & otm.delta.between(-0.5, -0.02)]

        atm = snap.atm_iv
        call_wing = _iv_at_delta(calls, TARGET_DELTA)
        put_wing = _iv_at_delta(puts, -TARGET_DELTA)
        rr = call_wing - put_wing
        bf = 0.5 * (call_wing + put_wing) - atm

        rows.append(
            {
                "expiry": snap.expiry,
                "dte": snap.dte,
                "atm_iv": atm,
                "iv_25d_call": call_wing,
                "iv_25d_put": put_wing,
                "rr_25d": rr,
                "bf_25d": bf,
                "n_quotes": int(len(otm)),
            }
        )
    return pd.DataFrame(rows).sort_values("dte").reset_index(drop=True)


def term_structure(snaps: list[ExpirySnapshot]) -> pd.DataFrame:
    """ATM vol by expiry, plus the forward vol implied between consecutive ones.

    Forward variance must be additive: sigma_fwd^2 (T2 - T1) = sigma2^2 T2 -
    sigma1^2 T1. A negative result means the quoted term structure is internally
    inconsistent (calendar arbitrage), usually a stale quote on one expiry.

    ``calendar_arb`` here is the at-the-money special case, which is all this
    function can see. :mod:`noarb` runs the same test across the whole
    overlapping moneyness range and is the authoritative one; a stale wing on a
    far expiry is invisible at the forward.
    """
    rows = [{"expiry": s.expiry, "dte": s.dte, "T": s.T, "atm_iv": s.atm_iv} for s in snaps]
    out = pd.DataFrame(rows).sort_values("T").reset_index(drop=True)

    total_var = out.atm_iv**2 * out["T"]
    d_var = total_var.diff()
    d_t = out["T"].diff()
    with np.errstate(invalid="ignore", divide="ignore"):
        fwd_var = d_var / d_t
    out["forward_iv"] = np.sqrt(np.where(fwd_var > 0, fwd_var, np.nan))
    out["calendar_arb"] = (d_var < 0).fillna(False)
    return out
