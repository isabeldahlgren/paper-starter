# paper-starter

An autonomous research pipeline for pure mathematics. You drop papers into an inbox; it reads each one as a research collaborator, finds a follow-up question, works it out (proof, counterexample search, computation), writes a short LaTeX note — and only shows you results that independently pass a **taste gate** and a **correctness gate**.

It optimizes for quality over volume: **one reviewable result per ~10 papers is success.** Everything else is archived with a reason. Bad results reaching you are treated as far worse than good results archived, so every gate errs toward rejection.

```
inbox/ ──► explore ──► self_check ──► novelty ──► taste ──► correctness ──► lean ──► review/
              ▲            │                                (2 blind      (optional,     │
              └────────────┴──── gaps / known result ────── passes)       non-gating)    └─► archive/
                                 loop back to rewrite                                        (with reasons)
```

Everything is files — no server, no queue. `orchestrate.py` is one stdlib-only script; each run's state lives in `runs/<slug>/state.json`. Re-running it resumes where things left off, and it is safe to run from cron or concurrently.

## Requirements

- The [Claude Code](https://claude.com/claude-code) CLI (`claude`) on your PATH
- `latexmk` + `pdflatex` (from [TeX Live](https://tug.org/texlive/) or MacTeX) — used to compile and lint every note
- Python **3.12+** (the orchestrator is stdlib-only)
- A venv for the generator's experiments: `python3 -m venv .venv && .venv/bin/pip install sympy mpmath`
- *Optional:* `pdftotext` (`brew install poppler`) for PDF intake; arXiv IDs don't need it
- *Optional:* the `aristotle` CLI (`uv tool install aristotlelib`) + `ARISTOTLE_API_KEY` for the free Lean stage

## Quickstart

```bash
git clone <this repo> && cd paper-starter
python3 -m venv .venv && .venv/bin/pip install sympy mpmath
cp .env.example .env                       # then add your ANTHROPIC_API_KEY

echo "2505.11846" > inbox/queue.txt        # arXiv IDs, one per line (or drop PDFs in inbox/)
python3 orchestrate.py --drain             # run every paper to review/ or archive/
```

- `python3 orchestrate.py` advances every run by one stage (the cron-friendly mode).
- `--drain` loops until everything is terminal; `--only <slug>` restricts to one run.
- `--status` prints where every run stands without advancing anything.

Survivors land in `review/<slug>/` with a one-page `SUMMARY.md`, the compiled PDF, and all referee reports (on macOS you also get a notification). Rejections land in `archive/<slug>/` with a `rejection.md`.

> **Before running unattended, read [DOCS.md → Cost & auth](DOCS.md#cost--auth).** Each paper costs roughly $8–17 in LLM calls. Strongly prefer an `ANTHROPIC_API_KEY` over your Claude subscription, and set a spend limit in the [Anthropic Console](https://console.anthropic.com).

## The three files you edit

Tuning the system means editing text, not code:

| File | What it controls |
|---|---|
| **`PROMPT.md`** | The research brief the generator runs on every paper — make it specific to your field. |
| **`config.toml`** | Models, budgets, retries, the gate list, the Lean toggle. |
| **`prompts/*.md`** | One prompt per stage; editing them changes how results are made and judged. |

**[DOCS.md](DOCS.md)** is the full customization guide — the config reference, how to add your own pass/fail gates with no code, the subscription-vs-API auth details, teaching the taste gate, and the Lean track. **[PLAN.md](PLAN.md)** is the design spec.

## What "verified" actually means

Be honest about the epistemics: apart from the self-check's computational counterexample search, every gate is an LLM refereeing an LLM, and **LLM referees share failure modes with LLM generators**. A result in `review/` has *survived independent refereeing* — it has not been formally verified. The one exception is a claim marked `proof-formalized ✓✓`, which carries a machine-checked Lean proof from the Aristotle track (still confirm the Lean statement says what the note says).
