#!/usr/bin/env python3
"""Orchestrator: intake -> explore -> self_check -> novelty -> taste -> correctness -> review/archive.

Advances every non-terminal run by one stage per invocation (or drains all
runs to a terminal state with --drain). Safe to re-run: state lives in
runs/<slug>/state.json, and terminal runs (review/archive) are left alone.
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
REFEREE_VIEWS = ("novelty_view", "taste_view", "correctness_view")


def load_config():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


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

def run_claude(run_dir, prompt, model, allowed_tools, cfg):
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
    timeout = cfg.get("stage_timeout_seconds", 1800)
    try:
        proc = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True, timeout=timeout)
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
    if any(marker in lowered for marker in ("session limit", "rate_limit", "overloaded", "credit balance", "insufficient credit")):
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


def fail_to_explore_or_archive(run_dir, state, reason, cfg):
    """A real finding (gap, counterexample) sends the run back to explore, or archives if explore is out of attempts."""
    if state["attempts"]["explore"] < cfg["max_explore_attempts"]:
        state["stage"] = "explore"
        state["feedback"] = reason
        append_history(state, "self_check", f"failed, sending back to explore: {reason}")
        save_state(run_dir, state)
        return True
    state["stage"] = "archive"
    state["rejection_reason"] = reason
    append_history(state, "self_check", f"failed and explore retries exhausted: {reason}")
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


def do_explore(run_dir, state, cfg):
    attempts = state["attempts"]["explore"]
    prompt_md = (ROOT / "PROMPT.md").read_text()
    explore_md = (ROOT / "prompts" / "explore.md").read_text()
    explore_md = explore_md.replace("{{VENV_PYTHON}}", str(ROOT / cfg["venv_python"]))
    prompt = prompt_md + "\n\n" + explore_md
    if state.get("feedback"):
        prompt += f"\n\n---\n\nA previous attempt failed. Fix this and try again:\n\n{state['feedback']}\n"

    ok, log = run_claude(run_dir, prompt, cfg["generator_model"], GENERATOR_TOOLS, cfg)
    (run_dir / f"explore_attempt_{attempts + 1}.log").write_text(log)
    track_cost(state, log)
    if not ok and is_transient_failure(log):
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
        return fail_to_explore_or_archive(run_dir, state, f"self-check left result.json invalid: {err}", cfg)

    claims = json.loads((run_dir / "result.json").read_text())["claims"]
    bad = [
        c for c in claims
        if c["label"] == "theorem-complete-proof" and c["proof_status"] in ("gap-found", "counterexample-found")
    ]
    if bad:
        details = "\n".join(f"- {c['id']}: {c['proof_status']} — {c.get('self_check_notes', '')}" for c in bad)
        return fail_to_explore_or_archive(run_dir, state, f"Self-check found problems:\n{details}", cfg)

    state["stage"] = "novelty"
    append_history(state, "self_check", "passed: no theorem-complete-proof claim has a gap or counterexample")
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
        return reject(run_dir, state, "novelty", f"Novelty check found prior work:\n{details}",
                      "archived: known result(s) found", cfg)

    state["stage"] = "taste"
    append_history(state, "novelty", "passed: no claim found in prior literature")
    save_state(run_dir, state)
    return True


def do_taste(run_dir, state, cfg):
    (run_dir / "referee").mkdir(exist_ok=True)
    view_dir = make_referee_dir(run_dir, "taste_view", include_paper=True)
    prompt = (ROOT / "prompts" / "taste.md").read_text() + build_taste_calibration()
    attempt = state["attempts"].get("taste", 0) + 1

    ok, log = run_claude(view_dir, prompt, cfg["taste_model"], "Read Write", cfg)
    (run_dir / f"taste_attempt_{attempt}.log").write_text(log)
    track_cost(state, log)
    if not ok and is_transient_failure(log):
        return defer_stage(run_dir, state, "taste", log)

    if not ok:
        return referee_stage_error(run_dir, state, "taste", f"invocation failed:\n{log}", cfg)
    verdict, err = load_verdict(view_dir / "verdict.json")
    if verdict is None:
        return referee_stage_error(run_dir, state, "taste", err, cfg)

    shutil.copy(view_dir / "verdict.json", run_dir / "referee" / "taste.json")
    if (view_dir / "report.md").exists():
        shutil.copy(view_dir / "report.md", run_dir / "referee" / "taste_report.md")

    if verdict.get("verdict") not in ("pass", "fail"):
        return referee_stage_error(run_dir, state, "taste", f"verdict.json has no valid 'verdict' field: {verdict.get('verdict')!r}", cfg)
    if verdict["verdict"] != "pass":
        return reject(run_dir, state, "taste",
                      f"Taste referee rejected: {verdict.get('notes', '')}\nScores: {verdict.get('scores')}",
                      "archived: failed taste gate", cfg)

    state["stage"] = "correctness"
    append_history(state, "taste", f"passed: scores {verdict.get('scores')}")
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
            return fail_to_explore_or_archive(run_dir, state, f"Correctness referee found fixable gaps:\n{reason}", cfg)
        return reject(run_dir, state, "correctness",
                      f"Correctness referee found unrecoverable issues:\n{reason}",
                      "archived: wrong/cannot-verify verdict", cfg)

    state["stage"] = "review"
    append_history(state, "correctness", "passed: both independent passes verified all load-bearing claims")
    save_state(run_dir, state)
    finalize(run_dir, state, cfg)
    return True


# --- finalize ----------------------------------------------------------------

def write_review_summary(dest, state):
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
    taste = read_json("taste.json")
    if taste:
        lines.append(f"- **Taste:** scores {taste.get('scores')} — {taste.get('notes', '')}")
    for i in (1, 2):
        cp = read_json(f"correctness_pass{i}.json")
        if cp:
            verdicts = ", ".join(f"{c.get('id')}: {c.get('verdict')}" for c in cp.get("claims", []))
            lines.append(f"- **Correctness pass {i}:** {verdicts}")
    lines.append("")
    lines.append("Verification caveat: apart from the self-check's computational counterexample search, all gates are LLM refereeing — treat as \"survived independent refereeing\", not as certainty.")
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
    for view in REFEREE_VIEWS:
        shutil.rmtree(dest / view, ignore_errors=True)
    if state["stage"] == "archive":
        reason = state.get("rejection_reason", "unknown")
        (dest / "rejection.md").write_text(f"# Rejected: {slug}\n\n{reason}\n")
    else:
        write_review_summary(dest, state)
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
    cap = cfg.get("max_usd_per_paper", 0)
    spent = state.get("cost_usd", 0.0)
    if cap and spent >= cap:
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
    if stage == "taste":
        return do_taste(run_dir, state, cfg)
    if stage == "correctness":
        return do_correctness(run_dir, state, cfg)
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
    args = parser.parse_args()

    preflight()
    cfg = load_config()
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
