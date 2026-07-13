You are triaging a mathematician's reading queue. Every note here already passed an independent cross-model correctness/novelty/taste referee — so the question is not "is this valid" but "in what order should a busy expert read these to get to the most worthwhile contribution fastest." Reading is the bottleneck; your ranking is what saves the user's time.

Read `digest.json` in the current directory. It has `passed` (the notes that cleared the crosscheck, each with its title, source paper, claims, per-claim novelty, taste scores, and a note-level `formalization` — the Aristotle/Lean outcome for the whole note: `status` ∈ `proof-formalized` (machine-checked, no `sorry`) / `statement-formalized` / `not-formalizable`, plus `error_reported`) and `rejected` (notes that failed, with a one-line reason).

Rank the `passed` notes from most to least worth reading. Weigh:
- **depth and strength** of the taste scores — a genuine new idea outranks a clean-but-routine result;
- **verification strength** — a note whose results are `proof-formalized` in Lean is more trustworthy than one resting on the referee pass alone;
- **novelty** — all-`novel` claims over ones with `uncertain` or partially-`known` content;
- **substance** — how much a working mathematician in the area would actually care.

**Flag any note whose `formalization.error_reported` is true at the very top of the list with a ⚠️**, regardless of its other scores: Aristotle believes it found a real proof error the crosscheck missed, and the user should look at `lean/ERROR.md` before trusting it (or before dismissing it, if the flag is a false alarm).

Write `PRIORITY.md` (in the current directory) with:

1. A one-line header.
2. A numbered list, best first. Each entry: the title in bold, then `` (`slug`, paper-ref) ``, then one sentence on **why it's worth reading** and one clause on **the main caveat/risk** (the weakest claim, an `uncertain` novelty, a statement-only Lean result, etc.). Name the single most exciting claim where you can.
3. A short `## Not recommended` section listing each `rejected` slug with its reason, so the user knows they were considered and why they were dropped.

Be decisive and specific — cite actual claim statements, not generic praise. If two notes are close, break the tie toward the one with stronger verification. Keep it scannable: this file is the first thing the user reads.
