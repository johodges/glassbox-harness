"""The ROUTER's LaTeX rules: the paper requests get exactly the LaTeX tools, and the other evaluate.py requests none."""

import pytest

from harness.classifiers import latex_rules
from harness.config import HarnessConfig

TOOLS = HarnessConfig().enabled_tools


@pytest.mark.parametrize("prompt, tools", [
    ("Audit workspace/latex_demo.", {"audit_latex"}),
    ("Fix the errors in the last audit of workspace/latex_demo.", {"fix_latex"}),  # no new audit
    ("Make a latexdiff of workspace/latex_demo against the fixed copy.", {"make_latexdiff"}),
    ('In workspace/latex_demo, change "a fast fire" to "a fast t-squared fire" and give me a latexdiff.',
     {"edit_tex", "make_latexdiff"}),
    ("Audit workspace/latex_demo, fix what you can and make a latexdiff for review",
     {"audit_latex", "fix_latex", "make_latexdiff"}),
    ("Does workspace/latex_demo build?", {"build_latex"}),
    ("Which sources does workspace/latex_demo cite?", {"scan_latex"}),
    ("Proofread the paper in ../paper.zip and check the references", {"audit_latex"}),
])
def test_paper_requests(prompt, tools):
    assert set(latex_rules(prompt, TOOLS)) == tools


def test_the_other_requests_are_untouched():
    from evaluate import TESTS
    others = [prompt for prompt, *_ in TESTS if "latex_demo" not in prompt]
    assert len(others) == 30 and {prompt: latex_rules(prompt, TOOLS) for prompt in others
                                  if latex_rules(prompt, TOOLS)} == {}


def test_the_last_audit_of_a_project_outside_workspace(tmp_path, monkeypatch):
    """'Fix the errors in the last audit of ../papers/note' from a folder beside it: fix_latex, and no new audit."""
    from conftest import write_project
    write_project(tmp_path / "papers" / "note", {"main.tex": "\\documentclass{article}\n\\begin{document}\nHi\n"
                                                             "\\end{document}\n"})
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    assert set(latex_rules("Fix the errors in the last audit of ../papers/note.", TOOLS)) == {"fix_latex"}
