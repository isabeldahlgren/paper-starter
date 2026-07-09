You are working in the current directory, which is a run directory for one paper. The paper source is under `./paper/` (LaTeX source if it came from arXiv, or `paper.txt` extracted from a PDF). Read it before doing anything else. Read each source file once, in full, near the start; keep it in mind rather than re-reading or re-catting the whole file again later — if you need to look something up afterward, use a targeted `grep`/`sed` for that passage instead. `./paper/` has already been stripped of figures, style files, and bibliography data (none of it carries mathematical content), so there is no reason to go looking for or trying to open any image, PDF, `.sty`, `.cls`, or `.bib` file — if one is somehow present, skip it.

**Literature check before you commit.** Once you know what you intend to prove — and again if you later pivot to a different main result — spend a few WebSearch/WebFetch queries checking whether that result is already known, *before* writing the note. Search the statement in your own terminology, in the source paper's terminology, and in the standard terminology of the underlying mathematical objects (these often differ), and ask specifically whether it is a corollary of a classical theorem in the area: if your argument reduces the problem to a clean univariate or special-case statement, assume that reduced statement is decades old until searching says otherwise. A known theorem re-proved is a failed run no matter how elegant the proof, and a downstream novelty referee will catch it after all your work is spent — so if the search finds the result, cite it as background and redirect your effort to what is actually open. Fold what you learned into the note's prior-work discussion, written as scholarly prose (see below) — never as a narration of the search itself. Keep this proportionate: a handful of searches at each decision point, not a running literature review.

Follow the research brief above exactly. When you are done, before finishing, you must have written exactly these two files in the current directory:

1. `note.tex` — the research note, in LaTeX, using the `amsart` document class. One paragraph per line (no wrapped paragraphs across multiple lines). **Exposition quality is the top priority** — the target is a note that could be submitted to a good journal as-is, and this matters as much as correctness. Follow standard conventions for good mathematical writing: *italics* (`\emph{}`) for terms at the point they are defined, inline math (`$...$`) for symbols and short expressions embedded in prose. Structure: abstract; introduction (motivation, main results, contributions, prior work); notation/preliminaries; statements and proofs with examples; open questions; references.

   **Front matter.**
   - Give the note a concise, specific title — a phrase, not a sentence-length summary of the results; if you are tempted to write "and its consequence for..." or similar, shorten it. If the title is long, supply a short running head via the optional argument: `\title[Short running head]{Full title}` (never use `\markboth` by hand).
   - The abstract states the question and the answer in a few sentences, with no citations and no `\ref`s.

   **Introduction.** A reader must know exactly what was proved within the first page. Concretely:
   - After a short motivating paragraph, state the main result(s) *as numbered theorem environments in the introduction itself* (with `\label`s), not as prose paraphrases pointing at a later section. If a statement needs notation too heavy for the introduction, state a clean special case there and reference the full version.
   - Include an explicit contributions paragraph making clear what this note *adds* relative to the source paper: which question it answers that the paper leaves open, which of its results are used as a black box vs. re-derived, and what is genuinely new here. Do not let this be implicit or scattered — state it directly, and if there are several contributions, enumerate them.
   - Include a prior-work discussion (`\subsection*{Relation to prior work}` or a clearly-marked paragraph) that situates the note relative to the source paper and the neighboring literature, citing each relevant work properly. This is where the outcome of your literature check lives, written as normal scholarly prose: "To the best of our knowledge, X has not been considered; the closest results are ... [\cite], which differ in ...". **Never narrate the research process**: no tool names ("WebSearch"), no search dates, no "we searched and found nothing" — a journal referee should see a related-work discussion, not a lab notebook.

   **Prose and citations.**
   - Never use a bracketed citation as a noun or a possessive: not "[1]'s theorem", not "as shown in [1]'s proof". Name the authors and attach the citation: "the rigidity theorem of Henry, Marchetti, and Kohn~\cite{HMK}", "Theorem~3.7 of~\cite{HMK}". On first mention, use the authors' names; afterwards "op. cit." style repetition of `\cite` is fine.
   - Do not start a sentence with a math symbol; do not stack two formulas separated only by a comma or period.
   - Every `\bibitem` must be complete: all author names, title, journal or venue (or `arXiv:XXXX.XXXXX` if unpublished), and year. An entry with no authors is a defect.

   **Displayed mathematics.** Use display math (`\[...\]`, or `\begin{equation}` with a `\label` when the formula is referenced later) for: any equation or identity central to an argument, any definition of a map or quantity by a formula, any expression too large to sit comfortably inline, and any matrix beyond $2\times 1$ that carries content (use `pmatrix`, not inline `smallmatrix`, for anything load-bearing). Displayed equations are part of the sentence: punctuate them, and use `align`/`aligned` for multi-step manipulations rather than a paragraph of inline algebra.

   **Computations inside proofs.** A proof must be checkable from the text alone. If a step is certified by a computation, present the mathematical certificate in the note itself — the exact matrices/points instantiated and the exact resulting nonzero value or rank, displayed — so the reader can verify it independently; then add a `\begin{remark}` (or footnote) noting that the computation is reproduced by the named script under `experiments/`. Never make a script filename the load-bearing step of a proof, and never cite raw file paths mid-proof. If a claim's verification is *only* a computation (no surrounding argument), it is a `computation`, not a theorem.

   It must compile standalone: run `latexmk -pdf note.tex` yourself before finishing and fix any compiler errors. Then look at the log for overfull boxes in critical places — do not paper over layout problems with `\sloppy`.

   Start the file exactly like this:
   ```tex
   \documentclass{amsart}
   \usepackage{amsmath,amssymb,amsthm}
   \theoremstyle{plain}
   \newtheorem{theorem}{Theorem}[section]
   \newtheorem{lemma}[theorem]{Lemma}
   \newtheorem{proposition}[theorem]{Proposition}
   \newtheorem{corollary}[theorem]{Corollary}
   \newtheorem{conjecture}[theorem]{Conjecture}
   \theoremstyle{definition}
   \newtheorem{definition}[theorem]{Definition}
   \newtheorem{example}[theorem]{Example}
   \theoremstyle{remark}
   \newtheorem{remark}[theorem]{Remark}
   \title{Your title}
   \author{{{AUTHOR}}}
   \date{}
   \begin{document}
   \begin{abstract}
   Your brief abstract.
   \end{abstract}
   \maketitle
   ```
   Do not change the `\author` line. All theorem-like environments share one running counter per section on purpose — this is standard AMS numbering: "Theorem 2.1, Lemma 2.2, Corollary 2.3". Add further environments (e.g. `speculation`) the same way if you need them. Use `\label{}`/`\ref{}` for cross-references, never hardcoded numbers. A plain, non-machine-managed "References" section via `\begin{thebibliography}{9}...\end{thebibliography}` with manual `\bibitem`/`\cite` keys is perfectly acceptable — you do not need to build a working `.bib` file. End the file with `\end{document}`.

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
- Every claim in `note.tex` that is stated as a theorem, proposition, conjecture, or computed fact must appear here with a matching `id` referenced in the note (e.g. a `\label` you can cross-reference).
- `statement` is the claim and nothing else — a precise, self-contained mathematical statement. No proof sketch, no "Proof: ..." summary, no description of the evidence or method (that lives in the note and, for conjectures, in the statement's own "supported by" clause only if the claim is *about* the evidence). Downstream referees see this field and must judge the claim, not your account of why you believe it.
- `label` must be honest. `theorem-complete-proof` means you have actually verified every step yourself, not that it "should" work. If you have a gap, use `theorem-sketch`. If it is evidence-based rather than proved, use `conjecture-with-evidence`.
- Set every claim's `proof_status` to `"unchecked"` — a separate self-check pass will update this field. Do not mark your own work verified.

If you run any computation or numerical experiment (e.g. checking a conjecture on small cases, symbolic verification), save the actual script under `./experiments/` so it can be re-run, not just its output. Use `{{VENV_PYTHON}}` to run Python (it has `sympy` installed); do not rely on a bare `python3` unless you have confirmed what packages it has.

**Never background a computation** — no `&`, `nohup`, `disown`, and do not use your Bash tool's `run_in_background` option. This is a single, one-shot session with no future turn to come back to — if you end your turn while anything is still running in the background, its output is gone, not picked up later, and you will have produced nothing. Run every command in the foreground and wait for it to finish before continuing. If a computation might be slow, make it smaller (fewer cases, a shorter range) rather than backgrounding it — a modest but completed check beats an ambitious one that never finishes.

**Write the artifacts early, then improve them in place.** This session can be cut off without warning (a hard wall-clock limit, a dropped connection), and the files on disk at that moment are all that survives — a complete-but-unpolished `note.tex` + `result.json` pair still enters the pipeline, whereas a perfect note that exists only in your head is a total loss. So as soon as your main result is settled, write a complete, compiling first version of both files, and only then iterate: strengthen proofs, add examples, polish exposition, recompile. Do not save the writing for a final act after all experiments are done.

Do not optimize for the number of claims. One genuinely interesting, correctly labeled result is worth more than five shallow ones. If nothing in this direction pans out after real effort, write up the strongest correct partial result honestly labeled — do not inflate a computation into a theorem.
