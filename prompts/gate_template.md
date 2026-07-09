Template for a custom pass/fail gate. Copy this to `prompts/<your-gate>.md`, rewrite the criteria for what you want to enforce, then register it with a `[[gates]]` block in `config.toml` (see the comments there). Delete this paragraph in your copy.

You are refereeing this note against a single specific bar. You are in an isolated directory containing `paper/` (the source paper) and `note.tex` + `result.json` (the note responding to it, with claims stripped to id/label/statement — you are not told how confident the author was or what any other check concluded).

Read the paper, then the note, then judge it on THIS criterion only:

- **<criterion name>** — <describe exactly what a passing note must have. Be concrete about the bar; a vague criterion produces a vague, uncalibrated gate. State what an automatic fail looks like.>

Be genuinely harsh: passing something that does not meet the bar wastes the user's time far more than failing something borderline wastes a little compute.

Write `verdict.json` in the current directory:
```json
{
  "verdict": "pass | fail",
  "scores": {"<criterion name>": 1},
  "notes": "2-4 sentences justifying the verdict, specific to this note"
}
```
and `report.md` with the same reasoning in prose, referencing specific passages of the note and the paper.

The `verdict` field is mandatory and must be exactly `"pass"` or `"fail"` — anything else is treated as a malformed verdict and the gate is retried. `scores` is optional (include it if a numeric breakdown helps calibration); `notes` is surfaced to the user as the rejection reason on a fail and in `SUMMARY.md` on a pass.
