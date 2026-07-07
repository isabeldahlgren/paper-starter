You are refereeing a research note for correctness, blind. You are in an isolated directory containing only `note.typ` and `result.json` (claims stripped to id/label/statement — you have no access to the source paper, no prior referee reports, and no information about how confident the author was). The note is written to be self-contained; if it isn't, that itself is a defect.

For every claim labeled `theorem-complete-proof`, read the stated proof in `note.typ` line by line and re-derive it from the note's own definitions, without assuming any step follows just because it looks plausible. Your job is to find the error, not to confirm the author is right. For claims labeled `conjecture-with-evidence` or `computation`, check that the note's own stated evidence actually supports what it claims (e.g. that a cited computation's logic is sound), but do not demand a full proof of something the note itself does not claim to have proven.

For each claim, assign exactly one verdict:
- `"correct"` — the proof (or, for non-theorem claims, the stated evidence) holds up under your own independent re-derivation.
- `"fixable-gap"` — there is a specific missing or under-justified step, but the overall approach is sound and you can say precisely what would need to be added.
- `"wrong"` — the claim as stated is false, or the proof has an error that isn't a mere gap (e.g. it proves a different, weaker, or subtly different statement).
- `"cannot-verify"` — the note relies on something you cannot check from its own content (e.g. an unstated external result), and you cannot determine correctness either way.

Write `verdict.json`:
```json
{
  "claims": [
    {"id": "claim-1", "verdict": "correct | fixable-gap | wrong | cannot-verify", "notes": "specific reason, citing the exact step if not correct"}
  ]
}
```
and `report.md`: a line-by-line referee report, precise enough that someone could locate and fix any gap you found without re-deriving it themselves.
