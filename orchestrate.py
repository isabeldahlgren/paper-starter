#!/usr/bin/env python3
"""proof-engineering orchestrator — the whole pipeline in one stdlib-only file.

READING GUIDE
=============
The pipeline moves each paper through a sequence of stages, one stage per
invocation (or all the way with --drain):

    inbox/  ->  explore  ->  self_check  ->  novelty  ->  <gates>  ->  correctness  ->  lean  ->  review/
                (generate)   (cheap kill)   (prior art)  (taste...)   (2 passes)     (bonus)      archive/

Every run is a directory under runs/<slug>/ with a state.json recording which
stage it is at. There is no server and no queue: the filesystem IS the database,
re-running the orchestrator just continues unfinished runs, and it is safe to
run concurrently (per-run .lock file).

Each stage is one call to `claude -p` driven by a prompt file in prompts/.
Tuning the system means editing those prompts and config.toml, not this code.

WHERE TO LOOK
-------------
    main()          entry point: load .env, preflight, intake, then advance runs
    advance()       the stage dispatcher — maps state["stage"] to a do_* function
    intake()        watches inbox/, creates runs/<slug>/ (intake_arxiv/intake_pdf)
    do_explore()    stage 1: generator writes note.tex + result.json  (prompts/explore.md)
    do_self_check() stage 2: adversarial re-derivation + counterexample search
    do_novelty()    stage 3: prior-art search referee
    do_gate()       stage 4: the generic pass/fail gates (taste + any you add)
    do_correctness()stage 5: two independent blind referee passes
    do_lean()       stage 5b: optional Aristotle/Lean formalization (bonus, non-gating)
    finalize()      moves a finished run into review/ or archive/

Stage order is data-driven (stage_sequence()): a fixed head/tail with the
user-configurable [[gates]] band spliced in the middle. See DOCS.md to customize.
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
GENERATOR_TOOLS = "Read Write Edit Bash"
# Explore additionally gets web access: prompts/explore.md requires a
# literature sanity check on the chosen direction BEFORE the note is written,
# so a known result costs a few searches instead of the full explore +
# self_check spend before the novelty gate catches it.
EXPLORE_TOOLS = GENERATOR_TOOLS + " WebSearch WebFetch"
# Repair only applies the self-check's listed fixes to existing files.
REPAIR_TOOLS = "Read Edit Bash"
# Stage machinery. The pipeline is: explore -> self_check -> novelty -> <gates>
# -> correctness -> lean -> review|archive. The head and tail stages have
# bespoke per-claim semantics; the middle band is a user-configurable list of
# pass/fail referee GATES (config.toml `[[gates]]`), of which `taste` ships as
# the default. See gate_specs()/do_gate() and "Adding a custom gate" in CLAUDE.md.
FIXED_HEAD_STAGES = ("explore", "self_check", "novelty")
FIXED_TAIL_STAGES = ("correctness", "lean")
RESERVED_STAGE_NAMES = set(FIXED_HEAD_STAGES) | set(FIXED_TAIL_STAGES) | {"review", "archive", "intake"}


# Defaults for every optional config.toml key, so a minimal config (or none at
# all) still runs. config.toml only needs to hold the values you want to
# override; the essays explaining each key live in DOCS.md, not here.
CONFIG_DEFAULTS = {
    "author": "Proof Engineer",
    "generator_model": "sonnet",
    "self_check_model": "sonnet",
    "taste_model": "sonnet",
    "referee_model": "sonnet",
    "gates": [{"name": "taste", "on_fail": "archive", "calibration": True}],
    "max_runs_per_day": 5,
    "max_usd_per_run": 10.0,
    "max_usd_per_paper": 0,
    "max_explore_attempts": 2,
    "max_self_check_attempts": 2,
    "max_referee_attempts": 2,
    "stage_timeout_seconds": 1800,
    "explore_timeout_seconds": 3600,
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
    the key must be absent from BOTH the shell environment and .env —
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
        "stage": "explore",
        "attempts": {"explore": 0, "self_check": 0},
        "feedback": None,
        "history": [{"stage": "intake", "at": now(), "note": f"created for {paper_ref}"}],
    }


def load_state(run_dir):
    return json.loads((run_dir / "state.json").read_text())


def save_state(run_dir, state):
    (run_dir / "state.json").write_text(json.dumps(state, indent=2))


def append_history(state, stage, note):
    state.setdefault("history", []).append({"stage": stage, "at": now(), "note": note})


# --- intake ----------------------------------------------------------------

def slugify_arxiv(arxiv_id):
    return "arxiv-" + arxiv_id.replace("/", "-")


def slugify_pdf(path):
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", path.stem).strip("-").lower()
    return f"pdf-{base}"


def fetch_arxiv_source(arxiv_id, dest_dir):
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "proof-engineering/0.1 (research pipeline)"})
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
# mathematical content the generator needs: .sty/.cls/.bst are camera-ready
# LaTeX rendering machinery, .bib/.bbl is bibliography data (the novelty stage
# does its own citation search), aux/build residue is compiler bookkeeping,
# and figures are images the model can only burn vision tokens on, not read
# for content it can't already get from the surrounding .tex prose/captions.
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


def intake_arxiv(arxiv_id):
    """Create a run for an arXiv ID. Returns True iff a new run was created."""
    slug = slugify_arxiv(arxiv_id)
    run_dir = ROOT / "runs" / slug
    if run_dir.exists() or (ROOT / "review" / slug).exists() or (ROOT / "archive" / slug).exists():
        print(f"[intake] {slug} already exists, skipping")
        return False
    run_dir.mkdir(parents=True)
    paper_dir = run_dir / "paper"
    paper_dir.mkdir()
    try:
        fetch_arxiv_source(arxiv_id, paper_dir)
    except Exception as e:
        shutil.rmtree(run_dir)
        print(f"[intake] failed to fetch {arxiv_id}: {e}")
        return False
    removed = prune_paper_dir(paper_dir)
    if removed:
        print(f"[intake] pruned {len(removed)} non-content file(s) from paper/: {', '.join(str(p) for p in removed)}")
    saved = strip_tex_comments(paper_dir)
    if saved:
        print(f"[intake] stripped {saved} bytes of TeX comments from paper/")
    save_state(run_dir, init_state(f"arxiv:{arxiv_id}"))
    print(f"[intake] created run {slug}")
    return True


def intake_pdf(pdf_path):
    """Create a run for a local PDF. Returns True iff a new run was created."""
    slug = slugify_pdf(pdf_path)
    run_dir = ROOT / "runs" / slug
    if run_dir.exists() or (ROOT / "review" / slug).exists() or (ROOT / "archive" / slug).exists():
        print(f"[intake] {slug} already exists, skipping")
        return False
    if shutil.which("pdftotext") is None:
        print(f"[intake] cannot ingest {pdf_path.name}: pdftotext not installed (brew install poppler)")
        return False
    run_dir.mkdir(parents=True)
    paper_dir = run_dir / "paper"
    paper_dir.mkdir()
    # Keep the original PDF outside paper/ so the generator's "read everything
    # under ./paper/" only ever sees the text extraction, never re-renders the
    # original PDF page images (a real, easy-to-hit token/vision cost).
    dest_pdf = run_dir / pdf_path.name
    shutil.copy(pdf_path, dest_pdf)
    proc = subprocess.run(
        ["pdftotext", str(dest_pdf), str(paper_dir / "paper.txt")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[intake] pdftotext failed for {pdf_path.name}: {proc.stderr}")
    save_state(run_dir, init_state(f"pdf:{pdf_path.name}"))
    print(f"[intake] created run {slug}")
    return True


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


def intake(cfg):
    inbox = ROOT / "inbox"
    processed = inbox / ".processed"
    processed.mkdir(exist_ok=True)
    quota = cfg.get("max_runs_per_day", 0)
    remaining = max(0, quota - runs_created_today()) if quota else None

    for f in sorted(inbox.iterdir()):
        if f.is_dir() or f.name.startswith("."):
            continue
        if f.suffix == ".pdf":
            if remaining == 0:
                print(f"[intake] max_runs_per_day reached; leaving {f.name} in inbox")
                continue
            if shutil.which("pdftotext") is None:
                print(f"[intake] leaving {f.name} in inbox: pdftotext not installed (brew install poppler)")
                continue
            if intake_pdf(f) and remaining is not None:
                remaining -= 1
            shutil.move(str(f), str(processed / f.name))
        elif f.suffix == ".txt":
            ids = [
                line.strip() for line in f.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            leftover = []
            for i, arxiv_id in enumerate(ids):
                if remaining == 0:
                    leftover = ids[i:]
                    break
                if intake_arxiv(arxiv_id) and remaining is not None:
                    remaining -= 1
            if leftover:
                f.write_text("\n".join(leftover) + "\n")
                print(f"[intake] max_runs_per_day reached; {len(leftover)} id(s) left queued in {f.name}")
            else:
                shutil.move(str(f), str(processed / f.name))


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
    # invocation and would hand the generator/referees tools they must not have.
    cmd += list(cfg.get("claude_extra_args", []))
    if cfg.get("max_usd_per_run", 0):
        cmd += ["--max-budget-usd", str(cfg["max_usd_per_run"])]
    timeout = timeout_s or cfg.get("stage_timeout_seconds", 1800)
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
# Verifier independence (design principle 3): referees must not see the
# generator's reasoning or confidence, only the finished artifact. We enforce
# this with the filesystem, not just instructions: each referee stage gets its
# own subdirectory containing only what it's allowed to see, and Claude Code's
# tool sandboxing means Read/Write/Bash inside that cwd cannot reach files
# outside it (no --add-dir is granted, so paper/, self_check/, prior referee/
# reports etc. are simply not visible).

def strip_result_for_referee(result_path, dest_path):
    data = json.loads(result_path.read_text())
    stripped = {
        "paper": data["paper"],
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
    if include_paper:
        shutil.copytree(run_dir / "paper", dest / "paper")
    shutil.copy(run_dir / "note.tex", dest / "note.tex")
    strip_result_for_referee(run_dir / "result.json", dest / "result.json")
    return dest


def build_taste_calibration():
    parts = []
    for verdict, dirname in (("ACCEPTED", "accepted"), ("REJECTED", "rejected")):
        d = ROOT / "taste" / dirname
        examples = sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")) if d.exists() else []
        for p in examples:
            parts.append(f"### Past {verdict} example ({p.name})\n\n{p.read_text()}\n")
    if not parts:
        return "\n\n(No calibration examples yet — use your own judgment applying the criteria above.)\n"
    return "\n\n## Calibration: this user's past judgments\n\n" + "\n".join(parts)


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
        for key in ("id", "label", "statement", "proof_status"):
            if key not in c:
                return False, f"claim missing '{key}': {c}"
        if c["label"] not in VALID_LABELS:
            return False, f"invalid label '{c['label']}' in {c.get('id')}"
        if c["proof_status"] not in VALID_STATUS:
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

def fail_or_archive(run_dir, state, stage_name, reason, cfg):
    """Retry the same stage, or archive if attempts are exhausted."""
    max_attempts = cfg[f"max_{stage_name}_attempts"]
    if state["attempts"][stage_name] < max_attempts:
        state["feedback"] = reason
        append_history(state, stage_name, f"attempt failed, retrying: {reason}")
        save_state(run_dir, state)
        return True
    state["stage"] = "archive"
    state["rejection_reason"] = reason
    append_history(state, stage_name, f"attempt failed, retries exhausted: {reason}")
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


def fail_to_explore_or_archive(run_dir, state, stage, reason, cfg):
    """A real but recoverable finding (gap, counterexample, partially-known
    results) sends the run back to explore, or archives if explore is out of attempts."""
    if state["attempts"]["explore"] < cfg["max_explore_attempts"]:
        state["stage"] = "explore"
        state["feedback"] = reason
        append_history(state, stage, f"failed, sending back to explore: {reason}")
        save_state(run_dir, state)
        return True
    state["stage"] = "archive"
    state["rejection_reason"] = reason
    append_history(state, stage, f"failed and explore retries exhausted: {reason}")
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


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


def explore_artifacts_fresh(run_dir, started_at):
    """True if note.tex and result.json both exist and were (re)written by the
    invocation that started at started_at — i.e. they are that invocation's own
    output, not stale leftovers from a previous explore attempt. Used to
    salvage a run whose invocation died (timeout, dropped connection) AFTER
    finishing its actual work: the lint gates, not the exit code, decide."""
    try:
        return all((run_dir / name).stat().st_mtime >= started_at for name in ("note.tex", "result.json"))
    except FileNotFoundError:
        return False


def format_feedback(feedback, limit=4000):
    """Feedback fed back into a generator prompt. Substantive feedback (gaps,
    novelty breakdowns) is model-written and short; operational failures embed
    the raw CLI JSON blob, which is noise and token cost to the next attempt —
    keep the head, which carries the actual error."""
    if len(feedback) <= limit:
        return feedback
    return feedback[:limit] + "\n[... truncated: full detail in state.json history ...]"


def do_explore(run_dir, state, cfg):
    attempts = state["attempts"]["explore"]
    prompt_md = (ROOT / "PROMPT.md").read_text()
    explore_md = (ROOT / "prompts" / "explore.md").read_text()
    explore_md = explore_md.replace("{{VENV_PYTHON}}", str(ROOT / cfg["venv_python"]))
    explore_md = explore_md.replace("{{AUTHOR}}", cfg.get("author", "Proof Engineer"))
    prompt = prompt_md + "\n\n" + explore_md
    if state.get("feedback"):
        prompt += f"\n\n---\n\nA previous attempt failed. Fix this and try again:\n\n{format_feedback(state['feedback'])}\n"

    started_at = time.time()
    ok, log = run_claude(run_dir, prompt, cfg["generator_model"], EXPLORE_TOOLS, cfg,
                         timeout_s=cfg.get("explore_timeout_seconds"))
    (run_dir / f"explore_attempt_{attempts + 1}.log").write_text(log)
    track_cost(state, log)
    if not ok:
        # Salvage before anything else: an invocation can die (wall-clock
        # timeout, connection dropped mid-response) after having already
        # written a complete note. If the artifacts are its own fresh output,
        # let the lint gates below judge them instead of discarding the work.
        if explore_artifacts_fresh(run_dir, started_at):
            append_history(state, "explore", "invocation reported failure but left freshly written note.tex + result.json; salvaging and applying the normal lint gates")
            ok = True
        elif is_transient_failure(log):
            return defer_stage(run_dir, state, "explore", log)
    state["attempts"]["explore"] = attempts + 1

    if not ok:
        return fail_or_archive(run_dir, state, "explore", f"claude invocation failed:\n{log}", cfg)

    if not (run_dir / "note.tex").exists() or not (run_dir / "result.json").exists():
        return fail_or_archive(run_dir, state, "explore", "Model did not produce both note.tex and result.json.", cfg)

    compile_ok, compile_err = compile_latex(run_dir)
    if not compile_ok:
        return fail_or_archive(run_dir, state, "explore", f"latex compile failed:\n{compile_err}", cfg)

    valid, err = validate_result_json(run_dir / "result.json")
    if not valid:
        return fail_or_archive(run_dir, state, "explore", f"result.json invalid: {err}", cfg)

    state["stage"] = "self_check"
    state["feedback"] = None
    append_history(state, "explore", "passed lint (latex compiles, result.json valid)")
    save_state(run_dir, state)
    return True


def do_self_check(run_dir, state, cfg):
    attempts = state["attempts"]["self_check"]
    self_check_md = (ROOT / "prompts" / "self_check.md").read_text()
    self_check_md = self_check_md.replace("{{VENV_PYTHON}}", str(ROOT / cfg["venv_python"]))

    ok, log = run_claude(run_dir, self_check_md, cfg["self_check_model"], GENERATOR_TOOLS, cfg)
    (run_dir / f"self_check_attempt_{attempts + 1}.log").write_text(log)
    track_cost(state, log)
    if not ok and is_transient_failure(log):
        return defer_stage(run_dir, state, "self_check", log)
    state["attempts"]["self_check"] = attempts + 1

    if not ok:
        # An invocation failure is the checker's problem, not the note's:
        # retry self_check itself rather than re-running the expensive
        # explore stage.
        return fail_or_archive(run_dir, state, "self_check", f"self-check invocation failed:\n{log}", cfg)

    valid, err = validate_result_json(run_dir / "result.json")
    if not valid:
        return fail_to_explore_or_archive(run_dir, state, "self_check", f"self-check left result.json invalid: {err}", cfg)

    claims = json.loads((run_dir / "result.json").read_text())["claims"]
    bad = [
        c for c in claims
        if c["label"] == "theorem-complete-proof" and c["proof_status"] in ("gap-found", "counterexample-found")
    ]
    if bad:
        details = "\n".join(f"- {c['id']}: {c['proof_status']} — {c.get('self_check_notes', '')}" for c in bad)
        return fail_to_explore_or_archive(run_dir, state, "self_check", f"Self-check found problems:\n{details}", cfg)

    append_history(state, "self_check", "passed: no theorem-complete-proof claim has a gap or counterexample")
    apply_self_check_fixes(run_dir, state, cfg)
    state["stage"] = "novelty"
    save_state(run_dir, state)
    return True


def claim_skeleton(result_text):
    """The structural fields of result.json that gates key off — a repair pass
    must leave these untouched (it may only reword statements/prose)."""
    data = json.loads(result_text)
    return [(c.get("id"), c.get("label"), c.get("proof_status")) for c in data.get("claims", [])]


def apply_self_check_fixes(run_dir, state, cfg):
    """Self-check may leave self_check/fixes.md: concrete minor defects that
    are not gaps in load-bearing proofs (prose contradicting the note's own
    table, a mislabeled script output) and so don't justify a full explore
    rerun — but would otherwise reach the correctness referee, whose verdict
    on them triggers exactly that expensive loop. Apply them with one cheap
    invocation before any referee sees the note. Best-effort: any failure
    restores the pre-repair files and the run proceeds with the original note."""
    fixes = run_dir / "self_check" / "fixes.md"
    if not fixes.exists():
        return
    attempt = state["attempts"].get("repair", 0) + 1
    state["attempts"]["repair"] = attempt
    backups = {name: (run_dir / name).read_text() for name in ("note.tex", "result.json")}
    skeleton_before = claim_skeleton(backups["result.json"])

    prompt = (ROOT / "prompts" / "repair.md").read_text()
    ok, log = run_claude(run_dir, prompt, cfg["self_check_model"], REPAIR_TOOLS, cfg)
    (run_dir / f"repair_attempt_{attempt}.log").write_text(log)
    track_cost(state, log)

    err = None
    if not ok:
        err = "repair invocation failed"
    else:
        compile_ok, compile_err = compile_latex(run_dir)
        if not compile_ok:
            err = f"repaired note.tex does not compile: {compile_err}"
        else:
            valid, verr = validate_result_json(run_dir / "result.json")
            if not valid:
                err = f"repair left result.json invalid: {verr}"
            elif claim_skeleton((run_dir / "result.json").read_text()) != skeleton_before:
                err = "repair changed claim ids/labels/proof_status (only statements may be reworded)"

    if err:
        for name, text in backups.items():
            (run_dir / name).write_text(text)
        fixes.rename(fixes.with_name("fixes.failed.md"))
        append_history(state, "self_check", f"repair pass failed, proceeding with unrepaired note: {err}")
        print(f"[self_check] repair pass failed on {run_dir.name}; proceeding with unrepaired note")
        return
    fixes.rename(fixes.with_name("fixes.applied.md"))
    append_history(state, "self_check", "repair pass applied self_check/fixes.md")


def gate_specs(cfg):
    """User-defined pass/fail referee gates (config.toml `[[gates]]`), run in
    config order between novelty and correctness. Each is a dict with a required
    `name` and optional `prompt`, `on_fail`, `include_paper`, `tools`, `model`,
    `calibration` (see do_gate for the contract)."""
    return cfg.get("gates", []) or []


def stage_sequence(cfg):
    """The full ordered stage list for this config, gates spliced in by name."""
    return list(FIXED_HEAD_STAGES) + [g["name"] for g in gate_specs(cfg)] + list(FIXED_TAIL_STAGES)


def next_stage(cfg, current):
    """The stage a passing `current` advances to. Used by novelty and the gates
    so the sequence stays data-driven: renaming/reordering/removing a gate in
    config.toml just works, no transition edits needed."""
    seq = stage_sequence(cfg)
    i = seq.index(current)
    return seq[i + 1] if i + 1 < len(seq) else "review"


def validate_gate_config(cfg):
    """Fail fast at startup on a malformed `[[gates]]` table rather than deep in
    a run: every gate needs a unique, non-reserved name and an existing prompt."""
    seen = set()
    for gate in gate_specs(cfg):
        name = gate.get("name")
        if not name or not isinstance(name, str):
            raise SystemExit(f"[[gates]] entry missing a string 'name': {gate!r}")
        if name in RESERVED_STAGE_NAMES:
            raise SystemExit(f"gate name {name!r} collides with a built-in stage; pick another")
        if name in seen:
            raise SystemExit(f"duplicate gate name {name!r} in config.toml")
        seen.add(name)
        prompt = ROOT / "prompts" / gate.get("prompt", f"{name}.md")
        if not prompt.exists():
            raise SystemExit(f"gate {name!r} references missing prompt file {prompt}")
        if gate.get("on_fail", "archive") not in ("archive", "explore"):
            raise SystemExit(f"gate {name!r} has invalid on_fail {gate.get('on_fail')!r} (expected 'archive' or 'explore')")


def do_gate(run_dir, state, cfg, gate):
    """A generic pass/fail referee gate — the shape shared by taste and any
    user-added gate. Runs one isolated `claude -p` over paper+note and reads a
    `verdict.json` of the form {"verdict": "pass"|"fail", "notes": "...",
    "scores": {...}}. On pass -> next stage; on fail -> archive (or back to
    explore if the gate sets on_fail = "explore"). Operational/transient
    failures are handled exactly as the built-in referee stages."""
    name = gate["name"]
    (run_dir / "referee").mkdir(exist_ok=True)
    view_dir = make_referee_dir(run_dir, f"{name}_view", include_paper=gate.get("include_paper", True))
    prompt = (ROOT / "prompts" / gate.get("prompt", f"{name}.md")).read_text()
    if gate.get("calibration"):
        prompt += build_taste_calibration()
    model = gate.get("model") or cfg.get("referee_model", "sonnet")
    tools = gate.get("tools", "Read Write")
    attempt = state["attempts"].get(name, 0) + 1

    ok, log = run_claude(view_dir, prompt, model, tools, cfg)
    (run_dir / f"{name}_attempt_{attempt}.log").write_text(log)
    track_cost(state, log)
    if not ok and is_transient_failure(log):
        return defer_stage(run_dir, state, name, log)

    if not ok:
        return referee_stage_error(run_dir, state, name, f"invocation failed:\n{log}", cfg)
    verdict, err = load_verdict(view_dir / "verdict.json")
    if verdict is None:
        return referee_stage_error(run_dir, state, name, err, cfg)

    shutil.copy(view_dir / "verdict.json", run_dir / "referee" / f"{name}.json")
    if (view_dir / "report.md").exists():
        shutil.copy(view_dir / "report.md", run_dir / "referee" / f"{name}_report.md")

    if verdict.get("verdict") not in ("pass", "fail"):
        return referee_stage_error(run_dir, state, name, f"verdict.json has no valid 'verdict' field: {verdict.get('verdict')!r}", cfg)
    if verdict["verdict"] != "pass":
        reason = f"{name} gate rejected: {verdict.get('notes', '')}"
        if verdict.get("scores"):
            reason += f"\nScores: {verdict.get('scores')}"
        if gate.get("on_fail") == "explore":
            return fail_to_explore_or_archive(run_dir, state, name, reason, cfg)
        return reject(run_dir, state, name, reason, f"archived: failed {name} gate", cfg)

    state["stage"] = next_stage(cfg, name)
    append_history(state, name, f"passed: {verdict.get('scores') or verdict.get('notes', 'ok')}")
    save_state(run_dir, state)
    return True


def do_novelty(run_dir, state, cfg):
    (run_dir / "referee").mkdir(exist_ok=True)
    view_dir = make_referee_dir(run_dir, "novelty_view", include_paper=True)
    prompt = (ROOT / "prompts" / "novelty.md").read_text()
    attempt = state["attempts"].get("novelty", 0) + 1

    ok, log = run_claude(view_dir, prompt, cfg["taste_model"], "Read Write WebSearch WebFetch", cfg)
    (run_dir / f"novelty_attempt_{attempt}.log").write_text(log)
    track_cost(state, log)
    if not ok and is_transient_failure(log):
        return defer_stage(run_dir, state, "novelty", log)

    if not ok:
        return referee_stage_error(run_dir, state, "novelty", f"invocation failed:\n{log}", cfg)
    verdict, err = load_verdict(view_dir / "verdict.json")
    if verdict is None:
        return referee_stage_error(run_dir, state, "novelty", err, cfg)

    shutil.copy(view_dir / "verdict.json", run_dir / "referee" / "novelty.json")
    if (view_dir / "report.md").exists():
        shutil.copy(view_dir / "report.md", run_dir / "referee" / "novelty_report.md")

    # The referee must return a status for every claim it was asked to check;
    # a silently missing claim is a malformed verdict, not a pass.
    result_claims = json.loads((run_dir / "result.json").read_text())["claims"]
    required_ids = {
        c["id"] for c in result_claims
        if c["label"] in ("theorem-complete-proof", "conjecture-with-evidence")
    }
    verdict_claims = verdict.get("claims")
    if not isinstance(verdict_claims, list):
        return referee_stage_error(run_dir, state, "novelty", "verdict.json has no 'claims' list", cfg)
    covered = {c.get("id") for c in verdict_claims}
    missing = required_ids - covered
    if missing:
        return referee_stage_error(run_dir, state, "novelty", f"verdict.json missing claim(s): {sorted(missing)}", cfg)

    known = [c for c in verdict_claims if c.get("novelty_status") == "known"]
    if known:
        details = "\n".join(f"- {c.get('id')}: known — {c.get('reference', '')}" for c in known)
        surviving = [c for c in verdict_claims if c.get("novelty_status") != "known"]
        # A run whose main result is known but whose other claims survived the
        # literature check still has salvageable content: send it back to
        # explore to be rebuilt around the surviving claims rather than
        # discarding the novel work along with the rediscovery. Archive only
        # when nothing substantive survived.
        if surviving:
            statuses = "\n".join(f"- {c.get('id')}: {c.get('novelty_status')}" for c in surviving)
            feedback = (
                f"The novelty referee found that some claims are already in the literature:\n{details}\n\n"
                f"These claims survived the literature check:\n{statuses}\n\n"
                "Rewrite the note around the surviving claims: demote each known result to cited background "
                "(using the references above), make the strongest surviving claim the main result, and try to "
                "strengthen or extend it now that the known material is settled context rather than the goal. "
                "The full novelty report is in ./referee/novelty_report.md."
            )
            return fail_to_explore_or_archive(run_dir, state, "novelty", feedback, cfg)
        return reject(run_dir, state, "novelty",
                      f"Novelty check found prior work for every substantive claim:\n{details}",
                      "archived: all substantive claims already known", cfg)

    state["stage"] = next_stage(cfg, "novelty")
    append_history(state, "novelty", "passed: no claim found in prior literature")
    save_state(run_dir, state)
    return True


def do_correctness(run_dir, state, cfg):
    (run_dir / "referee").mkdir(exist_ok=True)
    view_dir = make_referee_dir(run_dir, "correctness_view", include_paper=False)
    prompt = (ROOT / "prompts" / "correctness_referee.md").read_text()

    result_claims = json.loads((run_dir / "result.json").read_text())["claims"]
    load_bearing_ids = {c["id"] for c in result_claims if c["label"] == "theorem-complete-proof"}

    passes = []
    for i in (1, 2):
        for f in ("verdict.json", "report.md"):
            p = view_dir / f
            if p.exists():
                p.unlink()
        ok, log = run_claude(view_dir, prompt, cfg["referee_model"], "Read Write", cfg)
        (run_dir / f"correctness_pass{i}.log").write_text(log)
        track_cost(state, log)
        if not ok and is_transient_failure(log):
            return defer_stage(run_dir, state, "correctness", log)
        if not ok:
            return referee_stage_error(run_dir, state, "correctness", f"pass {i} invocation failed:\n{log}", cfg)
        verdict, err = load_verdict(view_dir / "verdict.json")
        if verdict is None:
            return referee_stage_error(run_dir, state, "correctness", f"pass {i}: {err}", cfg)
        shutil.copy(view_dir / "verdict.json", run_dir / "referee" / f"correctness_pass{i}.json")
        if (view_dir / "report.md").exists():
            shutil.copy(view_dir / "report.md", run_dir / "referee" / f"correctness_pass{i}_report.md")
        passes.append(verdict)

    # Every load-bearing claim must receive an explicit, valid verdict in BOTH
    # passes. A pass whose verdict.json omits a claim (or garbles a verdict
    # value) must not silently count as approval — false positives are the
    # expensive failure mode here.
    bad = []
    for i, res in enumerate(passes, start=1):
        claims = res.get("claims") if isinstance(res.get("claims"), list) else []
        by_id = {c.get("id"): c for c in claims}
        for cid in sorted(load_bearing_ids):
            c = by_id.get(cid)
            if c is None or c.get("verdict") not in VALID_CORRECTNESS_VERDICTS:
                return referee_stage_error(
                    run_dir, state, "correctness",
                    f"pass {i} verdict.json has no valid verdict for load-bearing claim '{cid}'", cfg)
            if c["verdict"] != "correct":
                bad.append((i, cid, c["verdict"], c.get("notes", "")))

    if bad:
        reason = "\n".join(f"- pass {i}/{cid}: {v} — {notes}" for i, cid, v, notes in bad)
        if all(v == "fixable-gap" for _, _, v, _ in bad):
            return fail_to_explore_or_archive(run_dir, state, "correctness", f"Correctness referee found fixable gaps:\n{reason}", cfg)
        return reject(run_dir, state, "correctness",
                      f"Correctness referee found unrecoverable issues:\n{reason}",
                      "archived: wrong/cannot-verify verdict", cfg)

    append_history(state, "correctness", "passed: both independent passes verified all load-bearing claims")
    if lean_enabled(cfg):
        state["stage"] = "lean"
        append_history(state, "correctness", "entering lean formalization track (bonus evidence, non-gating)")
        save_state(run_dir, state)
        return True
    state["stage"] = "review"
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


# --- lean track (Aristotle) ----------------------------------------------------
# Stage 5b: Lean 4 formalization as BONUS EVIDENCE, never a gate. The run has
# already passed both gates when it arrives here, so no Aristotle outcome may
# demote it — the only exit is review. Aristotle (Harmonic's cloud prover,
# free: no Anthropic tokens) can take hours per claim, so submissions are
# asynchronous (`aristotle formalize` WITHOUT --wait): the run sits at this
# stage across orchestrator invocations, and each tick advances whatever it
# can — submit pending claims, poll running ones, download finished ones —
# until every claim is resolved or hits lean_timeout_minutes.

ARISTOTLE_TERMINAL = {"COMPLETE", "COMPLETE_WITH_ERRORS", "OUT_OF_BUDGET", "FAILED", "CANCELED"}
ARISTOTLE_STATUSES = ARISTOTLE_TERMINAL | {"QUEUED", "IN_PROGRESS", "UNKNOWN"}
VALID_FORMALIZATION = {"proof-formalized", "statement-formalized", "not-formalizable"}


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


def lean_claim_stem(claim_id):
    return "claim-" + (re.sub(r"[^a-zA-Z0-9_-]+", "-", claim_id).strip("-") or "unnamed")


def write_lean_problem(run_dir, lean_dir, claim):
    """One self-contained problem file per claim: Aristotle's formalize
    endpoint takes a single file plus the fixed prompt "Formalize <filename>",
    so the instructions, the statement, and the note (for definitions and
    notation) all ride inside the file itself."""
    path = lean_dir / f"{lean_claim_stem(claim['id'])}.tex"
    path.write_text(
        "Formalize the claim below as a Lean 4 theorem and prove it. "
        "Produce a faithful, type-correct statement of exactly this claim; do not weaken or alter it to make the proof easier. "
        "If a complete proof is out of reach, leave the faithful statement with `sorry` rather than proving a different theorem.\n\n"
        f"CLAIM ({claim['id']}):\n\n{claim['statement']}\n\n"
        "CONTEXT — the research note this claim is taken from, for definitions and notation:\n\n"
        + (run_dir / "note.tex").read_text()
    )
    return path


def fetch_lean_result(lean_dir, cid, entry):
    """Download and unpack the finished project's Lean files. Best-effort: the
    verdict is the task status Aristotle reported; missing files never change it."""
    stem = lean_claim_stem(cid)
    tar_path = lean_dir / f"{stem}.result.tar.gz"
    run_aristotle(["download", entry["project_id"], "--destination", str(tar_path)])
    if not tar_path.exists():
        entry["download_failed"] = True
        return
    dest = lean_dir / stem
    dest.mkdir(exist_ok=True)
    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(dest, filter="data")
        tar_path.unlink()
    except (tarfile.TarError, OSError):
        entry["download_failed"] = True


def lean_done(run_dir, state, cfg, note):
    state["stage"] = "review"
    append_history(state, "lean", note)
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


def do_lean(run_dir, state, cfg):
    if not lean_enabled(cfg):
        return lean_done(run_dir, state, cfg, "skipped: lean_enabled=false, aristotle CLI missing, or ARISTOTLE_API_KEY unset")
    claims = json.loads((run_dir / "result.json").read_text())["claims"]
    targets = {c["id"]: c for c in claims if c["label"] == "theorem-complete-proof"}
    if not targets:
        return lean_done(run_dir, state, cfg, "skipped: no theorem-complete-proof claim to formalize")

    lean_dir = run_dir / "lean"
    lean_dir.mkdir(exist_ok=True)
    entries = state.setdefault("lean", {})
    for cid in targets:
        entries.setdefault(cid, {"status": "pending"})
    timeout_s = cfg.get("lean_timeout_minutes", 240) * 60
    progressed = False

    for cid, entry in entries.items():
        if entry["status"] == "pending":
            problem = write_lean_problem(run_dir, lean_dir, targets[cid])
            ok, log = run_aristotle(["formalize", str(problem)])
            m = re.search(r"Project created:\s*(\S+)", log)
            if m:
                entry.update({"status": "submitted", "project_id": m.group(1), "submitted_at": time.time()})
                append_history(state, "lean", f"{cid}: submitted to aristotle (project {m.group(1)})")
                progressed = True
            elif "too many requests" in log.lower():
                # Aristotle's concurrent-task cap, not this claim's fault:
                # stay pending and resubmit once a slot frees up.
                pass
            else:
                fails = entry["submit_failures"] = entry.get("submit_failures", 0) + 1
                if fails >= 3:
                    entry.update({"status": "not-formalizable", "aristotle_status": "submit-failed"})
                    append_history(state, "lean", f"{cid}: submission failed {fails} times, marking not-formalizable: {log[:500]}")
                    progressed = True
        elif entry["status"] == "submitted":
            ok, log = run_aristotle(["tasks", entry["project_id"], "--limit", "1"])
            status = parse_aristotle_task_status(log)
            if status in ARISTOTLE_TERMINAL:
                entry["aristotle_status"] = status
                if status in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
                    # COMPLETE means Aristotle proved the faithful statement;
                    # COMPLETE_WITH_ERRORS means partial progress — count it
                    # only as a formalized statement, never as a proof.
                    entry["status"] = "proof-formalized" if status == "COMPLETE" else "statement-formalized"
                    fetch_lean_result(lean_dir, cid, entry)
                else:
                    entry["status"] = "not-formalizable"
                append_history(state, "lean", f"{cid}: aristotle finished {status} -> {entry['status']}")
                progressed = True
            elif time.time() - entry.get("submitted_at", 0) > timeout_s:
                run_aristotle(["cancel", entry["project_id"]])  # best-effort: frees the account's concurrent-task slot
                entry.update({"status": "not-formalizable", "aristotle_status": "timeout"})
                append_history(state, "lean", f"{cid}: no result within lean_timeout_minutes ({cfg.get('lean_timeout_minutes', 240)}m), canceled and marked not-formalizable")
                progressed = True

    if all(e["status"] in VALID_FORMALIZATION for e in entries.values()):
        data = json.loads((run_dir / "result.json").read_text())
        for c in data["claims"]:
            if c["id"] in entries:
                c["formalization"] = entries[c["id"]]["status"]
        (run_dir / "result.json").write_text(json.dumps(data, indent=2))
        summary = ", ".join(f"{cid}: {e['status']}" for cid, e in sorted(entries.items()))
        return lean_done(run_dir, state, cfg, f"lean track finished (non-gating): {summary}")

    save_state(run_dir, state)
    waiting = sum(1 for e in entries.values() if e["status"] in ("pending", "submitted"))
    print(f"[lean] {run_dir.name}: {waiting} claim(s) still with aristotle; will poll on a later invocation")
    return progressed


# --- finalize ----------------------------------------------------------------

def write_review_summary(dest, state, cfg=None):
    """Mechanical one-page SUMMARY.md for the user: claims + all gate evidence."""
    lines = [f"# {dest.name} — passed all gates", ""]
    lines.append(f"Source paper: {state.get('paper', 'unknown')}. Compiled note: `note.pdf`; full referee reports under `referee/`. Total LLM cost: ${state.get('cost_usd', 0.0):.2f}.")
    lines.append("")
    try:
        claims = json.loads((dest / "result.json").read_text())["claims"]
    except (OSError, json.JSONDecodeError, KeyError):
        claims = []
    lines.append("## Claims")
    lines.append("")
    for c in claims:
        lines.append(f"- **{c.get('id')}** ({c.get('label')}, self-check: {c.get('proof_status', '?')}): {c.get('statement')}")
    lines.append("")
    lines.append("## Gate evidence")
    lines.append("")

    def read_json(name):
        try:
            return json.loads((dest / "referee" / name).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    novelty = read_json("novelty.json")
    if novelty:
        statuses = ", ".join(f"{c.get('id')}: {c.get('novelty_status')}" for c in novelty.get("claims", []))
        lines.append(f"- **Novelty:** {statuses}")
    for gate in gate_specs(cfg or {}):
        g = read_json(f"{gate['name']}.json")
        if g:
            detail = g.get("notes", "")
            if g.get("scores"):
                detail = f"scores {g.get('scores')} — {detail}"
            lines.append(f"- **{gate['name'].replace('_', ' ').title()}:** {detail}")
    for i in (1, 2):
        cp = read_json(f"correctness_pass{i}.json")
        if cp:
            verdicts = ", ".join(f"{c.get('id')}: {c.get('verdict')}" for c in cp.get("claims", []))
            lines.append(f"- **Correctness pass {i}:** {verdicts}")
    lean_bits = []
    for c in claims:
        f = c.get("formalization")
        if f:
            lean_bits.append(f"{c.get('id')}: {f}" + (" ✓✓" if f == "proof-formalized" else ""))
    if lean_bits:
        lines.append(f"- **Lean track (Aristotle, non-gating):** {', '.join(lean_bits)} — Lean sources under `lean/`")
    lines.append("")
    caveat = "Verification caveat: apart from the self-check's computational counterexample search, all gates are LLM refereeing — treat as \"survived independent refereeing\", not as certainty."
    if any(c.get("formalization") == "proof-formalized" for c in claims):
        caveat += " Exception: claims marked ✓✓ carry a machine-checked Lean proof from the Aristotle track."
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
    for view in dest.glob("*_view"):  # isolated referee views (novelty_view, correctness_view, <gate>_view)
        shutil.rmtree(view, ignore_errors=True)
    if state["stage"] == "archive":
        reason = state.get("rejection_reason", "unknown")
        (dest / "rejection.md").write_text(f"# Rejected: {slug}\n\n{reason}\n")
    else:
        write_review_summary(dest, state, cfg)
        if cfg is None or cfg.get("notify", True):
            notify_user("proof-engineering", f"{slug} passed all gates — ready for review")
    shutil.rmtree(run_dir)  # runs/ holds in-flight work only
    print(f"[finalize] {slug} -> {dest_root}/ (LLM cost ${state.get('cost_usd', 0.0):.2f})")


def advance(run_dir, cfg):
    state = load_state(run_dir)
    stage = state["stage"]
    if stage in ("review", "archive"):
        return False
    # Budget parking: checked BEFORE launching the next stage, so no money is
    # wasted on a call that would be cut off mid-flight. A parked run keeps all
    # completed-stage progress; raising max_usd_per_paper (or refilling the
    # account and raising the cap) resumes it exactly where it stopped.
    # The lean stage is exempt from parking: Aristotle costs no LLM dollars,
    # and parking a run that already passed both gates on its free bonus stage
    # would only delay the review handoff.
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
    if stage == "explore":
        return do_explore(run_dir, state, cfg)
    if stage == "self_check":
        return do_self_check(run_dir, state, cfg)
    if stage == "novelty":
        return do_novelty(run_dir, state, cfg)
    if stage == "correctness":
        return do_correctness(run_dir, state, cfg)
    if stage == "lean":
        return do_lean(run_dir, state, cfg)
    for gate in gate_specs(cfg):
        if stage == gate["name"]:
            return do_gate(run_dir, state, cfg, gate)
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
    for root, header in (("runs", "IN FLIGHT"), ("review", "REVIEW (passed all gates)"), ("archive", "ARCHIVED")):
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
        print("[preflight] warning: ANTHROPIC_API_KEY is not set — claude -p will bill your "
              "Claude subscription and share its session limit with your interactive usage; "
              "a 429 mid-pipeline pauses runs until the window resets (see README.md)")
    for d in ("inbox", "runs", "review", "archive", "taste/accepted", "taste/rejected"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drain", action="store_true",
                         help="keep advancing every run until all are terminal (review/archive)")
    parser.add_argument("--only", metavar="SLUG",
                         help="advance only runs/<SLUG>, ignoring all other pending runs")
    parser.add_argument("--status", action="store_true",
                         help="print where every run stands and exit (advances nothing)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    load_dotenv()  # before preflight: its ANTHROPIC_API_KEY warning must see .env keys
    preflight()
    cfg = load_config()
    validate_gate_config(cfg)
    if cfg.get("lean_enabled", True) and not lean_enabled(cfg):
        print("[main] lean track unavailable (needs aristotle CLI on PATH + ARISTOTLE_API_KEY); runs will skip formalization")
    intake(cfg)

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
            except Exception as e:
                print(f"[main] ERROR advancing {run_dir.name}: {e}")
        if not args.drain or not progressed:
            break


if __name__ == "__main__":
    main()
