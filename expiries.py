"""Which expirations to load when a ticker first opens.

The picker defaults are their own problem, separate from drawing anything, and
they carry enough judgement to be worth testing on their own -- which is why
they live here rather than in ``app.py``, where nothing is reachable without
starting Streamlit.

The monthly expiration is the anchor of
the whole curve -- it carries the overwhelming majority of open interest, it is
the only date with LEAPS behind it, and it is where dealer hedging and pinning
concentrate. Landing there on first load means the density, the expected move
and the gamma profile all describe the date the market itself is organised
around, rather than whichever weekly happened to be a few days out.

The rest of the selection is a ladder rather than a cluster, because the panels
it feeds want different things from it. A term structure needs a long arm to
have any curvature at all -- four dates inside a fortnight give a slope and
nothing else. Gamma exposure wants the dates where open interest actually
sits, which is the monthlies. The calendar arbitrage check needs neighbours to
compare. So: everything through the first monthly, then the second monthly,
then a quarter and half a year out.
"""

from __future__ import annotations

import benchmark
import earnings as earn
import prep


MIN_PRIMARY_DTE = 1.0  # skip a monthly expiring today; its density is degenerate
FALLBACK_PRIMARY_DTE = 5.0  # used only when no monthly is listed at all
MAX_DEFAULT_EXPIRIES = 8  # each expiry is a separate chain download
LADDER_DTE = (90.0, 180.0)  # the long arm, in calendar days
LADDER_OPEX_SLACK = 21.0  # days a monthly may miss a ladder target and still win it
MIN_EVENT_PRIMARY_DTE = 2.0  # a post-earnings expiry any nearer is mostly settlement


def first_opex(expirations: list[str]) -> str | None:
    """The nearest monthly expiration with more than a day left on it."""
    return next(
        (e for e in expirations
         if prep.is_opex(e) and prep.year_fraction(e)[0] >= MIN_PRIMARY_DTE),
        None,
    )


def _spread(items: list[str], n: int) -> list[str]:
    """``n`` of ``items``, evenly spaced, endpoints always kept.

    Trimming with a slice would take the first few, and on a name with daily
    expirations the first few are all inside one week -- a cluster, which is
    exactly what the term structure cannot use. Sampling across the range keeps
    the near date and the far one and spreads whatever budget is left between
    them.
    """
    if n >= len(items):
        return list(items)
    if n <= 0:
        return []
    if n == 1:
        return [items[0]]
    picks = {round(i * (len(items) - 1) / (n - 1)) for i in range(n)}
    return [items[i] for i in sorted(picks)]


def default_expiries(expirations: list[str], event=None) -> tuple[list[str], str]:
    """A maturity ladder for the surface, and the expiry worth studying closely.

    Everything through the first monthly, then the second monthly, then the
    listed expiries nearest 90 and 180 days. That is a curve rather than a
    cluster: short enough at the front to price the next fortnight, long enough
    at the back for the term structure to bend.

    ``event`` is the next earnings announcement, or None. When one lands inside
    the front of the curve the primary moves to the first expiry that captures
    it, because a density that excludes the jump is describing a different
    stock. The last expiry before it is pinned into the selection too -- that
    pair is what the earnings decomposition needs, and losing either to the
    budget would silently empty the panel.
    """
    opex = first_opex(expirations)
    if opex is None:
        # Nothing in the list is a third Friday. That happens when a monthly is
        # shifted to the Thursday by a holiday, and on thin names that list only
        # a few dates. Fall back to the first expiry far enough out to have a
        # chain worth fitting.
        opex = next(
            (e for e in expirations if prep.year_fraction(e)[0] >= FALLBACK_PRIMARY_DTE),
            expirations[-1],
        )

    cut = expirations.index(opex)
    front, later = expirations[: cut + 1], expirations[cut + 1:]

    # A chain expiring today has hours of life left in it. Its at-the-money vol
    # is mostly settlement mechanics, and carrying it into the surface bends the
    # term structure and fills the calendar arbitrage check with noise. Drop it
    # from the default -- anyone who wants it can still tick it.
    live = [e for e in front if prep.year_fraction(e)[0] >= MIN_PRIMARY_DTE]
    front = live or front

    # The long arm. The second monthly is picked by name rather than by days so
    # it lands on the date with the open interest; the other two are picked by
    # distance because no listing convention puts an expiry at exactly 90 days.
    anchors = [opex]
    second = next((e for e in later if prep.is_opex(e)), None)
    if second is not None:
        anchors.append(second)
    for target in LADDER_DTE:
        dte_of = lambda e: prep.year_fraction(e)[0]  # noqa: E731
        # A monthly near the target beats a weekly on it. The ladder exists to
        # give the term structure a long arm, and a thin off-cycle chain 90 days
        # out is a worse reading of that arm than a monthly 80 days out.
        monthlies = [e for e in later
                     if prep.is_opex(e) and abs(dte_of(e) - target) <= LADDER_OPEX_SLACK]
        pick = benchmark.nearest_expiry(monthlies or later, target, dte_of)
        if pick is not None and pick not in anchors:
            anchors.append(pick)

    primary = opex
    pinned = set(anchors)
    if event is not None:
        spanning = [e for e in front if earn.spans(e, event)]
        if spanning:
            # The first expiry that captures the event, unless it settles almost
            # immediately after it -- at a day or two out the density is being
            # priced off intrinsic value and says little about the jump.
            primary = next(
                (e for e in spanning if prep.year_fraction(e)[0] >= MIN_EVENT_PRIMARY_DTE),
                spanning[0],
            )
            pinned.add(primary)
            before = [e for e in front if not earn.spans(e, event)]
            if before:
                pinned.add(before[-1])

    # Whatever budget the anchors leave goes to the front of the curve. Daily
    # expiry names list a dozen dates before the monthly and each one is its own
    # download, so this is where the waiting is saved.
    budget = MAX_DEFAULT_EXPIRIES - len(pinned)
    chosen = pinned | set(_spread([e for e in front if e not in pinned], budget))

    # A name with only a handful of listings hits its ladder targets long before
    # it hits the budget -- everything from the second monthly to the far anchor
    # is one jump, with room to spare. Spend what is left filling that gap in.
    spare = MAX_DEFAULT_EXPIRIES - len(chosen)
    if spare > 0 and anchors:
        gap = [e for e in later if e not in chosen and e < max(anchors)]
        chosen |= set(_spread(gap, spare))
    return sorted(chosen), primary
