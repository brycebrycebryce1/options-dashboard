"""SEC EDGAR: insider transactions, XBRL fundamentals and 13F position changes.

All free, no API key, no vendor. The SEC's only requirement is that requests
declare a contact in the User-Agent header and stay under 10 requests/second;
both are handled here. Set the ``SEC_CONTACT`` environment variable (or pass
``contact=``) to something the SEC can reach you at -- requests with an obviously
fake contact do get blocked.

Three data paths:

* **Form 4** -- every officer, director and 10% holder must report their trades
  within two business days. The raw XML is parsed rather than the rendered HTML.
  Only transaction codes P (open-market purchase) and S (open-market sale) carry
  signal: code A is a grant the insider did not choose to take, M is an option
  exercise, F is shares withheld to pay tax on vesting. Lumping those in with
  purchases is the single most common way insider data gets misread.
* **XBRL company facts** -- the numbers straight out of the filings, so the
  fundamentals here are the company's own reported figures with no vendor
  normalisation in between. Two corrections matter: cash-flow lines are filed
  year-to-date and have to be un-cumulated into discrete quarters, and companies
  migrate between tags as the taxonomy changes (NVIDIA moved revenue from
  ``RevenueFromContractWithCustomerExcludingAssessedTax`` to ``Revenues`` in
  fiscal 2022), so the alternatives for each line item are merged rather than
  taking the first one that happens to have data.
* **13F-HR** -- institutional managers over $100m report long US equity
  positions 45 days after quarter end. Comparing consecutive filings shows what
  a manager bought and sold. The lag is the point of caution: a 13F describes a
  book that is up to four and a half months stale.
"""

from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
SEC_XBRL_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

# The SEC asks for <= 10 requests/second. Stay comfortably under it.
REQUEST_INTERVAL = 0.12

# 13F dollar values were reported in thousands until the amended form took
# effect for filings made on or after 23 January 2023.
THOUSANDS_CUTOFF = date(2023, 1, 23)

OPEN_MARKET_CODES = {"P", "S"}
TRANSACTION_CODES = {
    "P": "Open-market purchase",
    "S": "Open-market sale",
    "A": "Grant or award",
    "M": "Option exercise",
    "F": "Shares withheld for tax",
    "G": "Gift",
    "C": "Conversion",
    "D": "Disposition to issuer",
    "X": "In-the-money option exercise",
    "J": "Other",
}

# Guards the shared rate budget. The lock is held across the sleep on purpose:
# that is what makes the interval apply between request *starts* for the process
# as a whole rather than per thread, so several workers can overlap the latency
# of their requests without ever raising the rate the SEC sees.
_rate_lock = threading.Lock()
_last_request = 0.0


class EdgarError(RuntimeError):
    """Raised when EDGAR cannot be reached or returns something unusable."""


# EDGAR requires a contact in the User-Agent. This dashboard is single-user, so
# the owner's address is baked in; SEC_CONTACT overrides it if it is ever run
# by someone else.
OWNER_CONTACT = "brycebrycebryce222@gmail.com"


def default_contact() -> str:
    return os.environ.get("SEC_CONTACT", "").strip() or OWNER_CONTACT


@lru_cache(maxsize=8)
def _session(contact: str):
    import requests

    if not contact:
        raise EdgarError(
            "SEC requires a contact in the User-Agent. Set the SEC_CONTACT environment "
            "variable (an email address or name + email) or fill in the sidebar field."
        )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"market-dashboard/1.0 ({contact})",
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/xml, */*",
        }
    )
    return session


def _throttle() -> None:
    """Block until this thread is allowed to start another SEC request."""
    global _last_request
    with _rate_lock:
        wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _get(url: str, contact: str, as_json: bool = True):
    """Throttled GET against SEC hosts."""
    session = _session(contact)
    _throttle()
    response = session.get(url, timeout=30)

    if response.status_code == 403:
        raise EdgarError(
            "SEC returned 403. That is almost always the User-Agent: set SEC_CONTACT "
            "to a real email address."
        )
    if response.status_code == 404:
        raise EdgarError(f"Not found on EDGAR: {url}")
    response.raise_for_status()
    return response.json() if as_json else response.content


def _strip_namespaces(root: ET.Element) -> ET.Element:
    """EDGAR XML mixes namespaced and bare documents; normalise to bare tags."""
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _text(node: ET.Element | None, *path: str) -> str | None:
    if node is None:
        return None
    for step in path:
        node = node.find(step)
        if node is None:
            return None
    return (node.text or "").strip() or None


def _number(node: ET.Element | None, *path: str) -> float:
    raw = _text(node, *path)
    if raw is None:
        return float("nan")
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return float("nan")


# ---------------------------------------------------------------------------
# Company lookup
# ---------------------------------------------------------------------------


def ticker_to_cik(symbol: str, contact: str) -> tuple[str, str]:
    """Map a ticker to its zero-padded CIK and registered name."""
    table = _get(SEC_TICKERS, contact)
    symbol = symbol.strip().upper()
    for entry in table.values():
        if entry.get("ticker", "").upper() == symbol:
            return f"{int(entry['cik_str']):010d}", entry.get("title", symbol)
    raise EdgarError(f"'{symbol}' is not in the SEC's ticker list (ADRs and funds often are not).")


def normalise_cik(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        raise EdgarError(f"'{raw}' does not contain a CIK.")
    return f"{int(digits):010d}"


def submissions(cik: str, contact: str) -> dict:
    return _get(f"{SEC_SUBMISSIONS}/CIK{normalise_cik(cik)}.json", contact)


def recent_filings(cik: str, contact: str, form: str, limit: int = 60) -> pd.DataFrame:
    """Most recent filings of one form type from a company's submission index."""
    payload = submissions(cik, contact)
    recent = payload.get("filings", {}).get("recent", {})
    if not recent:
        return pd.DataFrame()

    frame = pd.DataFrame(recent)
    frame = frame[frame.form.astype(str).str.upper() == form.upper()].copy()
    if frame.empty:
        return frame
    frame["filingDate"] = pd.to_datetime(frame.filingDate, errors="coerce")
    frame["reportDate"] = pd.to_datetime(frame.get("reportDate"), errors="coerce")
    frame["entityName"] = payload.get("name", "")
    return frame.sort_values("filingDate", ascending=False).head(limit).reset_index(drop=True)


def _document_url(cik: str, accession: str, document: str) -> str:
    acc = accession.replace("-", "")
    # submissions lists the human-readable rendering (xslF345X03/foo.xml); the
    # raw XML sits at the same path with that prefix removed.
    document = re.sub(r"^xsl[^/]*/", "", document)
    return f"{SEC_ARCHIVES}/{int(cik)}/{acc}/{document}"


def filing_index_url(cik: str, accession: str) -> str:
    return f"{SEC_ARCHIVES}/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


# ---------------------------------------------------------------------------
# Form 4 - insider transactions
# ---------------------------------------------------------------------------


def parse_form4(xml_bytes: bytes) -> list[dict]:
    """Pull the reported transactions out of one Form 4 XML document."""
    root = _strip_namespaces(ET.fromstring(xml_bytes))

    owner = root.find("reportingOwner")
    name = _text(owner, "reportingOwnerId", "rptOwnerName") or "unknown"
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    roles = []
    if _text(rel, "isDirector") in {"1", "true"}:
        roles.append("Director")
    if _text(rel, "isOfficer") in {"1", "true"}:
        roles.append(_text(rel, "officerTitle") or "Officer")
    if _text(rel, "isTenPercentOwner") in {"1", "true"}:
        roles.append("10% owner")
    role = ", ".join(roles) or "Insider"

    period = _text(root, "periodOfReport")
    issuer = _text(root, "issuer", "issuerTradingSymbol")

    rows = []
    for table, derivative in (("nonDerivativeTable", False), ("derivativeTable", True)):
        section = root.find(table)
        if section is None:
            continue
        tag = "nonDerivativeTransaction" if not derivative else "derivativeTransaction"
        for txn in section.findall(tag):
            code = _text(txn, "transactionCoding", "transactionCode") or "?"
            shares = _number(txn, "transactionAmounts", "transactionShares", "value")
            price = _number(txn, "transactionAmounts", "transactionPricePerShare", "value")
            direction = _text(txn, "transactionAmounts", "transactionAcquiredDisposedCode", "value")
            sign = 1.0 if direction == "A" else -1.0
            rows.append(
                {
                    "transaction_date": _text(txn, "transactionDate", "value") or period,
                    "owner": name,
                    "role": role,
                    "symbol": issuer,
                    "code": code,
                    "code_meaning": TRANSACTION_CODES.get(code, "Other"),
                    "derivative": derivative,
                    "direction": direction,
                    "shares": shares,
                    "signed_shares": sign * shares,
                    "price": price,
                    "value": sign * shares * price if np.isfinite(price) else float("nan"),
                    "shares_after": _number(
                        txn, "postTransactionAmounts", "sharesOwnedFollowingTransaction", "value"
                    ),
                    "security": _text(txn, "securityTitle", "value"),
                }
            )
    return rows


def insider_transactions(symbol: str, contact: str, months: int = 12, max_filings: int = 60) -> pd.DataFrame:
    """Form 4 transactions reported against one issuer over a lookback window."""
    cik, _ = ticker_to_cik(symbol, contact)
    filings = recent_filings(cik, contact, "4", limit=max_filings)
    if filings.empty:
        return pd.DataFrame()

    cutoff = pd.Timestamp(datetime.now() - timedelta(days=int(months * 30.44)))
    filings = filings[filings.filingDate >= cutoff]

    rows = []
    for _, filing in filings.iterrows():
        url = _document_url(cik, filing.accessionNumber, filing.primaryDocument)
        try:
            parsed = parse_form4(_get(url, contact, as_json=False))
        except Exception:  # noqa: BLE001 - one malformed filing must not kill the pull
            continue
        for row in parsed:
            row["filed"] = filing.filingDate
            row["filing_url"] = filing_index_url(cik, filing.accessionNumber)
            rows.append(row)

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out["transaction_date"] = pd.to_datetime(out.transaction_date, errors="coerce")
    return out.sort_values("transaction_date", ascending=False).reset_index(drop=True)


def open_market(transactions: pd.DataFrame) -> pd.DataFrame:
    """Only discretionary open-market buys and sells of common stock."""
    if transactions.empty:
        return transactions
    return transactions[
        transactions.code.isin(OPEN_MARKET_CODES) & ~transactions.derivative
    ].reset_index(drop=True)


def insider_summary(transactions: pd.DataFrame) -> dict:
    """Net open-market activity, with grants and tax withholding excluded."""
    trades = open_market(transactions)
    if trades.empty:
        return {}
    buys = trades[trades.code == "P"]
    sells = trades[trades.code == "S"]
    return {
        "n_buys": int(len(buys)),
        "n_sells": int(len(sells)),
        "buy_value": float(buys.value.sum(skipna=True)),
        "sell_value": float(-sells.value.sum(skipna=True)),
        "net_value": float(trades.value.sum(skipna=True)),
        "unique_buyers": int(buys.owner.nunique()),
        "unique_sellers": int(sells.owner.nunique()),
        "first": trades.transaction_date.min(),
        "last": trades.transaction_date.max(),
    }


def cluster_buys(transactions: pd.DataFrame, window_days: int = 30, min_insiders: int = 3) -> pd.DataFrame:
    """Windows where several *different* insiders bought on the open market.

    A cluster buy is the strongest form of the signal in the academic
    literature: one executive buying can be a portfolio decision, three
    independently buying inside a month is much harder to explain that way.
    """
    buys = open_market(transactions)
    if buys.empty:
        return pd.DataFrame()
    buys = buys[buys.code == "P"].sort_values("transaction_date")
    if buys.empty:
        return pd.DataFrame()

    clusters = []
    dates = buys.transaction_date.to_list()
    for start in dates:
        window = buys[
            (buys.transaction_date >= start)
            & (buys.transaction_date < start + pd.Timedelta(days=window_days))
        ]
        if window.owner.nunique() >= min_insiders:
            clusters.append(
                {
                    "window_start": start,
                    "window_end": start + pd.Timedelta(days=window_days),
                    "insiders": window.owner.nunique(),
                    "trades": int(len(window)),
                    "total_value": float(window.value.sum(skipna=True)),
                    "names": ", ".join(sorted(window.owner.unique())),
                }
            )

    if not clusters:
        return pd.DataFrame()
    # Overlapping windows describe the same episode; keep the widest of each run.
    out = pd.DataFrame(clusters).sort_values(["insiders", "total_value"], ascending=False)
    return out.drop_duplicates(subset="names").reset_index(drop=True)


# ---------------------------------------------------------------------------
# XBRL company facts
# ---------------------------------------------------------------------------

# Ordered alternatives: companies tag the same line item differently, and the
# taxonomy has changed over the years.
FLOW_CONCEPTS = {
    "Revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "Gross profit": ["GrossProfit"],
    "Operating income": ["OperatingIncomeLoss"],
    # ``NetIncomeLoss`` is net income attributable to the parent; ``ProfitLoss``
    # includes any noncontrolling interest. They are the same number for a
    # company with no minority interests, and Broadcom is a live example of why
    # the fallback is needed -- it stopped tagging NetIncomeLoss after FY2024
    # and reports only ProfitLoss now, so the margin rows came back empty.
    # Whichever tag runs to the most recent date leads, so this only takes over
    # once the primary tag has gone stale.
    "Net income": ["NetIncomeLoss", "ProfitLoss"],
    "R&D expense": ["ResearchAndDevelopmentExpense"],
    "Operating cash flow": ["NetCashProvidedByUsedInOperatingActivities"],
}

STOCK_CONCEPTS = {
    "Assets": ["Assets"],
    "Liabilities": ["Liabilities"],
    "Shareholders equity": ["StockholdersEquity"],
    "Cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
}


def _no_freq(series: pd.Series) -> pd.Series:
    """A copy whose index carries no inferred frequency.

    pandas stamps a ``freq`` onto a DatetimeIndex whenever the dates happen to
    line up with one of its offsets, and it keeps that stamp through unions.
    Broadcom's fiscal quarters always end on the first Sunday of a month, which
    reads as a clean ``WeekOfMonth`` offset -- so two of its tag series arrive
    here both claiming a frequency. ``Index.union`` then takes a fast path that
    ends in a bare ``assert dates._freq == self.freq``, and a bare assert raises
    an AssertionError carrying no message at all. That is what produced the
    blank "Could not load XBRL facts:" on the page.

    Filing dates are not a regular series in any case -- fiscal calendars drift,
    and a 53-week year breaks the pattern outright -- so the frequency was never
    meaningful. Dropping it keeps the union on its general path.
    """
    if not isinstance(series.index, pd.DatetimeIndex) or series.index.freq is None:
        return series
    out = series.copy()
    out.index = pd.DatetimeIndex(out.index.to_numpy(), name=out.index.name)
    return out


def _concept(cik: str, tag: str, contact: str) -> pd.DataFrame:
    url = f"{SEC_XBRL_CONCEPT}/CIK{normalise_cik(cik)}/us-gaap/{tag}.json"
    try:
        payload = _get(url, contact)
    except EdgarError:
        return pd.DataFrame()

    units = payload.get("units", {})
    key = "USD" if "USD" in units else next(iter(units), None)
    if key is None:
        return pd.DataFrame()

    frame = pd.DataFrame(units[key])
    if frame.empty:
        return frame
    frame["end"] = pd.to_datetime(frame.end, errors="coerce")
    frame["start"] = pd.to_datetime(frame.get("start"), errors="coerce")
    frame["days"] = (frame.end - frame.start).dt.days
    frame["tag"] = tag
    return frame[frame.form.isin(["10-K", "10-Q", "20-F", "40-F"])]


def _quarterly_flow(frame: pd.DataFrame) -> pd.Series:
    """Discrete quarterly values, un-cumulating year-to-date figures.

    Income-statement lines are usually tagged for the discrete quarter, but cash
    flow statements are almost always year-to-date: a 10-Q carries 90, 181 and
    272-day periods and the 10-K carries the full year. Every filing inside one
    fiscal year shares the same period *start*, so grouping on that and taking
    consecutive differences recovers each discrete quarter. Skipping this step
    makes Q4 look four times larger than Q1, which is the single most common way
    XBRL data gets read wrong.

    Directly reported quarters always win; differences only fill the gaps.
    """
    if frame.empty:
        return pd.Series(dtype=float)

    periods = frame.dropna(subset=["start", "end"])
    periods = periods[periods.days > 0]
    if periods.empty:
        return pd.Series(dtype=float)

    direct = periods[periods.days.between(80, 100)]
    series = (
        direct.sort_values("end")
        .drop_duplicates(subset="end", keep="last")
        .set_index("end")["val"]
        .astype(float)
    )

    derived: dict = {}
    for start, group in periods[periods.days > 100].groupby("start"):
        steps = (
            pd.concat([direct[direct.start == start], group])
            .sort_values("days")
            .drop_duplicates(subset="days", keep="last")
        )
        ends = steps.end.to_list()
        values = steps.val.astype(float).to_list()
        for i in range(1, len(ends)):
            derived[ends[i]] = values[i] - values[i - 1]

    if derived:
        fill = pd.Series(derived, dtype=float)
        fill.index = pd.to_datetime(fill.index)
        series = _no_freq(series).combine_first(_no_freq(fill))

    return _no_freq(series.sort_index())


def _stock_series(frame: pd.DataFrame) -> pd.Series:
    """Point-in-time balances: no period length, just an as-of date."""
    if frame.empty:
        return pd.Series(dtype=float)
    balances = frame[frame.start.isna() | (frame.days.fillna(0) <= 1)]
    if balances.empty:
        balances = frame
    return _no_freq(
        balances.sort_values("end")
        .drop_duplicates(subset="end", keep="last")
        .set_index("end")["val"]
        .astype(float)
        .sort_index()
    )


def _merge_concepts(cik: str, tags: list[str], contact: str, extract) -> pd.Series:
    """Combine the alternative tags for one line item into a single series.

    Taking the first tag that has data is not enough: companies migrate between
    tags as the taxonomy changes, and NVIDIA is a live example -- it reported
    revenue under ``RevenueFromContractWithCustomerExcludingAssessedTax`` until
    fiscal 2022 and under ``Revenues`` after. Whichever tag runs to the most
    recent date leads, and the others backfill the history behind it.
    """
    built = []
    for tag in tags:
        series = extract(_concept(cik, tag, contact))
        if len(series):
            built.append(series)
    if not built:
        return pd.Series(dtype=float)

    built.sort(key=lambda s: s.index.max(), reverse=True)
    merged = _no_freq(built[0])
    for other in built[1:]:
        merged = _no_freq(merged.combine_first(_no_freq(other)))
    return merged.sort_index()


def fundamentals(symbol: str, contact: str, quarters: int = 20) -> tuple[pd.DataFrame, str]:
    """Quarterly income/cash-flow and balance-sheet series straight from XBRL."""
    cik, name = ticker_to_cik(symbol, contact)

    columns: dict[str, pd.Series] = {}
    for label, tags in FLOW_CONCEPTS.items():
        series = _merge_concepts(cik, tags, contact, _quarterly_flow)
        if len(series) >= 4:
            columns[label] = series
    for label, tags in STOCK_CONCEPTS.items():
        series = _merge_concepts(cik, tags, contact, _stock_series)
        if len(series) >= 4:
            columns[label] = series

    if not columns:
        raise EdgarError(f"No usable XBRL facts for {symbol.upper()}.")

    out = pd.DataFrame(columns).sort_index().tail(quarters)
    out.index.name = "period_end"

    if "Revenue" in out and "Net income" in out:
        out["Net margin %"] = out["Net income"] / out["Revenue"] * 100
    if "Revenue" in out and "Gross profit" in out:
        out["Gross margin %"] = out["Gross profit"] / out["Revenue"] * 100
    if "Revenue" in out and "Operating income" in out:
        out["Operating margin %"] = out["Operating income"] / out["Revenue"] * 100
    if "Revenue" in out and len(out) > 4:
        out["Revenue YoY %"] = out["Revenue"].pct_change(4) * 100
    return out, name


# ---------------------------------------------------------------------------
# 13F-HR - institutional holdings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThirteenF:
    manager: str
    cik: str
    period: pd.Timestamp
    filed: pd.Timestamp
    holdings: pd.DataFrame  # issuer, cusip, value, shares
    url: str

    @property
    def total_value(self) -> float:
        return float(self.holdings.value.sum())


def _information_table_url(cik: str, accession: str, contact: str) -> str:
    """Find the information-table XML inside a 13F filing directory."""
    acc = accession.replace("-", "")
    listing = _get(f"{SEC_ARCHIVES}/{int(cik)}/{acc}/index.json", contact)
    names = [item.get("name", "") for item in listing.get("directory", {}).get("item", [])]

    candidates = [
        n for n in names
        if n.lower().endswith(".xml") and "primary_doc" not in n.lower() and "index" not in n.lower()
    ]
    # Prefer a filename that says what it is; otherwise try what is left.
    candidates.sort(key=lambda n: ("infotable" not in n.lower().replace("_", "")), reverse=False)
    if not candidates:
        raise EdgarError(f"No information table found in filing {accession}.")
    return f"{SEC_ARCHIVES}/{int(cik)}/{acc}/{candidates[0]}"


def _looks_like_thousands(frame: pd.DataFrame) -> bool:
    """True when a filing's dollar values are plainly still written in thousands.

    value/shares is a price per share, and the median across a whole book of
    listed US equities lands in the tens or hundreds of dollars. A median under
    a dollar is not a portfolio of penny stocks -- it is a filer who never moved
    off the pre-2023 convention. The real cases sit near $0.10 against a normal
    $20-$300, so the threshold has two orders of magnitude of room either way.

    Only share rows count. A convertible note is reported as a principal amount,
    where the same ratio is a price per dollar of face value and sits just about
    at 1.0 -- close enough to the threshold that a note-heavy book could be read
    as mis-scaled and inflated a thousandfold on the strength of it.
    """
    usable = frame[(frame.shares > 0) & (frame.value > 0)]
    if "share_type" in usable.columns:
        shares_only = usable[usable.share_type.astype(str).str.upper() == "SH"]
        # Fall back only when the filer labelled nothing at all.
        if len(shares_only) or usable.share_type.astype(bool).any():
            usable = shares_only
    if len(usable) < 3:  # too few rows for a median to mean anything
        return False
    return float((usable.value / usable.shares).median()) < 1.0


def parse_information_table(xml_bytes: bytes, in_thousands: bool) -> pd.DataFrame:
    """Parse a 13F information table into issuer/cusip/value/shares rows.

    ``in_thousands`` says what the filing date implies. It is a starting point
    rather than the last word: a handful of managers kept reporting thousands
    long after the amended form took effect, and taking the date at its word
    published their positions at a thousandth of their size.
    """
    root = _strip_namespaces(ET.fromstring(xml_bytes))
    rows = []
    for entry in root.findall(".//infoTable"):
        rows.append(
            {
                "issuer": _text(entry, "nameOfIssuer") or "",
                "class": _text(entry, "titleOfClass") or "",
                "cusip": (_text(entry, "cusip") or "").upper(),
                "value": _number(entry, "value"),
                "shares": _number(entry, "shrsOrPrnAmt", "sshPrnamt"),
                "share_type": _text(entry, "shrsOrPrnAmt", "sshPrnamtType") or "",
            }
        )
    if not rows:
        raise EdgarError("Information table parsed to zero rows.")

    frame = pd.DataFrame(rows)
    # A manager can hold the same issuer across several accounts; roll them up.
    rolled = (
        frame.groupby(["cusip", "issuer"], as_index=False)
        .agg(value=("value", "sum"), shares=("shares", "sum"))
        .sort_values("value", ascending=False)
        .reset_index(drop=True)
    )

    # Judged on the unrolled rows, which still carry the share type the filer
    # declared. Rolling up sums value and shares together, so the price the
    # check reads is the same either way.
    if in_thousands or _looks_like_thousands(frame):
        rolled["value"] = rolled["value"] * 1000.0
    return rolled.sort_values("value", ascending=False).reset_index(drop=True)


def load_13f(cik: str, contact: str, count: int = 2) -> list[ThirteenF]:
    """The most recent 13F-HR filings for one manager, newest first."""
    cik = normalise_cik(cik)
    filings = recent_filings(cik, contact, "13F-HR", limit=count * 3)
    if filings.empty:
        raise EdgarError(f"CIK {cik} has filed no 13F-HR. Check it is an institutional manager.")

    out = []
    for _, filing in filings.iterrows():
        if len(out) >= count:
            break
        try:
            url = _information_table_url(cik, filing.accessionNumber, contact)
            in_thousands = filing.filingDate.date() < THOUSANDS_CUTOFF
            holdings = parse_information_table(_get(url, contact, as_json=False), in_thousands)
        except Exception:  # noqa: BLE001 - skip amendments and odd layouts
            continue
        out.append(
            ThirteenF(
                manager=filing.entityName,
                cik=cik,
                period=filing.reportDate,
                filed=filing.filingDate,
                holdings=holdings,
                url=filing_index_url(cik, filing.accessionNumber),
            )
        )

    if not out:
        raise EdgarError(f"Could not parse any 13F information table for CIK {cik}.")
    return out


def thirteen_f_delta(current: ThirteenF, prior: ThirteenF) -> pd.DataFrame:
    """Position changes between two consecutive 13F filings."""
    left = current.holdings.set_index("cusip")
    right = prior.holdings.set_index("cusip")

    merged = left.join(right, how="outer", lsuffix="_now", rsuffix="_prior")
    merged["issuer"] = merged.issuer_now.fillna(merged.issuer_prior)
    for col in ("value_now", "value_prior", "shares_now", "shares_prior"):
        merged[col] = merged[col].fillna(0.0)

    merged["share_change"] = merged.shares_now - merged.shares_prior
    merged["value_change"] = merged.value_now - merged.value_prior
    with np.errstate(divide="ignore", invalid="ignore"):
        merged["share_change_pct"] = np.where(
            merged.shares_prior > 0, merged.share_change / merged.shares_prior * 100, np.nan
        )
    merged["weight_now_pct"] = merged.value_now / max(current.total_value, 1) * 100

    def classify(row) -> str:
        if row.shares_prior == 0 and row.shares_now > 0:
            return "New"
        if row.shares_now == 0 and row.shares_prior > 0:
            return "Exited"
        if row.share_change > 0:
            return "Added"
        if row.share_change < 0:
            return "Trimmed"
        return "Unchanged"

    merged["action"] = merged.apply(classify, axis=1)
    cols = [
        "issuer", "action", "shares_now", "shares_prior", "share_change",
        "share_change_pct", "value_now", "value_prior", "value_change", "weight_now_pct",
    ]
    return (
        merged.reset_index()[["cusip"] + cols]
        .sort_values("value_change", key=lambda s: s.abs(), ascending=False)
        .reset_index(drop=True)
    )


# The roster scanned for per-ticker institutional activity. There is no free API
# that answers "who holds ticker X", so the only way to build that view is to
# pull each manager's filings and look for the name -- which means the answer is
# only ever as broad as this list. Add a CIK here to widen it.
# A CIK identifies a filing entity, not a firm, and firms move between entities.
# Pershing Square restructured in 2026: the old partnership now files a 13F-NT,
# a notice carrying no holdings, and PERSHING SQUARE INC. files the actual
# report. Greenlight's holdings have been filed by DME Capital Management since
# 2024 while the entity named "Greenlight Capital Inc" went quiet. Both showed
# up as a fund that had stopped reporting rather than as a stale pointer, which
# is the failure this roster is most prone to -- see load_13f for the fallback
# that makes it silent.
KNOWN_MANAGERS = {
    "Berkshire Hathaway": "0001067983",
    "Pershing Square": "0002026053",
    "Bridgewater Associates": "0001350694",
    "Renaissance Technologies": "0001037389",
    "Tiger Global": "0001167483",
    "Duquesne Family Office": "0001536411",
    "Appaloosa": "0001656456",
    "Third Point": "0001040273",
    "Greenlight Capital": "0001489933",
    "Baupost Group": "0001061768",
    "Lone Pine Capital": "0001061165",
    "Coatue Management": "0001135730",
    "Viking Global": "0001103804",
    "Soros Fund Management": "0001029160",
    "Elliott Investment Management": "0001791786",
    "Starboard Value": "0001517137",
    "ValueAct Holdings": "0001418814",
    "Engaged Capital": "0001559771",
    "Sachem Head Capital": "0001582090",
    "Corvex Management": "0001535472",
}

# Funds that buy a stake in order to change the company, rather than to express
# a view on it. The distinction is worth a label because it changes what a row
# means: a quant shop appearing in this table holds the name because its model
# said so, and may be out of it next quarter, while an activist opening a
# position has announced an intention to act on the business. Their filings are
# also the ones most often followed by a 13D, a board fight or a sale.
ACTIVIST_MANAGERS = frozenset({
    "Pershing Square",
    "Third Point",
    "Elliott Investment Management",
    "Starboard Value",
    "ValueAct Holdings",
    "Engaged Capital",
    "Sachem Head Capital",
    "Corvex Management",
})


def manager_tag(manager: str) -> str:
    """The label shown beside a manager's name, empty when there is nothing to say."""
    return "activist" if manager in ACTIVIST_MANAGERS else ""

# Corporate boilerplate that varies between filings for the same company:
# "NVIDIA CORP" in one information table and "NVIDIA CORPORATION" in the next.
_ISSUER_NOISE = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES",
    "LTD", "LIMITED", "PLC", "LP", "LLC", "NV", "SA", "AG", "SE",
    "HOLDING", "HOLDINGS", "HLDG", "HLDGS", "GROUP", "GRP",
    "COM", "CL", "CLASS", "A", "B", "C", "NEW", "ADR", "ADS", "SHS", "THE", "DEL",
    "COS",
}


def normalise_issuer(name: str) -> str:
    """Reduce an issuer name to a comparable core.

    13F filers write the same company a dozen ways, so matching raw strings
    misses most of them. Uppercasing, dropping punctuation and stripping trailing
    corporate boilerplate turns "NVIDIA CORPORATION", "NVIDIA CORP" and
    "NVIDIA CORP DEL" into the same key.
    """
    words = re.sub(r"[^A-Z0-9 ]+", " ", str(name).upper()).split()
    while words and words[-1] in _ISSUER_NOISE:
        words.pop()
    return " ".join(words)


# 13F information tables abbreviate; EDGAR's registrant index does not. The same
# company arrives as "FORD MTR CO" from one and "FORD MOTOR CO" from the other,
# and normalise_issuer -- which only strips trailing boilerplate -- reads those
# as two different companies. That silently answered "nobody holds Ford" while a
# scanned manager held eleven million shares of it, so matching gets a rougher
# key than display normalisation does.
#
# Three things separate the two spellings, and the key undoes all three:
# abbreviations (MTR/MOTOR, SYS/SYSTEMS, INTL/INTERNATIONAL) collapse to one
# canonical word; filing-office qualifiers ("/DE/", "/MN") drop, as do joining
# words; and the remaining words are sorted, because 13F writes "DISNEY WALT CO"
# where EDGAR writes "WALT DISNEY CO". The result is squashed to a single token
# so that "EXXON MOBIL" and "EXXONMOBIL" land in the same place too.
_CANONICAL = {}
for _canon, _variants in {
    "MOTOR": ("MTR", "MTRS", "MOTORS"),
    "SYSTEM": ("SYS", "SYSTEMS"),
    "INSTRUMENT": ("INSTR", "INSTRS", "INSTRUMENTS"),
    "INTERNATIONAL": ("INTL", "INTNL", "INTERNATL"),
    "MACHINE": ("MACH", "MACHS", "MACHINES"),
    "ELECTRIC": ("ELEC", "ELECTRICAL"),
    "ELECTRONIC": ("ELECTRS", "ELECTRONICS"),
    "GENERAL": ("GEN",),
    "INDUSTRIAL": ("INDL", "INDS", "INDUSTRIES", "INDUSTRY"),
    "REALTY": ("RLTY",),
    "TECHNOLOGY": ("TECH", "TECHS", "TECHNOLOGIES"),
    "PHARMACEUTICAL": ("PHARM", "PHARMA", "PHARMACEUTICALS"),
    "COMMUNICATION": ("COMM", "COMMS", "COMMUNICATIONS"),
    "FINANCIAL": ("FINL",),
    "SERVICE": ("SVC", "SVCS", "SERVICES"),
    "RESOURCE": ("RESOURCES",),
    "ENTERPRISE": ("ENTPR", "ENTERPRISES"),
    "NATIONAL": ("NATL",),
    "AMERICA": ("AMER", "AMERICAN"),
    "BANK": ("BK", "BANKS"),
    "PROPERTY": ("PPTY", "PPTYS", "PROPERTIES"),
    "MANAGEMENT": ("MGMT",),
    "HEALTH": ("HLTH",),
    "LABORATORY": ("LAB", "LABS", "LABORATORIES"),
    "STORE": ("STRS", "STORES"),
    "PETROLEUM": ("PETE", "PETROL"),
    "ENERGY": ("ENRGY",),
    "TRANSPORT": ("TRANSPORTATION",),
    "CAPITAL": ("CAP",),
    "INVESTMENT": ("INVT", "INVESTMENTS"),
    "NETWORK": ("NTWK", "NETWORKS"),
    "SEMICONDUCTOR": ("SEMICON", "SEMICONDUCTORS"),
    "ENTERTAINMENT": ("ENTMT",),
    "SOLUTION": ("SOLUTIONS",),
    "BRAND": ("BRANDS",),
    "RESTAURANT": ("REST", "RESTAURANTS"),
    # Fund names carry it constantly and 13F almost always shortens it, which is
    # what made "SPDR GOLD TR" and EDGAR's "SPDR GOLD TRUST" read as two things.
    "TRUST": ("TR", "TRUSTS"),
}.items():
    _CANONICAL[_canon] = _canon
    for _v in _variants:
        _CANONICAL[_v] = _canon

# Two-letter qualifiers EDGAR appends to say which state a registrant filed in.
_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

_JOINERS = {"OF", "AND", "THE", "FOR"}


def issuer_words(name: str) -> set[str]:
    """The distinguishing words of an issuer name, canonicalised."""
    words = re.sub(r"[^A-Z0-9 ]+", " ", str(name).upper()).split()
    words = [_CANONICAL.get(w, w) for w in words]
    return {
        w for w in words
        if w not in _ISSUER_NOISE and w not in _JOINERS and w not in _STATE_CODES
    }


def issuer_key(name: str) -> str:
    """A key two spellings of the same issuer both land on.

    Deliberately more destructive than :func:`normalise_issuer`, which exists to
    produce something readable. Nothing here is displayed; it only decides
    whether two names are the same company.
    """
    return "".join(sorted(issuer_words(name)))


_HOLDING_COLUMNS = ["cusip", "issuer", "value", "shares"]


def _match_holdings(holdings: pd.DataFrame, keys: set[str], cusips: set[str]) -> pd.DataFrame:
    # An empty frame carries no columns, so hand back one that does: callers sum
    # and read .cusip off the result unconditionally.
    if holdings.empty or not {"issuer", "cusip"}.issubset(holdings.columns):
        return pd.DataFrame(columns=_HOLDING_COLUMNS)
    by_name = holdings.issuer.map(issuer_key).isin(keys)
    by_cusip = holdings.cusip.isin(cusips) if cusips else False
    return holdings[by_name | by_cusip]


def corroborated_cusips(
    filings_by_manager: dict[str, list[ThirteenF]],
    seed_cusips: set[str],
    issuer_names: list[str],
) -> set[str]:
    """Seed CUSIPs that the filings themselves agree are this issuer.

    A CUSIP looked up from the ticker is the one thing that can bridge a rename
    or a fund's house style, where no spelling rule ever will. But the lookup is
    a name search under the hood and can hand back the wrong company outright,
    and quietly crediting one company's position to another is a worse failure
    than missing a holder. So a seed only counts if a filer reporting that CUSIP
    also wrote a name sharing a distinguishing word with the issuer's -- enough
    to rule out a wrong-company hit, and far looser than requiring the whole
    name to match, which is the thing that failed in the first place.
    """
    wanted = set()
    for n in issuer_names:
        wanted |= issuer_words(n)
    if not seed_cusips or not wanted:
        return set()

    good = set()
    for filings in filings_by_manager.values():
        for filing in filings:
            h = filing.holdings
            if h.empty or not {"issuer", "cusip"}.issubset(h.columns):
                continue
            for _, row in h[h.cusip.isin(seed_cusips)].iterrows():
                if issuer_words(row.issuer) & wanted:
                    good.add(row.cusip)
    return good


def scan_managers(
    filings_by_manager: dict[str, list[ThirteenF]],
    issuer_names: list[str],
    seed_cusips: set[str] | None = None,
) -> tuple[pd.DataFrame, set[str]]:
    """One row per manager: what they did in this issuer last quarter.

    Runs in two passes. The first matches on the normalised issuer name and
    collects whatever CUSIPs that turns up; the second re-matches including
    those CUSIPs, which catches filers whose spelling normalises differently but
    who are plainly holding the same security.

    ``seed_cusips`` adds CUSIPs known from outside the filings, for the cases no
    spelling rule reaches: a renamed company, or a fund every filer names after
    its sponsor. They are filtered through :func:`corroborated_cusips` first, so
    a bad lookup cannot invent a holding.

    Share classes are aggregated: the ticker-to-CUSIP lookup is per share class
    but the name match is not, so GOOGL and GOOG still roll into one row.
    """
    keys = {issuer_key(n) for n in issuer_names if issuer_key(n)}

    cusips: set[str] = set()
    for filings in filings_by_manager.values():
        for filing in filings:
            cusips |= set(_match_holdings(filing.holdings, keys, set()).cusip)
    if seed_cusips:
        cusips |= corroborated_cusips(filings_by_manager, set(seed_cusips), issuer_names)

    rows = []
    for manager, filings in filings_by_manager.items():
        if not filings:
            continue
        current = filings[0]
        prior = filings[1] if len(filings) > 1 else None

        now_rows = _match_holdings(current.holdings, keys, cusips)
        prior_rows = _match_holdings(prior.holdings, keys, cusips) if prior is not None else None

        shares_now = float(now_rows.shares.sum()) if len(now_rows) else 0.0
        value_now = float(now_rows.value.sum()) if len(now_rows) else 0.0
        shares_prior = float(prior_rows.shares.sum()) if prior_rows is not None and len(prior_rows) else 0.0
        value_prior = float(prior_rows.value.sum()) if prior_rows is not None and len(prior_rows) else 0.0

        if shares_now == 0 and shares_prior == 0:
            continue  # never held it; not worth a row

        change = shares_now - shares_prior
        if shares_prior == 0:
            action = "New"
        elif shares_now == 0:
            action = "Exited"
        elif change > 0:
            action = "Added"
        elif change < 0:
            action = "Trimmed"
        else:
            action = "Unchanged"

        rows.append(
            {
                "manager": manager,
                "tag": manager_tag(manager),
                "filer": current.manager,
                "action": action,
                "shares_now": shares_now,
                "shares_prior": shares_prior,
                "share_change": change,
                "share_change_pct": change / shares_prior * 100 if shares_prior > 0 else np.nan,
                "value_now": value_now,
                "value_change": value_now - value_prior,
                "weight_now_pct": value_now / current.total_value * 100 if current.total_value else np.nan,
                "period": current.period,
                "prior_period": prior.period if prior is not None else pd.NaT,
                "filed": current.filed,
                "url": current.url,
            }
        )

    if not rows:
        return pd.DataFrame(), cusips

    order = {"New": 0, "Added": 1, "Unchanged": 2, "Trimmed": 3, "Exited": 4}
    frame = pd.DataFrame(rows)
    return (
        frame.assign(_rank=frame.action.map(order))
        .sort_values(["_rank", "value_now"], ascending=[True, False])
        .drop(columns="_rank")
        .reset_index(drop=True)
    ), cusips
