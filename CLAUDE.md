# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`proof-engineering` is an autonomous research pipeline for pure mathematics. It reads a paper, executes `PROMPT.md` against it via headless Claude Code, and iterates until a result independently passes both a taste gate and a correctness gate. It optimizes for quality over volume — a yield of one reviewable result per ~10 papers is considered success. `PLAN.md` is the authoritative spec (design principles, directory layout, pipeline stages, milestones); read it before making structural changes, since this file only summarizes it.

## Commands

- Run one step of the pipeline (intake + advance every non-terminal run by one stage): `.venv/bin/python3 orchestrate.py`
- Drain every run to a terminal state (review/archive), looping stages until nothing progresses: `.venv/bin/python3 orchestrate.py --drain`
- Advance only one run, ignoring all others: `.venv/bin/python3 orchestrate.py --only <slug>` (e.g. `arxiv-2505.11846`)
- Compile a note by hand: `typst compile note.typ note.pdf` (run inside the relevant `runs/<slug>/` directory — `orchestrate.py` also does this as a lint after the explore stage)
- Python deps live in `.venv` (stdlib `orchestrate.py` itself has none; `sympy`/`mpmath` are there for generator-stage experiments): `.venv/bin/python3 -m pip install <pkg>`

There is no linter or build step beyond the Typst compile check baked into the `explore` stage. Orchestrator logic changes should be exercised offline (monkeypatch `run_claude` and point `orchestrate.ROOT` at a scratch directory) — the failure-path behavior below is load-bearing and cheap to test without any LLM calls.

## Architecture

**Filesystem is the database.** There is no server or queue. Each paper gets `runs/<slug>/` with a `state.json` recording `{stage, attempts, feedback, history}`. `orchestrate.py` is a single stdlib-only script that is resumable and idempotent — re-invoking it just continues unfinished runs — and safe to call concurrently via a per-run `.lock` file (see `run_lock`).

**Pipeline stages**, driven by `advance()` in `orchestrate.py`, one stage per invocation: `explore → self_check → novelty → taste → correctness → review|archive`. Each stage (except intake/finalize) shells out to `claude -p` with a prompt built from a file in `prompts/` (`explore.md`, `self_check.md`, `novelty.md`, `taste.md`, `correctness_referee.md`) plus `PROMPT.md` where relevant. **Tuning the system means editing prompts, not code.**

- `explore` (generator, `Read Write Edit Bash`): produces `note.typ` (Typst, `unequivocal-ams` template) and `result.json` (claims with `label` ∈ `{theorem-complete-proof, theorem-sketch, conjecture-with-evidence, computation, speculation}` and `proof_status` ∈ `{unchecked, verified, gap-found, counterexample-found}`). Gated by `compile_typst()` and `validate_result_json()` before advancing.
- `self_check` (cheap kill, fresh context): adversarially re-derives `theorem-complete-proof` claims and runs counterexample search. Any claim left `gap-found`/`counterexample-found` sends the run back to `explore` (via `fail_to_explore_or_archive`), archiving once `max_explore_attempts` is exhausted.
- `novelty` / `taste` / `correctness`: **referee stages**. Verifier independence is enforced by the filesystem, not just prompting — `make_referee_dir()` copies only the allowed files (`paper/` + `note.typ` + a `result.json` stripped of `proof_status`/`self_check_notes` via `strip_result_for_referee()`) into a fresh subdirectory (`novelty_view/`, `taste_view/`, `correctness_view/`) and runs `claude -p` with that as `cwd` and no `--add-dir`, so referees cannot see prior reasoning, self-check notes, or each other's verdicts.
  - `taste` includes few-shot calibration built from `taste/accepted/` and `taste/rejected/` via `build_taste_calibration()` — this is how the system is meant to learn the user's taste over time. Currently empty by choice; the gate judges on the stated rubric alone.
  - `correctness` requires **two independent passes** to both return `correct` on every load-bearing (`theorem-complete-proof`) claim; `fixable-gap` on all bad claims loops back to `explore` once, anything else archives.
- `finalize()`: **moves** the run into `review/<slug>/` (passed both gates — with a mechanically assembled `SUMMARY.md` and a macOS notification) or `archive/<slug>/` (with a `rejection.md` stating why); `runs/` holds in-flight work only. Referee view dirs and the `.lock` are stripped from the finalized copy.

**Failure semantics** (three distinct kinds — do not conflate them when editing):
- *Transient* (429/session-limit/overload, detected by `is_transient_failure()`): `defer_stage()` leaves the run at its current stage with **no attempt consumed**; a later invocation retries. Never archive for these.
- *Operational* (invocation crash, missing/malformed/incomplete `verdict.json`): retried in place — `fail_or_archive()` for explore/self_check (per-stage `max_*_attempts`), `referee_stage_error()` for referee stages (`max_referee_attempts`). A verdict that omits a required claim id counts as malformed, never as a pass.
- *Substantive* (gap/counterexample found, known result, taste fail, wrong/cannot-verify): loops back to explore where the spec allows (`fail_to_explore_or_archive()`), otherwise archives via `reject()` with the reason.

**Config** (`config.toml`): per-stage model choice (`generator_model`, `self_check_model`, `taste_model`, `referee_model`), an unused `referee_command` hook for a non-Claude cross-provider referee, `max_runs_per_day` (enforced at intake; surplus inbox items stay queued), `max_usd_per_run` (a `--max-budget-usd` cap per `claude -p` call — does **not** protect against Claude Pro session-limit exhaustion, only per-dollar cost), `max_referee_attempts`, `stage_timeout_seconds`, `claude_extra_args` (extra flags for every `claude -p` call; defaults to `--strict-mcp-config` to keep the operator's MCP servers out of pipeline calls; `--bare` gives full config isolation but requires `ANTHROPIC_API_KEY`), `notify`, retry caps, and `venv_python`.

**Directories**: `inbox/` (drop PDFs or a `.txt` of arXiv IDs, one per line — watched by `intake()`), `runs/<slug>/` (in-flight), `review/<slug>/` (passed both gates), `archive/<slug>/` (rejected, with reason), `taste/{accepted,rejected}/` (calibration corpus the user curates by hand after reviewing).

## Output convention: Typst, not LaTeX

Notes are written in Typst using the `unequivocal-ams` template, which only exports `ams-article`, `theorem`, and `proof` (no `lemma`/`corollary`/`definition` — `prompts/explore.md` shows the model how to define those itself on the same counter). This was a deliberate reversion after trying LaTeX/amsart per user request. Per the top-level style guide (`~/CLAUDE.md`), Typst source is one line per paragraph and the style imitates a top mathematical journal — a brief abstract, main result(s) stated within the first half page, and an explicit "what this note adds vs. the source paper" paragraph.

## Known operational hazards

- **Never let a `claude -p` invocation background a long computation.** Each pipeline stage is a single one-shot call with no future turn — if the model defers to a background job (`&`, `nohup`, the Bash tool's `run_in_background`), the output is silently lost (no `note.typ`/`result.json` ever written). `prompts/explore.md` and `prompts/self_check.md` (the only stages with Bash access) explicitly forbid this.
- **Claude Pro session limits, not just dollar cost, can stall the pipeline.** With no `ANTHROPIC_API_KEY` set, `claude -p` authenticates via OAuth and shares a session-based rate limit with the user's interactive usage; `max_usd_per_run` does not protect against this. The orchestrator now treats 429/session-limit/overload as transient (run pauses at its stage, no attempt consumed) rather than archiving, but on a subscription the pipeline and interactive usage still starve each other. Check whether `ANTHROPIC_API_KEY` is set before assuming a stalled run is a bug.
- **Token cost lives mostly in the paper source.** `intake` prunes non-content files (figures, `.sty`/`.bib`/`.bbl`, build residue) and strips full-line TeX comments before the first LLM call — extend `PAPER_SOURCE_STRIP_EXTS`/`strip_tex_comments()` rather than prompting models to ignore junk.
