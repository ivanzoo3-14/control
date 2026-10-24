# CONTROL — On-chain SPC for Solana

**Stop guessing. Read the chart.**

A visual, self-auditing on-chain intelligence tool that applies classic
manufacturing **Statistical Process Control** (control charts, median/MAD robust
statistics, sign tests) to Solana trading data — and renders the verdict directly,
not just a diagnosis.

It ingests a pre-registered kill-test rig's `signals.jsonl`, computes control charts
on forward returns and execution friction, and answers one question honestly:

> *Is this trading process in control, or is the loss/edge just noise?*

## The headline result (from 8,373 real signals)

| measurement | value |
|---|---|
| Median forward return @ 300s | **−2.52%** |
| One-sided sign test (median ≥ 0) | **p ≈ 0** — reject |
| % positive | 39.5% |
| Rugs (<−90%) / moonshots (>+100%) | 605 / 269 |
| Median friction / side | 0.99% |
| Total RPC calls (rig lifetime) | 102,006 (peak ~20k/day) |

**The verdict is systematic, not luck** — the loss is the *center* of the
distribution, and the fat tails (rugs and moonshots) are exactly what every
take-profit rule truncates.

## How it works

1. `control_spc.py` reads `signals.jsonl`, filters to the definitive config
   fingerprint, caps friction at the documented <20% bound, and folds forward fills.
2. It computes robust SPC statistics (median, MAD→σ, control limits = median ± 3σ).
3. It runs a live RPC probe (`getSlot` / `getVersion`) and aggregates real RPC usage.
4. It emits `control_dashboard.html` — a self-contained SVG dashboard, no build step.

## Run it

```bash
# optional: point at your own RPC endpoint (e.g. GetBlock)
set KILLTEST_RPC_URL=https://go.getblock.io/<YOUR-KEY>

python control_spc.py        # regenerates control_dashboard.html
```

Then open `control_dashboard.html` in any browser.

## Safety

- **Paper-only by construction** — holds no keys, signs nothing, places no orders.
- **No credentials in this repo.** The RPC endpoint is read from an env var.
- **Not investment advice.** The tool's whole job is to stop you believing
  unfalsifiable claims — including its own.

## License

MIT
