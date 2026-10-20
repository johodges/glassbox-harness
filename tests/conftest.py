"""Shared pieces for the unit tests: a ToolContext with its own run folder, and small LaTeX projects in it.
No GPU and no LLM; tests marked `tex` build with TeX Live and are skipped without it."""

import shutil
import textwrap
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.tools import latex_audit
from harness.tools.base import ToolContext
from harness.trace import Tracer

needs_tex = pytest.mark.skipif(shutil.which("latexmk") is None or shutil.which("latexdiff") is None,
                               reason="needs TeX Live (latexmk, latexdiff)")


def new_ctx(run_dir):
    """A ToolContext for one request, with its own run folder (a later request gets another)."""
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    return ToolContext(Tracer(verbosity=0, color=False), Path(run_dir), "cpu", 1, {},
                       HarnessConfig(ask_before_acting=False))


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """A ToolContext whose run folder is a temporary one; ledgers are saved there too, not in outputs/ledgers/."""
    monkeypatch.setattr(latex_audit, "LEDGERS_DIR", tmp_path / "ledgers")
    return new_ctx(tmp_path / "run")


def write_project(folder, files):
    """{relative path: text} -> files in folder (the text dedented), -> the folder."""
    folder = Path(folder)
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return folder
