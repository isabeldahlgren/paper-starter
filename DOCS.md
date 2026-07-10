# Customizing paper-starter

This is the guide for making the pipeline your own. For a first run see the [README](README.md); for the full design rationale see [PLAN.md](PLAN.md).

The whole system is meant to be tuned by **editing text files, not code**. `orchestrate.py` is a single stdlib-only script whose job is just to move each paper through the stages and enforce budgets — everything that shapes the *output* lives in prompts and config.

## The three files you edit

| File | What it controls |
|---|---|
| **`PROMPT.md`** | The research brief the generator executes on every paper. Point it at your field, your standards, what "interesting" means to you. This is the single highest-leverage file. |
| **`config.toml`** | Models per stage, budgets, retries, the gate list, the Lean toggle. See the reference below. |
| **`prompts/*.md`** | One prompt per stage. Editing `explore.md` changes how results are generated; editing the referee prompts changes the bar they enforce. |

Everything else — `orchestrate.py`, the directory layout, `state.json` — you can usually leave alone.

## Config reference

Every key is optional. Anything you omit falls back to `CONFIG_DEFAULTS` at the top of `orchestrate.py`, so `config.toml` only needs the values you want to override (an empty file, or none at all, runs with defaults).

| Key | Default | Meaning |
|---|---|---|
| `author` | `"Starter"` | Goes in `\author{}` on every note. Set it to your name. |
| `generator_model` | `sonnet` | Model for the explore stage. |
| `self_check_model` | `sonnet` | Model for self-check and the repair pass. |
| `taste_model` | `sonnet` | Model for the novelty-search referee. |
| `referee_model` | `sonnet` | Default model for correctness (two passes) and every gate. |
| `[[gates]]` | one `taste` gate | The pass/fail gate band — see [Custom gates](#custom-gates). |
| `max_runs_per_day` | `5` | Intake quota. Surplus inbox items stay queued for a later day. |
| `max_usd_per_run` | `10.0` | Hard cap per individual `claude -p` call (`--max-budget-usd`). |
| `max_usd_per_paper` | `0` (off) | Cumulative cap across all of a run's stages. At the cap the run is **parked** (all progress kept), not archived — raise the cap and re-invoke to resume. Use `0` on subscription auth. |
| `max_explore_attempts` | `2` | Explore retries before archiving. |
| `max_self_check_attempts` | `2` | Self-check retries before archiving. |
| `max_referee_attempts` | `2` | Operational retries per referee stage (crashes / malformed verdicts — never counts a real rejection). |
| `stage_timeout_seconds` | `1800` | Wall-clock limit per `claude -p` call. |
| `explore_timeout_seconds` | `3600` | Longer limit for explore, which runs experiments *and* writes the note. |
| `claude_extra_args` | `["--strict-mcp-config"]` | Extra flags on every `claude -p` call. See [Cost & auth](#cost--auth). |
| `notify` | `true` | macOS notification when a run reaches `review/`. |
| `lean_enabled` | `true` | The optional Aristotle/Lean formalization stage — see [The Lean track](#the-lean-track). |
| `lean_timeout_minutes` | `240` | Per-claim wall clock for Aristotle; on timeout the task is canceled and the claim marked `not-formalizable`. |
| `venv_python` | `.venv/bin/python3` | Interpreter offered to the generator for experiments. |

## Custom gates

`taste` is not special — it is one entry in a list of **pass/fail gates** that run in order between the novelty and correctness stages. Each gate is one isolated `claude -p` call that reads the paper + note and writes a verdict. Adding your own bar (say, "the note must contain convincing numerical experiments") takes two steps and no code.

**1. Write the prompt.** Copy `prompts/gate_template.md` to `prompts/experiments.md` and fill in the criterion. The gate runs in an isolated directory containing `paper/` + `note.tex` + `result.json` (claims stripped to id/label/statement) and must write `verdict.json`:

```json
{ "verdict": "pass" | "fail", "notes": "why, specific to this note", "scores": { "...": 1 } }
```

`verdict` is mandatory and must be exactly `"pass"` or `"fail"`; `notes` becomes the rejection reason on a fail and appears in `SUMMARY.md` on a pass; `scores` is optional. A missing or malformed verdict is retried (`max_referee_attempts`), never counted as a pass.

**2. Register it** in `config.toml`:

```toml
[[gates]]
name = "experiments"      # -> prompts/experiments.md, writes referee/experiments.json
on_fail = "explore"       # "archive" (default) drops the run; "explore" loops it back to be rewritten
# optional keys:
# prompt = "experiments.md"   # override the default prompt filename
# include_paper = true        # give the gate the source paper (default true)
# tools = "Read Write"        # tools the gate may use (add WebSearch WebFetch for a search gate)
# model = "opus"              # override referee_model for this gate
# calibration = false         # inject taste/{accepted,rejected} as few-shot examples
```

Gates run top-to-bottom, so **put cheaper or more-likely-to-fail gates first** — a run killed early is one you didn't pay to referee twice. Reorder the blocks to reorder the gates; delete the `taste` block to drop taste entirely. `validate_gate_config()` fails fast at startup on a missing prompt file, a duplicate/reserved name, or a bad `on_fail`, so mistakes surface immediately rather than deep in a run.

## Teaching it your taste

The taste gate is the weakest gate on day one and gets good only through feedback. After you review a result in `review/<slug>/`, move its `SUMMARY.md` (or any short writeup) into:

- `taste/accepted/`, or
- `taste/rejected/` with a one-line note saying why.

Everything in those two directories is injected into every future taste-gate call as few-shot calibration (this is what `calibration = true` does). Early on, expect to reject reviewable results — each rejection with a reason sharpens the gate.

## Cost & auth

Each `claude -p` stage call is real LLM work. Observed on `sonnet`: explore $2–6, self_check ~$2, novelty $1–3, taste $1–2, correctness ×2 $2–4 — roughly **$8–17 per paper**. Three cost layers, softest to hardest:

1. `max_usd_per_run` caps each individual invocation.
2. `max_usd_per_paper` caps a run's cumulative spend and **parks** (never archives) at the cap.
3. A workspace spend limit in the [Anthropic Console](https://console.anthropic.com) — the only server-side cap. Set one before running unattended.

**Prefer an `ANTHROPIC_API_KEY`** (from console.anthropic.com, set in `.env`) over your Claude subscription. Without an API key, `claude -p` authenticates via your subscription's OAuth and shares its *session* rate limit with your own interactive usage — an autonomous pipeline can eat your whole session window. The orchestrator treats 429 / session-limit / overload responses as transient (it pauses the run and retries later instead of archiving), but the two still starve each other on a subscription.

For fully reproducible unattended runs, add `"--bare"` to `claude_extra_args`: it isolates pipeline calls from your global Claude Code config (hooks, plugins, personal `CLAUDE.md`, MCP servers). `--bare` authenticates **only** via `ANTHROPIC_API_KEY`.

### Running on a subscription instead (zero marginal cost, slow)

To spend session quota instead of dollars:

1. **Keep `ANTHROPIC_API_KEY` out of the orchestrator's environment** — the CLI prefers the key whenever it is set. That means *both* sources: comment it out of `.env`, and if it also lives in your shell profile, strip it per-invocation:
   ```bash
   env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain
   ```
   (`env -u` alone is not enough if the key is still in `.env` — the orchestrator loads `.env` at startup and would put it back.)
2. **Don't add `--bare`** — bare mode refuses OAuth. The default flags work.
3. **Set `max_usd_per_paper = 0`** — the CLI reports a *notional* dollar cost even on subscription auth, so a nonzero cap would park runs over money you aren't spending. The session limit itself is your cap here.

When the window is exhausted, pending runs defer (no attempt consumed) and `--drain` exits cleanly; re-invoke after the reset (~5 hours) to continue. To automate, cron it:

```cron
0 * * * * cd /path/to/paper-starter && env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain >> /tmp/paper-starter.log 2>&1
```

Caveat: the pipeline and your interactive Claude Code usage drain the same window in both directions — best overnight or on weekends — and a full paper can take a day of wall clock. For a first end-to-end run, a few dollars of API credit buys an uninterrupted traversal and real per-stage cost numbers.

## The Lean track

After a run passes both gates, `lean_enabled = true` submits every `theorem-complete-proof` claim to [Aristotle](https://aristotle.harmonic.fun) (Harmonic's cloud Lean prover) for formalization. It is **bonus evidence, never a gate**: the run has already passed, and no Aristotle outcome can archive it — the only exit is review.

- Submission is asynchronous (proofs can take hours); the run waits at the `lean` stage across orchestrator ticks, each tick submitting/polling/downloading what it can.
- It costs **no Anthropic tokens** and is exempt from `max_usd_per_paper` parking.
- Outcomes land in `result.json` as `formalization`: `proof-formalized` (machine-checked proof, marked ✓✓ in `SUMMARY.md`), `statement-formalized` (partial), or `not-formalizable`. Expect `not-formalizable` to be common on genuinely hard results — that is fine.
- Requires the `aristotle` CLI (`uv tool install aristotlelib`) + `ARISTOTLE_API_KEY` in `.env`. Without them, or with `lean_enabled = false`, the stage is skipped straight to review.

## Tuning the prompts

Prompts are the system's genome. Some guidance:

- **`PROMPT.md`** — the research brief. Rewrite it for your field, your notion of a good question, and your standards. Everything downstream inherits from here.
- **`prompts/explore.md`** — wraps `PROMPT.md` with the output contract (the LaTeX/`amsart` note + `result.json` schema) and the mandatory pre-commit literature check. Edit here to change house style or the claim taxonomy.
- **The referee prompts** (`novelty.md`, `taste.md`, `correctness_referee.md`, and any gate) enforce the bars. Make them **harsher, not softer** if bad results are reaching review — the design assumes false positives are far more costly than false negatives.

A note on independence: referees run in isolated directories and never see the generator's reasoning, the self-check notes, or each other's verdicts. That isolation is enforced by the filesystem (`make_referee_dir()`), not just by prompting — so keep referee prompts self-contained; they cannot rely on context from earlier stages.

## Checking status

`python3 orchestrate.py --status` prints where every run stands (stage, cost, parked/rejection reason) without advancing anything. Safe to run any time.
