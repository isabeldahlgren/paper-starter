---

You are working in the current directory, which is a run directory for one paper. The paper source is under `./paper/` (LaTeX source if it came from arXiv, or `paper.txt` extracted from a PDF). Read it before doing anything else. Read each source file once, in full, near the start; keep it in mind rather than re-reading or re-catting the whole file again later — if you need to look something up afterward, use a targeted `grep`/`sed` for that passage instead. `./paper/` has already been stripped of figures, style files, and bibliography data (none of it carries mathematical content), so there is no reason to go looking for or trying to open any image, PDF, `.sty`, `.cls`, or `.bib` file — if one is somehow present, skip it.

Follow the research brief above exactly. When you are done, before finishing, you must have written exactly these two files in the current directory:

1. `note.typ` — the research note, in Typst, using the `unequivocal-ams` package (AMS-article style). One paragraph per line (no wrapped paragraphs across multiple lines). **Exposition quality is the top priority** — this matters as much as correctness. Concretely:
   - Include a brief abstract stating the question and the answer.
   - State the main result(s) clearly and prominently near the beginning, right after the introduction — a reader should know exactly what was proved within the first half page, not have to hunt for it after pages of setup.
   - Include an explicit paragraph (in the introduction) making clear what this note *adds* relative to the source paper: which question it answers that the paper leaves open, which of its results are used as a black box vs. re-derived, and what is genuinely new here. Do not let this novelty statement be implicit or scattered — state it directly.
   - Structure: abstract, introduction (motivation + main result + novelty statement), definitions, examples, precise statements, proofs, open questions, references.
   - It must compile standalone: run `typst compile note.typ note.pdf` yourself before finishing and fix any compiler errors.

   Start the file exactly like this:
   ```typ
   #import "@preview/unequivocal-ams:0.1.2": ams-article, theorem, proof

   #show: ams-article.with(
     title: [Your title],
     authors: ((name: "proof-engineering"),),
     abstract: [Your brief abstract.],
   )
   ```
   The package only exports `theorem` and `proof` (both styled, numbered). It does *not* export `lemma`, `corollary`, `definition`, `proposition`, or `remark` — if you need those (you almost always will), define them yourself by mirroring the package's own `theorem` function, e.g.:
   ```typ
   #let lemma(body, numbered: true) = figure(
     body, kind: "theorem", supplement: [Lemma],
     numbering: if numbered { n => counter(heading).display() + [#n] },
   )
   #let corollary(body, numbered: true) = figure(
     body, kind: "theorem", supplement: [Corollary],
     numbering: if numbered { n => counter(heading).display() + [#n] },
   )
   #let definition(body, numbered: true) = figure(
     body, kind: "theorem", supplement: [Definition],
     numbering: if numbered { n => counter(heading).display() + [#n] },
   )
   ```
   (same `kind: "theorem"` on purpose — they share one running counter per section, which is standard AMS numbering: "Theorem 2.1, Lemma 2.2, Corollary 2.3"). A plain, non-machine-managed "References" section (a heading plus a formatted list of citations) is perfectly acceptable — you do not need to build a working `.bib` file.

2. `result.json` — a machine-readable index of every claim in the note, in this exact schema:

```json
{
  "paper": "arxiv:XXXX.XXXXX",
  "claims": [
    {
      "id": "claim-1",
      "label": "theorem-complete-proof | theorem-sketch | conjecture-with-evidence | computation | speculation",
      "statement": "precise, self-contained statement of the claim",
      "proof_status": "unchecked"
    }
  ]
}
```

Rules for `result.json`:
- Every claim in `note.typ` that is stated as a theorem, proposition, conjecture, or computed fact must appear here with a matching `id` referenced in the note (e.g. a label you can cross-reference).
- `label` must be honest. `theorem-complete-proof` means you have actually verified every step yourself, not that it "should" work. If you have a gap, use `theorem-sketch`. If it is evidence-based rather than proved, use `conjecture-with-evidence`.
- Set every claim's `proof_status` to `"unchecked"` — a separate self-check pass will update this field. Do not mark your own work verified.

If you run any computation or numerical experiment (e.g. checking a conjecture on small cases, symbolic verification), save the actual script under `./experiments/` so it can be re-run, not just its output. Use `{{VENV_PYTHON}}` to run Python (it has `sympy` installed); do not rely on a bare `python3` unless you have confirmed what packages it has.

**Never background a computation** — no `&`, `nohup`, `disown`, and do not use your Bash tool's `run_in_background` option. This is a single, one-shot session with no future turn to come back to — if you end your turn while anything is still running in the background, its output is gone, not picked up later, and you will have produced nothing. Run every command in the foreground and wait for it to finish before continuing. If a computation might be slow, make it smaller (fewer cases, a shorter range) rather than backgrounding it — a modest but completed check beats an ambitious one that never finishes.

Do not optimize for the number of claims. One genuinely interesting, correctly labeled result is worth more than five shallow ones. If nothing in this direction pans out after real effort, write up the strongest correct partial result honestly labeled — do not inflate a computation into a theorem.
