# BLACK BOX — Formalisation, Adversarial Review, Sweep Design

## 0. ASSUMPTIONS (all context fields were left [FILL])

Every cost, session and tick-size number below is conditional on these. Correct them and re-read Phase 3.3 and Phase 4.4.

| Field | Assumed | Confidence |
|---|---|---|
| Universe | NSE — liquid index futures and/or top-decile-turnover cash equities | Low — inferred, not stated |
| Marking TF / execution TF | 15m mark / 1m execute | Low — pure default |
| Data | Intraday OHLCV + trade prints; L2 depth snapshots possible but unconfirmed | Low |
| Date span | Unknown. Walk-forward in Phase 4.4 is given parameterised + one worked example | — |
| Execution | Discount broker, ₹20/order, stop and limit orders available | Low |
| Current results | None supplied. **Every claim below is [UNTESTED].** No claim in this document is in-sample or held-out, because no test has been run. | — |

Microstructure items I will not invent — verify before costing anything:

- Tick size per contract (₹0.05 is typical for cash and futures, but not universal).
- STT on futures sell side (revised in the 2024 budget; I believe 0.02% but do not trust me on this).
- Exchange transaction charges (revised 2024, slab-dependent).
- Closing mechanics: NSE equity close is a VWAP of the last 30 minutes, with a separate closing session. Whether your instrument settles to that, and the exact auction windows, must be checked — it determines your hard flat-time.
- Circuit/price-band behaviour and how your data vendor stamps halted bars.
- Whether your feed's "volume" is exchange-reported traded quantity or a vendor reconstruction.

---

## 1. FORMALISED SPEC TABLE (Phase 1.1)

`ATR(n)` = ATR on the marking timeframe, n = 14 unless stated. `tick` = instrument tick size.

### 1.1 "Strong rally / rundown" (the impulse)

| Def | Rule | Trade-off |
|---|---|---|
| **I-A** Displacement | `\|close[t] − close[t−N]\| ≥ k·ATR(n)`, N=20, k=2.5 | Simplest, cheapest. Fires on slow grinds that accumulate the same displacement without ever trapping anyone. |
| **I-B** Displacement × efficiency *(recommended)* | I-A **and** Kaufman ER = `\|close[t]−close[t−N]\| / Σ\|close[i]−close[i−1]\|` ≥ 0.50 | Matches what the eye calls "strong" — direct, low-retrace travel. Costs one parameter; ER is unstable for N < 10. |
| **I-C** Swing-leg | ZigZag leg with no counter-swing > q·ATR, q=1.0 | Visually faithful. **Confirmation lag** — the leg is only known several bars after it ends. Directly imports look-ahead unless timestamped correctly (see 2.1). |

### 1.2 "The level that started the move"

| Def | Rule | Trade-off |
|---|---|---|
| **L-A** Swing-fractal registry *(recommended)* | Every fractal high/low of order w=3 enters a rolling registry, **timestamped at bar t but usable only from bar t+w**. Score = `touches_W × exp(−age/τ) × dwell_fraction`. Multiple candidates → argmax score, tie-break most recent. | Forward-computable. Introduces 4 params (w, W, τ, touch tolerance) — a large fitting surface. |
| **L-B** Session boundaries only | Registry = {PDH, PDL, PDC, overnight H/L, IB H/L, session open} | Near-zero parameters. Highest cross-participant agreement → thickest stop clusters. Far fewer setups; also the most crowded and therefore most adversely selected. |
| **L-C** Volume node edge | Upper/lower edge of the dominant HVN in the prior balance window | Closest to the actual inventory story. Needs volume-at-price; bin width is a hidden, sensitive parameter. **[DATA GAP]** if OHLCV only. |

Selection must be committed **before** the impulse exists. See 2.1 — this is the whole ballgame.

### 1.3 "First breakout"

| Def | Rule | Trade-off |
|---|---|---|
| **B-A** Close-through *(recommended)* | `close` beyond level by ≥ b·ATR, b = 0.10 | Balanced. b must be ≥ 2·tick or it degenerates to noise on tight instruments. |
| **B-B** Wick-through | Any trade beyond the level | Maximum sample, minimum precision. Every level breaks constantly under this. |
| **B-C** Two-bar | Close beyond, and next bar does not close back inside | Highest precision, latest signal, smallest sample. Also delays the trigger line by one bar. |

"First" needs a reset clock: **first break of that level since the level entered the registry, or since the last INVALIDATED/EXITED resolution on that level.** Without this, every level generates unlimited "first" breaks.

### 1.4 "Shallow pullback, not a deep reversal"

**Finding: this term is largely redundant and is a fitting trap.** The trigger line already imposes the minimum depth (price must return below the line for a re-cross to exist) and the invalidation rule already imposes the maximum. Adding a separate depth classifier adds parameters that do nothing the other two rules don't.

| Def | Rule | Trade-off |
|---|---|---|
| **P-A** No classifier *(recommended)* | Arm on any return through the line; let invalidation kill it | Zero added params. Admits some deep-retrace setups the discretionary version would skip. |
| **P-B** Excursion cap | Sweep excursion beyond the line ≤ s·ATR, s = 1.0 | The one depth constraint with a mechanism behind it — a stop sweep is shallow *relative to the line*, not relative to the impulse. |
| **P-C** Time cap | Re-cross must occur within `T_max` bars of the break, T_max = 1.5 × impulse bar count | Bounds setup lifetime, which the embargo design needs anyway. Time stops are the easiest thing in this whole document to overfit. |

### 1.5 "Re-crosses the line"

| Def | Rule | Trade-off |
|---|---|---|
| **X-A** Stop order at line + tol | tol = `max(2·tick, 0.05·ATR)`, entry on trade-through | Conditions on the move actually happening. Pays spread + slip. |
| **X-B** Limit at the line | Resting limit, fill assumed only if price trades ≥ 1 tick **through** your price | Better price, severe adverse selection: you are preferentially filled when a large seller is working through. |
| **X-C** Close-confirmed | Close beyond line + tol, enter next bar open | Fewest false triggers, worst entry price, gap risk on the next open. |

Tolerance must be anchored to tick size and typical spread, never to a percentage of price.

### 1.6 "Next opposing S-R zone"

| Def | Rule | Trade-off |
|---|---|---|
| **Z-A** Prior swing ladder *(recommended)* | At entry, freeze the ordered list of fractal extremes above (long) / below (short) entry, drawn only from bars < entry bar | Forward-only, mechanical. Ladders can be sparse after a large impulse. |
| **Z-B** Session levels | PDH/PDL/PDC/weekly H-L | Very few free parameters, very coarse targets. |
| **Z-C** LVN ladder | Nearest low-volume nodes | **[DATA GAP]** without volume-at-price. |

The ladder is computed **once, at entry, and frozen**. Advancing rule: on a close beyond `zone_k`, target becomes `zone_{k+1}` and stop moves to `zone_{k−1}`. Any re-derivation of the target after seeing the run is hindsight, and it is how "no fixed R:R, trail to the next zone" is usually backtested into a fantasy.

### 1.7 Stop buffer

| Def | Rule | Trade-off |
|---|---|---|
| **S-A** Beyond line | `line − max(2·tick, 0.15·ATR)` | Tight → good R multiple, but stops out inside the sweep band the strategy claims to exploit. |
| **S-B** Beyond sweep extreme *(mechanism-aligned)* | `sweep_low − 1·tick` | This is where the thesis actually dies. Risk is unbounded on deep sweeps → needs a cap, and the cap is a new parameter. |
| **S-C** `min(S-B, line − c·ATR)` with cap c = 1.5 | Compromise; discards the deepest sweeps rather than sizing into them |

### 1.8 "New lower low" (invalidation reference)

Must be pinned to a named bar. Candidates:

| Ref | Definition | Verdict |
|---|---|---|
| **V-A** | Below the impulse **origin** low | Too loose — the setup survives long after the thesis is dead |
| **V-B** *(recommended)* | Below the swing low immediately preceding the breakout bar | Below this, the level the setup was built on has been fully reclaimed and the trap inverts |
| **V-C** | Below the pullback's own running low, i.e. every new low kills | Too tight — kills on the sweep itself, which is the entry condition |

---

## 2. LOOK-AHEAD AUDIT (Phase 1.2)

| # | Rule as written | Verdict | Forward-detectable replacement | What is lost |
|---|---|---|---|---|
| 1 | "Find the origin of a strong move. Trace back to the level it broke." | **[HINDSIGHT] — fatal as written** | **Invert the pipeline.** Maintain a rolling registry of all candidate levels → detect each break as it happens → require the impulse qualification (1.1) to complete within a bounded window after the break. The level is registered *before* the move exists. | You can no longer use "the move was strong" to *select* the level, only to *qualify* a break after the fact. You carry many candidate levels that lead nowhere. And the registry will pick a different level than your eye would in a large fraction of cases — **measure that divergence on 50 hand-marked charts before trusting anything downstream.** |
| 2 | "First break is bait, do not trade it" | [FORWARD-DETECTABLE] | Needs only the reset clock in 1.3 | Nothing |
| 3 | "Mark the trigger line" | [FORWARD-DETECTABLE] once #1 is fixed | — | Nothing |
| 3b | *Implicit:* swing/fractal levels | **[HINDSIGHT] if naively coded** | A fractal of order w is only confirmed w bars after its bar. Levels enter the registry at `t+w` carrying price from `t`. No rule may reference a level before `t+w`. | w bars of reaction time. This is the single most common silent leak in this family of strategies and it inflates results substantially. |
| 4 | "Wait for a shallow pullback, not a deep reversal" | **[HINDSIGHT]** as a judgment — you only know it was not a deep reversal after it stopped | Drop the classifier (P-A) or use the *forward* excursion cap (P-B), which evaluates bar by bar | The discretionary "this looks like a healthy pullback" gestalt. Honestly: it is not recoverable, and I doubt it survives contact with the base-rate test in 3.2 anyway. |
| 5 | "Entry on re-cross" | [FORWARD-DETECTABLE] | — | Nothing |
| 6 | "Stop just beyond entry plus a buffer" | [FORWARD-DETECTABLE] once 1.7 is pinned | — | Nothing |
| 7a | "Target = next opposing zone" | [FORWARD-DETECTABLE] **only if frozen at entry** | Z-A ladder, computed at entry from prior bars only | Nothing, if disciplined |
| 7b | "If price blows through, trail to the next zone" | **[HINDSIGHT-CONTAMINATED]** as normally implemented | Pre-computed ladder + mechanical advancement rule (1.6) | The ability to recognise "this one is really running" — which is exactly the recognition that does not exist in real time |
| 8 | "New lower low kills it" | [FORWARD-DETECTABLE] once V-B pins the reference | — | Nothing |

**Intrabar resolution.** On bar data, a pullback that overshoots the invalidation level and recovers within the same bar is indistinguishable from one that never did. Convention: **evaluate invalidation on bar extremes, entry on closes/trade-through.** This makes invalidation strictly first in a tie. It is the conservative choice. The opposite convention silently inflates results and is the second most common leak here. If you have tick or L1 data for the execution timeframe, resolve the sequence properly and *report how often the two conventions disagree* — that number is a direct measure of how much of your edge is a data-resolution artefact.

---

## 3. CAUSAL MECHANISM (Phase 1.3)

**The other side.** Breakout momentum buyers who entered on the first break. Their stops sit as resting sell-stops in a band immediately below the trigger line, because the near-universal convention is "the broken level should now hold as support, so risk goes just below it." Those stops are market orders. When triggered, they are aggressive sells providing liquidity to anyone wanting size long at a discount.

**Why the re-cross continues.** After the sweep, two supply sources are exhausted: the trapped longs have been flushed, and the sellers who defended the level were already consumed on the first break. The offer side above the line is thin. Modest buying pressure moves price disproportionately.

**Why it persists.** This is not a mispricing that arbitrage removes. It is a structural artefact of where a large, heterogeneous, uncoordinated population *must* put risk given the level is publicly visible. It decays where a level is not widely watched, or where the participant mix is dominated by algos that do not use visible-level stops.

**The falsifiable prediction — and your best test.** If the mechanism is real, the sweep leg must show: (a) a volume/aggression spike concentrated in the band below the line, (b) fast rejection (the excursion is resolved in few bars relative to the impulse), and (c) visible bid-depth depletion then replenishment, if you have L2. **If you find no aggression signature at the sweep, the mechanism is not operating** and you are trading generic post-breakout mean reversion with a narrative attached. That is a finding worth more than the entire parameter sweep, and it is cheap. Run it first.

**Honest caveat.** The mechanism predicts an edge on *fills*, not on *forecasts*. It says liquidity is temporarily cheap at the sweep. Whether that translates into a directional edge large enough to clear costs is a separate empirical question, and the mechanism does not guarantee it.

---

## 4. PARAMETER TABLE (Phase 1.4)

| Name | Where | Default | Units | Type |
|---|---|---|---|---|
| `mark_tf` | all level logic | 15m | — | **structural** |
| `exec_tf` | entry/stop | 1m | — | **structural** |
| `level_source` | 1.2 | L-A registry | enum {L-A, L-B, L-C} | **structural** |
| `fractal_w` | 1.2 | 3 | bars | tunable |
| `registry_W` | 1.2 | 120 | bars | tunable |
| `touch_tol` | 1.2 | max(2·tick, 0.10·ATR) | price | tunable |
| `recency_tau` | 1.2 | 60 | bars | tunable |
| `impulse_def` | 1.1 | I-B | enum | **structural** |
| `impulse_N` | 1.1 | 20 | bars | tunable |
| `impulse_k` | 1.1 | 2.5 | ATR | tunable |
| `impulse_ER` | 1.1 | 0.50 | ratio | tunable |
| `break_def` | 1.3 | B-A | enum | **structural** |
| `break_b` | 1.3 | 0.10 | ATR | tunable |
| `qual_window` | 2.1 | 20 | bars | tunable |
| `pullback_def` | 1.4 | P-A | enum | **structural** |
| `sweep_cap_s` | 1.4 | 1.0 | ATR | tunable |
| `arm_timeout` | 1.4 | 1.5× impulse bars | bars | tunable |
| `entry_mode` | 1.5 | X-A | enum {X-A, X-B, X-C} | **structural** |
| `recross_tol` | 1.5 | max(2·tick, 0.05·ATR) | price | tunable |
| `stop_mode` | 1.7 | S-C | enum | **structural** |
| `stop_buffer` | 1.7 | 0.15 | ATR | tunable |
| `stop_cap_c` | 1.7 | 1.5 | ATR | tunable |
| `target_mode` | 1.6 | Z-A | enum | **structural** |
| `ladder_depth` | 1.6 | 3 | zones | tunable |
| `invalidation_ref` | 1.8 | V-B | enum | **structural** |
| `time_stop` | F variant | 3× impulse bars | bars | tunable |
| `max_concurrent` | 5.5 | 1 | setups | **structural** |
| `flat_time` | 5.5 | 15:20 IST | clock | **structural** |

12 structural, 16 tunable. **The structural count is the problem** — each is a fork in strategy identity, and testing all combinations is a multiple-testing catastrophe. Fix them by argument (as above) before touching the tunables, and record the fixing decision so you cannot quietly revisit it after seeing results.

---

## 5. STATE MACHINE

```
SCANNING ──(level registered, t+w)──> LEVEL_ARMED
LEVEL_ARMED ──(qualified break, 1.3)──> BREAKOUT_SEEN
LEVEL_ARMED ──(level age > registry_W)──> EXPIRED_LEVEL
BREAKOUT_SEEN ──(impulse qualifies within qual_window)──> IMPULSE_CONFIRMED
BREAKOUT_SEEN ──(qual_window elapses, no impulse)──> DISCARDED [logged]
IMPULSE_CONFIRMED ──(price returns through line)──> PULLBACK_ACTIVE
IMPULSE_CONFIRMED ──(arm_timeout, no return)──> EXPIRED_NO_PULLBACK [logged]
PULLBACK_ACTIVE ──(re-cross per entry_mode)──> IN_TRADE
PULLBACK_ACTIVE ──(V-B breached)──> INVALIDATED [logged]
PULLBACK_ACTIVE ──(excursion > sweep_cap_s, if P-B)──> INVALIDATED [logged]
PULLBACK_ACTIVE ──(arm_timeout)──> EXPIRED_ARMED [logged]
IN_TRADE ──(stop / ladder target / time_stop / flat_time)──> EXITED [logged]
```

**Every terminal state is logged as a setup outcome.** This is not bookkeeping hygiene, it is the entire defence against the base-rate problem in 6.2.

### Edge cases — all must be decided before any test runs

| Case | Rule |
|---|---|
| **Multiple re-crosses** | Only the **first** is tradeable. Subsequent re-crosses are logged as a separate labelled cohort (`recross_index = 2, 3…`) so you can later test whether they carry edge. Do not assume either way, and do not silently include them. |
| **Overshoot + recovery within one bar** | Invalidation evaluated on bar extremes wins the tie (see §2). Log a flag `intrabar_ambiguous = true` and report the fraction. |
| **Gap through the level** | If no tick trades at or through the trigger price, **X-B (limit) cannot fill — void the setup, do not assume a fill.** X-A (stop) fills at the gap open with modelled slippage of ≥ (gap size × 0.25) or 3 ticks, whichever is larger. The 09:15 print is a pre-open auction result — never treat it as a tradeable cross. |
| **New Black Box while one is active** | Default `max_concurrent = 1`. Same-direction nested setup → ignore entirely (it is the same trade; taking both double-counts a single independent episode and corrupts the sample-size accounting in 7.2). Opposite-direction setup forming → treat as a **soft downgrade**: tighten stop to breakeven, do not flip. |
| **Session close while ARMED** | Disarm at `flat_time`. Do not carry a trigger overnight — the overnight gap destroys the stop-cluster geometry the setup depends on. |
| **Session close while IN_TRADE** | Flat at `flat_time` (default 15:20 IST, before the closing mechanics). Log as `EXITED_TIME`, not as a win or loss on the thesis. **Verify NSE closing-session windows for your instrument before fixing this time.** |
| **Halt / circuit / band freeze** | Void the setup. Vendor-stamped halted bars must not be treated as tradeable. |

---

## 6. VARIANTS (Phase 2)

Ten variants, one per axis, plus a second on axis A.

| # | Axis | Description | Mechanism | Params (range, why those bounds) | Data — do you have it? | Most likely failure | Cost |
|---|---|---|---|---|---|---|---|
| **V1** | A | Levels restricted to session boundaries (PDH/PDL/PDC/ON H-L/IB H-L) | Highest cross-participant agreement → thickest, most homogeneous stop clusters | `level_set` (enum only); no continuous params. Bound: none to fit — that is the point | OHLCV + session tags. **You did not state.** Assumed yes | Sample starvation (~2–6 levels/day/instrument), and these are the *most* crowded levels so most adversely selected | trivial |
| **V2** | A | Volume-node-edge level | Stops cluster where inventory was actually built, not where a wick printed | `bin_width` ∈ [0.25, 1.0]·ATR (below 0.25 the profile is noise; above 1.0 nodes merge); `node_pctile` ∈ [70, 90] | Volume-at-price. **[DATA GAP]** — see §11 | Bin width is a hidden high-leverage parameter that will silently absorb the fitting | moderate |
| **V3** | B | Break-quality gate: aggression on the breakout bar | A break on real aggression recruits more breakout followers → more stops to sweep | `vol_mult` ∈ [1.2, 3.0]× median(vol, 50) — below 1.2 is within normal dispersion, above 3.0 leaves <5% of bars; `close_loc` ≥ 0.7 of bar range | Bar volume. Assumed yes | Volume is endogenous to volatility; you may just be re-selecting high-vol bars, which the ATR normalisation was already meant to handle | trivial |
| **V4** | C | Sweep-signature filter | **Direct observable of the mechanism.** Stops firing = a fast, high-volume, immediately-rejected excursion | `wick_body` ≥ ρ ∈ [1.0, 3.0] (below 1.0 is not a rejection; above 3.0 is a handful of bars); `sweep_vol` ≥ [1.5, 3.0]× median; `recovery_bars` ≤ [1, 4] | OHLCV + volume. Optional L2 upgrade: bid-depth depletion→replenishment in the band. **[DATA GAP]** on L2 | Wick:body on low timeframes is largely spread and tick-size noise; ρ will be violently instrument-specific | trivial (OHLCV) / heavy (L2) |
| **V5** | D | Stop-entry beyond the line vs limit-at-line | Limit gets a better price but is filled preferentially when a large seller is working through. Stop pays spread but conditions on the move happening | No new params — it is the same setup, two fill models | Trade prints for realistic fills | None — **this cannot fail, it can only inform.** It is a measurement, not a variant | trivial |
| **V6** | E | Stop beyond sweep extreme (S-B/S-C) vs beyond line (S-A) | S-B is where the thesis actually dies; S-A stops out inside the very band the strategy claims to exploit | `stop_cap_c` ∈ [0.75, 2.5] ATR — below 0.75 it collapses into S-A, above 2.5 the R multiple makes targets unreachable | OHLCV | Wider stops improve hit rate and destroy expectancy; you will see the hit rate rise and mistake it for edge | trivial |
| **V7** | F | Frozen zone ladder + structural trail + hard time stop | The trap has a half-life. If continuation has not come within T_max, the trapped population has already resolved and you hold a stale thesis | `ladder_depth` ∈ [2, 4]; `time_stop` ∈ [1, 4] × impulse bar count — bounds from the impulse's own duration, not from clock preference | OHLCV | Time stops are the single easiest thing in this document to overfit. Fix `time_stop` from the *pre-period distribution of continuation timing*, then never touch it | moderate |
| **V8** | G | Regime gate: trend alignment + middle-tercile volatility | Continuation needs continuation demand (trend); in high vol the sweep band and buffer blow out faster than ATR normalisation compensates | `trend_lb` ∈ [50, 200] bars; vol tercile from a *trailing* 60-day realised-vol distribution (must be trailing, or the terciles are look-ahead) | OHLCV | Halves the sample, and trend filters are notorious for looking magnificent in-sample. Also: the trailing-tercile detail is a leak waiting to happen | moderate |
| **V9** | H | Soft invalidation — downgrade rather than kill | The "new low" may itself be the sweep on a lower timeframe; require extra confirmation instead of dying | `soft_extra_conf` ∈ {close-through, close-through + volume}; `soft_extension` ∈ [0.25, 1.0] ATR beyond V-B | OHLCV | **This is the rule that lets losers back in.** Strong prior it is a decoy that will look good in-sample and evaporate. Included so it can be killed with evidence rather than assertion | trivial |
| **V10** | I | Inversion — trade the failed Black Box short | After invalidation there are now **two** trapped cohorts: those who bought the break, and those who bought expecting the re-cross. Denser fuel than the original setup | Entry: break of pullback low, or retest of line from below. `inv_confirm_bars` ∈ [1, 3] | OHLCV | Sample-starved (it is the residual of a filter), and the invalidation threshold was tuned for a different purpose entirely — reusing it here is parameter recycling | moderate |

---

## 7. ADVERSARIAL CUT (Phase 3)

### 7.1 The curve-fitting case against each

| # | Strongest case that this is fitting to memorable charts |
|---|---|
| V1 | You will find the session set that worked — someone always discovers PDH is "the good one." With 6 candidate levels and a handful of enum choices you have already burned real degrees of freedom, and it *feels* free because none of them are continuous |
| V2 | Bin width is a continuous knob with enormous leverage disguised as a display setting. Sweep it and you will find nodes exactly where the winners were |
| V3 | Volume filters improve almost any pattern in-sample because they select for the days that trended. Ablate it against a pure high-ATR-bar filter — if performance is identical, it is a volatility filter wearing a costume |
| V4 | Wick:body thresholds are the classic post-hoc rationalisation: you looked at three winning sweeps, they had long wicks, you set ρ just below the shortest one. Bound ρ **before** looking at outcomes |
| V5 | Not fittable — no free parameters |
| V6 | Stop mode is a two-way fork, but `stop_cap_c` is continuous and interacts hard with pullback depth. The pair will jointly find the exact cap that saved your three best trades |
| V7 | Time stops fit to the specific pace of the sample regime. Change the volatility regime and the optimal T_max moves, and you will not know until it has already cost you |
| V8 | Trend + vol regime = at least 3 free params applied to a halved sample. Worst ratio of parameters to independent evidence in the whole list |
| V9 | The most dangerous. "Soft invalidation" is a mechanism for retroactively converting a filtered-out loser into a winner whenever it happened to work. Its in-sample improvement is close to guaranteed and close to meaningless |
| V10 | The setups it trades are the ones another rule discarded. Any threshold you pick was already fitted for a different objective — the sample is both small and contaminated |

### 7.2 THE BASE RATE PROBLEM

The pattern is defined partly by what happened *after* the setup. This is the central threat and it must be solved in the detector's architecture, not with a post-hoc filter.

**Design rules:**

1. **A setup is BORN at the qualified break, not at entry.** From birth it resolves into exactly one terminal state: `DISCARDED`, `EXPIRED_NO_PULLBACK`, `INVALIDATED`, `EXPIRED_ARMED`, `ENTERED_WIN`, `ENTERED_LOSS`, `ENTERED_TIMEOUT`. Every one is written to disk with full context.
2. **The denominator is births.** Report the survival funnel explicitly:

   `P(pullback | born)` → `P(re-cross | pullback)` → `P(target | entered)`

   The visual pattern's apparent reliability comes entirely from only ever examining the last branch of the last stage.
3. **The independent unit is the setup, not the trade.** Two setups on the same level within one session, or on correlated instruments in the same 30 minutes, are one episode. Deduplicate before counting anything. This number — not bars, not trades — is what the sweep grid must be compared against in 8.2.
4. **Ship a random-level control.** Draw levels uniformly at random from the candidate registry, apply *identical* break qualification, pullback and entry logic. **If the scored-selection version does not beat the random-level control out of sample, level selection has no edge** and you are trading generic post-breakout mean reversion. This single control is worth more than the entire parameter sweep and costs one afternoon.
5. **Hand-mark 50 charts blind** (outcomes hidden), then compare against detector output. Agreement rate tells you whether you are testing your strategy or a different one that shares its name.

### 7.3 Cost check

Components for NSE futures, round trip, on the stated assumptions (**verify every rate**):

| Component | Assumed | Note |
|---|---|---|
| Brokerage | ₹40 (₹20 × 2) | discount broker |
| STT | ~0.02% sell side | **Verify — revised 2024** |
| Exchange txn | ~0.002% both sides | **Verify — slab and revised 2024** |
| SEBI + stamp | ~0.003% combined | Stamp buy side only |
| GST | 18% on brokerage + txn + SEBI | |
| **Explicit total** | **≈ 3–4 bps round trip** | On ₹10L notional: roughly ₹300–400 |
| Spread + slippage | 1–2 ticks per side | Instrument-dependent; you must measure |

**The number that matters is cost expressed in R, not bps.**

```
cost_in_R = round_trip_cost_ticks / stop_distance_ticks
```

Worked example: `C = 3 ticks` round trip, stop distance `S = 20 ticks` → **cost = 0.15R per trade.**

A 40% hit rate at 2R gross has expectancy 0.4(2) − 0.6(1) = **0.20R gross → 0.05R net.** Three-quarters of the edge is consumed by friction.

**This is the punchline of the whole exercise.** The strategy's defining feature — a tight stop just beyond a precisely-defined line — is exactly what makes cost a dominant fraction of R. Any variant that widens the stop (V6) trades hit rate for cost efficiency, and the two effects partially cancel. Test cost-adjusted expectancy from day one; a gross-PnL backtest of this strategy is worthless.

### 7.4 Adverse selection on the re-cross

Entering with a limit at a visible line means you are filled *conditional on* someone aggressively working size through that line. Conditional on fill, the immediate future is worse than unconditional. Concretely:

- **Fill model, non-negotiable:** assume **no fill unless price trades ≥ 1 tick through your limit price.** Assuming a fill on touch is the difference between a beautiful backtest and a dead one, and it is the most common way this family of strategies is oversold.
- **Quantify it directly:** V5 gives you the estimator. `adverse_selection ≈ E[R | X-B limit fills] − E[R | X-A stop fills] + spread_cost`. If limit fills underperform stop fills by more than the spread saving, the limit version is strictly worse despite the better nominal price.
- Expect the drag to be **largest exactly where the mechanism is strongest** — the sweep is by construction an aggressive-flow event, which is the definition of the environment in which resting limits get run over. This is an uncomfortable tension in the strategy and you should look at it directly rather than around it.

### 7.5 Ranking and recommendation

Ranked by (mechanism robustness × ease of testing):

| Rank | Variant | Why |
|---|---|---|
| **1** | **V5 — entry mechanics** | Zero new parameters, cannot overfit, and it measures the single largest uncertainty in the entire project (fill realism / adverse selection). Every other result is conditional on getting this right. Do it first |
| **2** | **V4 — sweep signature** | It tests the causal mechanism itself. If sweeps show no aggression signature, the story is wrong and most of the rest is moot. Cheap on OHLCV, with a clear L2 upgrade path |
| **3** | **V1 — session-boundary levels** | Collapses level selection — the largest fitting surface in the whole design — to near-zero free parameters. Gives a clean, honest baseline that the scored registry must beat to justify its 4 parameters |

**Deprioritise:** V3 (likely collinear with volatility — run as an ablation only), V7 (after V1/V4/V5), V6 (only jointly with pullback depth), V10 (park until the base population is understood).

**Kill list:**

| Killed | Reason |
|---|---|
| **V9 (soft invalidation)** | Its in-sample improvement is near-guaranteed and near-meaningless. The only version worth running is a *pre-registered* one: state the expected improvement before testing, and kill it if the OOS result does not exceed that number |
| **V2 (volume-node level)** | Blocked on [DATA GAP], and bin width is a high-leverage hidden knob. Revisit only after V1 establishes whether level identity matters at all |
| **V8 (regime gate)** | Worst ratio of free parameters to independent evidence. Halves an already-small sample. Revisit only if the base strategy shows edge and you have >500 independent setups |
| **P-A/B/C pullback classifier** | Redundant with the trigger line and the invalidation rule (see 1.4). Keep P-A (no classifier) as the default and treat P-B as part of V4, not a separate axis |

---

## 8. SWEEP DESIGN (Phase 4)

### 8.1 Parameter table for V1, V4, V5

| Param | Range | Step | Scale | Bound justification (structural, not round-number) |
|---|---|---|---|---|
| `recross_tol` | 1–8 ticks | 1 tick | linear | Lower bound = 1 tick (below is unrepresentable). Upper bound = the point at which the tolerance exceeds the typical spread + one standard bar range, beyond which "at the line" is meaningless. **Compute the upper bound from your instrument's spread distribution, do not accept 8** |
| `break_b` | 0.04–0.30 ATR | 0.04 | linear | Lower = 2 ticks expressed in ATR for your instrument. Upper = the median bar range in ATR terms; beyond that the break is no longer "first," it is already the impulse |
| `stop_buffer` | 0.05–0.60 ATR | 0.05 | linear | Lower = spread + 1 tick. Upper = the median sweep excursion depth measured on the **pre-period only** — beyond that you are not stopping at the sweep, you are sizing through it |
| `sweep_vol_mult` | 1.2–3.0 | 0.3 | linear | Lower = 1 s.d. of the volume distribution. Upper = the 95th percentile; beyond it fewer than 5% of bars qualify and the sample dies |
| `wick_body_rho` | 1.0–3.0 | 0.5 | linear | Lower = 1.0 (wick equals body = not a rejection). Upper = 95th percentile of the wick:body distribution on your instrument |
| `arm_timeout` | 1.0–3.0 × impulse bars | 0.5 | linear | Derived from the observed distribution of pullback-completion times on the pre-period. **Measure it, do not pick it** |

All ranges are placeholders until you compute the instrument-specific quantiles that define them. Deriving bounds from your own pre-period distributions rather than from round numbers is most of the robustness battle.

### 8.2 Method and combinatorics

Full grid on the six params above: 8 × 7 × 12 × 7 × 5 × 5 = **117,600 configs.** Absurd.

**The governing constraint is independent setups, not bars or trades.** A defensible ceiling:

```
max_configs ≈ n_independent_setups / 20
```

| Independent setups | Max configs | Implication |
|---|---|---|
| 200 | 10 | Sweep 2 params only, 3 levels each |
| 500 | 25 | 2 pairs, coarse |
| 2,000 | 100 | 3 params, moderate |
| 10,000 | 500 | Full coarse-to-fine viable |

**Prescription: coarse-to-fine, jointly on the interacting pairs, everything else frozen at argued defaults.**

- Stage 1 — 3×3 coarse grid on Pair 1 (9 configs), pick the plateau centre, freeze.
- Stage 2 — 3×3 on Pair 2 (9 configs), freeze.
- Stage 3 — 5×5 refinement around the winning region of whichever pair showed more sensitivity (25 configs).

Total ≈ 43 configs. **If you have fewer than ~900 independent setups, cut Stage 3 entirely.** Do not negotiate with this — the ratio is the whole reason walk-forward results ever generalise.

### 8.3 Interacting pairs — sweep jointly, never one at a time

| Pair | Why they interact |
|---|---|
| **`pullback_depth_cap` × `stop_buffer`** | Both measure distance below the line. A deep-sweep tolerance with a tight stop is internally contradictory — you admit setups you are guaranteed to be stopped out of. The optimum of one is a function of the other |
| **`break_b` × `recross_tol`** | If the break threshold is large, the line is effectively further from where price actually turned, so the re-cross tolerance must widen to compensate. Sweeping separately finds a spurious optimum for each |
| **`sweep_vol_mult` × `arm_timeout`** | A high volume requirement selects fast sweeps, which need shorter timeouts. Coupled through the same underlying event |

### 8.4 Validation

**Purged walk-forward with embargo.**

| Element | Value | Reasoning |
|---|---|---|
| Train window | 12 months | Must contain ≥ 200 independent setups. **Set by setup count, not calendar** — if 12 months gives 80 setups, extend the window or abandon the sweep |
| Test window | 3 months | ≥ 50 setups |
| Step | 3 months (non-overlapping test folds) | |
| **Embargo** | **5 trading sessions** | Must strictly exceed the max lifetime of a single setup. If `arm_timeout` + `time_stop` ≤ 1 session, 2 sessions suffices; 5 gives margin for ATR/registry state that spans the boundary. **Any setup whose birth and resolution straddle the split is dropped, not truncated** |
| Purge | Drop all setups born within `embargo` of a boundary | Prevents label leakage through overlapping ATR and registry windows |

**Worked example** (replace with your actual span). For data 2023-01-01 → 2026-06-30:

| Fold | Train | Embargo | Test |
|---|---|---|---|
| 1 | 2023-01-01 → 2023-12-29 | 2024-01-01 → 2024-01-05 | 2024-01-08 → 2024-03-28 |
| 2 | 2023-04-03 → 2024-03-28 | 2024-04-01 → 2024-04-05 | 2024-04-08 → 2024-06-28 |
| 3 | 2023-07-03 → 2024-06-28 | 2024-07-01 → 2024-07-05 | 2024-07-08 → 2024-09-30 |
| 4 | 2023-10-03 → 2024-09-30 | 2024-10-01 → 2024-10-07 | 2024-10-08 → 2024-12-31 |
| 5 | 2024-01-01 → 2024-12-31 | 2025-01-01 → 2025-01-07 | 2025-01-08 → 2025-03-31 |
| 6 | 2024-04-01 → 2025-03-31 | 2025-04-01 → 2025-04-07 | 2025-04-08 → 2025-06-30 |
| 7 | 2024-07-01 → 2025-06-30 | 2025-07-01 → 2025-07-07 | 2025-07-08 → 2025-09-30 |
| 8 | 2024-10-01 → 2025-09-30 | 2025-10-01 → 2025-10-07 | 2025-10-08 → 2025-12-31 |
| 9 | 2025-01-01 → 2025-12-31 | 2026-01-01 → 2026-01-07 | 2026-01-08 → 2026-03-31 |
| 10 | 2025-04-01 → 2026-03-31 | 2026-04-01 → 2026-04-07 | 2026-04-08 → 2026-06-30 |

Ten OOS folds. **Reserve the final 6 months as a true holdout that is never touched during any sweep, on pain of the entire exercise being pointless.**

### 8.5 Objective function

Not raw Sharpe. Per **setup**, not per trade.

```
J = median_bootstrap( E_net[R per setup] )
    × (1 − concentration_penalty)
    × (1 − λ · cost_share)
    subject to  n_independent_setups ≥ 50 per fold  (else J := −∞)

where:
  E_net[R per setup] = Σ(net R over ALL setups incl. non-entered) / n_setups
  concentration_penalty = (Σ top-5 winners' gross PnL) / (Σ all gross positive PnL)
  cost_share = total costs / total gross positive PnL
  λ = 1.0
  median_bootstrap = median of 1,000 block-bootstrap resamples,
                     block length = 1 session (preserves intraday dependence)
```

Non-entered setups contribute 0R, which is correct — they consume attention and would have been taken discretionarily. The concentration penalty is essential here: a strategy that trails to "the next zone" without fixed R:R will produce a fat right tail, and a handful of outsized winners can carry an otherwise dead configuration.

### 8.6 Multiple-testing correction

**Deflated Sharpe Ratio** (Bailey & López de Prado):

```
DSR = Z[ (SR − SR₀)·√(T−1) / √(1 − γ₃·SR + ((γ₄−1)/4)·SR²) ]
```

- `SR` = observed Sharpe of the selected config
- `T` = number of return observations
- `γ₃`, `γ₄` = skewness and kurtosis of the return series
- `SR₀` = expected maximum Sharpe under the null of zero true edge:

```
SR₀ = √(Var[SR_n]) · [ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]
```

- `N` = **number of configs actually tried, including every abandoned exploratory run.** This is where honesty lives. If you swept 43 configs but explored 200 while "just looking," N = 200
- `γ` = Euler–Mascheroni ≈ 0.5772
- `Var[SR_n]` = variance of Sharpe across the trials

**Also compute PBO** via combinatorially symmetric cross-validation: split into S=16 sub-periods, form all C(16,8) = 12,870 train/test partitions, and for each, take the IS-best config and record its OOS rank. `PBO = P(OOS rank of IS-best < median)`. **PBO > 0.5 means your selection procedure is worse than random.** Report it whether or not you like the number.

### 8.7 Robustness — plateau, not peak

Define the neighbourhood as **±1 grid step on every axis simultaneously.** For k swept params, evaluate the 3^k − 1 surrounding points (or a random sample of 50 if k > 3). Accept only if:

1. `min(J) over the neighbourhood ≥ 0.6 × J(centre)`
2. `sign(J)` is positive at **every** neighbourhood point — a single sign flip disqualifies
3. `J(centre) ≤ 1.5 × median(J over neighbourhood)` — if the centre towers over its neighbours, it is a spike, and spikes are noise
4. The plateau spans **≥ 3 consecutive grid points on each axis**

One-axis-at-a-time perturbation is not a substitute and will pass configurations that fail the joint test. Reporting per-axis sensitivity alone is one of the most common ways an overfit system gets waved through.

---

## 9. SWEEP CONFIG (YAML)

```yaml
# black_box_sweep.yaml
meta:
  strategy: black_box
  version: 0.1
  status: UNTESTED          # no claim in this file is in-sample or held-out
  independent_unit: setup   # NOT trade, NOT bar

instrument:
  symbol: FILL
  tick_size: FILL           # do not guess
  lot_size: FILL
  session:
    open:  "09:15"
    close: "15:30"
    flat_time: "15:20"      # VERIFY closing-session mechanics
  costs:
    brokerage_per_order_inr: 20
    stt_sell_pct: FILL      # VERIFY - revised 2024
    exch_txn_pct: FILL      # VERIFY - revised 2024
    stamp_buy_pct: 0.002
    gst_pct: 18
    assumed_slippage_ticks_per_side: 1

data:
  mark_tf: 15m
  exec_tf: 1m
  start: FILL
  end: FILL
  holdout_start: FILL       # final 6 months - NEVER touched during sweeps

structural:                 # fixed by argument, not swept
  level_source: L-A         # V1 runs this as L-B
  impulse_def: I-B
  break_def: B-A
  pullback_def: P-A
  entry_mode: X-A           # V5 runs X-A and X-B side by side
  stop_mode: S-C
  target_mode: Z-A
  invalidation_ref: V-B
  max_concurrent: 1
  fractal_confirm_lag_bars: 3   # levels usable only from t+w

fill_model:
  limit_requires_through_ticks: 1   # NON-NEGOTIABLE
  gap_slippage_ticks: 3
  gap_slippage_frac_of_gap: 0.25    # take the larger
  void_on_halt: true

sweep:
  method: coarse_to_fine
  stages:
    - name: pair_1
      joint: [pullback_depth_cap, stop_buffer]
      grid:
        pullback_depth_cap: [0.5, 1.0, 1.5]   # ATR
        stop_buffer:        [0.10, 0.25, 0.45] # ATR
    - name: pair_2
      joint: [break_b, recross_tol]
      grid:
        break_b:     [0.06, 0.12, 0.22]   # ATR
        recross_tol: [1, 3, 6]            # ticks
    - name: refine
      run_if_min_setups: 900
      joint: [sweep_vol_mult, arm_timeout]
      grid:
        sweep_vol_mult: [1.2, 1.6, 2.0, 2.4, 3.0]
        arm_timeout:    [1.0, 1.5, 2.0, 2.5, 3.0]   # x impulse bars

validation:
  scheme: purged_walk_forward
  train_months: 12
  test_months: 3
  step_months: 3
  embargo_sessions: 5
  min_setups_train: 200
  min_setups_test: 50
  drop_straddling_setups: true

objective:
  metric: net_R_per_setup
  aggregation: median_block_bootstrap
  bootstrap_n: 1000
  block_length: 1_session
  concentration_penalty: top5_share_of_gross_positive
  cost_penalty_lambda: 1.0
  min_setups_per_fold: 50

corrections:
  deflated_sharpe: true
  n_trials_include_exploratory: true   # count EVERY run, not just the grid
  pbo:
    method: cscv
    n_splits: 16

robustness:
  neighbourhood: simultaneous_one_step_all_axes
  min_ratio_to_centre: 0.60
  require_sign_consistency: true
  max_centre_over_median: 1.5
  min_plateau_width_points: 3

controls:
  random_level_control: true     # the single most informative run in this file
  hand_marked_agreement_n: 50
```

---

## 10. PSEUDOCODE

### 10.a Detector — emits setups, wins and failures alike

```python
def scan(bars, cfg):
    registry, active, setups = [], [], []

    for t in range(len(bars)):
        bar = bars[t]

        # --- level registry: fractal confirmed at t, usable from t+w ---
        w = cfg.fractal_w
        if t >= 2*w and is_fractal(bars, t-w, w):
            registry.append(Level(price=bars[t-w].extreme,
                                  born_bar=t-w,
                                  usable_from=t,          # NOT born_bar
                                  score=score_level(bars, t-w, cfg)))
        registry = [L for L in registry if t - L.born_bar <= cfg.registry_W]

        # --- break detection on usable levels only ---
        for L in registry:
            if t < L.usable_from or L.consumed:
                continue
            if broke(bar, L, cfg.break_b, cfg.atr[t]):
                s = Setup(level=L, break_bar=t, dir=sign(bar.close - L.price))
                s.state = "BREAKOUT_SEEN"
                s.invalidation_ref = preceding_swing(bars, t, s.dir)   # V-B
                active.append(s)
                L.consumed = True          # reset clock: one 'first' break
                setups.append(s)           # BORN. logged regardless of outcome

        # --- advance every active setup ---
        for s in list(active):
            s = step(s, bars, t, cfg)
            if s.state in TERMINAL:
                s.resolved_bar = t
                active.remove(s)           # already in `setups` - never dropped

    return setups                          # denominator = births


def step(s, bars, t, cfg):
    bar, atr = bars[t], cfg.atr[t]

    if s.state == "BREAKOUT_SEEN":
        if t - s.break_bar > cfg.qual_window:
            s.state = "DISCARDED"; return s
        if impulse_qualifies(bars, s, cfg):        # I-B, forward window only
            s.state = "IMPULSE_CONFIRMED"
            s.arm_deadline = t + cfg.arm_timeout * s.impulse_bars

    elif s.state == "IMPULSE_CONFIRMED":
        if t > s.arm_deadline:
            s.state = "EXPIRED_NO_PULLBACK"; return s
        if returned_through_line(bar, s):
            s.state = "PULLBACK_ACTIVE"
            s.sweep_extreme = bar.extreme(-s.dir)

    elif s.state == "PULLBACK_ACTIVE":
        s.sweep_extreme = extend(s.sweep_extreme, bar, s.dir)

        # ORDER MATTERS: invalidation on bar extremes is evaluated FIRST.
        # Flipping these two blocks silently inflates results.
        if breached(bar.extreme(-s.dir), s.invalidation_ref, s.dir):
            s.state = "INVALIDATED"; return s
        if excursion(s) > cfg.sweep_cap_s * atr:
            s.state = "INVALIDATED"; return s
        if t > s.arm_deadline:
            s.state = "EXPIRED_ARMED"; return s

        if s.recross_count >= 1:
            s.recross_count += 1               # logged, NOT traded
        elif recross_triggered(bar, s, cfg):
            fill = model_fill(bar, s, cfg)     # None if limit not traded through
            if fill is None:
                s.state = "VOIDED_NO_FILL"; return s
            s.entry = fill
            s.stop  = compute_stop(s, cfg)     # S-C
            s.ladder = freeze_zone_ladder(bars, t, s, cfg)   # FROZEN at entry
            s.recross_count = 1
            s.state = "IN_TRADE"

    elif s.state == "IN_TRADE":
        if bar.extreme(-s.dir) crosses s.stop:        # stop first, again
            s.state = "ENTERED_LOSS"; s.exit = s.stop
        elif reached(bar, s.ladder.current):
            s.ladder.advance()                        # mechanical, pre-computed
            s.stop = s.ladder.prev_zone
            if s.ladder.exhausted:
                s.state = "ENTERED_WIN"; s.exit = bar.close
        elif t - s.entry_bar > cfg.time_stop:
            s.state = "ENTERED_TIMEOUT"; s.exit = bar.close
        elif clock(bar) >= cfg.flat_time:
            s.state = "EXITED_TIME"; s.exit = bar.close

    return s
```

### 10.b Walk-forward sweep runner

```python
def walk_forward(bars, cfg, grid_stages):
    folds = build_folds(bars, cfg.validation)   # purged + embargoed
    frozen = dict(cfg.structural)
    results = []
    n_trials = 0                                # count EVERYTHING

    for stage in grid_stages:
        if stage.run_if_min_setups and count_independent(bars, frozen) < stage.min:
            log(f"SKIP {stage.name}: insufficient independent setups"); continue

        stage_scores = defaultdict(list)
        for fold in folds:
            for combo in product(*stage.grid.values()):   # JOINT, never 1-at-a-time
                params = {**frozen, **dict(zip(stage.grid, combo))}
                n_trials += 1

                tr = scan(bars[fold.train], params)
                te = scan(bars[fold.test],  params)
                tr = drop_straddling(tr, fold); te = drop_straddling(te, fold)

                if n_independent(tr) < cfg.min_setups_train: continue
                if n_independent(te) < cfg.min_setups_test:  continue

                stage_scores[combo].append({
                    "is":  objective(tr, cfg),
                    "oos": objective(te, cfg),
                    "funnel_is":  survival_funnel(tr),
                    "funnel_oos": survival_funnel(te),
                })

        best = select_plateau(stage_scores, cfg.robustness)   # plateau, not argmax
        if best is None:
            log(f"{stage.name}: NO PLATEAU. Keeping defaults."); continue
        frozen.update(best.params)
        results.append(best)

    control = scan_with_random_levels(bars, frozen)   # the honesty check
    report(results,
           dsr=deflated_sharpe(results, n_trials),
           pbo=cscv_pbo(stage_scores, n_splits=16),
           control=objective(control, cfg))
    return frozen, results
```

---

## 11. DIAGNOSTICS — and what BAD looks like

| Diagnostic | Good | **BAD** |
|---|---|---|
| **Heatmap: `pullback_depth_cap` × `stop_buffer`** | A broad contiguous region of positive J spanning ≥ 3 points per axis | Isolated islands, a narrow diagonal ridge, or checkerboarding. Checkerboarding means J is dominated by which individual trades happened to survive — pure noise |
| **Heatmap: `break_b` × `recross_tol`** | Positive region with a coherent gradient; the optimum drifts smoothly along the diagonal (expected, they interact) | The optimum sits at a grid edge — your bounds are wrong and the real optimum is outside, or there is no optimum and you are reading noise |
| **Equity-curve dispersion across the grid** | A tight fan, all curves same sign, differing in slope not direction | A fan that crosses zero. If some configs are strongly negative, the strategy has no mechanism-level edge and the sweep is selecting survivors |
| **Outcome distribution by SETUP** (not by trade) | A realistic funnel: many `DISCARDED`/`EXPIRED`, meaningful `INVALIDATED`, entries a modest minority | Few `INVALIDATED`. **This is the alarm.** It means the detector is filtering failures out rather than counting them, and every downstream statistic is selection-biased |
| **Survival funnel across folds** | `P(pullback\|born)`, `P(re-cross\|pullback)`, `P(target\|entered)` stable across folds | Any conditional probability swinging >20 points between folds — the setup population is regime-dependent and single-number results are meaningless |
| **IS vs OOS scatter** | Positive slope, ideally near 1; OOS shifted down by a modest, consistent amount | Slope ≈ 0 (IS says nothing about OOS) or negative (the best IS configs are the worst OOS — textbook overfitting). Either result kills the sweep |
| **Random-level control vs scored registry** | Scored beats control OOS by a margin larger than fold-to-fold noise | Control matches or beats it → **level selection has no edge.** Stop, and either simplify to V1 or abandon |
| **Limit vs stop fill comparison (V5)** | Difference explained by the spread | Limit fills underperform by more than the spread → adverse selection is real and large; drop X-B entirely |
| **PnL concentration** | Top 5 winners < 30% of gross positive PnL | Top 5 > 50% → you have a lottery ticket, not a strategy. The concentration penalty in the objective should already have caught this |
| **DSR / PBO** | DSR > 0 at 95%; PBO < 0.3 | PBO > 0.5 → the selection procedure is worse than random and the "best" config is a coin flip |
| **Intrabar-ambiguity rate** | < 5% of setups flagged | > 15% → your bar resolution cannot distinguish entry from invalidation, and the results are a data artefact. Drop to a finer execution timeframe or abandon |

---

## 12. [DATA GAP] — needs data you have not confirmed

| Item | Needs | Blocks |
|---|---|---|
| V2 — volume-node level selection | Volume-at-price (from trade prints or vendor profile) | Axis A alternative |
| V4 L2 upgrade — bid-depth depletion→replenishment in the sweep band | L2 depth snapshots at sufficient cadence, time-aligned to bars | The **direct** test of the causal mechanism |
| Aggression signature at the sweep (buy/sell classification) | Trade prints with aggressor side, or Lee-Ready classification from L1 quotes | Mechanism falsification test (§3) |
| Realistic queue-position modelling for X-B limit fills | Full order-book replay | Accurate adverse-selection estimate; without it, use the ≥1-tick-through rule as a conservative proxy |
| Z-C — LVN target ladder | Volume-at-price | Axis F alternative |
| True spread distribution for `recross_tol` bounds | L1 quotes | The bound justification in 8.1 falls back to guesswork without it |

Note: NSE depth feeds are typically 5-level or 20-level, not the full book. Hidden and iceberg liquidity are invisible, and snapshot cadence may miss the sweep event entirely. Treat any depth-based conclusion as directional evidence, not proof.

---

## 13. THE THREE QUESTIONS THAT WOULD MOST CHANGE THIS

**1. How many independent setups does the current discretionary version actually produce per month, and what is the realised hit rate and average R across all of them — including the ones you skipped after they formed?**

Everything in Phases 4 and 8 hinges on the independent-setup count. At 20/month the entire sweep design collapses to "test two parameters and pray." At 500/month, coarse-to-fine with joint pairs is viable. And the skipped-setup count is the direct measure of how much of the discretionary edge lives in judgment that the formalisation just deleted.

**2. Do you have trade prints with aggressor side, or L2 depth, time-aligned to your bars — and over what date span?**

This determines whether you can test the causal mechanism directly (§3) or only test the pattern. If you can measure aggression at the sweep, run that test *before* any sweep — it is one afternoon of work, and a null result invalidates most of this document, which would be the cheapest useful finding available to you.

**3. What is the actual stop distance in ticks, on your instrument, on your execution timeframe?**

The cost-in-R calculation in 7.3 is the binding constraint on the entire strategy. At 20-tick stops, cost is ~0.15R and the strategy needs unusually strong gross expectancy to survive. At 60-tick stops, cost is ~0.05R and the picture changes completely — but so does the R multiple you can reach before the next zone. This single number determines whether you should be optimising the entry (tighter stop, more cost sensitivity) or the exit (wider stop, more target sensitivity), and they pull in opposite directions.
