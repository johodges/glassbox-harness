"""latex_audit.py: the three auditors, the exact-match fixes they propose, JUDGE, and the demo paper's answer key."""

from types import SimpleNamespace

from conftest import needs_tex, write_project

from harness.tools.latex_audit import (audit, audit_crossrefs, audit_references, audit_text, exact_fix, judge,
                                       paragraph)
from harness.tools.latex_project import latex_project, scan

TEXT = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        The fire grew in
        in a room. It was \emph{very} very hot, and the $x$ the value.
        A structure-to-struture spread. On January 8. 2025 we said "hello".
        The wall .. was hot, as~\cite{k1}shows.

        The Castellammare station and the Castellammare district; the Castellamare fire.
        \end{document}
    """,
}


def checks_of(findings):
    return {(f["check"], f["evidence"]) for f in findings}


def test_paragraph_and_exact_fix():
    text = "first paragraph\n\nthe cat and the cat sat\nnext line\n\nlast"
    start, end = paragraph(text, 3)
    assert text[start:end] == "the cat and the cat sat\nnext line\n"
    at = text.index("cat")
    # "cat" occurs twice in its paragraph: the old text grows a word each side until it is unique
    assert exact_fix(text, at, at + 3, "dog") == {"old": "the cat and", "new": "the dog and"}


def test_text_checks(ctx):
    tree = write_project(ctx.run_dir / "paper", TEXT)
    findings = audit_text(scan(tree, tree / "main.tex"), tree)
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    doubled = {f["fix"]["old"]: f for f in by_check["doubled word"]}
    # across a line break and a macro (its closing brace stays), but not "the $x$ the"
    assert {old: f["fix"]["new"] for old, f in doubled.items()} == {"in\nin": "in", "very} very": "very}"}
    assert doubled["in\nin"]["line"] == 3 and doubled["in\nin"]["confidence"] == "certain"
    spelling = by_check["misspelling"][0]
    assert (spelling["fix"], spelling["confidence"]) == ({"old": "struture", "new": "structure"}, "certain")
    assert "January 8, 2025" in by_check["date"][0]["fix"]["new"]
    assert "``hello''" in by_check["straight quotes"][0]["fix"]["new"]
    assert by_check["double full stop"][0]["confidence"] == "certain"
    assert "\\cite{k1} shows" in by_check["no space after a citation"][0]["fix"]["new"]
    variant = by_check["name spelled two ways"][0]
    assert ("Castellammare" in variant["fix"]["new"], variant["confidence"]) == (True, "likely")


REFERENCES = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        As \cite{a} and \cite{zz} show.
        \bibliography{refs}
        \end{document}
    """,
    "refs.bib": r"""
        @article{a,
          author = {Doe, J.}, title = {A Study of Plumes}, journal = {Fire J.}, year = {2020},
          doi = {10.1000/xyz}
        }

        @book{b,
          author = {Roe, R.}, title = {Fires}, year = {2001}
        }

        @article{c,
          author = {Doe, J.}, title = {A study of plumes}, journal = {Fire J.}, year = {2020},
          doi = {https://doi.org/10.1000/xyz}
        }
    """,
}


def test_reference_checks(ctx):
    tree = write_project(ctx.run_dir / "paper", REFERENCES)
    findings = audit_references(scan(tree, tree / "main.tex"), tree, ctx)
    got = {(f["check"], f["file"], f["confidence"]) for f in findings}
    assert ("citation with no entry", "main.tex", "needs a person") in got
    assert ("entry never cited", "refs.bib", "likely") in got           # b and c
    assert ("missing fields", "refs.bib", "likely") in got              # the book has no publisher
    assert ("the same work twice", "refs.bib", "likely") in got         # c: the same DOI as a
    doi = next(f for f in findings if f["check"] == "DOI written as a link")
    assert doi["fix"] == {"old": "https://doi.org/10.1000/xyz", "new": "10.1000/xyz"}


CROSSREFS = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        \section[Results]{Measured Peak Rates}
        \label{sec:a}
        The sofa fire peaked early: as Figure 3 shows, the heat release rate of the sofa was high.
        Limits are in Section 4.0, and \ref{fig:none} is missing.
        \begin{figure}
          \caption{Heat release rate of a sofa fire}
          \label{fig:h}
        \end{figure}
        \end{document}
    """,
}


def test_crossref_checks(ctx):
    tree = write_project(ctx.run_dir / "paper", CROSSREFS)
    found = scan(tree, tree / "main.tex")
    aux = {"labels": {"sec:a": {"number": "1", "page": "1", "kind": ""}, "fig:h": {"number": "1", "page": "1",
                                                                                     "kind": ""}},
           "toc": [{"number": "1"}], "lof": [{"number": "1"}], "lot": [], "bibcite": {}, "citations": []}
    findings = {f["check"]: f for f in audit_crossrefs(found, aux, {})}
    wrong = findings["wrong figure number"]
    assert (wrong["fix"]["new"], wrong["confidence"]) == ("Figure~\\ref{fig:h}", "certain")
    assert findings["section that does not exist"]["confidence"] == "needs a person"
    assert findings["reference to an undefined label"]["confidence"] == "needs a person"
    short = findings["table of contents title differs"]
    assert short["fix"] == {"old": "\\section[Results]{Measured Peak Rates}", "new": "\\section{Measured Peak Rates}"}


def judged(reply, ctx):
    finding = {"id": "A001", "category": "text", "check": "misspelling", "file": "a.tex", "line": 1,
               "evidence": "the colr of smoke", "why": "not in the dictionary", "confidence": "likely",
               "fix": {"old": "colr", "new": "color"}, "judge": None, "status": "found"}
    ctx.agent = SimpleNamespace(llm=SimpleNamespace(generate=lambda *args, **kwargs: reply))
    judge([finding], ctx)
    return finding


def test_judge_can_confirm_or_reject_never_invent(ctx):
    assert judged("yes\nthe color of smoke", ctx)["confidence"] == "certain"
    assert judged("Yes.\nThe colour of the smoke", ctx)["confidence"] == "likely"  # another correction: no change
    rejected = judged("no\nthe colr of smoke", ctx)
    assert (rejected["status"], rejected["confidence"]) == ("rejected", "likely")
    assert judged("maybe", ctx)["judge"]["verdict"].startswith("still likely")


@needs_tex
def test_the_demo_paper_against_its_answer_key(ctx):
    """All 12 planted errors, each at the key's confidence, and no other finding (JUDGE off)."""
    from compare_audit import ANSWERS, score
    root, main = latex_project("workspace/latex_demo", ctx)
    ledger, paths, _, _ = audit(root, main, ctx, use_judge=False)
    caught, extra = score(ledger)
    assert sorted(caught) == [p["id"] for p in ANSWERS["planted"]] and not extra
    assert all(caught[p["id"]]["confidence"] == p["confidence"] for p in ANSWERS["planted"])
    assert paths["report"].is_file() and paths["csv"].is_file()
