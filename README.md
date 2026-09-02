# Market Analytics Dashboard

A Streamlit dashboard: type a ticker and get the option market's own view of it —
the implied distribution, dealer gamma, the vol surface, the expected move and
the volatility premium — plus SEC filing data and a cointegration screen.

New to options? Read **[README beginner.md](README%20beginner.md)** instead: it
walks the same page top to bottom assuming no prior knowledge, and explains how
to read each panel and how each one is commonly misread. This file is the
technical companion.

No AI, no paid data, no API keys. Yahoo Finance for prices and chains, SEC EDGAR
for filings, `numpy`/`scipy`/`statsmodels` for the maths, `plotly` for the charts.

Everything is delayed: Yahoo quotes by roughly 15 minutes, option open interest
by a session, 13F holdings by up to 45 days.

Once US markets shut the quotes stop moving, but they do not vanish at once. For
some hours after the close Yahoo keeps serving the closing bids and offers, and
the page shows them as `closing quotes of the <date> session`: the last book of
the day, still two-sided, and as precise as a live one. Overnight Yahoo then
starts withholding them -- it returns a bid and ask of exactly zero on every
strike of every chain -- and the only price left is each contract's last trade,
which for the strikes that traded that day *is* the closing quote. The page falls
back to those, using the date on each print to keep the session's closing trades
and discard the ones left over from months ago, and shows `closing prints from
<date>`. During the session itself the line reads `live quotes, delayed ~15 min`.
Which of the three it is appears under the title, and the session test behind
it, the page timestamps and every days-to-expiry figure are all on the exchange's
clock rather than the server's or the reader's -- run from Singapore, a local
clock reading half past four in the afternoon is half past four in the *morning*
in New York, and the local date is already tomorrow for the whole of New York's
afternoon, which read naively would shorten every maturity by a day. **Refresh data** in the sidebar drops
the market caches (quotes, chains, price history, Form 4) and re-pulls; XBRL
fundamentals and 13F filings keep their six-hour cache, since they change
quarterly and the 13F roster scan is the slowest thing here.

Downloads run concurrently. The option chains, the price history and the SEC
panels' data are independent requests that used to run one after another, which
on a name with eight expirations was most of the page's load time spent waiting
on a reply before asking for the next. They now go out together on a small pool
(`app.FETCH_WORKERS`), which took a cold load from about 66 seconds to about 32,
and a second ticker inside the cache window to about 10. The SEC side is
unaffected by the pool size: `edgar._throttle` holds a process-wide lock so the
request *rate* stays under the SEC's published limit no matter how many workers
are asking.

## Run

```bash
pip install -r requirements.txt
```

```bash
python -m streamlit run app.py
```

Then open http://localhost:8501.

EDGAR requires a contact in the User-Agent; the owner's address is baked into
`edgar.OWNER_CONTACT`. Set `SEC_CONTACT` to override it if someone else runs this.

**The sidebar holds only what genuinely varies:** ticker, which expirations feed
the surface, which expiry the density is built from, and the risk-free rate, then
the three action buttons beneath them. Refresh reruns the script rather than only
clearing the caches, so the top of the page cannot end up older than the bottom;
it sits *below* the expiry pickers precisely because Streamlit discards keyed
widget state for widgets a run never reaches, and a rerun fired from above them
would reset the chosen expirations.
Everything else has a right answer and is a constant at the top of `app.py` —
smile smoothing (1.0), gamma profile width (±20%), price history (3y), insider
lookback (12 months).

Expirations are labelled with days out and tagged `(OPEX)` on the monthly cycle,
`(OPEX quarterly)` on the four quad-witching months.

### What the pickers choose on their own

`expiries.py` seeds both pickers whenever the ticker changes. The selection is a
**maturity ladder**, not a cluster: everything through the first monthly, then
the second monthly, then the listed expiries nearest 90 and 180 days, capped at
eight because each one is its own chain download.

The shape is chosen by what the panels downstream need, which is not the same
thing in each case. A term structure needs a long arm or it has a slope and no
curvature — four dates inside a fortnight tell you nothing about the curve.
Gamma exposure wants the dates where open interest actually sits, which is the
monthlies. The calendar arbitrage check needs neighbours to compare against.
Three refinements follow from that:

- **A monthly near a ladder target beats a weekly on it.** A thin off-cycle
  chain at 90 days is a worse reading of the long arm than a monthly at 80, so a
  monthly within three weeks of the target wins it.
- **An expiry landing today is dropped.** Hours from settlement its at-the-money
  vol is mechanics rather than volatility, and carrying it in bends the term
  structure and fills the calendar check with noise.
- **The budget is spread, not sliced.** Taking the first eight dates on a name
  with daily expirations spends the whole allowance inside one week.

**Earnings moves the primary.** When an announcement falls inside the front of
the curve, the primary becomes the first expiry that captures it — a density
that excludes the jump is describing a different stock — unless that expiry
settles within a day of the event, where it is priced off intrinsic value and
says nothing about the jump. The last expiry *before* the announcement is pinned
into the selection alongside it, because that pair is what the decomposition in
4b needs and losing either to the budget would empty the panel silently.

The pickers are re-seeded on a ticker change rather than only reconciled against
the new option list. Standard listing conventions mean two unrelated names share
most of their expiration dates, so a selection carried over from the last ticker
survives a prune intact and the new defaults never apply — which is wrong,
because the defaults are specific to the name: where its monthlies fall, and
whether its earnings sit inside the front of the curve.

## Deploy

The point of the repository is an always-on copy on Streamlit Community Cloud,
and its root is exactly the set of files that needs: the modules,
`requirements.txt`, `packages.txt`, `.streamlit/config.toml` and the two READMEs.
Tests and local tooling live in `byproduct/`, which `.gitignore` keeps out of the
upload (delete that line to commit them too).

1. Push the folder to a GitHub repository.
2. On [share.streamlit.io](https://share.streamlit.io) choose **New app**, point
   it at the repository and branch, and set the main file to `app.py`.
3. Under **Advanced settings** pick Python 3.13, which is what this was built
   and tested on. Cloud may hand out a newer interpreter instead; every pin has
   a Linux wheel for 3.13 and 3.14 both, so the build works either way. Nothing
   needs a secret: the SEC contact is baked into `edgar.OWNER_CONTACT`, and a
   `SEC_CONTACT` entry in the app's secrets would override it only if that ever
   has to change.

   A pin without a wheel for the interpreter Cloud chose is the one way this
   build fails slowly rather than loudly: pip falls back to compiling from
   source and the app sits on the "in the oven" screen for as long as you let
   it. If that happens, read the log from **Manage app** and look for a
   `Downloading <package>.tar.gz` line — a source archive rather than a
   `.whl` is the package to move.

`requirements.txt` pins exact versions rather than lower bounds, so a rebuild
months from now installs the same libraries this was tested against instead of
whatever is newest on the day it builds. That freezes only this side of the
wire. Yahoo and the SEC can still change under a frozen client, and when Yahoo
changes its option endpoint the fix is a newer `yfinance` rather than an older
one, so treat that line as the one most likely to need bumping. The quarterly
check compares the pins against the versions it last verified and complains if
they have drifted apart.

`packages.txt` installs Chromium, which kaleido drives to render the PDF's
figures; the markdown export does not need it. The container's clock is UTC and
every date in the app is computed on New York's, so the timezone needs no
configuring. Cloud has no persistent filesystem, so the caches are lost on every
restart and the first load after one is the slow one (about half a minute); an
app nobody has opened for a while is put to sleep and takes a little time to
wake. Everything here was tested locally on Windows; the code has no
OS-specific paths, and the Linux container is the only place the Chromium
dependency has to be met by `packages.txt` rather than by a browser already on
the machine.

## The sections

### 1. Expected move

The at-the-money straddle is worth the discounted expected *absolute* move,
`straddle = df · E|S_T − F|`. One standard deviation is `F · σ_ATM · √T`, which
for a lognormal is about 25% larger — `E|X| = √(2/π)·σ = 0.798σ`. Calling the
straddle "the expected move" understates the 68% band by that factor, so both
numbers are reported separately.

The cone chart shows the implied ±1σ and ±2σ bands out across every selected
expiry, centred on the forward rather than the spot. Alongside it: what the stock
*actually* did over the same horizon historically, so you can see whether the
chain is charging more or less than the recent past.

### 2. Implied probability distribution

Breeden–Litzenberger: the second derivative of the call price with respect to
strike *is* the risk-neutral density.

```
pdf(K) = e^{rT} · ∂²C/∂K²
```

Differentiating quoted prices directly does not work — strikes are coarse, quotes
are pinned to a penny, and the second difference of that is noise that goes
negative everywhere. The fix, and the only real subtlety in the project, is to
**smooth in implied-vol space instead of price space**:

1. Convert every usable out-of-the-money quote to an implied vol.
2. Fit a smoothing spline to vol against log-moneyness, weighting each quote by
   how precisely its bid-ask spread pins its vol (`spread/2 ÷ vega`).
3. Extend beyond the quoted strikes by carrying total variance on linearly in
   log-moneyness, matching the fitted slope at the boundary and letting it decay
   over half a quoted width.
4. Re-price a dense, **uniformly spaced strike grid** off the smoothed smile,
   widening the grid until the density has actually died out at its edges.
5. Take the second difference of that, which is smooth by construction.

The chart overlays the plain Black-Scholes lognormal at the same ATM vol. The gap
between the two is exactly what the smile is pricing: a fatter left tail because
puts are bid, and a higher peak because the middle is correspondingly cheaper.

The sharpest check that the whole pipeline is consistent is the martingale
property — the mean of the extracted density must equal the forward.

Two details in step 3 exist only because that check failed, and finding out why
was the single most productive thing in the project:

- **The wing has to meet the fit smoothly.** Holding vol flat past the last
  quoted strike is the obvious choice and it is subtly wrong: the join is
  continuous in value but not in slope, and a kink in `σ(k)` becomes a
  delta-like spike in `∂²C/∂K²`, which *is* the density. On live chains that one
  kink threw 3–7% of the total mass out as negative density at exactly two grid
  points, both within a few cents of the quoted boundaries. Clipping it and
  renormalising then dragged the mean off the forward by up to 0.65%.
- **The grid has to be wide enough that the tails have died.** A fixed multiple
  of the quoted span is not enough, because how far the tail reaches depends on
  the smile, not on how many strikes happen to be listed. Whatever mass falls
  outside the grid gets redistributed by the renormalisation. A steep smile
  truncated at the old fixed 0.6 span lost 0.8% of its probability and broke the
  martingale by 0.4%. The grid now widens until under `1e-5` of probability is
  left outside, and `Density.tail_mass_missing` reports what remains.

How far the boundary slope is carried before it flattens is set by the
martingale property rather than by taste. On synthetic chains built from a known
smile, the mean lands on the forward only once the damping length reaches about
half a quoted width; cutting it shorter truncates real risk-neutral mass and put
the mean 0.53% below the forward on a steep smile. Continuing it forever is the
opposite error — it manufactures enormous tails out of the steepest thing the
fit ever saw, and put GME's excess kurtosis at 184.

With both fixed, ten live chains from SPY to GME land on the forward to within
0.05%, and the negative mass clipped is essentially zero.

#### Probability of reaching a level

The density says where the price *settles*. A separate panel answers whether it
gets there at all, which for anything path-dependent — a stop, a level, a
decision to act — is usually the question being asked.

First passage of a driftless process, which the forward measure supplies for
free since the forward is a martingale. In log space that leaves a drift of
`−σ²/2`, so the reflection principle carries a correction:

```
P(hit a) = N((−a + μT)/(σ√T)) + e^(2μa/σ²) · N((−a − μT)/(σ√T))
```

with `a = ln(level/F)` and `μ = −σ²/2`. The cruder habit of doubling the
terminal probability drops that correction; measured against this formula it is
out by up to 7 points of probability for a 50% vol name three months out, so it
is not used. `σ` is read off the fitted smile *at the level being tested*, so a
downside barrier is priced with the higher put vol the market actually charges
there — the one place the skew enters. The result is floored at the terminal
probability, which is a logical constraint rather than a numerical guard: the
price cannot finish beyond a level without having touched it. The test suite
checks it against a Monte Carlo of the same process.

**Cross-check: the same moments, replicated from the quotes.** The martingale
property is necessary but not sufficient — a density mangled by a bad strike
still integrates to one and still has its mean at the forward. So the skew and
kurtosis are computed a second time by a route that shares none of the pipeline
above it.

Carr–Madan says any smooth payoff can be replicated statically out of options,
so with `H(S) = Sⁿ` the raw moments of the settlement price are a weighted sum of
quoted mids — no spline, no differentiation, no density:

```
E[S]   = F                            (the replicating weight is identically zero)
E[S²]  = F² + ∫  2      · p(K) dK
E[S³]  = F³ + ∫  6K     · p(K) dK
E[S⁴]  = F⁴ + ∫ 12K²    · p(K) dK
```

where `p(K)` is the undiscounted out-of-the-money price. This is Bakshi, Kapadia
and Madan (2003), in the forward measure to match the rest of the codebase.

The two estimates are not expected to match exactly, and the reason is the
useful part. The quote integrals stop at the last listed strike — after the delta
filter, about ±2σ — so they are blind to the tail beyond it and systematically
understate kurtosis. The density keeps going on flat-vol wings, which are
extrapolation rather than data. **The gap between the two therefore measures how
much of the reported skew and kurtosis rests on strikes nobody quoted.** **The verdict is judged on the width and the mean, not on skew and kurtosis**,
and the reason is worth stating because it looks like an omission. Excess
kurtosis is a fourth moment, so it lives almost entirely in the tails — and the
quote-side integral stops around two standard deviations out. Measured across ten
live chains its kurtosis came back between 0.2 and 1.7 whatever the underlying
distribution actually looked like, while the density's ranged from 1.2 to 14. The
"difference" between them was therefore just the density's own number restated,
which is not a check on anything. The width is dominated by the middle of the
distribution, where both estimators can see.

The mean is the sharpest line in the table. From the quotes it is the forward
*exactly* — the replicating weight for the first moment is identically zero, so
no integration error can reach it. Across ten live chains the density matches it
to within 0.05% and the width agrees to 1.2–7.7%. Damaging a single strike in
the test suite — a stale quote the quality filters cannot reject — moves the
width by 46–406% and knocks the mean 1.4–20% off the forward. The thresholds sit
in the wide gap between those two regimes.

The one genuinely new number is **model-free implied vol**, `√(−2·E[ln(S/F)]/T)`
— the fair strike of a variance swap, from the same replication with
`H(S) = ln(S/F)`. It is what the VIX approximates, so it is the right number to
compare against the VIX line in section 5, and it sits above ATM vol whenever the
smile has curvature because it prices every strike rather than just one.

The ATM vol it is quoted against there is the *fitted* one -- the smoothing
spline read at the forward -- and not the headline figure at the top of the
page, which interpolates the raw quotes. The two differ by whatever the
smoothing removed, which is fractions of a point on a liquid chain and several
points on something like GME. The page says which is which; the gap is the
smoothing, not a disagreement about the market.

### 3. Gamma exposure

Dollar gamma per 1% move in spot, signed positive for calls and negative for puts:

```
GEX(K) = Γ(K) · OI(K) · 100 · S² · 0.01
```

The profile is recomputed across candidate spot prices with each strike's implied
vol held fixed (sticky-strike), and the **flip level** is where net gamma crosses
zero — the boundary between dealer hedging that damps moves and hedging that
amplifies them.

**The sign is an assumption, not a measurement.** Open interest is unsigned, so
this uses the standard convention that dealers are long call gamma and short put
gamma. Where customers overwrite calls or buy puts for protection, the true sign
is the opposite and every conclusion inverts.

Yahoo blanks the open-interest column for stretches at a time, especially outside
US market hours. When that happens the panel says so and offers volume as a
fallback weighting, clearly labelled — volume answers a different question (where
gamma was *traded today*, not where the position sits).

### 4. Volatility surface and skew

A 3D surface over (days to expiry, log-moneyness) built from OTM quotes, plus the
two numbers an FX desk would quote, in vol points:

- **25-delta risk reversal** = `IV(25Δ call) − IV(25Δ put)`. Negative means puts
  are bid over calls, the normal state for equities. The question is whether it is
  *more* negative than usual, not whether it is negative.
- **25-delta butterfly** = mean of the wings minus ATM. The smile's curvature.

Wings the chain does not actually quote out to are left blank rather than
extrapolated. The term-structure chart also derives forward vol between
consecutive expiries.

#### What the chain charges for earnings

An earnings announcement is the one scheduled event large enough to dominate
everything else on the page, and an expiry that spans one is not the same kind of
object as an expiry that does not — the first prices a diffusion *plus* a jump.
Total implied variance is additive in time, so with the last expiry before the
announcement as the diffusive anchor:

```
σ²_post · T_post  =  σ²_pre · T_post  +  J²
```

`J` is the one-session move the chain is charging for, as a fraction of spot,
reported alongside the share of the spanning expiry's total variance it accounts
for — a large jump on a small share is a different statement from a large jump on
a large one.

Three things keep this honest:

- **The move date, not the announcement date.** A company reporting after
  Tuesday's close moves the stock on Wednesday, so a Tuesday expiry does not
  capture it. Announcements are mapped to the session their move lands in, and
  the cut sits well before the close because Yahoo's times are approximate.
- **A noise floor.** The jump is a difference of two measured variances, so it
  inherits the uncertainty of both, propagated from the bid-ask widths near the
  money on each expiry. Without it, two chains built from the *same* volatility
  recover at-the-money vols a hundredth of a point apart — enough to manufacture
  a spurious 0.05% earnings move and announce it as a finding.
- **A warning when the anchor is short-dated**, where quoted vol is distorted by
  weekend decay and by how few strikes sit near the money.

The event is also marked on the cone and the term structure — the kink in a term
structure is almost always this date — and the volatility-premium panel says so
when the primary expiry spans it, because implied vol containing an event premium
compared against trailing realised vol containing no such event is not a
like-for-like comparison.

#### Against the index

A 25-delta risk reversal of −3 vol points means nothing on its own. Equity skew is
always negative, so the only useful question is whether it is steep *for what it
is*, and answering that needs a benchmark.

The obvious benchmark is the ticker's own history, and it is unavailable: an
implied-skew history has to be accumulated day by day, and Streamlit Cloud gives a
container no persistent filesystem to accumulate it in. So the comparison is
cross-sectional instead — the same three numbers for SPY, on the same afternoon,
at the nearest matching maturity. Both readings move with market-wide risk
appetite, so comparing them nets most of that out and leaves what is specific to
the name.

Two things it is not. The vol ratio is not a beta — implied vol carries
idiosyncratic risk the index has diversified away, so it exceeds 1 for
essentially every single name. And SPY is the market, not the sector; for a name
whose sector is having its own day, a peer would be the better comparison.

#### Static arbitrage checks

Three conditions a set of option prices has to satisfy for *no model at all*,
tested against the quoted mids rather than the fitted smile — testing the fit
would only confirm the spline is smooth, which is not in doubt:

| Check | Condition | What violating it means |
| --- | --- | --- |
| Vertical | `−1 ≤ dC/dK ≤ 0` | a call spread with negative cost, or one costing more than its maximum payoff |
| Butterfly | `C` convex in `K` | a butterfly with negative cost — equivalently, negative probability density |
| Calendar | `σ(k,T₁)²T₁ ≤ σ(k,T₂)²T₂` | a calendar spread with negative cost |

Only out-of-the-money quotes are used, so the put wing is converted to call
values through put-call parity using the chain's own regressed forward, which
makes the whole strike range one convex curve to test.

**The tolerance is the bid-ask spread, not a fixed epsilon.** A mid is not a
price, it is the centre of an interval the true value lies somewhere inside, so
a violation only counts when it cannot be reconciled at *any* price inside the
spreads of the strikes involved. Without that, every penny-wide chain in the
market lights up.

This exists because the quote-quality filters in `prep.py` have a blind spot
they cannot close. They reject a price that contradicts its own quote — a print
above the live ask, a wing that inflated its own vol. They cannot reject a stale
quote whose bid, ask and last all moved together and which is merely out of line
with the strikes either side of it. That quote passes every filter and still
drags the density around: in the test suite, one such strike moves excess
kurtosis from −0.06 to +3.99. The convexity check names the strike.

The calendar test runs across the whole overlapping moneyness range of each
consecutive pair, not only at the forward, because a stale wing on a far expiry
is invisible at the money. The `calendar_arb` column on the term structure is
the at-the-money special case of the same test.

Two things this is *not*. It is not an opportunity finder: on quotes delayed
fifteen minutes, anything showing as arbitrage was gone long before it reached
the page. And a clean result is not a guarantee the chain is right, only that it
is internally consistent.

### 5. Volatility risk premium

What sellers of volatility get paid: the vol charged minus the vol that showed up.

```
VRP_t = IV_t − RV_{t → t+h}
```

Note the alignment. The realised leg looks **forward** from `t`, which is the only
comparison that answers "was the option fairly priced". Comparing today's implied
to *trailing* realised — the version usually plotted — measures something weaker.
The consequence is that the last `h` trading days of any VRP series are unknowable,
and they are shown as blank rather than quietly truncated.

There is no free source of historical implied vol per ticker, so the premium's
*history* is computed market-wide from VIX against subsequent S&P realised. The
ticker panel reports today's implied against trailing realised, labelled as the
weaker comparison.

Realised vol is reported by four estimators. **Yang-Zhang** leads because it is the
efficient one: it combines the overnight gap, the open-to-close move and the
Rogers-Satchell range term, so it stays unbiased where Parkinson and Garman-Klass
miss opening jumps entirely.

### 6. Insider trades

SEC Form 4, parsed from the raw XML. Only codes **P** (open-market purchase) and
**S** (open-market sale) are discretionary trades and feed the totals. **A** is a
grant, **M** an option exercise, **F** shares withheld to pay tax on vesting —
counting those as buying or selling is the usual way this data gets misread.
Cluster buying (three or more *different* insiders inside 30 days) is flagged.

### 7. Institutional activity

13F, **filtered to the ticker on screen**: which managers added, trimmed, opened
or exited a position in it last quarter, rather than one manager's whole book.

No free API answers *who holds ticker X*, so the only way to build this is to pull
each manager's last two filings and look for the issuer — which means the answer is
only ever as broad as `edgar.KNOWN_MANAGERS`, currently 20 funds. Add a CIK there
to widen it; each one costs about a second. Filings are cached for six hours and
keyed per manager, so the roster scan happens once and switching tickers after
that is instant.

Managers who buy in order to *change* the company are tagged `(activist)`, on the
name itself so the label travels into the chart and both exports. It is a
different claim from the rest of the table: a quant shop holds a name because its
model selected it, while an activist opening a position has announced an
intention to act on the business.

Each row also carries the quarter that manager filed for. Usually they agree, but
a firm that has moved filing entity or simply not filed yet sits further back,
and the header names the newest quarter on the page rather than theirs. Where any
row lags, a line under the table says which.

A CIK identifies a filing *entity*, not a firm, and firms move between entities —
the most common way this panel goes quietly wrong. Pershing Square restructured in
2026 and its old partnership now files a 13F-NT, a notice carrying no holdings;
Greenlight's book has been filed by DME Capital Management since 2024. Both read
as funds that had stopped reporting rather than as stale pointers.

Issuer matching is by a deliberately rough key. 13F information tables abbreviate
where EDGAR's registrant index spells out, so `issuer_key` expands abbreviations
to one canonical spelling (`FORD MTR` → `FORD MOTOR`, `CISCO SYS` → `CISCO
SYSTEM`), drops filing-office qualifiers (`/DE/`, `/MN`) and joining words, sorts
the remainder (13F writes `DISNEY WALT CO` where EDGAR writes `WALT DISNEY CO`)
and squashes the result so `EXXON MOBIL` and `EXXONMOBIL` agree. A second pass
matches whatever CUSIPs the first turned up. Before this, a quarter of large-cap
tickers silently reported *no holders at all* — Ford read as unheld while a
scanned manager held 11.35m shares of `FORD MTR CO`.

No spelling rule reaches a **rename** or a **house style**, though. EDGAR calls
GE `GENERAL ELECTRIC CO` where filers write `GE AEROSPACE`; every filer names
SPY after its sponsor, `STATE STR SPDR S&P 500 ETF T`, against EDGAR's `SPDR S&P
500 ETF TRUST`. Both read as *nobody holds it* — the panel's worst failure, since
it states a falsehood rather than admitting a gap. So a third pass seeds the
CUSIP set from the ticker itself, taken from the security's ISIN, which for a US
listing is just the country code wrapped around the CUSIP.

That lookup is a name search underneath and does sometimes answer with the wrong
company outright — it returns a *Canadian* ISIN for GOOGL — and crediting one
company's position to another would be worse than missing it. So a seed only
counts once a filer reporting that CUSIP also wrote a name sharing a word with
the issuer's: `GE AEROSPACE` shares `GE`, the SPDR line shares `SPDR`, and a
wrong-company hit shares nothing and is dropped. Non-US ISINs are refused
outright, which is why Accenture — Irish-incorporated, filed as `ACCENTURE PLC
IRELAND` — is still missed. Share classes aggregate as before: the seed is one
CUSIP but the name match is not, so GOOGL and GOOG still roll together.

13F covers long US equity and listed options only — no shorts, no bonds, no
foreign listings, no cash — and it arrives up to 45 days stale.

Values were reported in thousands until the amended form took effect on 23
January 2023, but the filing date is not proof: **Baupost and Duquesne still
file in thousands today**, and taking the date at its word published their books
at $5.4m and $5.2m rather than $5.4bn and $5.2bn. So each filing's values are
checked against the share counts beside them — `value / shares` is a price, and
a book whose median is under a dollar is a filer who never switched, not a
portfolio of penny stocks.

### 8. Fundamentals

XBRL company facts: the numbers straight out of the filings. Two corrections
matter and most naive parsers miss both — cash-flow lines are filed year-to-date
and must be un-cumulated into discrete quarters (otherwise Q4 looks four times too
big), and companies migrate between tags as the taxonomy changes (NVIDIA moved
revenue from `RevenueFromContractWithCustomerExcludingAssessedTax` to `Revenues`
in fiscal 2022), so the alternatives for each line item are merged rather than
taking the first one with data.

### 9. Cointegration screen

At the bottom because it is not a per-ticker panel: cointegration is a property of
a *pair*, so it takes its own universe.

Engle-Granger on log prices, both directions, with three things a naive screen
leaves out:

- **Out-of-sample testing.** The hedge ratio is fitted on the first 70% and the
  test re-run on the held-out 30%.
- **A multiple-testing threshold.** Screening 50 tickers runs 1,225 tests and
  produces ~61 false positives at p < 0.05 by pure chance. The Bonferroni line is
  reported next to the raw p-values.
- **Half-life.** From an Ornstein-Uhlenbeck fit on the spread. A statistically
  stationary spread that reverts over three years is untradeable.

The common — and useful — outcome is that nothing survives.

## Design notes

**The forward is read off the chain, not assumed.** Put-call parity says
`C − P = df·(F − K)`, so regressing `C − P` on `K` across the liquid strikes
recovers the forward without needing a dividend forecast or borrow rate. Moneyness,
deltas, the density and the greeks all key off that one number, which keeps them
mutually consistent. The implied dividend yield falls out as a by-product.

**Implied vol is recomputed from mid prices, not taken from Yahoo.** Yahoo's
`impliedVolatility` field is frequently stale and occasionally built off the last
trade. It is kept only as a fallback for strikes with no usable quote. Inversion
is by vectorised bisection rather than Newton: option value is monotonic in vol,
so 80 halvings converge unconditionally with no starting guess and no divergence
on near-zero-vega quotes.

**Wings below one delta are excluded**, and the test is applied at the chain's
at-the-money vol rather than at each strike's own. Vol is barely identified in the
wings — a deep OTM option quoted at the minimum tick can imply almost any
volatility — and judging a quote by the vol derived from that same quote is
circular: the worse the price, the larger the vol it implies, and the more delta
it appears to carry. A live AMD chain admitted an $810 strike against a $469 spot
that way, eight standard deviations out on a nine-day expiry, purely because a
stale print implied a 196% vol. Anchoring the test on the median vol of the ten
strikes nearest the forward closes that loop.

**A one-sided quote is treated as one-sided.** Where only one side is live the
price falls back to the last trade, but that trade has to be consistent with the
side that is quoted — a print above the current offer is one the market has moved
away from, and it is dropped rather than fitted. The remaining uncertainty is
recorded as the full magnitude of the quoted side rather than a bid-ask width,
because a lone offer bounds the value at one end only. Two earlier defaults did
the opposite: a missing spread was filled with exactly the rejection threshold, so
an unquoted strike always passed an inclusive comparison, and it inherited a
5-vol-point error estimate, which is *better* than most genuine wing quotes get.

Together these three fixes cut the smile residual roughly threefold across
mega-caps and took AMD's reported excess kurtosis from +17.7 to +0.8. The cost is
that very thin chains — a $2 stock with fifty-cent strikes — now decline to draw a
density rather than drawing one from noise.

**Stale-quote detection.** Outside US hours Yahoo returns no two-sided markets at
all and every price falls back to the last trade. The dashboard says so rather
than silently producing a confident-looking density from day-old prints.

**Colour is load-bearing.** Green is calls and buying, red is puts and selling,
periwinkle `#A9C7EE` is the lead series where there is no direction to encode and
lilac `#EAC5FF` is the second one, teal `#5EEAD4` the third and this ticker's
implied-vol reference, and amber is reserved for the one market-wide (VIX) line.
Streamlit's default red accent is overridden in `.streamlit/config.toml` so that
red never means "this is a widget" and "this is a sale" on the same screen. The
vol surface keeps a continuous colourmap, where the colour encodes magnitude
rather than direction.

**Chart chrome does not collide.** Plotly stacks the title, a horizontal legend
and the modebar into the same top band by default, which squashes them together.
`layout()` reserves 86px above the plotting area, left-aligns the title and pins
it to the top of the container, so the title, the legend and the modebar occupy
three separate lanes. There is a check for this in the verification notes: every
chart on the page reports a 20-28px gap and zero overlaps.

**Widget state is keyed.** The expiry pickers use explicit `key=`s so a refresh
cannot silently move the selection, and `forget_stale()` prunes a remembered
value when a new ticker no longer offers it. The density's expiry is chosen from
the full expiration list rather than from the surface multiselect — tying them
together meant a date had to be added to one picker before it could be selected
in the other.

## Exports

Two, at the foot of the sidebar, for two different readers.

**Build PDF report** rebuilds the whole page as a document, then a **Download
PDF** button appears beneath it.

It is not a screenshot. Streamlit cannot capture itself, and a browser print
dialog would catch whatever happens to be on screen — collapsed expanders, charts
clipped at the viewport edge, the sidebar in the middle of the page. Instead every
figure the page draws is also handed to a `report.Report`, re-themed for white
paper, re-rendered at 3000px wide and laid out on A4 landscape with its headings,
metrics and tables around it. Nothing can be cut off because nothing is cropped
from a screen in the first place; a figure taller than the usable page is scaled
down rather than split.

Rendering runs through kaleido, which starts a headless browser. Exporting a dozen
figures one at a time costs about half a minute, so they go through
`plotly.io.write_images` in a single batch instead. Expect 20-40 seconds.

**Download markdown (for AI)** takes the same `Report` and writes it for a model
instead of a person. A model cannot see a picture, so `mdreport.py` replaces each
figure with the numbers behind it — every trace named, with its axis titles, its
values and its reference lines; surfaces come through as a sampled grid. Series
longer than 90 points are thinned to 60 evenly spaced samples, which preserves the
shape and keeps a full export around 45KB. There is no build step: it is text
assembled from blocks that already exist, so the button is always live and always
current.

Both exports carry whatever the page is showing at the time, the cointegration
screen included — the screen's submitted parameters are held in session state, so
it survives the reruns that clicking anything else causes.

## Tests

```bash
python byproduct/run_tests.py
```

Three suites, no network. They live in `byproduct/` with the assert harness they
share and import the app's modules from the root, so they run from anywhere.

| Suite | Covers |
| --- | --- |
| `byproduct/test_options.py` | Black-76 parity and monotonicity, implied-vol round-trip over a wide grid, greeks against finite differences, forward and vol recovery from synthetic chains, density integration/martingale/lognormal-reproduction, gamma sign conventions and flip detection, expected-move identities, skew metrics, static arbitrage checks against a deliberately damaged chain, BKM moments against closed-form lognormal values, first-passage probabilities against a Monte Carlo, wing-extension continuity, earnings move-date mapping and jump recovery from a known jump, benchmark comparison, default expiry selection across daily/weekly/degenerate listings, markdown-export table integrity, closed-market chains falling back to the last session's closing prints, leaving the width a one-sided quote already carries alone, the caption telling the after-hours closing book from a live market on the exchange's clock, maturities counted on New York's date rather than the caller's |
| `byproduct/test_vol_pairs.py` | Estimators recovering a simulated volatility from generated intraday paths, gap sensitivity, forward-looking alignment, cointegration detection, false-positive rate on independent random walks, half-life against a known AR(1) |
| `byproduct/test_edgar.py` | Form 4 and 13F parsing from real filing XML, transaction-code filtering, cluster detection, year-to-date un-cumulating, issuer-name normalisation, abbreviation/word-order/qualifier matching against the spelled-out name, activist tagging and roster integrity, the per-ticker manager scan, ticker-seeded CUSIP matching and the corroboration that keeps a bad lookup out, thousands scaling and the detection of filers who never left it, URL construction, frequency-stamped XBRL series merging |

Every chain in `test_options.py` is synthesised from a known smile, so the tests
check that the pipeline recovers the inputs it was built from rather than just
that it runs.

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit UI and charts |
| `data.py` | Yahoo Finance retrieval — the only module that touches the network for market data |
| `prep.py` | Chain cleaning: forward from parity, vol inversion, greeks, quote quality |
| `blackscholes.py` | Black-76 pricing, greeks, implied-vol inversion |
| `rnd.py` | Breeden–Litzenberger risk-neutral density, and first-passage probabilities |
| `bkm.py` | Model-free implied moments, replicated from quotes as a cross-check on `rnd.py` |
| `noarb.py` | Static arbitrage checks — vertical, butterfly, calendar — against the raw quotes |
| `gex.py` | Gamma exposure and the zero-gamma flip |
| `surface.py` | Vol surface, term structure, 25-delta skew |
| `earnings.py` | Which expiries span the announcement, and the jump priced into them |
| `expiries.py` | The maturity ladder both pickers open on |
| `benchmark.py` | The ticker's vol and skew against the index at a matched maturity |
| `expmove.py` | Expected move and historical comparison |
| `vol.py` | Realised-vol estimators and the volatility risk premium |
| `pairs.py` | Cointegration screening |
| `edgar.py` | SEC EDGAR: Form 4, XBRL, per-ticker 13F scan |
| `report.py` | Rebuilds the page as a PDF from the source figures |
| `mdreport.py` | Writes the same report as markdown, charts as numbers, for a model to read |
| `.streamlit/config.toml` | Dark base and a periwinkle accent, so red can mean "sell" everywhere |
| `requirements.txt`, `packages.txt` | Python and apt dependencies for Streamlit Cloud; the Python versions are pinned exactly, and the apt package is Chromium, which renders the PDF's figures |
| `byproduct/` | Tests, their assert harness, an overnight watcher and local tooling. Nothing the app imports lives there, and `.gitignore` keeps it out of the repository, so the root is exactly what Streamlit Cloud needs |

Nothing outside `app.py` imports Streamlit, and nothing outside `data.py` and
`edgar.py` touches the network, so every calculation is testable offline.

## Note on `yfinance` and TLS

`yfinance` defaults to `curl_cffi`, whose certificate verification is broken on
some Windows installs (`CertificateVerifyError`). `data.make_session()` hands it a
plain `requests` session instead, which uses `certifi` and works everywhere.

---

Educational tool, not investment advice. Everything here is a descriptive
statistic about prices, quotes and filings — none of it is a forecast.
