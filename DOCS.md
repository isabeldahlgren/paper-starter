# Customizing paper-starter

This is the guide for making the pipeline your own. For a first run see the [README](README.md); for the full design rationale see [PLAN.md](PLAN.md).

The system is meant to be tuned by **editing text files, not code**. `orchestrate.py` is a single stdlib-only script whose job is just to move each note through crosscheck → lean → review and enforce budgets — everything that shapes the *judgment* lives in prompts and config.

## The division of labor

Generation happens **outside** this tool: you run [`GPT-PROMPT.md`](GPT-PROMPT.md) (or any brief) in a chat window, because that's where a strong model does its best creative work and where you can steer it. paper-starter starts once you have candidate notes, and does only what a chat window can't: independent cross-model verification, Lean formalization, and ranking.

| File | What it controls |
|---|---|
| **`GPT-PROMPT.md`** | The generation brief. paper-starter never executes it — it's the prompt *you* paste into ChatGPT. Point it at your field and standards. |
| **`prompts/crosscheck.md`** | The single referee pass: correctness (re-derive proofs), novelty (literature check), taste (is it worth reading). Make it harsher, never softer. |
| **`prompts/lean.md`** | The instruction sent to Aristotle for formalization (defaults to "formalise all results; report proof errors in ERROR.md"). |
| **`prompts/rank.md`** | How `review/` notes are ordered into `PRIORITY.md`. |
| **`config.toml`** | Referee model, budgets, retries, the Lean toggle. See the reference below. |

## The input-folder contract

An **inbox item is a directory** (drop it in `inbox/`) containing at least:

- `note.tex` — a standalone `amsart` LaTeX note.
- `result.json` — the claim index GPT-PROMPT.md specifies:
  ```json
  {
    "paper": "arxiv:2505.11846",
    "claims": [
      {"id": "claim-1", "label": "theorem-complete-proof | theorem-sketch | conjecture-with-evidence | computation | speculation",
       "statement": "A precise, self-contained statement.", "proof_status": "verified | ..."}
    ]
  }
  ```

Only `id`, `label`, and `statement` are required per claim (`proof_status` and any `audit_notes` GPT adds are ignored — the crosscheck forms its own view, and never sees them). The `paper` field, when it holds an arXiv id, lets intake fetch the source paper into `paper/` so the referee can judge novelty and motivation; without it the note is still refereed (correctness from the note alone, novelty via web search). Anything else in the folder (a `verification_report.md`, experiment scripts, a `note.pdf`) is carried along but hidden from the referee.

Malformed folders (no `note.tex`/`result.json`, or an unparseable `result.json`) are skipped with a message and moved to `inbox/.processed/` so they aren't re-scanned.

## Config reference

Every key is optional. Anything you omit falls back to `CONFIG_DEFAULTS` at the top of `orchestrate.py`, so `config.toml` only needs the values you want to override (an empty file, or none at all, runs with defaults).

| Key | Default | Meaning |
|---|---|---|
| `referee_model` | `opus` | Model for the crosscheck referee and the ranker. Use a different family than your generator — that difference is what makes the check independent. |
| `max_runs_per_day` | `10` | Intake quota. Surplus inbox folders stay queued for a later day. |
| `max_usd_per_run` | `10.0` | Hard cap per individual `claude -p` call (`--max-budget-usd`). |
| `max_usd_per_paper` | `0` (off) | Cumulative cap across a run's stages. At the cap the run is **parked** (all progress kept), not archived — raise the cap and re-invoke to resume. Use `0` on subscription auth. |
| `max_referee_attempts` | `2` | Operational retries for crosscheck (crashes / malformed verdicts — never counts a real rejection). |
| `stage_timeout_seconds` | `3600` | Wall-clock limit per `claude -p` call. Crosscheck re-runs computations, so give it headroom. |
| `claude_extra_args` | `["--strict-mcp-config"]` | Extra flags on every `claude -p` call. See [Cost & auth](#cost--auth). |
| `notify` | `true` | macOS notification when a run reaches `review/`. |
| `lean_enabled` | `true` | The optional Aristotle/Lean formalization stage — see [The Lean track](#the-lean-track). |
| `lean_timeout_minutes` | `240` | Per-claim wall clock for Aristotle; on timeout the task is canceled and the claim marked `not-formalizable`. |
| `venv_python` | `.venv/bin/python3` | Interpreter the crosscheck referee uses to re-run the note's computations. |

## How crosscheck decides

One isolated `claude -p` call reads `paper/` + `note.tex` + a stripped `result.json` and writes `verdict.json`:

```json
{
  "verdict": "pass | fail",
  "scores": {"relevance": 0, "depth": 0, "naturality": 0, "strength": 0},
  "correctness": [{"id": "claim-1", "verdict": "correct | fixable-gap | wrong | cannot-verify", "notes": "..."}],
  "novelty": [{"id": "claim-1", "novelty_status": "novel | known | uncertain", "reference": "..."}],
  "notes": "the overall call and the key reason"
}
```

**Pass** advances to `lean`; **fail** archives with the reason. On top of the referee's own `verdict`, the orchestrator enforces two hard rules in code, so a generously-graded note can't slip through: a run fails if **any** `theorem-complete-proof` claim is worse than `correct`, or if **every** substantive claim is `known`. A missing or malformed verdict is retried (`max_referee_attempts`), never counted as a pass.

To raise or lower the bar, edit `prompts/crosscheck.md` — e.g. change the taste thresholds, add a required check ("reject unless numerical experiments are reproduced"), or tighten what counts as `known`. No code change is needed.

When a note fails on a **fixable gap**, the `rejection.md` spells out exactly what's missing — that text is the feedback to hand back to GPT for a revision, then re-drop the folder.

## Cost & auth

The only Claude spend is the crosscheck (one call per note) and the occasional rank call — generation is on your ChatGPT side. Observed on `opus`, a crosscheck runs roughly **$1–4** depending on how much the referee computes and searches. Three cost layers, softest to hardest:

1. `max_usd_per_run` caps each individual invocation.
2. `max_usd_per_paper` caps a run's cumulative spend and **parks** (never archives) at the cap.
3. A workspace spend limit in the [Anthropic Console](https://console.anthropic.com) — the only server-side cap. Set one before running unattended.

**On a subscription (default, zero marginal cost):** without an `ANTHROPIC_API_KEY`, `claude -p` authenticates via your subscription's OAuth and shares its *session* rate limit with your interactive usage. The orchestrator treats 429 / session-limit / overload responses as transient (it pauses the run and retries later instead of archiving). Because the only Claude work here is refereeing — not a full generation pipeline — the session pressure is far lighter than it would be if generation were on this side too.

To stay on the subscription:

1. **Keep `ANTHROPIC_API_KEY` out of the orchestrator's environment** — the CLI prefers the key whenever it is set, from *both* the shell and `.env`. Comment it out of `.env`, and strip it per-invocation if it lives in your profile:
   ```bash
   env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain
   ```
   (`env -u` alone is not enough if the key is still in `.env` — the orchestrator loads `.env` at startup and would put it back.)
2. **Don't add `--bare`** — bare mode refuses OAuth. The default flags work.
3. **Set `max_usd_per_paper = 0`** — the CLI reports a *notional* dollar cost even on subscription auth, so a nonzero cap would park runs over money you aren't spending.

**With an API key (per-token billing, no session contention):** add `ANTHROPIC_API_KEY` to `.env`. For fully reproducible unattended runs, also add `"--bare"` to `claude_extra_args`: it isolates pipeline calls from your global Claude Code config (hooks, plugins, personal `CLAUDE.md`, MCP servers) and authenticates **only** via the key.

Cron example (subscription):

```cron
0 * * * * cd /path/to/paper-starter && env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain >> /tmp/paper-starter.log 2>&1
```

## The Lean track

After a note passes crosscheck, `lean_enabled = true` sends the whole note to [Aristotle](https://aristotle.harmonic.fun) (Harmonic's cloud Lean prover) for formalization. It is **bonus evidence, never a gate**: the note has already passed, and no Aristotle outcome can archive it — the only exit is review.

- **One project per note**, via `aristotle submit "<prompt>" --project-dir` with `note.tex` as the payload. The prompt is **`prompts/lean.md`** — edit it freely; it defaults to *"formalise all results in this paper; if you find an actual error in a proof, try to fix it and record it in ERROR.md."* (Falls back to a built-in default if the file is missing.)
- Submission is asynchronous (proofs can take hours); the run waits at the `lean` stage across orchestrator ticks, each tick submitting/polling (`aristotle tasks`)/downloading (`aristotle download`, a `.tar.gz` unpacked into `lean/solution/`) what it can. `lean_timeout_minutes` bounds it, with a best-effort `aristotle cancel`.
- It costs **no Anthropic tokens** and is exempt from `max_usd_per_paper` parking.
- The note-level outcome lands in `result.json` under `formalization`: `proof-formalized` (Aristotle `COMPLETE`, **no `sorry`**, no reported error — machine-checked, marked ✓✓ and weighted heavily by the ranker), `statement-formalized` (built the statements but a `sorry` or error remains), or `not-formalizable`. Expect `not-formalizable` to be common on genuinely hard results — that is fine.
- **If Aristotle writes an `ERROR.md`** (it found and reported a real proof error), it's copied to `lean/ERROR.md`, the note stays in `review/` (Lean is non-gating), and it's surfaced *loudly*: a ⚠️ banner at the top of `SUMMARY.md` and a ⚠️ flag pushed to the top of `PRIORITY.md`. That's the signal the crosscheck referee missed something — check it before trusting (or dismissing) the note.
- Requires the `aristotle` CLI (`uv tool install aristotlelib`) + `ARISTOTLE_API_KEY` in `.env`. Without them, or with `lean_enabled = false`, the stage is skipped straight to review.

## A note on independence

The crosscheck runs in an isolated directory (`make_referee_dir()`) containing only the source paper, `note.tex`, and a `result.json` stripped to id/label/statement. GPT's `proof_status`, its `verification_report.md`, and any audit notes are never copied in — the referee cannot lean on the author's own confidence. That isolation is enforced by the filesystem, not just by prompting, so keep `prompts/crosscheck.md` self-contained.

## Checking status

`python3 orchestrate.py --status` prints where every run stands (stage, cost, parked/rejection reason) without advancing anything. Safe to run any time. `--rank` rebuilds `PRIORITY.md` from the current `review/` and `archive/` contents without advancing anything either.
