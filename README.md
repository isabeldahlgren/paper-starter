# proof-engineering

An autonomous research pipeline for pure mathematics. You drop papers into an inbox; it reads each one as a research collaborator, finds a follow-up question, works it out (proof, counterexample search, computation), writes a short research note in LaTeX — and only shows you results that independently pass a **taste gate** and a **correctness gate**.

It optimizes for quality over volume. A yield of **one reviewable result per ~10 papers is success**; everything else is archived with an explicit rejection reason so the pipeline stays auditable. False positives (bad results reaching you) are treated as 10× worse than false negatives (good results archived), and every threshold errs toward rejection.

`PLAN.md` is the full design spec. This README covers running it.

## How it works

```
inbox/ ──► explore ──► self_check ──► novelty ──► taste ──► correctness ──► lean ──► review/
              ▲            │                                (2 blind      (Aristotle,   │
              └────────────┴──── gaps / fixable issues ──── passes)       optional &    └─► archive/ (everything
                                 loop back once                           non-gating)        else, with reasons)
```

- **explore** (generator): reads the paper, finds one or two natural follow-up questions, works them seriously (with Python/SymPy for experiments), and writes `note.tex` + `result.json`. Every claim gets an honest label: `theorem-complete-proof`, `theorem-sketch`, `conjecture-with-evidence`, `computation`, or `speculation`.
- **self_check** (cheap kill): a fresh context adversarially re-derives every claimed proof and runs a **computational counterexample search** on small cases. Numerics beat second opinions — this is the highest-value check per dollar.
- **novelty**: searches arXiv and the web; anything already known is archived with the reference.
- **taste**: an independent referee scores relevance, depth, naturality, and strength against a rubric — calibrated over time by your own accept/reject decisions (see below). Taste is one of a **configurable band of pass/fail gates**: you can add your own (e.g. "has convincing experiments", "is self-contained") by dropping a prompt file in `prompts/` and one `[[gates]]` block in `config.toml` — no code changes (see [Custom gates](#custom-gates)).
- **correctness**: two independent blind referee passes must both return `correct` on every load-bearing claim. Referees see *only* the finished note — isolation is enforced by the filesystem (each referee runs in a fresh directory containing only what it is allowed to see), not just by prompting.
- **lean** (optional, free): every proved claim is submitted to [Aristotle](https://aristotle.harmonic.fun) (Harmonic's cloud Lean prover) for formalization. Strictly **bonus evidence, never a gate** — the run has already passed both gates and no outcome can archive it. Submission is asynchronous (proofs can take hours; the run just waits at this stage across orchestrator ticks) and costs no Anthropic tokens. A machine-checked proof is marked ✓✓ in the review summary. Skipped entirely without `ARISTOTLE_API_KEY`.
- **review handoff**: survivors land in `review/<slug>/` with a one-page `SUMMARY.md`, the compiled PDF, and all referee reports; on macOS you get a notification.

Everything is files: no server, no queue. `orchestrate.py` is a single stdlib-only script; each run's state lives in `runs/<slug>/state.json`. Re-running the orchestrator resumes wherever things left off, and it is safe to run from cron or concurrently (per-run lock files, stale locks self-clear).

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI (`claude`) on your PATH
- A LaTeX distribution providing `latexmk` and `pdflatex` (e.g. [TeX Live](https://tug.org/texlive/) or MacTeX) — used to compile and lint every note
- Python **3.12+** (the orchestrator itself is stdlib-only)
- Optional: `pdftotext` (`brew install poppler`) — only needed for PDF intake; arXiv IDs use LaTeX source directly and don't need it
- Optional: the `aristotle` CLI (`uv tool install aristotlelib`) + `ARISTOTLE_API_KEY` in `.env` ([free key](https://aristotle.harmonic.fun/dashboard/keys)) — enables the Lean formalization stage; the pipeline runs fine without it
- A venv for the generator's experiments: `python3 -m venv .venv && .venv/bin/pip install sympy mpmath`

## Quickstart

```bash
git clone <this repo> && cd proof-engineering
python3 -m venv .venv && .venv/bin/pip install sympy mpmath
cp .env.example .env                       # then fill in your API keys (see "Cost & auth")

echo "2505.11846" > inbox/queue.txt        # arXiv IDs, one per line (or drop PDFs in inbox/)
python3 orchestrate.py --drain             # run every paper to a terminal state
```

API keys live in `.env` (gitignored; loaded by the orchestrator at startup, so cron jobs see them without shell-profile tricks): `ANTHROPIC_API_KEY` for the LLM stages — strongly recommended, see "Cost & auth" — and optionally `ARISTOTLE_API_KEY` for the Lean stage. Variables already set in your environment take precedence over `.env`.

`python3 orchestrate.py` advances every non-terminal run by exactly one stage (the cron-friendly mode); `--drain` loops until everything reaches `review/` or `archive/`; `--only <slug>` restricts to one run. For unattended operation, put `python3 orchestrate.py` on a cron schedule and drop papers in `inbox/` whenever — `max_runs_per_day` in `config.toml` caps intake, and extra inbox items stay queued.

## Cost & auth — read this before running unattended

Each `claude -p` stage call is real LLM work. Observed costs on `sonnet`: explore $2–6, self_check ~$2, novelty $1–3, taste $1–2, correctness ×2 $2–4 — roughly **$8–17 per paper** end-to-end. Three cost layers, from softest to hardest:

1. `max_usd_per_run` caps each individual `claude -p` invocation (`--max-budget-usd`).
2. `max_usd_per_paper` caps a run's *cumulative* spend, checked before each stage launches. A run at the cap is **parked, not rejected**: all completed-stage progress stays in `state.json`, and raising the cap resumes it exactly where it stopped. Running out of API credits behaves the same way — billing errors are treated as transient, so refill and re-invoke.
3. A workspace spend limit in the [Anthropic Console](https://console.anthropic.com) — the only cap enforced server-side no matter what the pipeline does. Set one before running unattended.

**Use an `ANTHROPIC_API_KEY` (console.anthropic.com), set in `.env`, rather than a Claude subscription for this.** Without an API key, `claude -p` authenticates via your subscription's OAuth and shares its *session-based* rate limit with your own interactive usage — an autonomous pipeline can silently eat your whole session window, and `max_usd_per_run` does not protect against that. The orchestrator treats 429/session-limit/overload responses as transient (it pauses the run and retries on a later invocation instead of consuming an attempt or archiving), but on a subscription the pipeline and your own usage still starve each other.

For fully reproducible unattended runs, add `"--bare"` to `claude_extra_args` in `config.toml`: it isolates pipeline calls from your global Claude Code config (hooks, plugins, personal `CLAUDE.md` files, MCP servers). Note `--bare` authenticates **only** via `ANTHROPIC_API_KEY`. The default (`--strict-mcp-config`) already keeps your MCP servers out of pipeline calls.

### Running on a Claude subscription instead (zero marginal cost, slow)

If you have a Claude Pro/Max subscription and would rather spend session quota than dollars, the pipeline supports that: when it hits your session limit, every pending run pauses in place and resumes after the window resets (~5 hours). Setup:

1. **Keep `ANTHROPIC_API_KEY` out of the orchestrator's environment** — the CLI prefers the key whenever it's set. That means *both* places the orchestrator gets keys from: comment it out of `.env` (or don't create one), and if it also lives in your shell profile, strip it per-invocation:
   ```bash
   env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain
   ```
   (`env -u` alone is not enough if the key is still in `.env` — the orchestrator loads `.env` at startup and would put it right back.) `claude -p` then falls back to your subscription's OAuth login. The preflight warning about the missing key is informational — expected in this mode.
2. **Don't add `--bare` to `claude_extra_args`** — bare mode authenticates only via API key and will refuse OAuth. The default flags work.
3. **Set `max_usd_per_paper = 0` in `config.toml`.** The CLI reports a *notional* dollar cost even on subscription auth, so a nonzero cap would park runs over money you aren't actually spending. The session limit itself is your cap in this mode.

Then run `--drain`: stages execute until the window is exhausted, at which point runs defer (no attempt consumed, nothing archived) and `--drain` exits cleanly; re-invoking after the reset continues exactly where it stopped. To automate the re-invoking, cron it — a tick that lands inside an exhausted window costs one tiny failed call per pending run and gives up:

```
0 * * * * cd /path/to/proof-engineering && env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain >> /tmp/proof-engineering.log 2>&1
```

Two caveats. The pipeline and your interactive Claude Code usage drain the **same** window, in both directions — a long explore stage can lock you out of interactive work for hours, and vice versa; this mode is best overnight or on weekends. And expect a full paper to take a day or more of wall clock instead of one sitting. For a first end-to-end validation run, a few dollars of API credit buys you an uninterrupted traversal and real per-stage cost numbers in the ledger.

## Teaching it your taste

The taste gate is the weakest gate on day one and becomes good only through feedback. After you review a result in `review/<slug>/`:

- move its `SUMMARY.md` (or any short writeup) into `taste/accepted/`, or
- into `taste/rejected/` with a one-line note saying why.

Everything in those two directories is injected into every future taste-referee call as few-shot calibration. Early on, expect to reject things — each rejection with a reason makes the gate stronger.

## Custom gates

`taste` is not special — it is one entry in a list of **pass/fail gates** that run in order between the novelty and correctness stages. Add your own bar (say, "the note must contain convincing numerical experiments") in two steps, no code:

1. **Write the prompt.** Copy `prompts/gate_template.md` to `prompts/experiments.md` and fill in the criteria. A gate reads `paper/` + `note.tex` + `result.json` in an isolated directory and must write `verdict.json` of the form `{"verdict": "pass" | "fail", "notes": "...", "scores": {...}}` (`scores` optional). That contract is the whole interface.
2. **Register it.** Add a block to `config.toml`:

   ```toml
   [[gates]]
   name = "experiments"      # -> prompts/experiments.md, writes referee/experiments.json
   on_fail = "explore"       # "archive" (default) drops the run; "explore" sends it back to be rewritten
   # optional: prompt, include_paper, tools, model, calibration
   ```

Gates run top-to-bottom, so put cheaper or more likely-to-fail gates first (a run killed early is a run you didn't pay to referee twice). A failing gate archives the run with its `notes` as the reason, or — with `on_fail = "explore"` — loops it back to the generator to try again. Remove the `taste` block to drop taste entirely; reorder the blocks to reorder the gates. Malformed or missing verdicts are retried (`max_referee_attempts`), never counted as a pass.

## Tuning

**Prompts are files** — tuning the system means editing `prompts/*.md` and `PROMPT.md` (the research brief the generator executes; edit it to point the pipeline at your own field and standards), not code. `config.toml` holds the knobs:

| key | meaning |
|---|---|
| `generator_model`, `self_check_model`, `taste_model`, `referee_model` | model per stage (`sonnet`, `opus`, `haiku`, …); `referee_model` is the default for correctness and every gate |
| `[[gates]]` | the pass/fail gate band (see [Custom gates](#custom-gates)) — `name`, and optional `prompt`/`on_fail`/`include_paper`/`tools`/`model`/`calibration` |
| `max_runs_per_day` | intake quota; extra inbox items stay queued |
| `max_usd_per_run` | `--max-budget-usd` cap per `claude -p` call |
| `max_usd_per_paper` | cumulative cap across a run's stages — at the cap the run is *parked* (progress kept), and resumes if you raise the cap; `0` disables (use on subscription auth) |
| `max_explore_attempts`, `max_self_check_attempts` | retries before archiving |
| `max_referee_attempts` | operational retries per referee stage (crashes/malformed verdicts — never counts a real rejection) |
| `stage_timeout_seconds` | wall-clock limit per stage invocation |
| `claude_extra_args` | extra flags for every `claude -p` call (see Cost & auth) |
| `notify` | macOS notification when a run reaches `review/` |
| `lean_enabled` | Aristotle formalization stage after the correctness gate (free, non-gating; needs `ARISTOTLE_API_KEY` + the `aristotle` CLI, silently skipped without them) |
| `lean_timeout_minutes` | per-claim wall clock for Aristotle, measured from submission; on timeout the task is canceled and the claim marked `not-formalizable` |
| `referee_command` | reserved hook for a cross-provider correctness referee (not wired in yet) |

## What "verified" actually means

Be honest with yourself about the epistemics: apart from the self-check's computational counterexample search, every gate is an LLM refereeing an LLM, and **LLM referees share failure modes with LLM generators**. Two models agreeing is much weaker evidence than one counterexample search. A result in `review/` has *survived independent refereeing* — it has not been formally verified. The one exception is the Lean track: a claim whose `SUMMARY.md` line says `proof-formalized ✓✓` carries an Aristotle-produced, machine-checked Lean proof of the formalized statement — the strongest evidence the pipeline can produce (though you should still confirm the Lean statement in `lean/` says what the note says). Expect `not-formalizable` to be the common outcome on genuinely interesting results; that is fine and costs nothing.

## Layout

```
PROMPT.md         the research brief the generator executes — edit for your field
PLAN.md           full design spec
orchestrate.py    the entire orchestrator (stdlib only)
config.toml       models, budgets, retries
.env.example      copy to .env (gitignored) and fill in API keys
prompts/          one markdown prompt per stage
inbox/            drop PDFs or a .txt of arXiv IDs here
runs/<slug>/      in-flight runs (state.json, note.tex, result.json, referee/, lean/)
review/<slug>/    passed both gates — SUMMARY.md, note.pdf, referee reports, Lean files
archive/<slug>/   rejected, each with rejection.md stating why
taste/            your accept/reject calibration corpus
```
