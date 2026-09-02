"""Static no-arbitrage checks on the raw quotes.

Everything downstream -- the density, the moments, the expected move -- is built
on the assumption that the quoted chain is internally consistent. It usually is.
When it is not, the failure is silent: a stale print produces a density that
still integrates to one and still has a mean at the forward, so none of the
existing checks fire, and the number on the page is simply wrong. That is
exactly how a $810 strike on a $469 spot survived long enough to report a skew
of +3.3.

These are the three static conditions a set of option prices has to satisfy for
*no* model at all, at a single point in time. They are tested against the quoted
mids, not against the fitted smile -- testing the fit would only tell us the
spline is smooth, which we already know.

**Vertical.** Call value must fall as strike rises, and must not fall faster
than one-for-one::

    -1 <= dC/dK <= 0

A rising call price means a call spread with negative cost and positive payoff.
Falling faster than 1:1 means the spread costs more than its maximum payoff.

**Butterfly.** Call value must be convex in strike::

    C(K2) <= lambda C(K1) + (1 - lambda) C(K3),  lambda = (K3 - K2) / (K3 - K1)

Violating it means a butterfly with negative cost. This condition is equivalent
to the density being non-negative, which is why it catches the exact quotes that
corrupt a Breeden-Litzenberger density.

**Calendar.** Total implied variance must not fall as expiry lengthens, at any
fixed log-moneyness::

    sigma(k, T1)^2 T1 <= sigma(k, T2)^2 T2   for T1 < T2

Violating it means a calendar spread with negative cost.

Two design decisions matter more than the formulas.

*Puts and calls are put on one curve.* Only out-of-the-money quotes are used --
they are the liquid side -- so the put wing is converted to call values through
put-call parity, C = P + df(F - K), using the chain's own regressed forward.
That makes the whole strike range one convex curve to test. The cost is that an
error in the forward shifts the put side by a constant and leaves the call side
alone, which puts a small kink at the crossover. The tolerance below absorbs it.

*The tolerance is the bid-ask spread, not a fixed epsilon.* A mid is not a
price; it is the centre of an interval the true value lies somewhere inside. A
violation only counts as a violation if it is larger than the quote uncertainty
of the strikes involved -- otherwise every penny-wide chain in the market would
light up. That means these checks flag prices that cannot be reconciled *at any
point inside their own spreads*, which is a real inconsistency rather than a
rounding artefact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from prep import ExpirySnapshot

CALENDAR_GRID = 25  # log-moneyness points sampled across each overlapping pair


@dataclass(frozen=True)
class ArbReport:
    """Every static violation found across the selected expiries."""

    vertical: pd.DataFrame
    butterfly: pd.DataFrame
    calendar: pd.DataFrame
    checks_run: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "vertical": len(self.vertical),
            "butterfly": len(self.butterfly),
            "calendar": len(self.calendar),
        }

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def clean(self) -> bool:
        return self.total == 0

    @property
    def expiries_affected(self) -> list[str]:
        seen: list[str] = []
        for frame, column in ((self.vertical, "expiry"), (self.butterfly, "expiry"),
                              (self.calendar, "far expiry")):
            if not frame.empty:
                for value in frame[column]:
                    if value not in seen:
                        seen.append(value)
        return sorted(seen)

    def all_violations(self) -> pd.DataFrame:
        """The three frames stacked into one shape, for a single table.

        The calendar frame names two expiries; it is folded onto the same
        columns as the others by reporting the far leg, which is the one whose
        quote is out of line.
        """
        columns = ["check", "expiry", "where", "excess", "detail"]
        parts = []
        for kind, frame in (("Vertical", self.vertical), ("Butterfly", self.butterfly),
                            ("Calendar", self.calendar)):
            if frame.empty:
                continue
            tidy = frame.copy()
            if kind == "Calendar":
                tidy["detail"] = "vs " + tidy["near expiry"] + ": " + tidy["detail"]
                tidy = tidy.rename(columns={"far expiry": "expiry"}).drop(columns=["near expiry"])
            tidy.insert(0, "check", kind)
            parts.append(tidy[columns])
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)


def call_curve(snap: ExpirySnapshot) -> pd.DataFrame:
    """Out-of-the-money quotes as one undiscounted call curve.

    Undiscounted (forward-measure) values are used throughout so the slope bound
    is exactly [-1, 0] rather than [-df, 0], and so the numbers are comparable
    across expiries with different discount factors.
    """
    otm = snap.otm()
    strike = otm.strike.to_numpy(float)
    mid = otm.mid.to_numpy(float) / snap.df
    # Parity converts the put wing to call values: C = P + df(F - K).
    call = np.where(otm.is_call.to_numpy(bool), mid, mid + (snap.forward - strike))
    return pd.DataFrame({
        "strike": strike,
        "call": call,
        "half_spread": 0.5 * otm.spread.to_numpy(float) / snap.df,
        "is_call": otm.is_call.to_numpy(bool),
    }).sort_values("strike").reset_index(drop=True)


def vertical_violations(snap: ExpirySnapshot, curve: pd.DataFrame | None = None) -> pd.DataFrame:
    """Adjacent strikes where the call curve rises, or falls faster than 1:1."""
    c = call_curve(snap) if curve is None else curve
    if len(c) < 2:
        return _empty()

    k = c.strike.to_numpy()
    v = c.call.to_numpy()
    h = c.half_spread.to_numpy()

    dk = np.diff(k)
    dv = np.diff(v)
    tol = h[:-1] + h[1:]

    # dv should sit in [-dk, 0]. Two ways out, so two excess measures.
    rising = dv - tol  # positive: value went up with strike
    steep = (-dv - dk) - tol  # positive: value fell by more than the strike gap

    rows = []
    for i in range(len(dk)):
        if rising[i] > 0:
            rows.append({
                "expiry": snap.expiry,
                "where": f"${k[i]:,.2f} → ${k[i + 1]:,.2f}",
                "excess": float(rising[i]),
                "detail": f"call value rises {dv[i]:+,.2f} with strike",
            })
        elif steep[i] > 0:
            rows.append({
                "expiry": snap.expiry,
                "where": f"${k[i]:,.2f} → ${k[i + 1]:,.2f}",
                "excess": float(steep[i]),
                "detail": f"falls ${-dv[i]:,.2f} across a ${dk[i]:,.2f} strike gap",
            })
    return pd.DataFrame(rows) if rows else _empty()


def butterfly_violations(snap: ExpirySnapshot, curve: pd.DataFrame | None = None) -> pd.DataFrame:
    """Consecutive strike triples where the call curve is concave.

    Concavity here is the same statement as negative probability density over
    that strike range, so a hit is a direct explanation for a density that had
    to be clipped.
    """
    c = call_curve(snap) if curve is None else curve
    if len(c) < 3:
        return _empty()

    k = c.strike.to_numpy()
    v = c.call.to_numpy()
    h = c.half_spread.to_numpy()

    k1, k2, k3 = k[:-2], k[1:-1], k[2:]
    v1, v2, v3 = v[:-2], v[1:-1], v[2:]
    h1, h2, h3 = h[:-2], h[1:-1], h[2:]

    with np.errstate(divide="ignore", invalid="ignore"):
        lam = (k3 - k2) / (k3 - k1)
    chord = lam * v1 + (1.0 - lam) * v3
    tol = lam * h1 + h2 + (1.0 - lam) * h3
    excess = (v2 - chord) - tol

    rows = []
    for i in np.flatnonzero(np.isfinite(excess) & (excess > 0)):
        rows.append({
            "expiry": snap.expiry,
            "where": f"${k1[i]:,.2f} / ${k2[i]:,.2f} / ${k3[i]:,.2f}",
            "excess": float(excess[i]),
            "detail": f"middle strike sits ${v2[i] - chord[i]:,.2f} above the chord "
                      f"(spreads allow ${tol[i]:,.2f})",
        })
    return pd.DataFrame(rows) if rows else _empty()


def calendar_violations(snaps: list[ExpirySnapshot], grid: int = CALENDAR_GRID) -> pd.DataFrame:
    """Consecutive expiry pairs where total variance falls going further out.

    Checked across the whole overlapping moneyness range rather than only at the
    forward: a stale wing on the far expiry is invisible at the money.
    """
    ordered = sorted([s for s in snaps if np.isfinite(s.T) and s.T > 0], key=lambda s: s.T)
    rows = []

    for near, far in zip(ordered, ordered[1:]):
        a, b = near.otm(), far.otm()
        if len(a) < 3 or len(b) < 3:
            continue

        ka = a.log_moneyness.to_numpy(float)
        kb = b.log_moneyness.to_numpy(float)
        lo, hi = max(ka.min(), kb.min()), min(ka.max(), kb.max())
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            continue  # the two chains do not quote a common strike range

        ks = np.linspace(lo, hi, grid)
        iv_a = np.interp(ks, ka, a.iv.to_numpy(float))
        iv_b = np.interp(ks, kb, b.iv.to_numpy(float))

        w_a = iv_a**2 * near.T
        w_b = iv_b**2 * far.T

        # First-order propagation of each chain's own vol uncertainty into total
        # variance: dw = 2 iv T d(iv).
        err_a = float(np.nanmedian(a.iv_error.to_numpy(float)))
        err_b = float(np.nanmedian(b.iv_error.to_numpy(float)))
        tol = 2 * iv_a * near.T * err_a + 2 * iv_b * far.T * err_b

        drop = w_a - w_b - tol
        hits = np.flatnonzero(np.isfinite(drop) & (drop > 0))
        if hits.size == 0:
            continue

        worst = hits[int(np.argmax(drop[hits]))]
        # Expressed as the vol the far expiry would need to be arbitrage-free,
        # which is a readable number in a way that a variance gap is not.
        needed = float(np.sqrt(w_a[worst] / far.T))
        rows.append({
            "near expiry": near.expiry,
            "far expiry": far.expiry,
            "where": f"{(np.exp(ks[worst]) - 1) * 100:+.1f}% vs forward",
            "excess": needed - float(iv_b[worst]),
            "detail": f"{hits.size} of {grid} sampled strikes; {far.expiry} quotes "
                      f"{iv_b[worst] * 100:.1f}% where {needed * 100:.1f}% is the floor",
        })

    return pd.DataFrame(rows) if rows else _empty(calendar=True)


def check(snaps: list[ExpirySnapshot]) -> ArbReport:
    """Run all three checks over a list of expiries."""
    vertical, butterfly = [], []
    strikes = 0
    for snap in snaps:
        curve = call_curve(snap)
        strikes += len(curve)
        vertical.append(vertical_violations(snap, curve))
        butterfly.append(butterfly_violations(snap, curve))

    return ArbReport(
        vertical=_stack(vertical),
        butterfly=_stack(butterfly),
        calendar=calendar_violations(snaps),
        checks_run={
            "expiries": len(snaps),
            "strikes": strikes,
            "pairs": max(len(snaps) - 1, 0),
        },
    )


def summary(report: ArbReport) -> str:
    """One sentence saying what the checks found."""
    run = report.checks_run
    scope = (f"{run.get('strikes', 0)} quoted strikes across "
             f"{run.get('expiries', 0)} expirations")
    if report.clean:
        return (f"No static arbitrage: {scope} satisfy the vertical, butterfly and "
                "calendar conditions to within their own bid-ask spreads.")

    counts = report.counts
    parts = [f"{n} {name}" for name, n in counts.items() if n]
    return (f"{report.total} static arbitrage violation{'s' if report.total > 1 else ''} "
            f"({', '.join(parts)}) across {scope}. These are quotes that cannot be "
            "reconciled at any price inside their own spreads, so they are almost "
            "certainly stale prints rather than opportunities — and anything fitted "
            "through them inherits the error.")


def _empty(calendar: bool = False) -> pd.DataFrame:
    columns = (["near expiry", "far expiry", "where", "excess", "detail"] if calendar
               else ["expiry", "where", "excess", "detail"])
    return pd.DataFrame(columns=columns)


def _stack(frames: list[pd.DataFrame]) -> pd.DataFrame:
    live = [f for f in frames if not f.empty]
    return pd.concat(live, ignore_index=True) if live else _empty()
