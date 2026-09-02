# The dashboard, explained from scratch

This guide assumes you know what a share price is and nothing else. It walks the
dashboard top to bottom, in the order the page renders, and explains what each
panel is showing, how to read it, and — just as important — the ways each one is
commonly misread.

Nothing here is advice. Every number on the page is a *description* of what
prices, quotes and filings currently say. None of it is a prediction, and the
guide tries hard to be honest about which numbers are solid and which are held
together with assumptions.

---

## Part 0: the ten minutes of options theory you need

Skip this if you already trade options.

### What an option is

An **option** is a contract that gives you the right — not the obligation — to
trade a stock at a fixed price on a fixed date.

- A **call** gives you the right to *buy* 100 shares at the **strike** price.
- A **put** gives you the right to *sell* 100 shares at the strike price.

The fixed date is the **expiration** (or *expiry*). The price you pay for the
contract is the **premium**. One contract covers **100 shares** — this is why
every dollar figure on this dashboard has a factor of 100 in it somewhere.

Example: NVDA trades at $217. A $230 call expiring in three weeks might cost
$3.50. You pay $350 (100 shares × $3.50). If NVDA is above $230 at expiry you can
buy at $230; below $230 the contract expires worthless and you lose the $350.

### The two people in every trade

Someone **buys** that call and someone **sells** (or *writes*) it. The buyer's
most is the premium; the seller collects the premium and takes on the obligation.
Several panels here are about what the *sellers* — usually market-making desks —
are left holding, because their hedging can push the stock around.

### Moneyness

- **In the money (ITM)**: the option already has value if exercised now. A $200
  call with the stock at $217 is $17 in the money.
- **At the money (ATM)**: strike ≈ current price. These are the most traded.
- **Out of the money (OTM)**: no value if exercised now. A $250 call with the
  stock at $217 is out of the money — it's a bet the stock rises.

OTM options are the liquid, actively traded ones, which is why this dashboard
builds almost everything from them.

### Implied volatility — the single most important idea here

**Volatility** is how much a stock moves around, expressed as an annualised
percentage. A stock with 30% volatility typically moves about 30% over a year,
and correspondingly less over shorter windows.

Two flavours, and the difference between them drives several panels:

- **Realised volatility** is measured from what the stock *actually did*. It is a
  fact about history.
- **Implied volatility (IV)** is backed out of the option's price. Option pricing
  models take volatility as an input and produce a price; run it backwards from
  the market price and you get the volatility the market must be assuming. It is
  a *forecast*, and it is what you are really buying or selling when you trade an
  option.

When people say "options are expensive", they mean implied volatility is high.

### Why "annualised" trips people up

All volatility numbers are quoted per year even when the option expires in nine
days. To convert, multiply by the square root of the fraction of a year:

```
move over T years ≈ price × IV × √T
```

At $217 with 35% IV and 9 days to expiry (T = 9/365 = 0.0247):

```
217 × 0.35 × √0.0247 = 217 × 0.35 × 0.157 = $11.9
```

So a 35% IV on a nine-day option means the market expects roughly a ±$12 move.
The **√T** is why a 4× longer expiry only implies a 2× bigger move.

### Delta and gamma

- **Delta** is how much the option price moves when the stock moves $1. A call
  with 0.30 delta gains about $0.30 per $1 rise. It is loosely — and only loosely
  — read as "roughly a 30% chance of finishing in the money".
- **Gamma** is how fast delta itself changes as the stock moves. High gamma means
  your exposure shifts quickly. It peaks at the money and near expiry.

Gamma matters here because dealers who sell options hedge by trading the stock,
and gamma determines how much stock they must trade as the price moves. That is
the entire subject of section 3.

### Open interest vs volume

- **Volume** is how many contracts traded *today*.
- **Open interest (OI)** is how many contracts are currently *held open*.

Volume is flow; open interest is position. Open interest updates overnight, so it
always reflects yesterday's close.

---

## Part 1: the sidebar

Top to bottom: the ticker, then the settings the page is built from, then the
buttons that act on them.

**Ticker** — the stock symbol. Everything on the page keys off it, including the
browser tab, which reads `Market Analytics · NVDA`.

**Expirations** — the set of expiry dates used for the volatility surface, the
skew metrics and gamma exposure. More dates means a fuller surface.

**Primary expiration** — the single expiry the probability distribution and the
headline expected-move numbers are built from. Independent of the list above.

Each date is labelled with days out and, where applicable, an **OPEX** tag:

- `2026-10-16 · 46d (OPEX)` — the monthly expiration, always the third Friday.
- `2026-09-18 · 18d (OPEX quarterly)` — a monthly that is also quarterly
  ("quad witching", in March, June, September and December).

Why care? Monthly expiries hold the overwhelming majority of open interest.
Weeklies are thin by comparison. If a panel looks strange on a weekly, the answer
is usually just that few contracts exist there.

**Risk-free rate** — the interest rate used in the option maths, pulled from the
13-week Treasury bill. It only matters for longer expiries. Leave it ticked.

**Refresh data** — re-pulls quotes, chains and price history. Yahoo's data is
delayed about 15 minutes, so this is as fresh as this dashboard gets. Your chosen
expirations survive the refresh. SEC filings are deliberately *not* re-pulled:
they change quarterly, and the 13F scan is the slowest thing here.

The first load of the day takes around half a minute, because it downloads the
whole manager roster. That is kept for six hours, so every ticker you look at
afterwards loads in about ten seconds.

Refreshing outside US market hours will not make the page any more live, because
there is nothing live to fetch — see the note on running it after the close,
below.

**Build PDF report** and **Download markdown (for AI)** — the two exports, both
described at the end of this guide.

---

### What it picks for you when the page opens

Every time you type a new ticker, both boxes are filled in for you. What you get
is a **ladder of dates**, spread out rather than bunched together:

- every expiration between now and the next monthly,
- the monthly after that,
- whatever is listed nearest three months out,
- and nearest six months out.

Eight dates at most, because each one is a separate download and the page waits
for all of them.

"Monthly" means the third Friday of the month, and it is not an arbitrary choice
— it is where the market is organised. The overwhelming majority of open interest
sits there, it is the only date with long-dated options behind it, and it is
where dealer hedging and pinning concentrate. That is why the primary starts
there: the distribution, the expected move and the gamma profile then all
describe the date the market itself cares about, rather than whichever weekly
happened to be a few days out.

The spread matters as much as the dates do. Several panels further down compare
one expiry against another — how volatility changes as you look further out, and
whether the near and far dates are priced consistently. Four dates inside the
same fortnight cannot answer either question. So the ladder deliberately reaches
out to six months even though most of the page is about the front of it.

Two smaller things it does quietly. It skips an expiry that settles **today** —
with hours left on the clock those options are priced by the mechanics of
settlement rather than by any view on volatility, and including one distorts
several charts. And where a ladder rung falls near a monthly, it takes the
monthly: a thin, rarely-traded date at exactly three months is a worse reading
than a heavily traded one a fortnight earlier.

**If earnings are coming up**, the primary moves. It becomes the first expiry
that *includes* the announcement, because a distribution built from an expiry
that settles before the event is describing a stock that has not had its
earnings yet — a different thing entirely. The last expiry *before* the
announcement is kept in the list too. That pairing is what lets the page work
out what the event is worth, and losing either half would make that panel
disappear without explanation.

You can change either box freely; nothing is locked. Note that switching ticker
and switching back resets them — the selection describes one ticker, and quietly
keeping one company's dates on another company's chain is the kind of thing that
misleads you without ever looking wrong.

---

## Part 2: the header row

| Metric | What it is |
| --- | --- |
| **Spot** | The current share price (delayed ~15 min). |
| **Forward** | Where the market prices the stock *for delivery at expiry*. |
| **ATM implied vol** | The market's volatility forecast to that expiry. |
| **Realised vol (21d)** | How volatile the stock actually was over the last month. |
| **Vol premium** | Implied minus realised. Positive = options priced above recent reality. |
| **Days to expiry** | Calendar days to the primary expiration. |

**Forward vs spot** confuses people. If you agree today to buy a share in three
months, you pay slightly more than spot (you keep your cash and earn interest
until then) and slightly less if the stock pays a dividend you won't receive. The
forward nets those out. Over a week the difference is pennies; over a year on a
dividend payer it is real. This dashboard reads the forward straight out of the
option chain rather than assuming a dividend, and everything else is measured
against it.

**Vol premium** at, say, +5 points means the market is charging for more movement
than the stock has recently delivered. That's normal — see section 5.

### The earnings line

Under the header you may see a line saying the primary expiry **spans earnings**
or **settles before earnings**.

This matters more than almost anything else on the page. An earnings
announcement is a scheduled event where the stock can gap on a single morning,
and an option that covers that date is pricing something categorically different
from one that does not. Two expiries a week apart can have completely different
implied volatilities for no reason other than which side of the announcement they
fall on.

Note the date it gives is the **session the move lands in**, not the announcement
date. A company reporting after Tuesday's close moves the stock on Wednesday, so
a Tuesday expiry does not capture it.

### Running it after the close

The line under the title always says which of three things you are looking at:

- **`live quotes, delayed ~15 min`** — US markets are open. Bids and offers are
  real, roughly a quarter-hour behind.
- **`closing quotes of the <date> session`** — US markets have shut, but Yahoo
  is still serving the bids and offers as they stood at the close. This is the
  last book of the day and every bit as precise as a live one; it just will not
  move until the next open. From Singapore this is what you see for most of the
  working day, since 4pm in New York is 4am here.
- **`closing prints from <date>`** — some hours after the close Yahoo blanks
  every bid and ask, so the only price left for each contract is its last trade
  of that session. Those are the closing quotes, and the page uses them.

The third is worth reading and is *not* stale data — it is where the market
finished. Two honest limits, though. Different strikes last traded at different
moments of the session, so the smile is assembled from prices minutes or hours
apart rather than one instant, and it is quoted less precisely to reflect that.
And a strike that has not traded since long before that session is thrown out
rather than believed, so a thin chain has fewer strikes overnight than it does
intraday.

Note the timestamp beside it is New York time, not yours, and so is every
"days to expiry" figure on the page. If you are in Asia or Europe, an afternoon
on your clock is the middle of the night on the exchange's, and your calendar
may already be a day ahead of New York's.

### Warnings you may see

- *"No live market on this chain"* — the closing-prints case above. Expected
  outside US market hours; come back during them for live bids and offers.
- *"Under a day to expiry"* — same-day options behave wildly. Prefer an expiry a
  week or more out.
- *"Yahoo Finance is rate limiting this address"* — not a bug, and not something
  you did. Yahoo caps how many requests one internet address may make, and on
  Streamlit Cloud this app shares its address with every other app hosted there,
  so the cap can already be used up by strangers. **Do not keep refreshing**:
  the page has to ask Yahoo about sixteen things to draw itself, so every
  refresh spends another sixteen requests on a limit that is already full and
  pushes the reset further away. The page now refuses to ask for a minute after
  a refusal and tells you how long is left. Wait it out, then refresh once. If
  it happens repeatedly, it is the hosted copy's shared address; running the app
  on your own machine uses your own.

---

## Part 3, section 1: Expected move

**The question it answers:** how big a move is the option market charging for
between now and expiry?

Two numbers, and the difference between them is a genuine source of confusion:

- **1σ move** ("one standard deviation") — the range that covers about **68%** of
  outcomes. This is the band most people mean.
- **Expected absolute move** — the average size of the move, ignoring direction.
  It equals the price of the ATM **straddle** (buying a call and a put at the same
  strike), and it is about **20% smaller** than the 1σ move.

You will constantly see the straddle price called "the expected move". It is a
real quantity, just not the 68% band. Both are shown so you don't have to guess
which one you're looking at.

### Reading the cone chart

The horizontal axis is days to expiry; the shaded bands are where the market
prices the stock landing. The dark band is 68%, the pale one 95%. The cone widens
with time — but by **√time**, not linearly, which is why it curves.

### "What actually happened, historically"

Below the numbers, the dashboard checks the same horizon against the stock's own
past. If the chain is charging ±5.7% for the next nine days and nine-day moves
have historically averaged ±5.9%, options look fairly priced. If it were charging
±9%, the market is either expecting something specific (earnings, a court ruling)
or is simply nervous.

**Don't over-trust this comparison.** It uses overlapping windows, so the
observations aren't independent, and it says nothing about *why* the market might
be pricing something different this time. Earnings dates are the usual answer.

---

## Part 3, section 2: Implied probability distribution

**The question it answers:** where does the whole option market think this stock
will be on the expiry date — not just how far, but with what shape?

This is the most sophisticated panel on the page and the most useful once it
clicks.

### The idea

Options at every strike are all trading at once. A $250 call is a bet on
finishing above $250; a $260 call is a bet on finishing above $260. The
*difference* in their prices tells you what the market charges for landing
between $250 and $260. Do that across every strike and you can reconstruct the
market's entire probability distribution.

Formally that's the second derivative of call price with respect to strike, which
is the **Breeden–Litzenberger** result. The mechanics are in the technical README;
what matters is that the curve is extracted from real prices, not assumed.

### Reading the chart

The solid blue curve is the market-implied probability density. Taller = more
likely. The shaded region is the middle 80% of the probability.

The **dashed grey line** is what the textbook Black-Scholes model would predict
using the same at-the-money volatility. **The gap between them is the whole
point.** Typically you'll see:

- The blue curve has a **fatter left tail** — the market charges more for crashes
  than a bell curve implies, because crashes happen more often than a bell curve
  implies.
- The blue curve has a **higher peak** — the middle is correspondingly cheaper.

### The percentile table

Read it as: "the market prices a 10% chance of finishing below $319.77". The
`vs spot %` column converts to a percentage move. This is the most directly
useful output on the page.

### Skew and kurtosis

- **Implied skew** — negative means a fatter left tail (crash worry). Note that
  even a perfectly ordinary lognormal has mildly *positive* skew, because a stock
  can rise indefinitely but only fall to zero. Compare readings across dates and
  tickers rather than reading a single number as good or bad.
- **Excess kurtosis** — how fat both tails are. 0 is a normal bell curve.
  Positive means big moves in either direction are priced as more likely.

### "Reaching a level before expiry"

This small table answers a different question from everything above it, and it is
usually the more practical one.

The distribution says where the price is likely to **finish** on expiry day. It
says nothing about the journey. A level the stock touches on Wednesday and falls
back from does not show up in it at all — but if you had a stop there, or an alert,
or a plan to act, it absolutely happened to you.

So the right-hand column gives the probability of the price **trading at that
level at any point** before expiry. It is always the larger of the two, often
roughly double, and the gap between them is exactly the amount of activity the
settlement-day view misses.

Two details worth knowing. The volatility used for each level is read off the
smile *at that level*, so the downside is priced with the higher volatility the
market genuinely charges there rather than the at-the-money one. And the touch
probability can never come out below the finishing probability — the price cannot
end up beyond a level without having crossed it — which is enforced rather than
hoped for.

### The honest caveats, printed beneath the chart

- **"Quoted strikes span $280–$465"** — beyond that range there are no quotes.
  The curve is continued outward from the edge of the real data and then allowed
  to settle, so the extreme tails are extrapolation, not data.
- **"0.000% of the probability falls outside the strike grid"** — how much of the
  distribution got cut off by where the calculation stopped. It should be
  essentially zero; the grid widens itself until it is.
- **"RMS residual 0.74 vol points"** — how closely the fitted curve tracks actual
  quotes. Under ~1 point is a good fit; several points means the chain is illiquid
  and the whole distribution should be taken loosely.

### The green tick: "the quotes support the reported shape"

Underneath all of the above there is a second opinion on the skew and kurtosis
numbers, and it is worth understanding why it is there.

Getting from option prices to that curve takes five or six steps — fit a line
through the volatilities, re-price a grid, subtract twice, tidy up what comes
out negative. If one of those steps goes wrong, **nothing else on the page would
tell you**. The curve would still be a valid probability distribution. It would
just be the wrong shape.

So the same numbers are worked out a second time, by a completely different
method that skips every one of those steps: there is a classic result (Bakshi,
Kapadia and Madan) saying they can be got at directly, as a weighted sum of the
option prices themselves. No curve-fitting at all. If the two agree, the fitting
did not break anything. If they disagree, the prices win.

**The line the check really turns on is the "mean / forward" row.** The forward
price is where the market says the stock will be on average, and any honest
probability curve has to average out to exactly that. The direct method returns
it perfectly by construction — the arithmetic cannot get it wrong. So if the
fitted curve misses it, something in the fitting has gone astray. On a healthy
chain it lands within 0.05%. In testing, deliberately corrupting a single option
quote knocked it 1.4% to 20% off.

Skew and especially kurtosis are shown but deliberately **not** what the verdict
is based on, and the reason is worth knowing. Kurtosis is all about the extreme
tails, and the direct method can only see strikes the market actually lists —
about two standard deviations either side. It is blind past that. So its
kurtosis comes out small no matter what the real distribution looks like, and
"comparing" the two would just be reading the fitted number back to yourself.
What the gap *does* tell you is how much of the reported tail shape comes from
real quotes and how much from the curve being continued past them.

One extra number comes out of the same calculation: **model-free implied vol**.
Ordinary ATM implied vol reads a single strike. This one prices the whole chain
at once, and it is the same construction the VIX uses — so it is the fair
comparison against the VIX line in section 5. It is normally a little above ATM
vol, because it accounts for the wings too.

---

## Part 3, section 3: Gamma exposure

**The question it answers:** are the dealers who sold all these options likely to
be damping the stock's moves, or amplifying them?

### The mechanism

When a customer buys an option, a market maker sells it. Market makers don't want
a directional bet, so they hedge with the stock — and because gamma makes their
exposure change as the price moves, they must keep *re-hedging*.

- **Positive gamma**: dealers sell as the stock rises and buy as it falls. This
  pushes against moves and **damps volatility**.
- **Negative gamma**: dealers buy as it rises and sell as it falls. This pushes
  with moves and **amplifies volatility**.

The **flip level** is the price where net gamma crosses zero — the boundary
between the two regimes. Above it, moves tend to get absorbed; below it, they
tend to extend.

### Reading the charts

- **Left**: gamma by strike, green for calls above the line, red for puts below.
  Tall bars are strikes with lots of contracts outstanding.
- **Right**: total gamma across candidate stock prices, with spot and flip marked.

Units are dollars of stock dealers would need to trade per 1% move.

### The caveat that matters more than the numbers

Open interest doesn't record who bought and who sold — only that contracts exist.
This panel assumes the standard convention (customers buy calls and sell puts, so
dealers are long call gamma and short put gamma). On plenty of tickers that's
backwards, and then every conclusion inverts.

**Treat it as a hypothesis about positioning, not a measurement.**

### If it says "zero open interest"

Yahoo blanks that column for hours at a time, especially outside US market hours.
The panel offers **volume** as a fallback, which answers a different question:
where gamma was *traded today* rather than where the position sits. Useful, but
not the same thing.

---

## Part 3, section 4: Volatility surface and skew

**The question it answers:** is the market charging more for some strikes and
expiries than others, and in what pattern?

### The surface

A 3D chart of implied volatility across strike (left–right of current price) and
time to expiry. In a textbook world it would be flat. It never is:

- Volatility is almost always **higher for downside strikes** — crash protection
  is expensive.
- Volatility usually **rises with time** in calm markets and **falls with time**
  when something scary is imminent.

The corners are interpolated where no options are quoted; treat those as
decoration.

### The two skew numbers

- **25-delta risk reversal** — the volatility charged for a moderately
  out-of-the-money call minus the same for a put. **Negative is normal** for
  stocks: puts cost more. The signal is whether it's *more* negative than usual.
- **25-delta butterfly** — how much more the market charges for both wings versus
  at the money. It measures how fat the market thinks the tails are.

Both are in "vol points": −3 means the put's implied vol is 3 percentage points
above the call's.

Blank spots mean the chain simply doesn't quote that far out. The dashboard
leaves those undefined rather than inventing a number.

### Term structure

At-the-money volatility by expiry, plus **forward vol** — the volatility implied
*between* two expiries. If 30-day vol is 30% and 60-day vol is 32%, the market is
pricing something more volatile in days 30–60.

### "What the chain charges for earnings"

If the expiries you have selected straddle an earnings date, this panel prices
the event.

The logic is simpler than it looks. Volatility accumulates with time, so an
expiry that covers the announcement contains *everything a normal expiry
contains, plus the announcement*. Take the last expiry before the event as a
reading of "normal", subtract it from the first expiry after, and what is left is
the event.

It reports two numbers, and you want both:

- **Implied earnings move** — the one-day move the market is charging for.
- **Share of the expiry's variance** — how much of everything that expiry prices
  is this single event. A 6% move that is 20% of the variance is a very different
  situation from a 6% move that is 80% of it.

Sometimes it says no premium can be told apart from noise. That is an honest
answer, not a failure. Quoted prices are ranges, not points, and on a
wide-spread chain an event of that size is simply not measurable — the panel
tells you the size of the noise floor so you can see why.

The assumption doing the work is that "normal" volatility is the same either side
of the event. It never is exactly. Read the output as the size of the premium
being charged, not as a forecast of what will happen.

### "Against the index"

A risk reversal of −3 points is meaningless without something to compare it to.
Equity skew is *always* negative, so the number on its own tells you nothing.

Ideally you would compare a ticker against its own history. That needs a record
built up day after day, and this app has nowhere to keep one — it runs in a
container that forgets everything when it restarts. So instead it compares
sideways: the same three numbers for **SPY**, this afternoon, at the closest
matching expiry. Both move together with the general mood of the market, so
comparing them cancels most of that out and leaves what is specific to your
ticker.

The useful read is the **difference** row. If your ticker's risk reversal is much
more negative than SPY's, the market is paying up for protection on *that name*
rather than on stocks generally.

Two warnings printed under it. The volatility ratio is **not a beta** — a single
stock carries risk the index has diversified away, so it is above 1× for almost
everything and that is not a signal. And SPY is the whole market, not your
ticker's sector; if the sector is having its own day, a competitor would be the
better comparison.

### Static arbitrage checks

At the bottom of this section is a panel that either shows a green tick or lists
strikes. It is checking three things that have to be true of *any* set of option
prices, whatever you believe about the stock:

1. A call must get cheaper as the strike rises — and by less than the strike
   moved. (Paying more for the right to buy at a worse price makes no sense.)
2. Plot call price against strike and the curve must bend one way only. A dent
   in it is the same thing as the probability curve above going *negative*.
3. A longer-dated option cannot price less total movement than a shorter-dated
   one. There is more time for things to happen, not less.

Breaking any of these would be free money if you could trade it. You cannot:
these quotes are fifteen minutes old, so anything that shows up here vanished
long ago. **What it is actually for is finding the bad quote.** A single stale
price sitting out of line with its neighbours will quietly drag the probability
curve above into the wrong shape, and this panel names the exact strike doing
it.

A violation is only reported when it is bigger than the bid-ask spread of the
strikes involved. A quoted "price" is really a range, and something only counts
as broken if it stays broken anywhere inside that range — otherwise every
normally-quoted stock would light up with false alarms.

Green tick means the chain is internally consistent. That is not a promise the
prices are *right*, only that they do not contradict each other.

---

## Part 3, section 5: Volatility risk premium

**The question it answers:** do options tend to be priced above or below what
actually happens — and by how much?

### The core fact

Implied volatility is, on average, **higher** than the volatility that follows.
Option sellers get paid for taking on risk that buyers want to shed. This is one
of the most durable patterns in markets.

### The top panel: this ticker, today

The current implied volatility next to the recent realised volatility, with four
different estimators. **Yang-Zhang** is the one to read: it uses the open, high,
low and close, so it captures the overnight gap and intraday range that
close-to-close throws away, and needs roughly a quarter of the data for the same
precision.

This compares a *forecast* against the *recent past*, which is the weaker
comparison. It's labelled as such on the page.

### The bottom panel: the market-wide premium

This is the rigorous version. VIX (the S&P 500's implied volatility) is plotted
against the volatility that actually arrived over the **following** 21 trading
days. The difference is the true premium.

The last 21 days are deliberately blank — that future hasn't happened yet, and
filling it in would be fiction.

Typical reading: the premium is positive about 85% of the time and averages a few
volatility points. **The 15% is what matters.** The negative episodes cluster
together and are far larger than the positive ones. Selling volatility makes
small amounts of money most months and loses a great deal occasionally. The chart
shows that asymmetry directly: look at the depth of the spikes below zero versus
the height of the ordinary band above it.

---

## Part 3, section 6: Insider trades

**The question it answers:** are the executives and directors of this company
buying or selling their own stock?

Anyone who is an officer, director or 10% holder must report their trades to the
SEC within two business days, on **Form 4**. This panel parses those filings
directly.

### The single most important thing on this panel

**Only two transaction codes are real trades:**

| Code | Meaning | Signal? |
| --- | --- | --- |
| **P** | Open-market purchase | **Yes** — they chose to buy with their own money |
| **S** | Open-market sale | **Yes** — they chose to sell |
| A | Grant or award | No — compensation, not a decision |
| M | Option exercise | No — usually mechanical |
| F | Shares withheld for tax | No — automatic on vesting |
| G | Gift | No |

Headlines about "insiders dumping $800m of stock" routinely include grants,
exercises and tax withholding. The totals here count only **P** and **S**.

### How to read what you see

Green bars are purchases, red are sales.

**Selling is weak evidence.** Executives are paid in stock and sell constantly for
ordinary reasons — diversification, a house, a tax bill, a scheduled plan. Heavy
insider selling at a company whose stock has tripled is what you would expect.

**Buying is stronger evidence.** There's really only one reason to put your own
cash into your employer's shares.

**Cluster buying is the strongest form.** When the dashboard flags three or more
*different* insiders buying within 30 days, that's much harder to explain as
individual portfolio housekeeping. It's the version of this signal with the most
support in the academic literature.

---

## Part 3, section 7: Institutional activity

**The question it answers:** which well-known investment managers added to or cut
their position in this stock last quarter?

Managers running over $100m must file a **13F** listing their US stock holdings,
45 days after each quarter ends. Comparing consecutive filings shows what changed.

Green bars added, red bars sold. The table shows share counts, dollar values, and
what percentage of that manager's portfolio the position represents — that last
column matters most. A billion-dollar position is routine for a giant fund and a
massive conviction bet for a small one.

### The (activist) label

Some managers are tagged **(activist)**, and it changes what the row means.

Most funds buy a stock because they think it will go up. An activist buys it in
order to *change the company* — push for a sale, a break-up, board seats, a new
chief executive. So a new activist position is not just an opinion, it is a
stated intention to do something, and it is often followed by a public campaign.

The rest of the table deserves less weight than people give it. A quant fund
appearing here holds the stock because a model selected it, alongside three
thousand others, and may be out of it next quarter.

### The quarter column

Each row says which quarter that manager filed for. Usually they all agree. When
one doesn't, a line under the table says so.

This matters because a fund can change the legal entity it files under, and the
old one goes quiet. That looks identical to a fund that has stopped filing, and
the dashboard used to show whichever filing it could still find without telling
you it was older than the rest.

### Limits you must keep in mind

- **It is old.** Filed up to 45 days after quarter end, and you're reading it
  later still. The manager may have sold the whole thing weeks ago.
- **It is long-only equity.** No short positions, no bonds, no foreign listings,
  no cash. A fund showing a large long may be hedged in ways 13F never reveals.
- **The list is a roster, not the whole market.** No free data source answers "who
  holds this stock", so the dashboard checks a fixed list of 20 well-known
  managers. "Nobody is buying" really means "nobody on this list".
- **Share classes are merged.** GOOGL and GOOG can't be separated here.
- **A foreign-incorporated company can still go missing.** Filers write the
  issuer's name and the dashboard has to recognise it. Abbreviations are handled
  (`FORD MTR CO` is `Ford Motor Co`), and where the name is hopeless the
  dashboard falls back on the stock's CUSIP — its permanent ID number — which is
  how it still finds GE under `GE Aerospace` and SPY under `STATE STR SPDR S&P
  500 ETF T`. That ID is only readable for US-listed securities, so a company
  incorporated abroad, like Accenture in Ireland, can slip through and read as
  unheld when it isn't.

---

## Part 3, section 8: Fundamentals

**The question it answers:** is the underlying business actually growing, and how
profitable is it?

These figures come straight from the company's own SEC filings — no data vendor
in between.

- **Revenue** — total sales.
- **Gross profit** — revenue minus the direct cost of what was sold.
- **Operating income** — after running costs, before interest and tax.
- **Net income** — the bottom line.
- **Operating cash flow** — actual cash generated. Can differ sharply from net
  income, and when it does, that's worth understanding.
- **Margins** — each of the above as a percentage of revenue. Rising margins mean
  growth is getting more profitable, not just bigger.

Two quiet corrections are applied that most simple parsers get wrong: cash-flow
figures are filed year-to-date and are converted here into individual quarters
(otherwise Q4 appears about four times too large), and companies that changed
their filing tags over the years have their history stitched back together.

Figures are **as filed at the time** — later restatements aren't back-propagated.

---

## Part 3, section 9: Cointegration screen

**The question it answers:** are there two stocks whose prices move together
closely enough that the gap between them reliably closes?

This one isn't about your ticker; it takes its own list of stocks.

### The idea

Two stocks are **cointegrated** if some combination of them is stable even though
each individually wanders. Think of two dogs on a long shared leash — each
wanders freely, but they can't drift apart forever. If they do drift, the trade is
to bet on them converging.

### Why the screen is deliberately pessimistic

Most apparent relationships are coincidence, and this panel is built to show you
that rather than hide it:

- **Testing both ways and out of sample.** The relationship is fitted on the first
  70% of history and re-tested on the held-out 30%. Anything that only works
  in-sample described the past, not a relationship.
- **The multiple-testing threshold.** Testing 50 stocks means 1,225 pairs. At the
  usual 5% significance level you'd expect about 61 false positives from pure
  chance. The **Bonferroni threshold** is the much stricter bar that survives
  correcting for how many tests you ran, and it's shown next to the raw numbers.
- **Half-life.** How long the gap takes to close halfway. A statistically perfect
  relationship that takes three years to converge is untradeable.

### Reading the z-score chart

The **z-score** is how far the gap is from its normal level, in standard
deviations. Beyond ±2 is unusual. The right-hand portion of the chart is out of
sample — the fit never saw it — so that's the part worth judging.

**The usual result is that nothing survives.** That is the screen working, not
failing.

---

## The colour key

| Colour | Meaning |
| --- | --- |
| **Green** | Calls, buying, added positions |
| **Red** | Puts, selling, trimmed positions |
| **Periwinkle blue** | The main series where there's no direction to show |
| **Lilac** | The second series in the same kind of chart |
| **Teal** | The third series, and this ticker's implied-vol reference line |
| **Amber** | The market-wide (VIX) line, the one comparison that is not about this stock |
| **Grey dashed** | Spot price, zero lines, theoretical comparisons |

The volatility surface uses a continuous colour scale where colour means
magnitude, not direction.

---

## Exporting

There are two, and they are for two different readers.

**Build PDF report** — for you. It regenerates every chart at full resolution and
lays them out as a document. It is not a screenshot: nothing is cut off at the
window edge, and collapsed sections are included. It takes 20–40 seconds because
each chart is re-rendered. When it finishes, a **Download PDF** button appears
beneath it.

**Download markdown (for AI)** — for a model. Same content, but a model cannot
see a picture, so every chart is written out as the numbers behind it: each line
named, with its axis labels and its values. Long series are thinned to sixty
evenly spaced samples, which keeps the shape and keeps the file small enough to
paste into a chat. Download it and hand it to Claude or ChatGPT with a question
like "what is this options chain pricing in?". There is no build step — it is
always current.

Both exports contain whatever is on the page at the time, the cointegration
screen included, so run the screen first if you want it in there.

---

## Glossary

**ATM / ITM / OTM** — at / in / out of the money. See Part 0.

**Bonferroni threshold** — a stricter significance bar that accounts for how many
statistical tests you ran.

**Call** — the right to buy 100 shares at the strike price.

**Cointegration** — two prices whose gap is stable even though each wanders.

**Delta** — how much an option's price moves per $1 move in the stock.

**Expiry / expiration** — the date the contract settles.

**Forward** — the price for delivery at expiry, adjusted for interest and
dividends.

**Gamma** — how fast delta changes as the stock moves.

**Implied volatility (IV)** — the volatility figure implied by an option's market
price. A forecast.

**Log-moneyness** — ln(strike ÷ forward). A scale-free way of saying how far a
strike is from the money.

**Open interest** — contracts currently held open. Updates overnight.

**OPEX** — the monthly expiration, the third Friday. Where most open interest sits.

**Put** — the right to sell 100 shares at the strike price.

**Realised volatility** — volatility measured from what actually happened.

**Risk-neutral density** — the probability distribution implied by option prices.
"Risk-neutral" because it blends real probabilities with what people will pay to
avoid risk — the market's fear of a crash inflates the left tail beyond the true
odds. Read it as *what the market charges for*, not *what will happen*.

**Skew** — the pattern of implied volatility across strikes. Usually higher for
downside strikes.

**Spot** — the current share price.

**Straddle** — a call and a put at the same strike. Its price is the market's
expected absolute move.

**Strike** — the fixed price at which an option lets you trade.

**Term structure** — how implied volatility varies with time to expiry.

**Volatility risk premium** — implied volatility minus the volatility that
subsequently arrived. Usually positive.

**Yang-Zhang** — an efficient way of measuring realised volatility that uses the
open, high, low and close rather than closes alone.

---

## Six honest limitations

1. **Everything is delayed.** Quotes by ~15 minutes, open interest overnight, 13F
   holdings by up to 45 days. Nothing here is real time.
2. **Outside US market hours there are no live quotes.** For some hours after
   the close you see the closing book; overnight, prices fall back to each
   strike's last trade, which can be days old on illiquid strikes. The line under
   the title says which, and the dashboard warns you in the second case; take the
   warning seriously.
3. **Gamma exposure rests on an unverifiable assumption** about who is on which
   side of the trade.
4. **The implied distribution's tails are extrapolation** beyond the quoted strike
   range.
5. **The 13F roster is a fixed list**, not the whole institutional market.
6. **None of this is predictive.** These are descriptions of current prices and
   past filings. A market-implied 10% chance of a fall is a statement about what
   options cost today, not about what will occur.

---

*Educational tool, not investment advice.*
