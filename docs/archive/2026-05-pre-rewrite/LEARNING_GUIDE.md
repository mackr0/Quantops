# QuantOpsAI — The Complete Guide

**Read a chapter a day. By the end you'll understand every number, every strategy, and every decision the system makes.**

---

## Chapter 1: The Dashboard — Your Home Base

The dashboard shows you what's happening right now across all your trading profiles.

### Equity

The total value of your account: cash plus the current market value of everything you hold. If you deposited $10,000 and your equity is $10,200, you're up $200. This number moves throughout the day as stock prices change.

### Buying Power

How much cash is available to open new positions. This is NOT the same as your cash balance — Alpaca may reserve some for pending orders or margin requirements. Think of equity as "what you're worth" and buying power as "what you can spend right now."

### Cash

Actual dollars sitting uninvested. When the AI buys a stock, cash goes down. When it sells, cash goes up. You want some cash available so the system can act on new opportunities.

### Open Positions

Every stock the system currently holds. Each row shows:

- **Symbol** — the ticker (AAPL, IONQ, etc.)
- **Qty** — how many shares
- **Price** — what you paid per share (your entry price, also called cost basis)
- **AI Conf** — how confident the AI was when it decided to buy (0-100%). Higher is better but even 60% trades can win.
- **P&L** — profit or loss since you bought. Green with a + means you're up. Red with a - means you're down. The percentage tells you how much the position has moved relative to your entry.
- **"unrealized"** — this gain/loss isn't locked in yet. It changes every 15 seconds as the price moves. It only becomes "realized" when you sell.

Click any row to expand it and see the AI's reasoning for entering the trade, plus the stop-loss and take-profit levels.

### Pending Orders

Orders that have been submitted to Alpaca but haven't filled yet. This happens when you use limit orders (waiting for a specific price) or when orders are submitted outside market hours and are queued for the next session.

- **TIF** (Time in Force) — how long the order stays active. "DAY" means it cancels at market close if unfilled. "GTC" means it stays until you cancel it.

### Profile Schedule Status

Shows whether each profile is currently scanning for trades. The schedule types:

- **Market Hours** — 9:30 AM to 4:00 PM Eastern, Monday through Friday. This is when the major US exchanges are open.
- **Extended Hours** — 4:00 AM to 8:00 PM Eastern. Includes pre-market and after-hours sessions where you can still trade but with less liquidity (fewer buyers and sellers, so prices can jump more).
- **24/7** — always running. Used for crypto markets which never close.

---

## Chapter 2: The Trades Page — Your Complete History

Every trade the system has ever made, newest first. Click any column header to sort.

### Reading a Trade Row

- **Time** — when the order was submitted, shown in Eastern Time (ET) to match market hours
- **Profile** — which of your trading profiles executed this trade (Mid Cap, Small Cap, Large Cap)
- **Symbol** — the stock ticker
- **Side** — BUY means you opened a long position (betting the price goes up). SELL means you closed it.
- **Qty** — number of shares
- **Price** — the price per share at the time of the trade
- **AI Conf** — the AI's confidence level when making this decision. This is NOT a prediction of how much money you'll make — it's how strongly the AI believed this was a good trade given all available information.
- **P&L** — profit or loss in dollars and percentage. On a BUY row, this is the realized gain/loss from the complete round-trip (buy then sell). On a SELL row, it's the realized P&L at the moment of exit.

### The Expanded Detail Row

Click any trade to see:

- **AI Reasoning** — the actual explanation the AI wrote for why it made this trade. This is the most valuable thing to read. Over time you'll start to see patterns in what the AI gets right vs wrong.
- **Stop** — the price at which the system would automatically sell to limit losses (your downside protection)
- **Target** — the price at which the system would automatically sell to lock in profits
- **Slippage** — the difference between the price the AI saw when deciding and the price you actually got when the order filled. Lower is better. Anything under 0.1% is excellent. Above 0.5% means you're paying a meaningful cost to enter/exit.

### What Makes a Good Trade History?

Look for:
- More green than red (winning trades outnumber losers)
- Average winners larger than average losers (even a 40% win rate is profitable if winners are 3x the size of losers)
- Consistent AI reasoning that makes sense in hindsight — not random

---

## Chapter 3: Performance — Executive Summary

This is the report card. Everything rolls up into these numbers.

### Total Return

Your overall profit or loss as a percentage. Started with $10,000, now at $10,500? That's +5.0%.

**What's good:** Any positive number in the first few months. Institutional funds target 15-25% per year. If you're beating 10% annualized after fees and slippage, you're outperforming most professional money managers.

### Annualized Return

Your return scaled to a full year. If you made 3% in one month, that's roughly 36% annualized (3% x 12). This lets you compare short-period results fairly against longer benchmarks.

**Caution:** Annualized returns from short periods are unreliable. A good week annualizes to absurd numbers. Trust this metric only after 90+ days of trading.

### Sharpe Ratio

The single most important number in quantitative finance. It measures **return per unit of risk**.

Formula: (your return - risk-free rate) / volatility of your returns

In plain English: how much are you getting paid for the ups and downs you're experiencing?

| Sharpe | What It Means |
|---|---|
| Below 0 | You're losing money |
| 0 to 0.5 | Poor — not enough return for the risk |
| 0.5 to 1.0 | Acceptable — most mutual funds live here |
| 1.0 to 2.0 | Good — better than most hedge funds |
| 2.0 to 3.0 | Excellent — top-tier quantitative strategies |
| Above 3.0 | Exceptional — either very skilled or not enough data yet |

**Important:** A Sharpe of 4.0 after 10 trades means almost nothing. You need 60+ trading days for this number to stabilize.

### Sortino Ratio

Like Sharpe, but only counts *downside* volatility. A stock that goes up a lot and rarely goes down has a high Sortino even if it's volatile — because the volatility is in your favor.

**Why it matters:** Sharpe penalizes you for big up days (they count as "risk"). Sortino doesn't. Most traders prefer Sortino because upside volatility is a good thing.

**What's good:** Same scale as Sharpe. Above 2.0 is strong.

### Max Drawdown

The worst peak-to-trough decline your account experienced. If your account went from $10,500 to $9,800 before recovering, your max drawdown is 6.7%.

This is the number that makes or breaks a strategy. A strategy that returns 30% per year but has 50% drawdowns will get shut down — because the drawdown might happen first, and you'll quit before the recovery.

| Max Drawdown | What It Means |
|---|---|
| Under 5% | Very conservative — capital well protected |
| 5-10% | Moderate — typical of a well-managed strategy |
| 10-20% | Aggressive — acceptable if returns justify it |
| 20-30% | Dangerous — hard to recover from psychologically |
| Above 30% | Strategy may have a structural problem |

### Calmar Ratio

Annualized return divided by max drawdown. Answers the question: "How much return am I getting per unit of worst-case pain?"

| Calmar | What It Means |
|---|---|
| Below 0.5 | Poor return for the risk taken |
| 0.5 to 1.0 | Acceptable |
| 1.0 to 2.0 | Good |
| Above 2.0 | Excellent — strong return with controlled drawdown |

### Gross vs Net Return

- **Gross** — your return if every order filled at exactly the price the AI saw (theoretical best case)
- **Net** — your actual return after slippage (reality)

The gap between them is the cost of execution. If gross is +8% but net is +5%, slippage is eating 3% of your performance. That's a signal to consider limit orders or more liquid names.

---

## Chapter 4: Risk & Stability — How Bad Can It Get?

This tab answers the question every professional investor asks first: "What's my downside?"

### Annualized Volatility

How much your daily returns swing, scaled to a year. Think of it as the "bumpiness of the ride."

| Volatility | What It Feels Like |
|---|---|
| Under 10% | Very smooth — like a savings account with some variance |
| 10-20% | Moderate — typical for a balanced portfolio |
| 20-30% | Bumpy — similar to holding the S&P 500 directly |
| Above 30% | Roller coaster — expect big daily swings |

### Value at Risk (VaR) at 95%

"95% of the time, my worst daily loss will be less than X%."

If VaR is -2.1%, that means on 95 out of 100 trading days, you won't lose more than 2.1% in a single day. On the other 5 days, you might lose more.

**What's good:** VaR under -3% means your day-to-day risk is well controlled.

### Conditional VaR (CVaR) at 95%

VaR tells you the boundary. CVaR tells you **how bad the bad days actually are** on average.

If CVaR is -3.5%, that means on the 5% of days when things go wrong (past the VaR boundary), you lose an average of 3.5%.

CVaR is always worse than VaR. The gap between them tells you about "tail risk" — the risk of rare but severe losses.

### Max Drawdown Duration

How many trading days it took to recover from the worst drawdown. A 10% drawdown that recovers in 5 days is very different from one that takes 60 days.

**What's good:** Under 30 days. If recovery takes longer than 2 months, the strategy may be broken, not just unlucky.

### Rolling Sharpe and Rolling Returns

These charts show whether your performance is **stable or deteriorating**. A flat or rising rolling Sharpe means the strategy is consistently working. A declining rolling Sharpe is an early warning sign.

---

## Chapter 5: Trade Analytics — Your Track Record

### Win Rate

Percentage of trades that made money.

**Critical insight:** Win rate alone tells you almost nothing. A strategy that wins 30% of the time but makes 5x on winners what it loses on losers is extremely profitable. A strategy that wins 90% of the time but gives back everything on the 10% losers is a disaster.

| Win Rate | Context Needed |
|---|---|
| 30-40% | Fine IF profit factor is above 2.0 |
| 40-50% | Solid with decent win/loss ratio |
| 50-60% | Good — slight edge is all you need |
| 60-70% | Strong — but watch for big losers hiding |
| Above 70% | Suspicious — might be cutting winners too short |

### Profit Factor

Total dollars won divided by total dollars lost. This is the single best measure of a strategy's edge.

| Profit Factor | What It Means |
|---|---|
| Below 1.0 | Losing money — total losses exceed total gains |
| 1.0 to 1.2 | Breakeven after costs |
| 1.2 to 1.5 | Slight edge — viable but fragile |
| 1.5 to 2.0 | Solid edge — this is where most good strategies live |
| 2.0 to 3.0 | Strong edge |
| Above 3.0 | Exceptional — or not enough data yet |

### Expectancy per Trade

How much you expect to make (or lose) on each trade, on average. Calculated as: (win rate x average win) - (loss rate x average loss).

If expectancy is +$15 per trade and you make 200 trades per year, expected annual profit is $3,000. Simple but powerful.

### Win/Loss Ratio

Average winning trade divided by average losing trade. If your average win is $50 and average loss is $30, your win/loss ratio is 1.67.

**What matters:** Win/Loss Ratio x Win Rate should be well above 1.0. A 50% win rate with 2:1 win/loss ratio is very profitable (expected value = 0.5 x $2 - 0.5 x $1 = +$0.50 per dollar risked).

### Streaks

- **Current streak** — are you on a hot or cold run right now?
- **Max consecutive wins/losses** — how extreme the streaks have gotten

Streaks matter psychologically. Even a profitable strategy will have 5-7 consecutive losers from time to time. Knowing this in advance prevents you from panicking and shutting off the system during a normal losing streak.

---

## Chapter 6: Market Relationship — Are You Actually Beating the Market?

### Alpha

Your excess return above what the market delivered. If the S&P 500 returned 10% and you returned 15%, your alpha is approximately 5%.

**Why it matters:** If your strategy just buys volatile stocks and the market goes up, you'll make money — but so would anyone. Alpha measures what YOU added beyond what the market gave for free.

| Alpha | What It Means |
|---|---|
| Negative | Underperforming the market — you'd be better off buying SPY |
| 0 to 2% | Slight edge, might be noise |
| 2-5% | Meaningful alpha — you're adding value |
| 5-10% | Strong — most hedge funds would be thrilled |
| Above 10% | Exceptional or early-stage (small sample) |

### Beta

How much your portfolio moves with the market. A beta of 1.0 means you move exactly with the S&P 500. A beta of 0.5 means you move half as much.

| Beta | What It Means |
|---|---|
| 0.0 | No relationship to the market (market-neutral) |
| 0.0 to 0.5 | Low market sensitivity — good for diversification |
| 0.5 to 1.0 | Moderate — moves with the market but less |
| 1.0 | Moves exactly with the market |
| Above 1.0 | More volatile than the market — amplifies moves |

**Ideal:** Low beta + positive alpha = you're making money independently of what the market does.

### Correlation

How closely your returns follow SPY, QQQ, or BTC. Correlation ranges from -1.0 (perfectly opposite) to +1.0 (perfectly together).

| Correlation | What It Means |
|---|---|
| Below 0.3 | Low — your returns are doing their own thing (good!) |
| 0.3 to 0.7 | Moderate — some market influence |
| Above 0.7 | High — your strategy is mostly riding the market |

**Why low correlation is valuable:** When the market crashes, a low-correlation strategy holds up. That's the whole point of running your own strategy instead of buying an index fund.

### Net and Gross Exposure

- **Net Exposure** = long positions minus short positions, as a percentage of equity. +80% means you're strongly betting on stocks going up. 0% means you're market-neutral (equal longs and shorts).
- **Gross Exposure** = total positions regardless of direction. Measures how much capital is deployed. 120% means you're using some leverage.

---

## Chapter 7: Scalability — Can This Strategy Grow?

### Slippage

The gap between the price the AI saw when it decided to trade and the price you actually got when the order filled. If the AI saw $100.00 and you filled at $100.05, that's 0.05% slippage.

**Why it matters:** Every trade costs slippage. It's an invisible tax. At small account sizes ($10K), slippage is negligible — a few cents per trade. At large sizes ($1M+), you're moving enough stock that your own orders push the price against you.

### Market Impact (Square Root Model)

As your position sizes grow, slippage doesn't grow linearly — it grows with the **square root** of size. Doubling your capital increases slippage by about 1.4x, not 2x. This is the industry-standard market impact model.

### The Scaling Projection Table

Shows two scenarios at each capital level:

- **Market orders** — you take whatever price is offered. Fast but expensive at scale.
- **Limit orders** — you name your price and wait. Saves ~60% on slippage but you might not get filled if the price runs away.

The **Profile** column shows which type of stocks you should be trading at each scale. The key insight: as your account grows, you migrate to larger, more liquid stocks. Small-cap names that work great at $10K would cause massive slippage at $1M. Mid-cap and large-cap names trade 10-100x more volume, absorbing your orders without moving the price.

### Slippage vs Gross Profit

What percentage of your gross (theoretical) profit is eaten by slippage. Target: under 20%. If slippage is eating 30%+ of your profits, you need better execution (limit orders) or more liquid names.

---

## Chapter 8: AI Intelligence — How Smart Is the AI?

### Prediction Win Rate vs Trade Win Rate

These are different:

- **Prediction Win Rate** — of all the predictions the AI made (BUY/SELL/HOLD), how often was the direction correct? This includes predictions it made but didn't trade on.
- **Trade Win Rate** — of the trades actually executed, how many made money?

The prediction win rate should be higher because the system filters — it only trades the predictions it's most confident about.

### Confidence Bands

The AI assigns a confidence percentage (0-100%) to each prediction. The "Accuracy by Confidence Band" table shows how well the AI performs at different confidence levels.

**What to look for:** Win rate should increase with confidence. If 80%+ confidence trades win 65% of the time but 40% confidence trades also win 65%, the confidence score isn't meaningful and needs recalibration.

### Self-Tuning Status

The self-tuning system watches the AI's own track record and adjusts parameters automatically. It needs **20 resolved predictions** per profile before it starts adjusting — predictions take about 5 trading days to resolve (the system checks if the predicted direction actually happened).

Once active, it can:
- Raise or lower the AI confidence threshold
- Adjust stop-loss and take-profit percentages
- Review its own past adjustments and reverse ones that made things worse

### Meta-Model (Phase 1)

A second AI that learns *when the first AI is likely to be wrong*. It trains on the features (technical indicators, market conditions) present when the AI made correct vs incorrect predictions. Think of it as a quality filter on top of the AI's output.

Needs 100+ resolved predictions to train. Currently collecting data.

---

## Chapter 9: The 16 Strategies — What Each One Does

The system runs multiple strategies simultaneously and combines their votes. Each strategy independently scans the universe of stocks and says BUY, SELL, or HOLD. When multiple strategies agree, the signal is stronger.

### Scoring System

| Agreement | Signal |
|---|---|
| 2+ strategies say BUY | STRONG_BUY |
| 1 strategy says BUY | BUY |
| No consensus | HOLD |
| 1 strategy says SELL | SELL |
| 2+ strategies say SELL | STRONG_SELL |

### The Core Strategies

**1. Market Structure Engine** — The primary strategy for each cap tier. Combines 4 sub-strategies (mean reversion, volume spike, gap-and-go, momentum continuation for small caps; different mixes for mid and large caps) and scores them.

**2. Insider Buying Cluster** — Watches for company insiders (CEOs, directors) buying their own stock. When multiple insiders buy within a short window, it often signals they know something the market doesn't yet.

**3. Earnings Drift** — After a company reports earnings, the stock often continues drifting in the direction of the surprise for days or weeks. This strategy catches that drift.

**4. Volatility Regime** — Detects when a stock's volatility is expanding or contracting and positions accordingly. Low volatility often precedes big moves (the "coiled spring" effect).

**5. Max Pain Pinning** — Options market makers have a financial incentive to push stock prices toward the "max pain" level (the price where the most options expire worthless). This strategy trades the convergence toward max pain near options expiration.

**6. Gap Reversal** — When a stock gaps down at the open on high volume but holds support, it often reverses hard. This strategy catches the bounce.

### The Expanded Library

**7. Short-Term Reversal** — Stocks that dropped sharply over 3-5 days tend to bounce back (mean reversion over very short periods). Based on academic research by Jegadeesh and Lehmann.

**8. Sector Momentum Rotation** — Money flows between sectors (tech, healthcare, energy, etc.) in waves. When a sector shows relative strength, this strategy overweights stocks in that sector.

**9. Analyst Revision Drift** — When analysts upgrade a stock, the price tends to keep rising for days afterward as more investors notice the upgrade. The reverse happens with downgrades.

**10. 52-Week Breakout** — When a stock breaks above its highest price in the past year on high volume, it often signals the start of a new trend. Volume confirmation filters out false breakouts.

**11. Short Squeeze Setup** — When a heavily-shorted stock starts rising, short sellers are forced to buy (to cover their losses), which pushes the price up further, which forces more covering. This strategy identifies the conditions for a squeeze before it starts.

**12. High IV Rank Fade** — When a stock's implied volatility (from options pricing) is at historic highs, it often contracts back to normal. This strategy positions for the volatility contraction.

**13. Insider Selling Cluster** — The bearish mirror of insider buying. When multiple insiders sell at the same time, it can signal trouble ahead.

**14. News Sentiment Spike** — Detects sudden shifts in news sentiment (very positive or very negative) and trades the momentum before the broader market fully reacts.

**15. Volume Dry-up Breakout** — When volume contracts to very low levels (a "dry-up") and then suddenly expands, it often signals a new move. The dry-up means selling pressure has exhausted itself.

**16. MACD Cross with Confirmation** — The classic MACD indicator (moving average convergence/divergence) crossover, but with additional confirmation from price being above SMA50 and histogram turning positive. Reduces false signals.

---

## Chapter 10: Risk Management — Your Safety Net

### Stop-Loss

A price level below your entry that triggers an automatic sell. If you buy at $100 with a 3% stop-loss, the system sells if the price drops to $97. Purpose: limit how much you can lose on any single trade.

**ATR-based stops** are smarter — instead of a fixed percentage, they use the stock's actual daily price range (Average True Range). A volatile stock gets a wider stop so it doesn't get triggered by normal fluctuations. A calm stock gets a tighter stop.

### Take-Profit

The opposite of stop-loss: a price level above your entry that triggers an automatic sell to lock in gains. If you buy at $100 with a 10% take-profit, the system sells at $110.

**The tradeoff:** Fixed take-profit caps your upside on runaway winners. The "Conviction Take-Profit Override" feature addresses this — when the AI still has high confidence and the trend is intact (strong ADX, making new highs), it skips the fixed TP and lets the trailing stop manage the exit instead.

### Trailing Stop

Once a trade is profitable, the trailing stop follows the price up (for longs) but never moves back down. If the price rises from $100 to $120 and then reverses, the trailing stop might be at $115 — locking in $15 of the $20 gain even if the stock falls.

The **trailing ATR multiplier** controls how tight the trail is. 1.5x ATR means the stop trails 1.5 times the stock's average daily range behind the current price. Tighter = more profit protection but more chance of getting stopped out on normal fluctuations.

### Drawdown Protection

- **Drawdown Reduce** (default 10%): When your account drops 10% from its peak, position sizes are automatically cut in half. This slows the bleeding.
- **Drawdown Pause** (default 20%): When your account drops 20% from its peak, all trading stops completely. This is the emergency brake.

### Correlation Management

**Max Correlation** (default 0.7): Prevents the system from holding too many stocks that move together. If you already hold three tech stocks that are 80% correlated, adding a fourth is just concentrating risk, not diversifying.

**Max Positions per Sector** (default 5): Hard cap on how many stocks from the same sector you can hold.

---

## Chapter 11: The Specialist Ensemble — Your AI Team

Instead of relying on a single AI analysis, the system uses four specialized AIs that each evaluate candidates from a different angle, then synthesizes their opinions.

### The Four Specialists

**Earnings Analyst** — Focuses on the company's financial fundamentals. Reads recent earnings reports, revenue trends, guidance. Abstains on symbols it can't assess (no recent earnings data).

**Pattern Recognizer** — Reads technical chart patterns. Looks at price action, volume patterns, support/resistance levels, momentum indicators. This is pure chart analysis.

**Sentiment & Narrative** — Evaluates the story around the stock. News sentiment, sector headwinds/tailwinds, macro environment. What's the narrative the market is trading on?

**Risk Assessor** — The skeptic. Evaluates what could go wrong: concentration risk, regulatory exposure, event risk, regime sensitivity. Has **veto power** — if the risk assessor vetoes a trade, it's dropped regardless of what the other three say.

### How They Combine

Each specialist gives a verdict (BUY, SELL, HOLD, or ABSTAIN) with a confidence score. The ensemble synthesizes these into a consensus verdict. A trade where 3 out of 4 specialists agree BUY with 70%+ confidence is much stronger than a trade where only the pattern recognizer likes it.

---

## Chapter 12: Crisis Detection — The Emergency System

The system monitors six cross-asset signals that historically precede market crises:

1. **VIX Level** — The "fear index." Measures expected market volatility. Above 22 = elevated, above 32 = crisis, above 45 = severe.

2. **VIX Term Inversion** — Normally, longer-term volatility is higher than short-term. When this inverts (short-term higher), the market is pricing in immediate danger.

3. **Cross-Asset Correlation Spike** — In normal markets, stocks, bonds, and gold move somewhat independently. When everything starts moving together (correlation above 0.75), it means panic selling is hitting all asset classes.

4. **Bond/Stock Divergence** — When bonds (TLT) rally sharply while stocks (SPY) fall, it signals a "flight to safety" — professional money is running from risk.

5. **Gold Safe-Haven Rally** — When gold rallies 3%+ in 5 days, it signals fear. Gold is the oldest safe-haven asset.

6. **Credit Spread Stress** — When high-yield corporate bonds (HYG) drop relative to investment-grade bonds (LQD), it signals that the market is pricing in corporate defaults.

### Crisis Levels

| Level | Position Sizes | New Trades |
|---|---|---|
| Normal | 100% | All allowed |
| Elevated | 50% | Reduced but allowed |
| Crisis | 0% | No new longs; sells/shorts only |
| Severe | Liquidation | All positions closed |

---

## Chapter 13: Settings — Tuning Your Machine

### AI Confidence Threshold

Minimum confidence the AI must have before approving a trade (default: 25%). Lower = more trades but lower quality. Higher = fewer trades but higher conviction.

**Recommendation:** Start at 25% to generate enough data for self-tuning. After 100+ trades, raise to 40-50% if win rate supports it. The self-tuner will also adjust this automatically.

### Screener Parameters

These filter the 8,000+ stock universe down to ~15 candidates:

- **Min/Max Price** — the price range for this profile. Small Cap: $5-$20. Mid Cap: $20-$100. Large Cap: $50-$500.
- **Min Volume** — minimum daily shares traded. Higher = more liquid = lower slippage. Default 500,000.
- **Volume Surge Multiplier** — how much above average volume a stock needs to be to get flagged. Default 2.0x. Higher = fewer but stronger signals.
- **RSI Overbought/Oversold** — RSI (Relative Strength Index) measures momentum on a 0-100 scale. Above the overbought level, a stock may be due for a pullback. Below oversold, it may be due for a bounce.

### ATR Multipliers

ATR (Average True Range) measures how much a stock typically moves in a day.

- **Stop-Loss multiplier** (default 2.0x): Your stop is set 2x the daily range below entry. If a stock moves $1/day on average, your stop is $2 below where you bought.
- **Take-Profit multiplier** (default 3.0x): Your target is set 3x the daily range above entry. This gives you a 1.5:1 reward-to-risk ratio by default (3x target / 2x stop).
- **Trailing multiplier** (default 1.5x): Once profitable, the trailing stop follows 1.5x daily range behind the price.

### MAGA Mode

When enabled, the AI factors political news (tariffs, executive orders, congressional actions) into its analysis. It looks for stocks that might benefit from or be hurt by political developments, and adjusts its trades accordingly. Particularly useful during periods of high political volatility affecting markets.

---

## Chapter 14: Alpha Decay — Why Strategies Die

Every trading edge eventually stops working. This is alpha decay.

Reasons edges decay:
- Other traders discover the same pattern and trade it away
- Market microstructure changes (new regulations, new exchange rules)
- The economic regime shifts (what works in low interest rates fails in high rates)

The system monitors each strategy's rolling 30-day Sharpe ratio against its lifetime average. If the rolling Sharpe drops 30%+ below the lifetime average for 30 consecutive days, the strategy is automatically deprecated (removed from active trading). If it recovers for 14 consecutive days, it's restored.

**This is one of the most important features.** Most trading systems cling to dead strategies forever because nobody measures decay. This system kills them automatically.

---

## Chapter 15: The Numbers That Matter Most

If you remember nothing else from this guide, remember these five metrics and their targets:

| Metric | Target | Why |
|---|---|---|
| **Sharpe Ratio** | Above 1.5 | Are you getting paid enough for the risk? |
| **Max Drawdown** | Under 15% | Can you survive the worst period? |
| **Profit Factor** | Above 1.5 | Are winners meaningfully larger than losers? |
| **Win Rate** | Above 45% | Combined with profit factor, are you making money? |
| **Alpha** | Positive | Are you actually beating the market, or just riding it? |

Everything else is diagnostic detail. These five tell you whether the strategy is working.

---

## Glossary

**ADX** — Average Directional Index. Measures trend strength on a 0-100 scale. Below 20 = no trend. 20-25 = emerging trend. Above 25 = established trend. Above 40 = very strong trend.

**ATR** — Average True Range. The average daily price movement of a stock, measured in dollars. A $100 stock with ATR of $3 moves about $3 per day on average.

**Cost Basis** — What you paid for a position. If you bought 10 shares at $50, your cost basis is $500.

**Drawdown** — A decline from a peak to a trough in account value. A 10% drawdown from a $10,500 peak means your account dropped to $9,450 before recovering.

**FIFO** — First In, First Out. When you sell shares, the system attributes the sale to the oldest shares you bought first. This is standard accounting practice.

**Liquidity** — How easily a stock can be bought or sold without moving the price. High-volume stocks (AAPL, MSFT) are very liquid. Low-volume penny stocks are illiquid.

**Long** — Buying a stock expecting it to go up. You profit when the price rises.

**MACD** — Moving Average Convergence Divergence. A momentum indicator that shows the relationship between two moving averages. When MACD crosses above its signal line, it's bullish. Below = bearish.

**MFI** — Money Flow Index. Combines price and volume to measure buying/selling pressure. Similar to RSI but accounts for volume. Above 80 = overbought. Below 20 = oversold.

**P&L** — Profit and Loss. Your gains or losses on a trade or portfolio.

**RSI** — Relative Strength Index. Momentum oscillator on a 0-100 scale. Above 70 is traditionally overbought (may pull back). Below 30 is oversold (may bounce). The system's thresholds are configurable.

**Short** — Selling a stock you don't own (borrowing shares), expecting the price to drop. You profit when the price falls. Risk: unlimited losses if price rises.

**Slippage** — The difference between expected and actual execution price. Always costs you money. Caused by the bid-ask spread and your order's impact on the market.

**SMA** — Simple Moving Average. The average closing price over N days. SMA20 = last 20 days. Price above SMA20 suggests an uptrend. Below suggests a downtrend.

**StochRSI** — Stochastic RSI. An indicator of the indicator — measures where RSI is within its own recent range. More sensitive than RSI alone. 0-20 = oversold, 80-100 = overbought.

**VaR** — Value at Risk. The maximum expected loss at a given confidence level over a given time period. "95% VaR of -2%" means you expect to lose more than 2% on only 5% of trading days.

**VIX** — The CBOE Volatility Index. Measures the market's expectation of 30-day volatility in the S&P 500. Often called the "fear index." Normal: 12-18. Elevated: 18-25. High: 25-35. Crisis: 35+.

**VWAP** — Volume-Weighted Average Price. The average price a stock traded at throughout the day, weighted by volume. Institutional benchmark for execution quality. If you bought below VWAP, you got a good fill.
