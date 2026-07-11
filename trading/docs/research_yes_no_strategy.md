# Research Note: YES/NO Weather Trading Design

> Research record dated 2026-06-12. Examples are SFO-specific historical
> analysis; current operations cover fifteen city markets and use the shared
> maker-first portfolio scanner.

This note records the technical basis for the side-aware SFO Kalshi strategy.
It is not financial advice and it does not prove profitability. It explains the
math the program uses and why the risk gates are intentionally strict.

## Core Contract Math

A Kalshi binary contract pays `1.00` if the side you bought wins and `0.00` if
it loses.

For either BUY_YES or BUY_NO:

```text
edge = p_win - ask - fee
```

That is equivalent to:

```text
edge = p_win * (1 - ask) - (1 - p_win) * ask - fee
```

So the same expected-value formula works for both sides. The only difference is
the win probability and the side-specific entry price:

```text
BUY_YES p_win = P(bucket resolves YES)
BUY_NO  p_win = 1 - P(bucket resolves YES)
```

The conservative lower-bound edge is:

```text
edge_lcb = p_win_lcb - ask - fee
```

For BUY_NO, the program does not naively use `1 - YES_LCB`, because that would
be an upper-confidence idea for NO. It uses:

```text
no_lcb = max(0, 1 - yes_p - (yes_p - yes_lcb))
```

That subtracts the YES-side uncertainty from the NO probability and is stricter
when the bucket model is uncertain.

## Kalshi YES/NO Book Mapping

Kalshi's orderbook documentation says binary books are reciprocal: a YES bid at
`X` is economically a NO ask at `1 - X`, and a NO bid at `Y` is economically a
YES ask at `1 - Y`.

The code therefore treats the selected side generically:

```text
YES entry ask = yes_ask
YES exit bid  = yes_bid

NO entry ask  = no_ask
NO exit bid   = no_bid
```

When the public market payload does not expose explicit NO sizes, the program
uses the reciprocal top-of-book size proxy:

```text
NO bid size proxy = YES ask size
NO ask size proxy = YES bid size
```

This follows the same reciprocal orderbook relationship. It is still only a
top-of-book proxy, so the liquidity gates remain conservative.

## Fees

Kalshi's 2026 fee schedule gives the general taker fee as:

```text
fee = round_up(0.07 * contracts * price * (1 - price))
```

The program estimates fees with that quadratic formula and rounds up to the next
cent. A 1-cent contract can still have a 1-cent fee for one contract, which is
why penny tails are not automatically attractive.

## Probability Research Basis

The strategy should optimize calibrated probabilities, not raw win streaks.

- Glenn Brier's 1950 probability-forecast verification paper is the foundation
  for Brier score style evaluation.
- Gneiting and Raftery emphasize proper scoring rules and the calibration versus
  sharpness goal for probabilistic forecasts.
- Ensemble weather research from Gneiting/Raftery and Raftery et al. supports
  using ensembles for uncertainty, but with statistical post-processing because
  raw ensembles can be biased or underdispersed.

This matches the current design: the center forecast remains station-aligned,
while the Open-Meteo GFS ensemble affects the distribution shape and uncertainty.

## Market Research Basis

Prediction-market prices are useful information but not truth.

- Wolfers and Zitzewitz describe prediction markets as aggregators of dispersed
  information and as useful probability signals for well-designed contracts.
- Manski's critique is a reminder that prediction market prices are not
  mechanically equal to objective probabilities under heterogeneous beliefs and
  risk constraints.

The program therefore uses market price as a cautious prior, while rejecting
large model/market disagreement.

## Liquidity Research Basis

Bid-ask spread is not noise. Glosten and Milgrom's market microstructure model
shows how information asymmetry can create positive spreads and make observed
returns diverge from realizable returns. For this project, that means:

- A cheap ask without bid support is not enough.
- Wide spreads are real transaction costs.
- Exit liquidity matters, especially before settlement.
- Top-of-book size should cap paper fill size.

## Sizing Research Basis

Kelly's 1956 information-rate paper supports bankroll sizing based on edge, but
full Kelly is too aggressive for a small, miscalibration-prone research system.
This project uses fractional Kelly plus hard position caps.

## Implemented Design

The current program supports:

- YES-only analysis by default.
- `analyze --side no` for NO-only ranking.
- `analyze --side both` for side-by-side YES/NO candidates.
- Side-aware paper order storage.
- Side-aware paper settlement.
- Side-aware early paper close using the stored side's live bid.
- Conservative NO lower-confidence probability.

## Sources

- [Brier 1950, Verification of Forecasts Expressed in Terms of Probability](https://journals.ametsoc.org/doi/10.1175/1520-0493%281950%29078%3C0001%3AVOFEIT%3E2.0.CO%3B2)
- [Gneiting and Raftery 2007, Strictly Proper Scoring Rules, Prediction, and Estimation](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)
- [Raftery et al. 2005, Using Bayesian Model Averaging to Calibrate Forecast Ensembles](https://journals.ametsoc.org/view/journals/mwre/133/5/mwr2906.1.xml)
- [Gneiting and Raftery 2005, Weather Forecasting with Ensemble Methods](https://pubmed.ncbi.nlm.nih.gov/16224011/)
- [Wolfers and Zitzewitz 2004, Prediction Markets](https://www.aeaweb.org/articles?id=10.1257%2F0895330041371321)
- [Manski 2006, Interpreting the Predictions of Prediction Markets](https://www.scholars.northwestern.edu/en/publications/interpreting-the-predictions-of-prediction-markets/)
- [Glosten and Milgrom 1985, Bid, Ask, and Transaction Prices](https://business.columbia.edu/faculty/research/bid-ask-and-transaction-prices-specialist-market-heterogeneously-informed-traders)
- [Kelly 1956, A New Interpretation of Information Rate](https://colab.ws/articles/10.1109/TIT.1956.1056803)
- [Kalshi Orderbook Responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi Fee Schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
