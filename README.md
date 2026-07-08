# proof-engineering

An autonomous research pipeline for pure mathematics. You drop papers into an inbox; it reads each one as a research collaborator, finds a follow-up question, works it out (proof, counterexample search, computation), writes a short research note in LaTeX — and only shows you results that independently pass a **taste gate** and a **correctness gate**.

It optimizes for quality over volume. A yield of **one reviewable result per ~10 papers is success**; everything else is archived with an explicit rejection reason so the pipeline stays auditable. False positives (bad results reaching you) are treated as 10× worse than false negatives (good results archived), and every threshold errs toward rejection.

`PLAN.md` is the full design spec. This README covers running it.

## How it works

```
inbox/ ──► explore ──► self_check ──► novelty ──► taste ──► correctness ──► review/
              ▲            │                                  (2 blind      │
              └────────────┴──── gaps / fixable issues ────── passes)       └─► archive/ (everything
                                 loop back once                                  else, with reasons)
```

- **explore** (generator): reads the paper, finds one or two natural follow-up questions, works them seriously (with Python/SymPy for experiments), and writes `note.tex` + `result.json`. Every claim gets an honest label: `theorem-complete-proof`, `theorem-sketch`, `conjecture-with-evidence`, `computation`, or `speculation`.
- **self_check** (cheap kill): a fresh context adversarially re-derives every claimed proof and runs a **computational counterexample search** on small cases. Numerics beat second opinions — this is the highest-value check per dollar.
- **novelty**: searches arXiv and the web; anything already known is archived with the reference.
- **taste**: an independent referee scores relevance, depth, naturality, and strength against a rubric — calibrated over time by your own accept/reject decisions (see below).
- **correctness**: two independent blind referee passes must both return `correct` on every load-bearing claim. Referees see *only* the finished note — isolation is enforced by the filesystem (each referee runs in a fresh directory containing only what it is allowed to see), not just by prompting.
- **review handoff**: survivors land in `review/<slug>/` with a one-page `SUMMARY.md`, the compiled PDF, and all referee reports; on macOS you get a notification.

Everything is files: no server, no queue. `orchestrate.py` is a single stdlib-only script; each run's state lives in `runs/<slug>/state.json`. Re-running the orchestrator resumes wherever things left off, and it is safe to run from cron or concurrently (per-run lock files, stale locks self-clear).

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI (`claude`) on your PATH
- A LaTeX distribution providing `latexmk` and `pdflatex` (e.g. [TeX Live](https://tug.org/texlive/) or MacTeX) — used to compile and lint every note
- Python **3.12+** (the orchestrator itself is stdlib-only)
- Optional: `pdftotext` (`brew install poppler`) — only needed for PDF intake; arXiv IDs use LaTeX source directly and don't need it
- A venv for the generator's experiments: `python3 -m venv .venv && .venv/bin/pip install sympy mpmath`

## Quickstart

```bash
git clone <this repo> && cd proof-engineering
python3 -m venv .venv && .venv/bin/pip install sympy mpmath
export ANTHROPIC_API_KEY=sk-ant-...        # strongly recommended, see "Cost & auth"

echo "2505.11846" > inbox/queue.txt        # arXiv IDs, one per line (or drop PDFs in inbox/)
python3 orchestrate.py --drain             # run every paper to a terminal state
```

`python3 orchestrate.py` advances every non-terminal run by exactly one stage (the cron-friendly mode); `--drain` loops until everything reaches `review/` or `archive/`; `--only <slug>` restricts to one run. For unattended operation, put `python3 orchestrate.py` on a cron schedule and drop papers in `inbox/` whenever — `max_runs_per_day` in `config.toml` caps intake, and extra inbox items stay queued.

## Cost & auth — read this before running unattended

Each `claude -p` stage call is real LLM work. Observed costs on `sonnet`: explore $2–6, self_check ~$2, novelty $1–3, taste $1–2, correctness ×2 $2–4 — roughly **$8–17 per paper** end-to-end. Three cost layers, from softest to hardest:

1. `max_usd_per_run` caps each individual `claude -p` invocation (`--max-budget-usd`).
2. `max_usd_per_paper` caps a run's *cumulative* spend, checked before each stage launches. A run at the cap is **parked, not rejected**: all completed-stage progress stays in `state.json`, and raising the cap resumes it exactly where it stopped. Running out of API credits behaves the same way — billing errors are treated as transient, so refill and re-invoke.
3. A workspace spend limit in the [Anthropic Console](https://console.anthropic.com) — the only cap enforced server-side no matter what the pipeline does. Set one before running unattended.

**Use an `ANTHROPIC_API_KEY` (console.anthropic.com) rather than a Claude subscription for this.** Without an API key, `claude -p` authenticates via your subscription's OAuth and shares its *session-based* rate limit with your own interactive usage — an autonomous pipeline can silently eat your whole session window, and `max_usd_per_run` does not protect against that. The orchestrator treats 429/session-limit/overload responses as transient (it pauses the run and retries on a later invocation instead of consuming an attempt or archiving), but on a subscription the pipeline and your own usage still starve each other.

For fully reproducible unattended runs, add `"--bare"` to `claude_extra_args` in `config.toml`: it isolates pipeline calls from your global Claude Code config (hooks, plugins, personal `CLAUDE.md` files, MCP servers). Note `--bare` authenticates **only** via `ANTHROPIC_API_KEY`. The default (`--strict-mcp-config`) already keeps your MCP servers out of pipeline calls.

### Running on a Claude subscription instead (zero marginal cost, slow)

If you have a Claude Pro/Max subscription and would rather spend session quota than dollars, the pipeline supports that: when it hits your session limit, every pending run pauses in place and resumes after the window resets (~5 hours). Setup:

1. **Keep `ANTHROPIC_API_KEY` out of the orchestrator's environment** — the CLI prefers the key whenever it's set. If it lives in your shell profile, strip it per-invocation:
   ```bash
   env -u ANTHROPIC_API_KEY python3 orchestrate.py --drain
   ```
   `claude -p` then falls back to your subscription's OAuth login. (The preflight warning about the missing key is informational — expected in this mode.)
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

## Tuning

**Prompts are files** — tuning the system means editing `prompts/*.md` and `PROMPT.md` (the research brief the generator executes; edit it to point the pipeline at your own field and standards), not code. `config.toml` holds the knobs:

| key | meaning |
|---|---|
| `generator_model`, `self_check_model`, `taste_model`, `referee_model` | model per stage (`sonnet`, `opus`, `haiku`, …) |
| `max_runs_per_day` | intake quota; extra inbox items stay queued |
| `max_usd_per_run` | `--max-budget-usd` cap per `claude -p` call |
| `max_usd_per_paper` | cumulative cap across a run's stages — at the cap the run is *parked* (progress kept), and resumes if you raise the cap; `0` disables (use on subscription auth) |
| `max_explore_attempts`, `max_self_check_attempts` | retries before archiving |
| `max_referee_attempts` | operational retries per referee stage (crashes/malformed verdicts — never counts a real rejection) |
| `stage_timeout_seconds` | wall-clock limit per stage invocation |
| `claude_extra_args` | extra flags for every `claude -p` call (see Cost & auth) |
| `notify` | macOS notification when a run reaches `review/` |
| `referee_command` | reserved hook for a cross-provider correctness referee (not wired in yet) |

## What "verified" actually means

Be honest with yourself about the epistemics: apart from the self-check's computational counterexample search, every gate is an LLM refereeing an LLM, and **LLM referees share failure modes with LLM generators**. Two models agreeing is much weaker evidence than one counterexample search. A result in `review/` has *survived independent refereeing* — it has not been formally verified. A Lean formalization track (statement-level typechecking as a cheap sanity gate) is planned but not wired in; see `PLAN.md`.

## Layout

```
PROMPT.md         the research brief the generator executes — edit for your field
PLAN.md           full design spec
orchestrate.py    the entire orchestrator (stdlib only)
config.toml       models, budgets, retries
prompts/          one markdown prompt per stage
inbox/            drop PDFs or a .txt of arXiv IDs here
runs/<slug>/      in-flight runs (state.json, note.tex, result.json, referee/)
review/<slug>/    passed both gates — SUMMARY.md, note.pdf, referee reports
archive/<slug>/   rejected, each with rejection.md stating why
taste/            your accept/reject calibration corpus
```
