You are applying a fixed list of corrections to a research note. You are in a run directory containing `note.tex`, `result.json`, experiment scripts under `./experiments/`, and `./self_check/fixes.md`. An adversarial self-check pass has already verified the mathematics; `fixes.md` lists the concrete minor defects it found (exposition errors, prose contradicting a table, mislabeled script output). Your only job is to apply exactly those fixes.

Rules:
- Apply every fix listed in `./self_check/fixes.md`, and nothing else. Do not improve, restructure, or re-derive anything not listed there.
- You may edit `note.tex`, scripts under `./experiments/`, and the `"statement"` text of claims in `result.json` where a fix requires it. Never change a claim's `id`, `label`, or `proof_status`, and never add or remove claims.
- Preserve the note's conventions: one paragraph per line, `amsart` environments, `\label`/`\ref` cross-references.
- When done, recompile with `latexmk -pdf note.tex` and fix any compile error you introduced. **Never background anything** — no `&`, no `nohup`, no `run_in_background`; this is a single one-shot session, so run every command in the foreground and wait for it to finish.
