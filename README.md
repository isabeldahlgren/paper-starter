# paper-starter

You write the math with a strong model in a chat window; **paper-starter does the three things a chat window can't.** Run [`GPT-PROMPT.md`](GPT-PROMPT.md) (or your own brief) in ChatGPT to produce candidate research notes — one folder per project, each with a `note.tex` and a `result.json`. Drop those folders in `inbox/`, and on your Claude subscription this tool:

1. **cross-checks** each note with a *different* model — re-derives the proofs, checks the literature, judges the taste. A note GPT wrote, refereed by Claude, is genuine independent verification;
2. **formalizes** the proved claims in Lean via [Aristotle](https://aristotle.harmonic.fun) (free, non-gating bonus);
3. **ranks** everything that survives into a `PRIORITY.md` reading list — because your reading time is the bottleneck.

```
inbox/ ──► crosscheck ──► lean ──► review/ ──► PRIORITY.md
(a GPT     (a different    (Aristotle  │        (ranked
 project    model           proves)    └─► archive/  reading list)
 folder)    referees it)                   (with reasons)
```

Everything is files — no server, no queue. `orchestrate.py` is one stdlib-only script; each run's state lives in `runs/<slug>/state.json`. Re-running it resumes where things left off, and it is safe to run from cron or concurrently.

## Requirements

- The [Claude Code](https://claude.com/claude-code) CLI (`claude`) on your PATH, logged in
- `latexmk` + `pdflatex` (from [TeX Live](https://tug.org/texlive/) or MacTeX) — compiles each note to a PDF and lints it
- Python **3.12+** (the orchestrator is stdlib-only)
- A venv so the cross-checker can re-run computations: `python3 -m venv .venv && .venv/bin/pip install sympy mpmath`
- *Optional:* the `aristotle` CLI (`uv tool install aristotlelib`) + `ARISTOTLE_API_KEY` for the free Lean stage

## Quickstart

```bash
git clone <this repo> && cd paper-starter
python3 -m venv .venv && .venv/bin/pip install sympy mpmath

# 1. In ChatGPT, run GPT-PROMPT.md on your paper(s). Save each result as a
#    folder containing note.tex + result.json.
# 2. Drop those folders into inbox/:
mv ~/Downloads/my-note-folder inbox/

python3 orchestrate.py --drain      # cross-check + formalize everything, then rank
```

This runs on your Claude subscription by default (zero marginal cost) — just make sure `claude` is logged in. Add an `ANTHROPIC_API_KEY` in `.env` (copy from `.env.example`) if you'd rather pay per token and not touch your session limits — see [DOCS.md → Cost & auth](DOCS.md#cost--auth).

- `python3 orchestrate.py` advances every run by one stage (the cron-friendly mode).
- `--drain` loops until everything is terminal, then regenerates `PRIORITY.md`.
- `--only <slug>` restricts to one run; `--rank` just rebuilds `PRIORITY.md`.
- `--status` prints where every run stands without advancing anything.

Survivors land in `review/<slug>/` with a one-page `SUMMARY.md`, the compiled PDF, and the full crosscheck report; rejects land in `archive/<slug>/` with a `rejection.md`. Start at `PRIORITY.md`.

## The files you edit

Tuning the system means editing text, not code:

| File | What it controls |
|---|---|
| **`GPT-PROMPT.md`** | The generation brief you paste into ChatGPT. Make it specific to your field. (paper-starter never runs it — you do.) |
| **`prompts/crosscheck.md`** | How Claude referees each note: the correctness, novelty, and taste bar. |
| **`prompts/lean.md`** | The instruction sent to Aristotle for Lean formalization. |
| **`prompts/rank.md`** | How the reading list is ordered. |
| **`config.toml`** | Referee model, budgets, retries, the Lean toggle. |

**[DOCS.md](DOCS.md)** is the full customization guide — the config reference, the input-folder contract, subscription-vs-API auth, and the Lean track. **[PLAN.md](PLAN.md)** is the design spec.

## What "passed crosscheck" actually means

Be honest about the epistemics: the crosscheck is one model refereeing another's note. Because GPT wrote it and Claude checks it, this is real *cross-model* independence — a much stronger signal than a model reviewing its own work — but it is still LLM refereeing, not proof. A note in `review/` has *survived independent cross-model refereeing*. The one hard exception is a claim marked `proof-formalized ✓✓`, which carries a machine-checked Lean proof from the Aristotle track (still confirm the Lean statement says what the note says).
