#!/usr/bin/env python3
"""paper-starter orchestrator — the whole pipeline in one stdlib-only file.

WHAT THIS IS
============
You generate candidate research notes elsewhere (run GPT-PROMPT.md in a chat
window; it emits one folder per project with note.tex + result.json). This tool
takes those folders and, on your Claude subscription, does the three things a
chat window can't:

    inbox/  ->  crosscheck  ->  lean  ->  review/     -->  PRIORITY.md
    (a GPT     (a DIFFERENT   (Aristotle  archive/
     project    model         formalizes
     folder)    referees it)   proofs)

    crosscheck  one isolated `claude -p` pass: re-derive the proofs, check the
                literature, judge the taste. GPT wrote the note, so a different
                model refereeing it is genuine independence — pass -> lean,
                fail -> archive with the referee's reasons.
    lean        Aristotle (Harmonic's cloud Lean prover) formalizes each proved
                claim. Bonus evidence, never a gate; free (no Anthropic tokens).
    rank        across everything that passed, Claude writes PRIORITY.md — a
                ranked reading list, because your reading time is the bottleneck.

Every run is a directory under runs/<slug>/ with a state.json recording which
stage it is at. There is no server and no queue: the filesystem IS the database,
re-running the orchestrator just continues unfinished runs, and it is safe to
run concurrently (per-run .lock file).

WHERE TO LOOK
-------------
    main()           entry point: load .env, preflight, intake, advance, rank
    advance()        the stage dispatcher — maps state["stage"] to a do_* function
    intake()         watches inbox/ for GPT project folders (intake_folder)
    do_crosscheck()  the one referee stage: correctness + novelty + taste
    do_lean()        optional Aristotle/Lean formalization (bonus, non-gating)
    finalize()       moves a finished run into review/ or archive/
    do_rank()        writes PRIORITY.md — the ranked reading list

Tuning the system means editing prompts/crosscheck.md, prompts/rank.md and
config.toml, not this code.
"""
import argparse
import contextlib
import datetime as dt
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VALID_LABELS = {
    "theorem-complete-proof",
    "theorem-sketch",
    "conjecture-with-evidence",
    "computation",
    "speculation",
}
VALID_STATUS = {"unchecked", "verified", "gap-found", "counterexample-found"}
VALID_CORRECTNESS_VERDICTS = {"correct", "fixable-gap", "wrong", "cannot-verify"}
# The crosscheck referee gets Bash (+ the venv python) to re-run computations
# itself and web tools to do a real literature check — but never Write access to
# anything but its own verdict, and never sight of the source paper's build junk.
CROSSCHECK_TOOLS = "Read Write Bash WebSearch WebFetch"


# Defaults for every optional config.toml key, so a minimal config (or none at
# all) still runs. config.toml only needs to hold the values you want to
# override; the essays explaining each key live in DOCS.md, not here.
CONFIG_DEFAULTS = {
    "referee_model": "opus",       # crosscheck + rank; opus catches more errors
    "max_runs_per_day": 10,
    "max_usd_per_run": 10.0,
    "max_usd_per_paper": 0,
    "max_referee_attempts": 2,
    "stage_timeout_seconds": 3600,
    "claude_extra_args": ["--strict-mcp-config"],
    "notify": True,
    "lean_enabled": True,
    "lean_timeout_minutes": 240,
    "venv_python": ".venv/bin/python3",
}


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    path = ROOT / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def load_dotenv(path=None):
    """Load KEY=VALUE lines from ROOT/.env into os.environ, so API keys
    (ANTHROPIC_API_KEY, ARISTOTLE_API_KEY) don't depend on shell profiles —
    cron/launchd invocations see them too. Variables already set in the real
    environment always win. Corollary: to run on Claude subscription auth,
    ANTHROPIC_API_KEY must be absent from BOTH the shell environment and .env —
    `env -u ANTHROPIC_API_KEY` alone can't unset what .env then re-adds."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --- state.json helpers ---------------------------------------------------

def init_state(paper_ref):
    return {
        "paper": paper_ref,
        "stage": "crosscheck",
        "attempts": {},
        "history": [{"stage": "intake", "at": now(), "note": f"ingested {paper_ref}"}],
    }


def load_state(run_dir):
    return json.loads((run_dir / "state.json").read_text())


def save_state(run_dir, state):
    (run_dir / "state.json").write_text(json.dumps(state, indent=2))


def append_history(state, stage, note):
    state.setdefault("history", []).append({"stage": stage, "at": now(), "note": note})


# --- intake ----------------------------------------------------------------
# A GPT project folder is any immediate subdirectory of inbox/ that contains
# note.tex + result.json (the artifacts GPT-PROMPT.md is told to emit). We copy
# it into runs/<slug>/, fetch the source paper it responds to (so the crosscheck
# referee can judge novelty and motivation), and set it going at crosscheck.

def slugify_folder(name):
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    return base or "project"


ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7})")


def arxiv_id_from_paper_ref(paper_ref):
    """Pull an arXiv id out of result.json's "paper" field (e.g. "arxiv:2505.11846"),
    so intake can fetch the source paper for the referee. None if it isn't an arXiv ref."""
    if not isinstance(paper_ref, str):
        return None
    m = ARXIV_RE.search(paper_ref)
    return m.group(1) if m else None


def fetch_arxiv_source(arxiv_id, dest_dir):
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-starter/0.1 (research pipeline)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    raw_path = dest_dir / "raw.download"
    raw_path.write_bytes(data)

    try:
        with tarfile.open(raw_path, "r:gz") as tf:
            tf.extractall(dest_dir, filter="data")
        raw_path.unlink()
        return
    except tarfile.ReadError:
        pass

    try:
        with gzip.open(raw_path, "rb") as gz:
            content = gz.read()
        (dest_dir / "main.tex").write_bytes(content)
        raw_path.unlink()
        return
    except gzip.BadGzipFile:
        pass

    raw_path.rename(dest_dir / "main.tex")


# Files arXiv source tarballs carry that cost real tokens but hold no
# mathematical content the referee needs: .sty/.cls/.bst are camera-ready
# LaTeX rendering machinery, .bib/.bbl is bibliography data (the crosscheck does
# its own citation search), aux/build residue is compiler bookkeeping, and
# figures are images the model can only burn vision tokens on, not read for
# content it can't already get from the surrounding .tex prose/captions.
PAPER_SOURCE_STRIP_EXTS = {
    ".sty", ".cls", ".bst",
    ".bib", ".bbl", ".blg",
    ".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".nav", ".snm",
    ".vrf", ".spl", ".synctex",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".svg", ".eps", ".pdf",
}
PAPER_SOURCE_STRIP_SUFFIXES = (".synctex.gz",)
PAPER_SOURCE_STRIP_NAMES = {".ds_store"}


def strip_tex_comments(paper_dir):
    """Drop full-line % comments from .tex sources (arXiv uploads often carry
    thousands of commented-out lines). A leading % always starts a comment in
    TeX, so this is safe; inline trailing comments are left alone to avoid
    mangling escaped \\% uses."""
    saved = 0
    for p in paper_dir.rglob("*.tex"):
        lines = p.read_text(errors="replace").splitlines(keepends=True)
        kept = [l for l in lines if not l.lstrip().startswith("%")]
        if len(kept) < len(lines):
            saved += sum(len(l) for l in lines) - sum(len(l) for l in kept)
            p.write_text("".join(kept))
    return saved


def prune_paper_dir(paper_dir):
    removed = []
    for p in paper_dir.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if (
            p.suffix.lower() in PAPER_SOURCE_STRIP_EXTS
            or name in PAPER_SOURCE_STRIP_NAMES
            or any(name.endswith(s) for s in PAPER_SOURCE_STRIP_SUFFIXES)
        ):
            removed.append(p.relative_to(paper_dir))
            p.unlink()
    # drop directories left empty (e.g. Figures/ after stripping images)
    for p in sorted((d for d in paper_dir.rglob("*") if d.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            p.rmdir()
    return removed


def fetch_paper_for(run_dir, paper_ref):
    """Best-effort: put the source paper the note responds to under paper/ so the
    crosscheck referee can check novelty and motivation. A note whose paper we
    can't fetch is still refereed (correctness from the note alone, novelty via
    web search) — so any failure here is logged, never fatal."""
    if (run_dir / "paper").exists():
        return  # GPT already shipped the paper alongside the note
    arxiv_id = arxiv_id_from_paper_ref(paper_ref)
    if not arxiv_id:
        print(f"[intake] {run_dir.name}: no arXiv id in result.json 'paper' field; refereeing without the source paper")
        return
    paper_dir = run_dir / "paper"
    paper_dir.mkdir()
    try:
        fetch_arxiv_source(arxiv_id, paper_dir)
    except Exception as e:
        shutil.rmtree(paper_dir, ignore_errors=True)
        print(f"[intake] {run_dir.name}: could not fetch source paper {arxiv_id} ({e}); refereeing without it")
        return
    prune_paper_dir(paper_dir)
    strip_tex_comments(paper_dir)


def runs_created_today():
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    count = 0
    for root in ("runs", "review", "archive"):
        base = ROOT / root
        if not base.exists():
            continue
        for run in base.iterdir():
            state_path = run / "state.json"
            if not state_path.exists():
                continue
            try:
                created = json.loads(state_path.read_text())["history"][0]["at"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if isinstance(created, str) and created.startswith(today):
                count += 1
    return count


def intake_folder(folder):
    """Ingest one GPT project folder. Returns True iff a new run was created."""
    if not (folder / "note.tex").exists() or not (folder / "result.json").exists():
        print(f"[intake] {folder.name}: not a project folder (missing note.tex or result.json), skipping")
        return False
    valid, err = validate_result_json(folder / "result.json")
    if not valid:
        print(f"[intake] {folder.name}: result.json invalid ({err}), skipping")
        return False

    slug = slugify_folder(folder.name)
    if (ROOT / "runs" / slug).exists() or (ROOT / "review" / slug).exists() or (ROOT / "archive" / slug).exists():
        print(f"[intake] {slug} already exists, skipping")
        return False

    run_dir = ROOT / "runs" / slug
    shutil.copytree(folder, run_dir)
    paper_ref = json.loads((folder / "result.json").read_text()).get("paper", "unknown")
    fetch_paper_for(run_dir, paper_ref)
    save_state(run_dir, init_state(paper_ref))
    print(f"[intake] created run {slug} (paper: {paper_ref})")
    return True


def intake(cfg):
    inbox = ROOT / "inbox"
    processed = inbox / ".processed"
    processed.mkdir(exist_ok=True)
    quota = cfg.get("max_runs_per_day", 0)
    remaining = max(0, quota - runs_created_today()) if quota else None

    for child in sorted(inbox.iterdir()):
        if child.name.startswith("."):
            continue
        if not child.is_dir():
            print(f"[intake] ignoring {child.name}: drop GPT project folders (a dir with note.tex + result.json) in inbox/, not loose files")
            continue
        if remaining == 0:
            print(f"[intake] max_runs_per_day reached; leaving {child.name} in inbox")
            continue
        created = intake_folder(child)
        # Move the source folder aside whether or not it produced a run (a
        # malformed folder shouldn't be re-scanned every invocation); a fresh
        # drop with the same name after fixing it still gets a new slug check.
        shutil.move(str(child), str(processed / child.name))
        if created and remaining is not None:
            remaining -= 1


# --- claude invocation -------------------------------------------------------

def run_claude(run_dir, prompt, model, allowed_tools, cfg, timeout_s=None):
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--permission-mode", "acceptEdits",
        "--allowedTools", allowed_tools,
        "--output-format", "json",
    ]
    # --strict-mcp-config (default via claude_extra_args) keeps the operator's
    # personal MCP servers out of pipeline calls: they cost tokens in every
    # invocation and would hand the referee tools it must not have.
    cmd += list(cfg.get("claude_extra_args", []))
    if cfg.get("max_usd_per_run", 0):
        cmd += ["--max-budget-usd", str(cfg["max_usd_per_run"])]
    timeout = timeout_s or cfg.get("stage_timeout_seconds", 3600)
    env = os.environ.copy()
    # Hard guard against backgrounded computations (prompt-level prohibition
    # alone has failed in practice: a real run died with "Background tasks
    # still running after 600s; terminating"). Disabling the feature removes
    # the Bash tool's run_in_background option AND the CLI's auto-backgrounding
    # of long foreground commands; the wait ceiling caps the shutdown tail if
    # a stray job slips through anyway.
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "15000"
    try:
        proc = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        return False, f"TIMEOUT after {e.timeout}s\nstdout so far:\n{e.stdout}\nstderr so far:\n{e.stderr}"
    if proc.returncode != 0:
        return False, f"exit code {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return True, proc.stdout


def extract_cost_usd(log):
    """Pull total_cost_usd out of a claude -p JSON result. Present even on
    failed/aborted invocations (which still cost money); 0.0 if unparseable."""
    m = re.search(r'"total_cost_usd"\s*:\s*([0-9.eE+-]+)', log)
    if m:
        with contextlib.suppress(ValueError):
            return float(m.group(1))
    return 0.0


def track_cost(state, log):
    """Accumulate per-run LLM spend in state.json; advance() parks the run
    once it reaches max_usd_per_paper."""
    state["cost_usd"] = round(state.get("cost_usd", 0.0) + extract_cost_usd(log), 6)


def is_transient_failure(log):
    """Rate/session limits, overload, and an empty API credit balance are not
    the run's fault: retrying later (after the limit window resets or the
    account is refilled) is free, whereas consuming an attempt or archiving
    throws away a run that may already have survived expensive stages."""
    lowered = log.lower()
    if any(marker in lowered for marker in ("session limit", "rate_limit", "overloaded", "credit balance", "insufficient credit", "connection closed mid-response")):
        return True
    return any(
        f'"api_error_status":{code}' in log.replace(" ", "")
        for code in (429, 502, 503, 529)
    )


def defer_stage(run_dir, state, stage, log):
    """Leave the run at its current stage (no attempt consumed) so the next
    orchestrator invocation retries it — e.g. after a rate-limit window resets."""
    append_history(state, stage, "transient failure (rate/session limit or overload); will retry, no attempt consumed")
    save_state(run_dir, state)
    print(f"[{stage}] transient failure on {run_dir.name}; will retry on a later invocation")
    return False


# --- referee isolation -------------------------------------------------------
# The crosscheck referee must judge the finished note, not GPT's own confidence
# in it. We enforce this with the filesystem, not just instructions: the referee
# gets its own subdirectory containing only what it's allowed to see (paper/ +
# note.tex + a result.json stripped to id/label/statement), and Claude Code's
# tool sandboxing means Read/Write/Bash inside that cwd cannot reach files
# outside it (no --add-dir is granted, so GPT's proof_status, audit_notes and
# verification_report.md are simply not visible).

def strip_result_for_referee(result_path, dest_path):
    data = json.loads(result_path.read_text())
    stripped = {
        "paper": data.get("paper", "unknown"),
        "claims": [
            {"id": c["id"], "label": c["label"], "statement": c["statement"]}
            for c in data["claims"]
        ],
    }
    dest_path.write_text(json.dumps(stripped, indent=2))


def make_referee_dir(run_dir, name, include_paper):
    dest = run_dir / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    if include_paper and (run_dir / "paper").exists():
        shutil.copytree(run_dir / "paper", dest / "paper")
    shutil.copy(run_dir / "note.tex", dest / "note.tex")
    strip_result_for_referee(run_dir / "result.json", dest / "result.json")
    return dest


# --- validation --------------------------------------------------------------

def compile_latex(run_dir):
    proc = subprocess.run(
        ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "note.tex"],
        cwd=run_dir, capture_output=True, text=True, timeout=120,
    )
    ok = proc.returncode == 0 and (run_dir / "note.pdf").exists()
    for ext in ("aux", "log", "out", "fls", "fdb_latexmk", "bbl", "blg", "toc"):
        f = run_dir / f"note.{ext}"
        if f.exists():
            f.unlink()
    if not ok:
        return False, proc.stdout + proc.stderr
    return True, None


def validate_result_json(path):
    if not path.exists():
        return False, "result.json does not exist"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return False, f"invalid JSON: {e}"
    if "claims" not in data or not isinstance(data["claims"], list) or not data["claims"]:
        return False, "missing or empty 'claims' list"
    for c in data["claims"]:
        for key in ("id", "label", "statement"):
            if key not in c:
                return False, f"claim missing '{key}': {c}"
        if c["label"] not in VALID_LABELS:
            return False, f"invalid label '{c['label']}' in {c.get('id')}"
        # proof_status is GPT's own self-assessment; accept it when present and
        # well-formed, but don't require it (the crosscheck forms its own view).
        if "proof_status" in c and c["proof_status"] not in VALID_STATUS:
            return False, f"invalid proof_status '{c['proof_status']}' in {c.get('id')}"
    return True, None


def load_verdict(path):
    """Parse a referee-written verdict.json; malformed output is a stage error
    (retryable), never a pass."""
    if not path.exists():
        return None, "verdict.json was not written"
    try:
        return json.loads(path.read_text()), None
    except json.JSONDecodeError as e:
        return None, f"verdict.json is not valid JSON: {e}"


# --- stage transitions ---------------------------------------------------

def referee_stage_error(run_dir, state, stage, reason, cfg):
    """A referee stage failed operationally (invocation error, missing or
    malformed verdict). That is not a verdict on the run: retry the stage a
    bounded number of times before giving up. Never archives a run as
    'rejected' for what is actually an infrastructure failure."""
    attempts = state["attempts"].get(stage, 0) + 1
    state["attempts"][stage] = attempts
    if attempts < cfg.get("max_referee_attempts", 2):
        append_history(state, stage, f"stage error, will retry: {reason[:1000]}")
        save_state(run_dir, state)
        return True
    state["stage"] = "archive"
    state["rejection_reason"] = f"{stage} stage failed {attempts} time(s), giving up. Last error:\n{reason}"
    append_history(state, stage, "archived: stage retries exhausted (operational failure, not a referee rejection)")
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


def reject(run_dir, state, stage, reason, note, cfg):
    state["stage"] = "archive"
    state["rejection_reason"] = reason
    append_history(state, stage, note)
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


# --- crosscheck: the one referee stage -----------------------------------
# GPT wrote the note; a DIFFERENT model referees it here, so this single
# isolated pass is genuine independent verification (not the same-model
# self-review GPT-PROMPT.md itself warns can't be trusted). It folds the three
# checks that matter into one verdict.json:
#   correctness  re-derive every theorem-complete-proof claim from the note's
#                own definitions, and re-run computations with Bash.
#   novelty      a real literature check per substantive claim.
#   taste        is this worth the user's reading time at all?
# The referee's overall verdict decides pass/fail, but with a hard safety net:
# no note passes while a load-bearing proof is anything but "correct", or while
# every substantive claim is already "known". Those are the false positives that
# waste the user's time, so code enforces them regardless of the verdict field.

def do_crosscheck(run_dir, state, cfg):
    # Compile for the reader's note.pdf and as a lint. A note that doesn't
    # compile is a defect worth flagging, but the referee reads the .tex source
    # regardless, so don't abort the run over it — record and continue.
    compile_ok, compile_err = compile_latex(run_dir)
    if not compile_ok:
        state["compile_failed"] = True
        append_history(state, "crosscheck", "note.tex does not compile (refereeing the source anyway)")

    (run_dir / "referee").mkdir(exist_ok=True)
    view_dir = make_referee_dir(run_dir, "crosscheck_view", include_paper=True)
    prompt = (ROOT / "prompts" / "crosscheck.md").read_text()
    prompt = prompt.replace("{{VENV_PYTHON}}", str(ROOT / cfg["venv_python"]))
    attempt = state["attempts"].get("crosscheck", 0) + 1

    ok, log = run_claude(view_dir, prompt, cfg["referee_model"], CROSSCHECK_TOOLS, cfg)
    (run_dir / f"crosscheck_attempt_{attempt}.log").write_text(log)
    track_cost(state, log)
    if not ok and is_transient_failure(log):
        return defer_stage(run_dir, state, "crosscheck", log)
    if not ok:
        return referee_stage_error(run_dir, state, "crosscheck", f"invocation failed:\n{log}", cfg)

    verdict, err = load_verdict(view_dir / "verdict.json")
    if verdict is None:
        return referee_stage_error(run_dir, state, "crosscheck", err, cfg)
    shutil.copy(view_dir / "verdict.json", run_dir / "referee" / "crosscheck.json")
    if (view_dir / "report.md").exists():
        shutil.copy(view_dir / "report.md", run_dir / "referee" / "crosscheck_report.md")

    if verdict.get("verdict") not in ("pass", "fail"):
        return referee_stage_error(run_dir, state, "crosscheck",
                                   f"verdict.json has no valid 'verdict' field: {verdict.get('verdict')!r}", cfg)

    # The referee must return a verdict for every claim it was asked about; a
    # silently missing load-bearing claim is a malformed verdict, not a pass.
    claims = json.loads((run_dir / "result.json").read_text())["claims"]
    load_bearing = [c["id"] for c in claims if c["label"] == "theorem-complete-proof"]
    substantive = [c["id"] for c in claims if c["label"] in ("theorem-complete-proof", "conjecture-with-evidence")]
    correctness = {c.get("id"): c for c in verdict.get("correctness", []) if isinstance(c, dict)}
    novelty = {c.get("id"): c for c in verdict.get("novelty", []) if isinstance(c, dict)}

    for cid in load_bearing:
        v = correctness.get(cid, {}).get("verdict")
        if v not in VALID_CORRECTNESS_VERDICTS:
            return referee_stage_error(run_dir, state, "crosscheck",
                                       f"verdict.json has no valid correctness verdict for load-bearing claim '{cid}'", cfg)

    # Hard safety net (enforced regardless of the referee's overall verdict).
    bad_proofs = [(cid, correctness[cid]["verdict"], correctness[cid].get("notes", ""))
                  for cid in load_bearing if correctness[cid]["verdict"] != "correct"]
    all_known = bool(substantive) and all(novelty.get(cid, {}).get("novelty_status") == "known" for cid in substantive)

    fail_reasons = []
    if bad_proofs:
        fail_reasons.append("Correctness — a proved claim did not survive independent re-derivation:\n"
                            + "\n".join(f"  - {cid}: {v} — {notes}" for cid, v, notes in bad_proofs))
    if all_known:
        known = "\n".join(f"  - {cid}: {novelty.get(cid, {}).get('reference', '')}" for cid in substantive)
        fail_reasons.append("Novelty — every substantive claim is already in the literature:\n" + known)
    if verdict["verdict"] == "fail" and verdict.get("notes"):
        fail_reasons.append(f"Referee verdict: fail — {verdict['notes']}")

    if fail_reasons:
        reason = "\n\n".join(fail_reasons)
        if verdict.get("scores"):
            reason += f"\n\nTaste scores: {verdict['scores']}"
        return reject(run_dir, state, "crosscheck", reason, "archived: failed crosscheck", cfg)

    append_history(state, "crosscheck", f"passed: {verdict.get('scores') or verdict.get('notes', 'ok')}")
    if lean_enabled(cfg):
        state["stage"] = "lean"
        append_history(state, "crosscheck", "entering lean formalization track (bonus evidence, non-gating)")
        save_state(run_dir, state)
        return True
    state["stage"] = "review"
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


# --- lean track (Aristotle) ----------------------------------------------------
# Lean 4 formalization as BONUS EVIDENCE, never a gate. The run has already
# passed crosscheck when it arrives here, so no Aristotle outcome may demote it —
# the only exit is review. Aristotle (Harmonic's cloud prover, free: no Anthropic
# tokens) can take hours per claim, so submissions are asynchronous (`aristotle
# formalize` WITHOUT --wait): the run sits at this stage across orchestrator
# invocations, and each tick advances whatever it can — submit pending claims,
# poll running ones, download finished ones — until every claim is resolved or
# hits lean_timeout_minutes.

ARISTOTLE_TERMINAL = {"COMPLETE", "COMPLETE_WITH_ERRORS", "OUT_OF_BUDGET", "FAILED", "CANCELED"}
ARISTOTLE_STATUSES = ARISTOTLE_TERMINAL | {"QUEUED", "IN_PROGRESS", "UNKNOWN"}
# Note-level formalization outcomes recorded in result.json's "formalization":
#   proof-formalized      COMPLETE, no sorry, no reported error (machine-checked)
#   statement-formalized  built the statements but a sorry or error remains
#   not-formalizable      failed / out-of-budget / timed out


def lean_enabled(cfg):
    return (
        bool(cfg.get("lean_enabled", True))
        and bool(os.environ.get("ARISTOTLE_API_KEY"))
        and shutil.which("aristotle") is not None
    )


def run_aristotle(args, timeout_s=180):
    """Submit/poll/download are all quick calls (the hours happen server-side);
    a hung CLI call must not stall the whole orchestrator tick. The CLI exits 0
    even after logging an error (bad key, API failure) and logs to stderr, so
    callers parse the combined output rather than trusting the return code."""
    try:
        proc = subprocess.run(["aristotle", *args], capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"aristotle {' '.join(args)}: {e}"
    return proc.returncode == 0, proc.stdout + "\n" + proc.stderr


def parse_aristotle_task_status(log):
    """STATUS is the last column of the first data row of `aristotle tasks`
    output (the header row ends in the literal 'STATUS', which never matches).
    None if no task row is visible yet — a fresh submission's task can lag the
    project's creation."""
    for line in log.splitlines():
        tokens = line.split()
        if tokens and tokens[-1] in ARISTOTLE_STATUSES:
            return tokens[-1]
    return None


LEAN_DEFAULT_PROMPT = (
    "Formalise all results in this paper as Lean 4 theorems and prove them. "
    "State each result faithfully and type-correctly; do not weaken or alter a statement to make a proof easier "
    "(leave `sorry` on a faithful statement rather than proving a different one). "
    "If while formalising you find an actual error in one of the paper's proofs, try to fix it, and record what was "
    "wrong and how you fixed it (or why you couldn't) in a file named ERROR.md at the top of the project."
)


def read_lean_prompt(cfg):
    """The instruction sent to `aristotle submit`. Editable as prompts/lean.md
    (the 'prompts are files' convention) — falls back to LEAN_DEFAULT_PROMPT."""
    p = ROOT / "prompts" / "lean.md"
    if p.exists():
        text = p.read_text().strip()
        if text:
            return text
    return LEAN_DEFAULT_PROMPT


def download_lean_project(lean_dir, entry):
    """Download and unpack the finished project (a .tar.gz from `aristotle
    download`) into lean/solution/. Best-effort: the verdict is the task status
    Aristotle reported; a missing/broken download never changes it."""
    tar_path = lean_dir / "result.tar.gz"
    run_aristotle(["download", entry["project_id"], "--destination", str(tar_path)])
    if not tar_path.exists():
        entry["download_failed"] = True
        return None
    dest = lean_dir / "solution"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(dest, filter="data")
        tar_path.unlink()
    except (tarfile.TarError, OSError):
        entry["download_failed"] = True
        return None
    return dest


def scan_lean_output(dest):
    """Two best-effort signals from a finished project: an ERROR.md the agent may
    have written (per the prompt — an actual proof error it found) and the count
    of remaining `sorry`s in the Lean sources (an incomplete proof). Neither is
    authoritative; both feed the SUMMARY/ranking, never a gate."""
    error_text = None
    sorries = 0
    for p in dest.rglob("*"):
        if not p.is_file():
            continue
        if p.name.lower() == "error.md" and error_text is None:
            with contextlib.suppress(OSError):
                error_text = p.read_text(errors="replace").strip()
        elif p.suffix == ".lean":
            with contextlib.suppress(OSError):
                sorries += len(re.findall(r"\bsorry\b", p.read_text(errors="replace")))
    return sorries, error_text


def lean_finish(run_dir, state, cfg, entry, note):
    """Record the note-level formalization outcome into result.json, then hand off
    to review. Called on every terminal Lean path (proved / statement-only /
    not-formalizable). The status is a fact about the note as a whole — one
    Aristotle project per note — not per claim."""
    data = json.loads((run_dir / "result.json").read_text())
    data["formalization"] = {
        "status": entry["status"],
        "aristotle_status": entry.get("aristotle_status"),
        "sorries": entry.get("sorries"),
        "error_reported": bool(entry.get("error_reported")),
        "project_id": entry.get("project_id"),
    }
    (run_dir / "result.json").write_text(json.dumps(data, indent=2))
    state["stage"] = "review"
    append_history(state, "lean", note)
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


def lean_skip(run_dir, state, cfg, note):
    state["stage"] = "review"
    append_history(state, "lean", note)
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


def do_lean(run_dir, state, cfg):
    if not lean_enabled(cfg):
        return lean_skip(run_dir, state, cfg, "skipped: lean_enabled=false, aristotle CLI missing, or ARISTOTLE_API_KEY unset")
    claims = json.loads((run_dir / "result.json").read_text())["claims"]
    if not any(c["label"] == "theorem-complete-proof" for c in claims):
        return lean_skip(run_dir, state, cfg, "skipped: no theorem-complete-proof claim to formalize")

    lean_dir = run_dir / "lean"
    lean_dir.mkdir(exist_ok=True)
    entry = state.setdefault("lean", {"status": "pending"})
    timeout_s = cfg.get("lean_timeout_minutes", 240) * 60

    # One Aristotle project per note: `aristotle submit "<prompt>" --project-dir`
    # sends the whole note.tex with our editable natural-language instruction
    # (prompts/lean.md). Asynchronous — a proof can take hours — so the run sits
    # here across invocations, submitting then polling then downloading.
    if entry["status"] == "pending":
        project_dir = lean_dir / "project"
        project_dir.mkdir(exist_ok=True)
        shutil.copy(run_dir / "note.tex", project_dir / "note.tex")
        ok, log = run_aristotle(["submit", read_lean_prompt(cfg), "--project-dir", str(project_dir)])
        m = re.search(r"Project created:\s*(\S+)", log)
        if m:
            entry.update({"status": "submitted", "project_id": m.group(1), "submitted_at": time.time()})
            append_history(state, "lean", f"submitted whole note to aristotle (project {m.group(1)}); can take hours")
            save_state(run_dir, state)
            print(f"[lean] {run_dir.name}: submitted (project {m.group(1)}); will poll on later invocations")
            return True
        if "too many requests" in log.lower():
            # Aristotle's concurrent-task cap: stay pending, resubmit when a slot frees.
            print(f"[lean] {run_dir.name}: aristotle at concurrent-task cap; staying pending")
            return False
        fails = entry["submit_failures"] = entry.get("submit_failures", 0) + 1
        if fails >= 3:
            entry.update({"status": "not-formalizable", "aristotle_status": "submit-failed"})
            return lean_finish(run_dir, state, cfg, entry, f"submit failed {fails}x, marking not-formalizable: {log[:300]}")
        save_state(run_dir, state)
        return False

    if entry["status"] == "submitted":
        ok, log = run_aristotle(["tasks", entry["project_id"], "--limit", "1"])
        status = parse_aristotle_task_status(log)
        if status in ARISTOTLE_TERMINAL:
            entry["aristotle_status"] = status
            if status in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
                dest = download_lean_project(lean_dir, entry)
                sorries, error_text = scan_lean_output(dest) if dest else (None, None)
                entry["sorries"] = sorries
                if error_text:
                    entry["error_reported"] = True
                    (lean_dir / "ERROR.md").write_text(error_text)
                # A real machine-checked proof only when Aristotle reports COMPLETE,
                # no sorry remains, and it flagged no proof error. Anything else is
                # at most a formalized statement.
                if status == "COMPLETE" and not entry.get("error_reported") and (sorries or 0) == 0:
                    entry["status"] = "proof-formalized"
                else:
                    entry["status"] = "statement-formalized"
            else:
                entry["status"] = "not-formalizable"
            return lean_finish(run_dir, state, cfg, entry, lean_result_note(entry, status))
        if time.time() - entry.get("submitted_at", 0) > timeout_s:
            run_aristotle(["cancel", entry["project_id"]])  # best-effort: frees the concurrent-task slot
            entry.update({"status": "not-formalizable", "aristotle_status": "timeout"})
            return lean_finish(run_dir, state, cfg, entry,
                               f"no result within lean_timeout_minutes ({cfg.get('lean_timeout_minutes', 240)}m), canceled and marked not-formalizable")
        save_state(run_dir, state)
        print(f"[lean] {run_dir.name}: still {status or 'starting'} with aristotle; will poll on a later invocation")
        return False

    # Defensive: an already-terminal entry (shouldn't re-enter, since lean_finish
    # moves the run to review) — hand off rather than loop.
    return lean_finish(run_dir, state, cfg, entry, f"lean already {entry['status']}")


def lean_result_note(entry, aristotle_status):
    note = f"lean track finished (non-gating): {entry['status']} (aristotle {aristotle_status})"
    if entry.get("sorries"):
        note += f", {entry['sorries']} sorry(s) remain"
    if entry.get("error_reported"):
        note += " — ⚠️ Aristotle reported a proof error (lean/ERROR.md)"
    return note


# --- finalize ----------------------------------------------------------------

def write_review_summary(dest, state):
    """Mechanical one-page SUMMARY.md for the user: claims + crosscheck evidence."""
    lines = [f"# {dest.name} — passed crosscheck", ""]
    lines.append(f"Source paper: {state.get('paper', 'unknown')}. Compiled note: `note.pdf`; full referee report in `referee/crosscheck_report.md`. Total LLM cost: ${state.get('cost_usd', 0.0):.2f}.")
    if state.get("compile_failed"):
        lines.append("")
        lines.append("> ⚠️ note.tex did not compile cleanly — see the crosscheck report; the note was refereed from source.")
    lines.append("")
    try:
        data = json.loads((dest / "result.json").read_text())
        claims = data["claims"]
    except (OSError, json.JSONDecodeError, KeyError):
        data, claims = {}, []
    form = data.get("formalization")

    # Loudest thing first: if Aristotle flagged an actual proof error, the reader
    # must see it before anything else. (Lean is non-gating, so the note still
    # passed the crosscheck referee — but this is a second opinion worth checking.)
    if form and form.get("error_reported"):
        lines.append("> ⚠️ **Aristotle flagged a possible proof error** while formalizing this note. "
                     "Read `lean/ERROR.md` before trusting the proofs — the crosscheck referee did not catch this, "
                     "so it may be a real gap (or a false alarm from the formalizer).")
        lines.append("")

    try:
        verdict = json.loads((dest / "referee" / "crosscheck.json").read_text())
    except (OSError, json.JSONDecodeError):
        verdict = {}
    correctness = {c.get("id"): c for c in verdict.get("correctness", []) if isinstance(c, dict)}
    novelty = {c.get("id"): c for c in verdict.get("novelty", []) if isinstance(c, dict)}

    lines.append("## Claims")
    lines.append("")
    for c in claims:
        cid = c.get("id")
        bits = [c.get("label")]
        if cid in correctness:
            bits.append(f"correctness: {correctness[cid].get('verdict')}")
        if cid in novelty:
            bits.append(f"novelty: {novelty[cid].get('novelty_status')}")
        lines.append(f"- **{cid}** ({', '.join(str(b) for b in bits)}): {c.get('statement')}")
    lines.append("")

    if verdict.get("scores"):
        lines.append(f"## Taste\n\nScores: {verdict['scores']}. {verdict.get('notes', '')}")
        lines.append("")

    if form:
        label = {
            "proof-formalized": "**proof-formalized** ✓✓ — machine-checked Lean proof, no `sorry`",
            "statement-formalized": "statement-formalized — Lean statement(s) built, proof incomplete",
            "not-formalizable": "not-formalizable",
        }.get(form.get("status"), str(form.get("status")))
        extra = ""
        if form.get("sorries"):
            extra += f"; {form['sorries']} `sorry`(s) remain"
        if form.get("error_reported"):
            extra += "; see `lean/ERROR.md`"
        lines.append(f"## Lean (Aristotle, non-gating)\n\n{label}{extra}. Lean sources under `lean/solution/`.")
        lines.append("")

    caveat = ("Verification caveat: the crosscheck is a single independent model refereeing GPT's note — "
              "treat as \"survived independent cross-model refereeing\", not as certainty.")
    if form and form.get("status") == "proof-formalized":
        caveat += " Exception: Aristotle produced a machine-checked Lean proof (no `sorry`) of the note's results."
    lines.append(caveat)
    lines.append("")
    (dest / "SUMMARY.md").write_text("\n".join(lines))


def notify_user(title, message):
    if sys.platform != "darwin":
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"],
            capture_output=True, timeout=10,
        )


def finalize(run_dir, state, cfg=None):
    slug = run_dir.name
    dest_root = "review" if state["stage"] == "review" else "archive"
    dest = ROOT / dest_root / slug
    if dest.exists():
        shutil.rmtree(dest)
    (ROOT / dest_root).mkdir(exist_ok=True)
    shutil.copytree(run_dir, dest)
    # strip in-flight machinery from the finalized copy
    (dest / ".lock").unlink(missing_ok=True)
    for view in dest.glob("*_view"):  # isolated referee views (crosscheck_view)
        shutil.rmtree(view, ignore_errors=True)
    if state["stage"] == "archive":
        reason = state.get("rejection_reason", "unknown")
        (dest / "rejection.md").write_text(
            f"# Not recommended: {slug}\n\n{reason}\n\n"
            "---\nIf this is a fixable correctness gap, the detail above is the feedback to hand back to GPT for a revision.\n")
    else:
        write_review_summary(dest, state)
        if cfg is None or cfg.get("notify", True):
            notify_user("paper-starter", f"{slug} passed crosscheck — ready for review")
    shutil.rmtree(run_dir)  # runs/ holds in-flight work only
    print(f"[finalize] {slug} -> {dest_root}/ (LLM cost ${state.get('cost_usd', 0.0):.2f})")


# --- rank: the reading list ------------------------------------------------
# Reading is the bottleneck, so after runs finish, Claude ranks everything that
# passed crosscheck into PRIORITY.md — strongest/most-verified first, with a
# one-line reason and risk per note. Deterministic fallback if the call fails,
# so PRIORITY.md is always produced.

def note_title(note_path):
    try:
        m = re.search(r"\\title(?:\[[^\]]*\])?\{(.+?)\}", note_path.read_text(), re.DOTALL)
    except OSError:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def collect_reviewed():
    """Compact factual digest of every run in review/, for ranking."""
    base = ROOT / "review"
    entries = []
    for run in sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []:
        try:
            state = json.loads((run / "state.json").read_text())
            result = json.loads((run / "result.json").read_text())
            claims = result["claims"]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
        try:
            verdict = json.loads((run / "referee" / "crosscheck.json").read_text())
        except (OSError, json.JSONDecodeError):
            verdict = {}
        novelty = {c.get("id"): c for c in verdict.get("novelty", []) if isinstance(c, dict)}
        entries.append({
            "slug": run.name,
            "paper": state.get("paper", "unknown"),
            "title": note_title(run / "note.tex") or run.name,
            "scores": verdict.get("scores", {}),
            "taste_notes": verdict.get("notes", ""),
            "formalization": result.get("formalization"),  # note-level Lean outcome
            "claims": [
                {
                    "id": c.get("id"), "label": c.get("label"), "statement": c.get("statement"),
                    "novelty": novelty.get(c.get("id"), {}).get("novelty_status"),
                }
                for c in claims
            ],
        })
    return entries


def collect_rejected():
    base = ROOT / "archive"
    out = []
    for run in sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []:
        try:
            state = json.loads((run / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        reason = (state.get("rejection_reason", "") or "").splitlines()
        out.append((run.name, reason[0] if reason else "archived"))
    return out


def rank_fallback_key(entry):
    """Deterministic ordering when the ranker call is unavailable: Lean-proved
    first, then taste depth/strength, then how many claims survived as novel. A
    flagged proof error sinks the note (negative), since it needs checking."""
    scores = entry.get("scores", {})
    form = entry.get("formalization") or {}
    proved = 1 if form.get("status") == "proof-formalized" else 0
    flagged = -1 if form.get("error_reported") else 0
    novel = sum(1 for c in entry["claims"] if c.get("novelty") == "novel")
    return (flagged, proved, scores.get("depth", 0), scores.get("strength", 0), novel)


def write_priority_fallback(entries, rejected):
    lines = ["# Reading priority\n", "_Mechanically ordered (ranker unavailable): Lean-proved, then depth, then novelty; flagged errors last._\n"]
    for i, e in enumerate(sorted(entries, key=rank_fallback_key, reverse=True), 1):
        s = e.get("scores", {})
        form = e.get("formalization") or {}
        tag = ""
        if form.get("status") == "proof-formalized":
            tag = " — Lean-proved ✓✓"
        if form.get("error_reported"):
            tag += " — ⚠️ Aristotle flagged a proof error (see lean/ERROR.md)"
        lines.append(f"{i}. **{e['title']}** (`{e['slug']}`, {e['paper']}) — scores {s or 'n/a'}{tag}")
    if rejected:
        lines.append("\n## Not recommended\n")
        for slug, reason in rejected:
            lines.append(f"- `{slug}` — {reason}")
    (ROOT / "PRIORITY.md").write_text("\n".join(lines) + "\n")


def do_rank(cfg):
    entries = collect_reviewed()
    rejected = collect_rejected()
    if not entries:
        print("[rank] nothing in review/ to rank yet")
        return
    work = ROOT / ".rank"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    (work / "digest.json").write_text(json.dumps({"passed": entries, "rejected": [
        {"slug": s, "reason": r} for s, r in rejected]}, indent=2))
    prompt = (ROOT / "prompts" / "rank.md").read_text()

    ok, log = run_claude(work, prompt, cfg["referee_model"], "Read Write", cfg)
    produced = work / "PRIORITY.md"
    if ok and produced.exists():
        shutil.move(str(produced), str(ROOT / "PRIORITY.md"))
        print(f"[rank] wrote PRIORITY.md ({len(entries)} note(s) ranked)")
    else:
        write_priority_fallback(entries, rejected)
        print(f"[rank] ranker call failed; wrote mechanical PRIORITY.md ({len(entries)} note(s))")
    shutil.rmtree(work, ignore_errors=True)


# --- dispatch --------------------------------------------------------------

def advance(run_dir, cfg):
    state = load_state(run_dir)
    stage = state["stage"]
    if stage in ("review", "archive"):
        return False
    # Budget parking: checked BEFORE launching the next stage, so no money is
    # wasted on a call that would be cut off mid-flight. A parked run keeps all
    # completed-stage progress; raising max_usd_per_paper resumes it where it
    # stopped. The lean stage is exempt: Aristotle costs no LLM dollars, and
    # parking a run that already passed crosscheck on its free bonus stage would
    # only delay the review handoff.
    cap = cfg.get("max_usd_per_paper", 0)
    spent = state.get("cost_usd", 0.0)
    if cap and spent >= cap and stage != "lean":
        if not state.get("budget_parked"):
            state["budget_parked"] = True
            append_history(state, stage, f"parked: cumulative cost ${spent:.2f} >= max_usd_per_paper ${cap:.2f}; raise the cap in config.toml to resume")
            save_state(run_dir, state)
        print(f"[budget] {run_dir.name} parked at {stage}: spent ${spent:.2f} >= max_usd_per_paper ${cap:.2f}")
        return False
    if state.get("budget_parked"):
        state["budget_parked"] = False
        append_history(state, stage, "budget cap satisfied again; resuming")
        save_state(run_dir, state)
    if stage == "crosscheck":
        return do_crosscheck(run_dir, state, cfg)
    if stage == "lean":
        return do_lean(run_dir, state, cfg)
    raise ValueError(f"unknown stage {stage!r} in {run_dir}")


# --- locking + main --------------------------------------------------------

@contextlib.contextmanager
def run_lock(run_dir):
    lock_path = run_dir / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def lock_is_stale(lock_path):
    """A lock left by a crashed/killed orchestrator (its PID is gone) should
    not block the run forever."""
    try:
        pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def list_run_dirs():
    runs_dir = ROOT / "runs"
    if not runs_dir.exists():
        return []
    return sorted(p for p in runs_dir.iterdir() if p.is_dir())


def print_status():
    """A quick human-readable snapshot of every run and where it sits. Read-only:
    never advances anything, so it is always safe to call (`orchestrate.py --status`)."""
    for root, header in (("runs", "IN FLIGHT"), ("review", "REVIEW (passed crosscheck)"), ("archive", "NOT RECOMMENDED")):
        base = ROOT / root
        dirs = sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []
        print(f"\n{header} — {len(dirs)}")
        for run in dirs:
            try:
                state = json.loads((run / "state.json").read_text())
            except (OSError, json.JSONDecodeError):
                print(f"  {run.name}: (no readable state.json)")
                continue
            stage = state.get("stage", "?")
            cost = state.get("cost_usd", 0.0)
            extra = ""
            if root == "archive" and state.get("rejection_reason"):
                extra = f" — {state['rejection_reason'].splitlines()[0][:80]}"
            if state.get("budget_parked"):
                extra = " — PARKED (raise max_usd_per_paper to resume)" + extra
            print(f"  {run.name}: {stage} (${cost:.2f}){extra}")
    print()


def preflight():
    missing = [tool for tool in ("claude", "latexmk") if shutil.which(tool) is None]
    if missing:
        raise SystemExit(f"missing required tool(s) on PATH: {', '.join(missing)} — see README.md for setup")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[preflight] ANTHROPIC_API_KEY not set — claude -p will bill your Claude "
              "subscription and share its session limit with your interactive usage; "
              "a 429 mid-pipeline pauses runs until the window resets (see README.md)")
    for d in ("inbox", "runs", "review", "archive"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drain", action="store_true",
                         help="keep advancing every run until all are terminal (review/archive), then rank")
    parser.add_argument("--only", metavar="SLUG",
                         help="advance only runs/<SLUG>, ignoring all other pending runs")
    parser.add_argument("--status", action="store_true",
                         help="print where every run stands and exit (advances nothing)")
    parser.add_argument("--rank", action="store_true",
                         help="(re)generate PRIORITY.md from review/ and exit (advances nothing)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    load_dotenv()  # before preflight: its ANTHROPIC_API_KEY warning must see .env keys
    preflight()
    cfg = load_config()

    if args.rank:
        do_rank(cfg)
        return

    if cfg.get("lean_enabled", True) and not lean_enabled(cfg):
        print("[main] lean track unavailable (needs aristotle CLI on PATH + ARISTOTLE_API_KEY); runs will skip formalization")
    intake(cfg)

    reached_review = False
    max_sweeps = 50 if args.drain else 1
    for _ in range(max_sweeps):
        progressed = False
        for run_dir in list_run_dirs():
            if args.only and run_dir.name != args.only:
                continue
            state = load_state(run_dir)
            if state["stage"] in ("review", "archive"):
                continue
            lock_path = run_dir / ".lock"
            if lock_path.exists():
                if lock_is_stale(lock_path):
                    print(f"[main] removing stale lock on {run_dir.name}")
                    lock_path.unlink(missing_ok=True)
                else:
                    print(f"[main] {run_dir.name} is locked, skipping")
                    continue
            try:
                with run_lock(run_dir):
                    print(f"[main] advancing {run_dir.name} (stage={state['stage']})")
                    changed = advance(run_dir, cfg)
                    progressed = progressed or changed
                    if not (ROOT / "runs" / run_dir.name).exists():
                        reached_review = reached_review or (ROOT / "review" / run_dir.name).exists()
            except Exception as e:
                print(f"[main] ERROR advancing {run_dir.name}: {e}")
        if not args.drain or not progressed:
            break

    # After a --drain, refresh the reading list once if anything new landed in
    # review/. (A single-step run doesn't auto-rank — use --rank to refresh.)
    if args.drain and reached_review:
        do_rank(cfg)


if __name__ == "__main__":
    main()
