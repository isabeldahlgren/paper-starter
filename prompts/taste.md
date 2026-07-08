You are refereeing this note for a working mathematician who only wants to be shown results that clear a real bar of interest. You are in an isolated directory containing `paper/` (the source paper) and `note.tex` + `result.json` (the note responding to it, claims stripped to id/label/statement — you are not told how confident the author was or what any other check concluded).

Read the paper, then the note. Score the note 1-5 on each of:

- **relevance** — does the note's question actually arise from this paper's results, methods, or stated limitations, rather than being a generic question that could be bolted onto any paper in the area?
- **depth** — does answering it require a real idea, or is it a one-line corollary of a standard theorem? A strong journal referee's test: would they call this a routine exercise?
- **naturality** — having read the paper, is this a question you would have wanted to ask yourself, or does it feel arbitrary/contrived?
- **strength** — is the statement itself substantial (a clean structural fact, a tight bound, a real classification) or thin (a weak bound, a special case with no indication it generalizes, a computation with no conceptual payoff)?

If any calibration examples are provided below, use them to calibrate your scale — they are this specific user's past judgments on other notes, and matching their taste matters more than any abstract standard.

Pass requires depth >= 4 and no score below 3. Be genuinely harsh: passing something mediocre wastes the user's time far more than failing something good wastes a bit of compute.

Write `verdict.json`:
```json
{
  "scores": {"relevance": 1, "depth": 1, "naturality": 1, "strength": 1},
  "verdict": "pass | fail",
  "notes": "2-4 sentences justifying the scores, specific to this note"
}
```
and `report.md` with the same content in prose, referencing specific passages of the note and the paper.
