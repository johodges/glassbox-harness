"""
What the Word and PowerPoint review tools share (word_*.py, deck_*.py): who the edits are by, where results may be
written, whether a person approves, and whether Office has the file open. Plain Python, no ML.

Three rules from the jobs these tools copy (OFFICE_COLLAB_DESIGN.md, F2):
  - the review author names the model and the person, never the person alone: "Llama 3.2 3B Instruct, via.
    Glass-box harness (for Jonathan Hodges)". With the user's own name, Word can't tell their revisions from ours;
  - every result is a new file in the run folder (output_path); no tool ever writes the file it read;
  - a file Word or PowerPoint has open (a ~$ lock file next to it) is read from a copy and never written.
"""

import getpass
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from ..config import MODELS, REVIEW_AUTHOR, REVIEW_INITIALS


def model_name(model_id):
    """'unsloth/Llama-3.2-3B-Instruct' -> 'Llama 3.2 3B Instruct'; 'claude-opus-5-5' -> 'Claude Opus 5.5'."""
    name = str(model_id).rstrip("/").split("/")[-1]
    if name.lower().startswith("claude-"):
        words = name.split("-")
        version = ".".join(w for w in words if w.isdigit())
        return " ".join(w.capitalize() for w in words if not w.isdigit()) + (f" {version}" if version else "")
    return name.replace("-", " ").replace("_", " ")


@lru_cache(maxsize=1)
def _git_user():
    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def person(ctx):
    """Whom the harness works for: HarnessConfig.review_for, else GLASSBOX_USER, else git's user.name, else the
    login name."""
    config = getattr(ctx, "config", None)
    return ((config and getattr(config, "review_for", None)) or os.environ.get("GLASSBOX_USER") or _git_user()
            or getpass.getuser())


def planner_model(ctx):
    """The model that planned this request (the PLANNER stage may have swapped in the code model)."""
    agent = getattr(ctx, "agent", None)
    llm = getattr(agent, "llm", None)
    if llm is not None and getattr(llm, "model_id", None):
        return llm.model_id
    config = getattr(ctx, "config", None)
    return MODELS.get(getattr(config, "llm", "llm") or "llm", MODELS["llm"])


def review_author(ctx):
    """The author written on every tracked change and comment: config.REVIEW_AUTHOR filled in."""
    return REVIEW_AUTHOR.format(model=model_name(planner_model(ctx)), user=person(ctx))


def review_initials(ctx):
    return REVIEW_INITIALS


def output_path(ctx, source, tag, suffix=None):
    """
    Where a result goes: outputs/run_.../<name>_<tag>.<ext>, numbered if taken. Never the source, never outside the
    run folder: whatever a tool does, the user's file stays as it was, so no request can lose their work.
    """
    run_dir = Path(ctx.run_dir).resolve()
    source = Path(source)
    stem = re.sub(r"_(redline|commented|accepted|rejected|compared|edited)(_\d+)?$", "", source.stem)
    dest = run_dir / f"{stem}_{tag}{suffix or source.suffix}"
    n = 2
    while dest.exists():
        dest = run_dir / f"{stem}_{tag}_{n}{suffix or source.suffix}"
        n += 1
    if not dest.resolve().is_relative_to(run_dir) or dest.resolve() == source.resolve():
        raise PermissionError(f"refusing to write {dest}: results go only to new files in {run_dir}")
    return dest


def lock_file(path):
    """The '~$' owner file Word or PowerPoint writes next to a document it has open, or None. Word drops the first
    two characters of a long name ('~$st_plan.docx'), PowerPoint keeps it whole ('~$briefing.pptx')."""
    path = Path(path)
    for name in (f"~${path.name}", f"~${path.name[2:]}" if len(path.stem) > 6 else None):
        if name and (path.parent / name).exists():
            return path.parent / name
    return None


def ask_person(ctx, question, what="a change"):
    """APPROVAL inside a tool, as in agent.py: a person answers y/n. HarnessConfig.ask_before_acting (on unless
    run_agent.py --yes) decides whether anyone is asked; nothing approved here can touch the original, since every
    tool writes a new file."""
    from .base import approve_in_tool
    return approve_in_tool(ctx, question, what)
