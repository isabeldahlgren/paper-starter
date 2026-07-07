You are doing a literature review, not a token search. You are in an isolated directory containing `paper/` (the source paper this note responds to), `note.typ` (a research note), and `result.json` (the note's claims, stripped to id/label/statement only — you are not told how confident the author was).

For every claim in `result.json` labeled `theorem-complete-proof` or `conjecture-with-evidence` (skip `computation` and `speculation` — those are not the kind of thing that gets "scooped"), determine whether it is already known. Treat this as a real referee's literature check, not a formality:

1. Search arXiv and the web using several *different* phrasings per claim — the note's own terminology, the source paper's terminology, and standard terminology for the underlying mathematical objects (these often differ). A single query is not enough.
2. Check whether anyone has already published a follow-up to the source paper itself — search for the paper's title, its arXiv ID, and its authors' names combined with terms from the claim. A result proved here is most likely to already exist as someone else's follow-up to the same paper, so this is the highest-value search to get right.
3. Skim the source paper's own related-work section and bibliography for near-misses — a claim that turns out to be a small corollary of a paper already cited in `paper/` counts as known, even if the note doesn't say so.

Only mark a claim `"known"` if you can point to an actual matching or subsuming statement — title, authors, arXiv ID or DOI, and which theorem/section. Do not mark `"known"` on vague thematic resemblance. Mark `"novel"` if, after real searching, you found nothing that states or subsumes the claim. Use `"uncertain"` only when you searched properly and the honest answer is "I can't tell" (e.g. the area is far outside standard indexing) — not as a shortcut for skipping the search.

When done, write two files in the current directory:

- `verdict.json`:
```json
{
  "claims": [
    {"id": "claim-1", "novelty_status": "novel | known | uncertain", "reference": "citation if known, else empty string"}
  ]
}
```
- `report.md`: for each claim, the searches you actually ran and what you found (or didn't), with links. Enough detail that someone could redo your search and get the same answer.
