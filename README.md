# WatchDigest

A personal research agent that watches prediction-market wallets for you —
and only reports what actually changed, with every number checked against
the data.

Leaderboards lie by omission: a wallet can top the monthly PnL board while
being deeply negative over its lifetime (we verified exactly this in our
own research). WatchDigest answers a narrower, more useful question:
*what changed since the baseline, and is this wallet still the same kind
of actor it used to be?*

Built for the Nebius × NVIDIA Global AI Hackathon. Tracks: **Best Apps &
Agents** and **Personal AI**.

## How it works — detect, narrate, verify

1. **Detect (deterministic).** A rule engine compares each watched wallet
   against its stored baseline and fires typed alerts: `value_change`,
   `cadence_collapse`, `bot_like`, `dormant`, `sweep`, `concentration`
   (definitions in `rules.json`, logic in `digest.py`). No model is
   involved in detection.
2. **Narrate (LLM).** `nemotron_brief.py` sends *only* the alert payload
   JSON to **nvidia/Nemotron-3.5-Super-VL** on **Nebius Token Factory**
   (OpenAI-compatible API, via `nebius_adapter.py`) and asks it to write a
   short weekly briefing. The model never calculates; it only narrates
   figures that are already in the payload.
3. **Verify (deterministic).** Before anything is written to `brief.md`,
   a numeric verifier extracts every number from the generated text and
   checks that each one exists in the source `digest.json`. If any figure
   cannot be traced, the briefing is **rejected** (saved separately as
   `brief_rejected.md`) and `brief.md` is left untouched.

Data sources are public APIs only. The tool is read-only: no trading, no
market accounts, no investment advice, and no recommendation to copy any
wallet.

## Requirements

- Python 3.10+ — standard library only, nothing to `pip install`.
- For the live narration run only: a Nebius Token Factory API key, passed
  **exclusively through the `NEBIUS_API_KEY` environment variable**. The
  key is never written to code, files, or logs. Optional overrides:
  `NEBIUS_BASE_URL` (default `https://api.tokenfactory.nebius.com/v1`)
  and `NEBIUS_MODEL` (default `nvidia/Nemotron-3.5-Super-VL`).

## Quick start

### 1. Offline self-test (no network, no key)

```sh
python3 nemotron_brief.py --selftest
```

Runs the full narration + verification pipeline against a mock adapter:
a faithful briefing passes, and a briefing with tampered/invented figures
is rejected. Expected result: `ALL OK`.

### 2. Offline demo briefing (mock adapter)

```sh
python3 nemotron_brief.py --demo
```

Generates `output/brief.md` from the bundled `demo_output/digest.json`
using the mock adapter. **This is a mock run** — it exists so the whole
pipeline can be exercised offline. It is not output of the real model.

### 3. Live run against Nebius Token Factory (real API)

```sh
export NEBIUS_API_KEY=<your key>   # environment variable only
WATCHDIGEST_NEBIUS_ADAPTER=tokenfactory \
  python3 nemotron_brief.py --demo --adapter tokenfactory --out-dir output_live
```

On success this prints `[verify] PASS` and writes
`output_live/brief.md`. Token usage and finish reason are printed to
stderr by the adapter.

### 4. Regenerate the digest itself

```sh
# From the bundled public-data snapshot (2026-10-24 fetch; this snapshot is
# ~10.8 hours newer than the stored baselines, so it fires 11 alerts,
# including the two-point value_change and cadence_collapse alerts):
python3 digest.py --input snapshots/snapshot-20261024-1629.json

# Or fetch fresh data from the public Polymarket data-api (read-only):
python3 digest.py --fetch
```

Note: `digest.py --demo` in the original research workspace reads a
private research cache that is not part of this repository; the frozen
output of that demo run is bundled as `demo_output/` and is what the
narration commands above consume.

## Verified live run (2026-10-28)

WatchDigest was run end-to-end against the real Nebius Token Factory API:

- Model: `nvidia/Nemotron-3.5-Super-VL`
- Result: **numeric verification PASS — all 39 figures** in the generated
  briefing traced to the source data
- Measured usage: **1,459 prompt tokens / 2,418 completion tokens**
  (2,073 of them reasoning tokens), ≈ **$0.0026 per briefing** at list
  prices ($0.30 input / $0.90 output per 1M tokens)
- The actual generated file, header included, is in
  [`examples/brief-live-2026-10-28.md`](examples/brief-live-2026-10-28.md)

One practical finding from the live run: this model spends most of its
budget on reasoning tokens, so a 1,200-token cap returned an empty
message (`finish_reason=length`). The pipeline therefore requests
`max_tokens=4000`.

### Mock vs. live — how to tell them apart

| | Mock (`--selftest`, `--demo` default) | Live (`--adapter tokenfactory`) |
|---|---|---|
| Adapter name in output | `mock` | `tokenfactory` |
| Model shown | `mock-nemotron (offline canned)` | `nvidia/Nemotron-3.5-Super-VL` |
| Network / key | none | real API, `NEBIUS_API_KEY` required |
| Purpose | offline pipeline verification | the real briefing |

Every generated `brief.md` carries a header naming the adapter and model
that produced it, so a mock output can never be mistaken for a live one.

## Repository layout

```
watchdigest/
├── digest.py            # collection / aggregation / rule engine / digest output
├── nemotron_brief.py    # narration pipeline + numeric verifier + self-test
├── nebius_adapter.py    # mock adapter + Nebius Token Factory adapter
├── watchlist.json       # 7 watched wallets + baselines
├── rules.json           # alert thresholds and fetch protocol
├── demo_output/         # frozen demo digest (digest.md / digest.json)
├── snapshots/           # public-data snapshot used by digest.py --input
├── examples/            # real live-run output (see above)
└── LICENSE              # MIT
```

## Alert definitions

| kind | condition | meaning |
|---|---|---|
| value_change | portfolio value moved ≥ ±50% vs baseline | large capital movement |
| cadence_collapse | median trade interval fell to ≤ 1/10 of baseline | automation suspected |
| bot_like | median trade interval < 10 seconds | bot-like primary filter |
| dormant | no trades for ≥ 14 days (from data-window end) | dormancy suspected |
| sweep | > 80% of trades priced ≥ 0.95 | near-settlement sweep pattern |
| concentration | largest position ≥ 50% of portfolio value | single-view concentration |

In the bundled demo data the engine fires 6 alerts, including a sweep +
bot-like pair for one wallet whose median trade interval is 0 seconds
with 99.8% of trades priced at 0.95 or above, and a dormancy alert for a
wallet inactive for 23.0 days.

## Limitations

- The bundled demo compares a snapshot against a baseline taken from the
  same window, so the two-point alerts (`value_change`,
  `cadence_collapse`) do not fire in the demo; in production each new
  snapshot is compared against the previous one.
- Closed-position samples are capped (50 per side); they are not suitable
  for win-rate calculations, and WatchDigest does not compute one.
- Open positions are capped at 500 per wallet by the upstream API.

## License

MIT — see [LICENSE](LICENSE).
