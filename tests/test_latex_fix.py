"""latex_fix.py and latexdiff.py: exact-match edits, CHECK ARGS for edit_tex, what a diff can't show, the latexdiff
fallbacks, and the whole audit -> fix -> edit -> latexdiff chain on the demo paper."""

from conftest import needs_tex, write_project

from harness.tools import latexdiff as diff_module
from harness.tools.latex_fix import EditTexTool, FixLatexTool, apply_fixes, words_pattern
from harness.tools.latexdiff import MakeLatexdiffTool, not_shown
from harness.tools.latex_project import copy_project, latex_project


def fix(id_, line, old, new, file="a.tex"):
    return {"id": id_, "file": file, "line": line, "fix": {"old": old, "new": new}}


def test_exact_match_edits_keep_line_endings(tmp_path):
    (tmp_path / "a.tex").write_bytes(b"one two\r\nthree two\r\n\r\nfour\r\nfive\r\n")
    applied, failed = apply_fixes(tmp_path, [fix("A1", 2, "three two", "three 2"),
                                             fix("A2", 1, "e", "E"),               # 3 times in its paragraph: refused
                                             fix("A3", 4, "four\nfive", "four five")])  # joins two lines
    assert [f["id"] for f, _, _ in applied] == ["A3", "A1"]  # bottom up, so earlier lines never move
    assert [f["id"] for f, _ in failed] == ["A2"]
    assert (tmp_path / "a.tex").read_bytes() == b"one two\r\nthree 2\r\n\r\nfour five\r\n"


def test_edit_tex_takes_the_users_quoted_words(ctx):
    tool = EditTexTool()
    ctx.request = 'In workspace/latex_demo, change "a fast fire" to "a fast t-squared fire".'
    args, _, note = tool.check_request({"path": "x", "old": "fast fire", "new": "fast t-squared fire"}, ctx)
    assert (args["old"], args["new"]) == ("a fast fire", "a fast t-squared fire") and note
    ctx.request = "Make the fires faster."
    args, _, note = tool.check_request({"path": "x", "old": "slow", "new": "fast"}, ctx)
    assert (args["old"], note) == ("slow", None)


def test_words_pattern_crosses_line_breaks():
    assert words_pattern("a fast fire").search("assume a fast\n  fire for")


def test_what_a_diff_cannot_show(tmp_path):
    (tmp_path / "main.tex").write_text("\\documentclass{article}\n\\usepackage{x}\n\\begin{document}\nHi\n"
                                       "\\end{document}\n", encoding="utf-8")
    ledger = {"findings": [
        {"id": "A1", "file": "refs.bib", "line": 3, "check": "DOI written as a link", "status": "verified",
         "fix": {"old": "https://doi.org/10.1/x", "new": "10.1/x"}},
        {"id": "A2", "file": "main.tex", "line": 2, "check": "x", "status": "verified",
         "fix": {"old": "{x}", "new": "{y}"}},
        {"id": "A3", "file": "main.tex", "line": 4, "check": "no space after a citation", "status": "verified",
         "fix": {"old": "\\cite{k}can", "new": "\\cite{k} can"}},
        {"id": "A4", "file": "main.tex", "line": 4, "check": "table of contents title differs", "status": "verified",
         "fix": {"old": "\\section[S]{Long}", "new": "\\section{Long}"}},
        {"id": "A5", "file": "main.tex", "line": 4, "check": "misspelling", "status": "verified",
         "fix": {"old": "teh", "new": "the"}}]}
    notes = not_shown(ledger, ["refs.bib"], "main.tex", tmp_path)
    assert [n.split()[0] for n in notes] == ["refs.bib", "A1", "A2", "A3", "A4"]  # A5 shows in the diff


@needs_tex
def test_latexdiff_falls_back_to_unmarked(ctx, monkeypatch):
    """A marked-up file that never compiles: two other markups are tried, then it goes in unmarked, and is named."""
    tree = write_project(ctx.run_dir / "paper", {
        "main.tex": "\\documentclass{article}\n\\begin{document}\n\\input{sec}\n\\end{document}\n",
        "sec.tex": "A fast fire.\n"})
    corrected = write_project(ctx.run_dir / "fixed", {
        "main.tex": "\\documentclass{article}\n\\begin{document}\n\\input{sec}\n\\end{document}\n",
        "sec.tex": "A fast t-squared fire.\n"})
    real = diff_module.latexdiff

    def broken(old, new, out, options):
        counts = real(old, new, out, options)
        if out.name == "sec.tex":
            out.write_text("\\nosuchcommand " + out.read_text(encoding="utf-8"), encoding="utf-8")
        return counts

    monkeypatch.setattr(diff_module, "latexdiff", broken)
    root, _ = latex_project(str(tree), ctx)
    ctx.facts["latex_corrected"] = {str(root): str(corrected)}
    out = MakeLatexdiffTool().run(ctx, str(tree))["observations"]
    assert out["attempts"] == ["underline", "no maths markup", "font change (CFONT)", "unmarked"]
    assert out["unmarked"][0].startswith("sec.tex")


@needs_tex
def test_a_fix_that_makes_a_new_problem_is_undone(ctx):
    from pathlib import Path
    from harness.tools.latex_audit import audit
    tree = write_project(ctx.run_dir / "paper", {
        "main.tex": "\\documentclass{article}\n\\begin{document}\nThe cat sat on the mat.\n\\end{document}\n"})
    root, main = latex_project(str(tree), ctx)
    ledger, _, _, _ = audit(root, main, ctx, use_judge=False)
    ledger["findings"].append({"id": "A099", "category": "text", "check": "a test", "file": "main.tex", "line": 3,
                               "evidence": "The cat sat", "why": "a bad fix", "confidence": "certain",
                               "fix": {"old": "cat sat", "new": "cat cat sat"}, "source": "test", "judge": None,
                               "status": "found"})
    out = FixLatexTool().run(ctx, str(tree), which="A099")["observations"]
    assert "0 verified, 1 failed and undone" in out["summary"] and "doubled word" in out["failed"][0]
    corrected = Path(ctx.facts["latex_corrected"][str(root)])
    assert (corrected / "main.tex").read_text(encoding="utf-8") == (tree / "main.tex").read_text(encoding="utf-8")
    assert not list(corrected.glob("*.aux"))  # the rebuild after the undo ran on a copy


@needs_tex
def test_audit_fix_edit_and_latexdiff_on_the_demo(ctx):
    from harness.tools.latex_audit import AuditLatexTool
    AuditLatexTool().run(ctx, "workspace/latex_demo")
    fixed = FixLatexTool().run(ctx, "workspace/latex_demo")["observations"]
    assert fixed["summary"].startswith("5 fix(es) tried: 5 verified, 0 failed")
    ctx.request = 'change "a fast fire" to "a fast t-squared fire"'
    edited = EditTexTool().run(ctx, "workspace/latex_demo", old="a fast fire", new="a fast t-squared fire")
    assert edited["observations"]["changed"] == "sections/design_fires.tex:29"
    diff = MakeLatexdiffTool().run(ctx, "workspace/latex_demo")["observations"]
    assert diff["attempts"] == ["underline"] and diff["diff_pdf"].endswith("latex_demo_latexdiff.pdf")
    assert set(diff["changed_files"]) == {"main.tex", "sections/introduction.tex", "sections/results.tex",
                                          "sections/design_fires.tex"}
    root, _ = latex_project("workspace/latex_demo", ctx)
    assert copy_project(root, ctx, "check")  # the original is untouched: it still has its planted errors
    assert "struture" in (root / "sections" / "introduction.tex").read_text(encoding="utf-8")
