You are refereeing a research note, adversarially. You are in the run directory. Read `note.tex` and `result.json`. You have no information about how confident the author was — treat every claim as suspect until you have personally re-derived it. Your job is to find the error, not to confirm the author is right. `note.tex` is meant to be self-contained (definitions, statements, and proofs restated in full) — you should not need `./paper/`; only open it if the note is genuinely ambiguous about what the source paper established, and if you do, read it once rather than repeatedly.

Do two things for every claim in `result.json`:

1. **Re-derivation.** For every claim labeled `theorem-complete-proof`, work through the proof in `note.tex` line by line yourself, from definitions to conclusion, without assuming any step. Do not skim. If a step doesn't follow, that is a gap even if the surrounding argument looks plausible.

2. **Counterexample search.** For every claim that is universally quantified over a domain with small or enumerable instances (small integers, small finite groups, low-degree polynomials, etc.), write a Python script under `./self_check/` and run it with `{{VENV_PYTHON}}` to test as many small cases as feasible. Save the script and its output. A single confirmed counterexample is a hard failure for that claim regardless of how the proof reads. **Never background this** — no `&`, `nohup`, and do not use your Bash tool's `run_in_background` option. This is a single, one-shot session with no future turn to come back to, so anything left running in the background when you end your turn is simply lost. Run it in the foreground and wait; if it might be slow, shrink the search space rather than backgrounding it.

When you are done, update `result.json` in place: keep every existing field, and set each claim's `proof_status` to exactly one of:
- `"verified"` — you independently re-derived it (or exhaustively checked it computationally) and found no issue.
- `"gap-found"` — the proof has a specific step that does not follow; explain which one.
- `"counterexample-found"` — you found and verified a concrete counterexample.
- `"unchecked"` — only if the claim is genuinely outside what you can cheaply re-derive or enumerate (e.g. it depends on a deep external result you cannot verify from first principles in this session). Use this sparingly — it is not a safe default.

Also add a string field `"self_check_notes"` to each claim explaining your verdict in one or two sentences (the specific gap, or the counterexample, or what you checked).

Finally, write `./self_check/report.md`: a short referee report listing every claim, its verdict, and — for anything not `verified` — the specific reason, in enough detail that someone could reproduce your finding without rerunning your search.

Be harsh. A note that survives this pass because you were lenient wastes the user's time later; a note that fails this pass because you found a real gap saves it.
