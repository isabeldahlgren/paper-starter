# paper-starter — Design Spec

A triage-and-verification engine for pure mathematics. **Generation happens elsewhere:** the user runs a strong model (e.g. GPT via `GPT-PROMPT.md`) in a chat window and produces candidate research notes — one folder per project, each with a `note.tex` and a `result.json` claim index. paper-starter ingests those folders and does only what a chat window can't: independent **cross-model verification**, **Lean formalization**, and **ranking** into a reading list. Optimize for the quality of what reaches the user's eyes; the user's reading time is the scarcest resource.

This document is the spec. Where it is silent, choose the simplest option.

## Design principles

1. **Filesystem is the database.** No server, no queue. Each candidate note gets a run directory; a `state.json` records the stage. The orchestrator is resumable and idempotent: re-running it continues unfinished runs, and it is safe to invoke concurrently (per-run lock file).
2. **Prompts are files.** The two judgments the system makes — the crosscheck and the ranking — are markdown prompts in `prompts/`, executed via headless Claude Code (`claude -p`). Tuning the bar means editing prompts, not code.
3. **Cross-model independence is the whole point.** The note's author (GPT) and its referee (Claude) are *different model families*. That is a materially stronger signal than a model reviewing its own work — and it's the one guarantee a single chat window cannot provide. Verifier isolation is enforced by the filesystem: the referee sees only the source paper, `note.tex`, and a `result.json` stripped to id/label/statement — never the author's own confidence, self-report, or proof-status.
4. **Honest claim labels.** Every claim carries a label: `theorem-complete-proof` | `theorem-sketch` | `conjecture-with-evidence` | `computation` | `speculation`. Only `theorem-complete-proof` claims must survive line-by-line correctness refereeing; a note may still pass on the strength of a well-evidenced conjecture, but the labels must survive.
5. **False positives are ~10× worse than false negatives.** A sloppy note reaching review wastes the user's reading time; a good note archived costs only a re-drop. All thresholds err toward rejection, and code enforces the two rules that most protect the user (no broken proof passes; nothing all-known passes) regardless of the referee's overall verdict. Every rejection is archived with reasons.

## Directory layout

```
paper-starter/
  GPT-PROMPT.md          # the generation brief the USER runs in a chat window (not executed here)
  PLAN.md                # this file
  orchestrate.py         # single stdlib-only orchestrator
  config.toml            # referee model, budgets, Lean toggle
  prompts/
    crosscheck.md        # the one referee pass: correctness + novelty + taste -> verdict.json
    lean.md              # the natural-language instruction sent to `aristotle submit`
    rank.md              # orders review/ into PRIORITY.md
  inbox/                 # user drops GPT project folders here (a dir with note.tex + result.json)
  runs/<slug>/
    note.tex, result.json, <aux files carried from the folder>
    paper/               # source paper (fetched from the arXiv id in result.json, for the referee)
    crosscheck_view/     # isolated referee cwd (paper + note + stripped result.json)
    referee/             # crosscheck.json + crosscheck_report.md
    lean/                # project/ (note.tex sent to Aristotle) + solution/ (downloaded) + ERROR.md
    state.json           # {stage, attempts, cost_usd, lean, history}
  review/<slug>/         # passed crosscheck, with SUMMARY.md + note.pdf
  archive/<slug>/        # rejected, with rejection.md stating why
  PRIORITY.md            # the ranked reading list across all of review/
```

## Pipeline stages

Stage flow: `intake → crosscheck → lean → review | archive`, then a global `rank`.

### Intake
Watch `inbox/`. Each immediate subdirectory with `note.tex` + `result.json` is a candidate. Validate `result.json` (a non-empty `claims` list; each claim has id/label/statement; label is valid). Copy the folder into `runs/<slug>/`; read the `paper` field and, if it is an arXiv id, fetch the LaTeX source into `paper/` (pruned of build junk and stripped of full-line TeX comments) so the referee can judge novelty and motivation. A note whose paper can't be fetched is still refereed. Malformed folders are skipped with a message. Set `stage: crosscheck`. `max_runs_per_day` throttles intake; surplus folders stay queued.

### Crosscheck (the one gate)
One isolated `claude -p` call, model `referee_model` (a *different* family than the generator), tools `Read Write Bash WebSearch WebFetch`, cwd = a view directory containing only `paper/` + `note.tex` + stripped `result.json`. It performs three checks and writes one `verdict.json`:
- **Correctness** — re-derive every `theorem-complete-proof` proof line by line from the note's own definitions; re-run finite computations with the venv Python rather than trusting the note's numbers. Per-claim verdict: `correct` / `fixable-gap` / `wrong` / `cannot-verify`.
- **Novelty** — a real literature check per substantive claim (several phrasings; especially existing follow-ups to the source paper). Per-claim: `novel` / `known` / `uncertain`, with a citation when known.
- **Taste** — score relevance/depth/naturality/strength 1–5.

The referee sets an overall `verdict: pass | fail`. **Pass → lean; fail → archive** with the reasons. On top of the referee's call, the orchestrator enforces two hard rules in code, so a generous grade can't sail a bad note through: fail if any `theorem-complete-proof` claim is worse than `correct`, or if every substantive claim is `known`. A missing/malformed verdict (or one omitting a load-bearing claim) is an operational error — retried up to `max_referee_attempts`, never a silent pass. A `fixable-gap` rejection records exactly what's missing, so the user can hand that feedback back to GPT and re-drop.

Compilation (`latexmk -pdf`) runs first as a lint and to produce `note.pdf`; a non-compiling note is flagged but still refereed from source.

### Lean (bonus evidence, non-gating)
Lean 4 formalization via the `aristotle` CLI (Harmonic's cloud autoformalization/proving service). Strictly **non-gating**: the note has already passed crosscheck, so no Aristotle outcome can archive it — the only exit is review. **One project per note** (not per claim): `aristotle submit "<prompt>" --project-dir <dir>` with `note.tex` as the payload and an editable natural-language prompt (`prompts/lean.md`, default: "formalise all results; if you find an actual error in a proof, try to fix it and record it in ERROR.md"). Because a proof can take hours, submission is **asynchronous** (*without* `--wait`); the run sits at the stage across orchestrator invocations, each tick polling with `aristotle tasks` (never `aristotle show`, which blocks while a task runs) and downloading the finished project (`aristotle download` → a `.tar.gz` unpacked into `lean/solution/`). The **note-level** outcome lands in `result.json` under `formalization`: `proof-formalized` (Aristotle `COMPLETE` **and** no `sorry`/no reported error — machine-checked, marked ✓✓ and weighted by the ranker), `statement-formalized` (`COMPLETE_WITH_ERRORS`, or a remaining `sorry`, or a reported error — never counted as a proof), or `not-formalizable` (failure / out-of-budget / `lean_timeout_minutes` exceeded, with a best-effort `aristotle cancel`). Aristotle's "too many requests" cap is stay-pending, not a failure. If Aristotle writes an `ERROR.md` (it found a real proof error the crosscheck missed), it is copied to `lean/ERROR.md` and surfaced loudly — a ⚠️ banner atop `SUMMARY.md` and a ⚠️ flag at the top of `PRIORITY.md` — but stays non-gating (most formalization failures mean "too hard," not "wrong"; the user decides on reading). Costs no Anthropic tokens; exempt from budget parking. Requires `ARISTOTLE_API_KEY` + the CLI (`uv tool install aristotlelib`); skipped straight to review without them or with `lean_enabled = false`.

### Review handoff
A note passing crosscheck lands in `review/<slug>/` with a mechanical `SUMMARY.md` (claims with their correctness/novelty/Lean status, taste scores, the honest verification caveat), `note.pdf`, and `referee/crosscheck_report.md`. macOS notification on handoff (`notify`).

### Rank
After a `--drain` (or on demand via `--rank`), one `claude -p` call reads a factual digest of every `review/` note and writes `PRIORITY.md`: a ranked reading list, best first, weighing depth/strength, verification strength (Lean-proved > referee-only), and novelty, with a one-line reason and caveat per note and a short "not recommended" section. A deterministic fallback ordering is written if the call fails, so `PRIORITY.md` is always produced.

## Orchestrator (`orchestrate.py`)

Single Python file, stdlib only. API keys (`ANTHROPIC_API_KEY`, `ARISTOTLE_API_KEY`) load from a gitignored `.env` at repo root (template `.env.example`; already-set environment variables win). Responsibilities: scan `inbox/`, advance each run one stage per invocation via `subprocess` calls to `claude -p` (`--permission-mode acceptEdits`, per-stage `--allowedTools`), enforce budgets, write `state.json` after every transition, finalize into `review/`/`archive/`, and rank. Run it under cron or a loop; concurrency-safe via a per-run lock file.

**Failure semantics** (kept distinct): *transient* (429/session-limit/overload — pause the run, no attempt consumed, retry later); *budget parking* (cumulative `cost_usd` ≥ `max_usd_per_paper` parks the run with progress kept, never archives; lean exempt); *operational* (crash / malformed verdict — retried up to `max_referee_attempts`); *substantive* (the referee's fail, or the two hard code rules — archives with a reason).

`config.toml` keys: `referee_model`, `max_runs_per_day`, `max_usd_per_run`, `max_usd_per_paper`, `max_referee_attempts`, `stage_timeout_seconds`, `claude_extra_args`, `notify`, `lean_enabled`, `lean_timeout_minutes`, `venv_python`.

## Known operational hazards

- **Never let a `claude -p` invocation background a long computation.** Each stage is a single one-shot call with no future turn — a deferred background job silently loses all output. The crosscheck prompt forbids it, and `run_claude()` hard-disables backgrounding via `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` (+ a wait-ceiling env var). These are undocumented CLI internals; re-verify (`grep -a` the CLI bundle for `BACKGROUND`) if a CLI upgrade changes backgrounding behavior.
- **Subscription session limits, not just dollar cost, can stall the pipeline.** With no `ANTHROPIC_API_KEY`, `claude -p` shares a session rate limit with interactive usage; `max_usd_per_run` does not protect against it. 429s are treated as transient. Subscription mode requires the key absent from *both* the shell and `.env`. Because only refereeing runs here (not generation), the session pressure is much lighter than a full generation pipeline would impose.

## Known weaknesses (do not paper over these)

- **The crosscheck is still LLM refereeing.** Cross-model independence (GPT author, Claude referee) is a real and meaningful improvement over self-review, but two LLMs can still share a blind spot. That is why the referee re-runs computations itself and why the Lean track exists. Never present a `review/` result as more than "survived independent cross-model refereeing" — except a ✓✓ Lean-proved claim, which is machine-checked.
- **Research-level formalization is usually out of reach.** Expect `not-formalizable` to be the common Lean outcome on genuinely interesting results; the statement-level check still catches real errors.
- **Garbage in, garbage out.** paper-starter judges what GPT produces; it cannot make a thin idea deep. The generation brief (`GPT-PROMPT.md`) is where output quality is set — the crosscheck only filters.

## History

This repo previously ran generation *inside* the pipeline: an `explore → self_check → repair → novelty → taste → correctness → lean` sequence that coaxed a note out of a headless generator and refereed it with same-family Claude passes. It was simplified to the current triage design at the user's request: a good one-shot generation prompt in a chat window produces better notes more token-efficiently, and moving generation out enabled genuine *cross-model* refereeing (GPT author vs. Claude referee) — the one thing the old same-model pipeline couldn't do. The Aristotle/Lean track and the filesystem-as-database core carried over unchanged. The output format is LaTeX + `amsart` (chosen for standard mathematical-writing conventions); the generation brief specifies the preamble and house style.

## Sources

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — single-file-edit, fixed-time-budget research loop; the minimalism this orchestrator borrows.
- [ZIB-IOL/The-Agentic-Researcher](https://github.com/ZIB-IOL/The-Agentic-Researcher) — sandboxed multi-agent research framework; source of the run-tracking-file pattern (`state.json`).
- [LeanConjecturer](https://arxiv.org/html/2506.22005v1) — LLM + rule-based generation of Lean 4 conjectures; motivates statement-level formalization as a cheap, high-yield check.
- [LeanMarathon](https://arxiv.org/html/2606.05400v1) — long-horizon autoformalization on Erdős problems; evidence that full-proof formalization is often out of reach but partial formalization is tractable.
