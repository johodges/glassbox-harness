"""latex_audit.py: the three auditors, the exact-match fixes they propose, JUDGE, and the demo paper's answer key."""

import shutil
from types import SimpleNamespace

import pytest
from conftest import needs_tex, write_project

from harness.tools.latex_audit import (audit, audit_crossrefs, audit_references, audit_text, exact_fix,
                                       increasing_run, judge, long_form, paragraph)
from harness.tools.latex_project import latex_project, read_blg, scan

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


def test_judge_sees_both_entries_of_the_same_work_twice(ctx):
    """No fix for 'the same work twice' (merging changes every \\cite), so JUDGE gets both .bib entries as written:
    its yes confirms the finding (still likely: a person merges them), its no rejects it."""
    tree = write_project(ctx.run_dir / "paper", REFERENCES)
    asked = []

    def twin(reply):
        f = next(f for f in audit_references(scan(tree, tree / "main.tex"), tree, ctx)
                 if f["check"] == "the same work twice")
        ctx.agent = SimpleNamespace(llm=SimpleNamespace(generate=lambda messages, *a: asked.append(messages) or reply))
        judge([f], ctx)
        return f

    confirmed = twin("yes\nthe same DOI and title")
    question = asked[0][1]["content"]
    assert "@article{a," in question and "@article{c," in question and "https://doi.org/10.1000/xyz" in question
    assert (confirmed["confidence"], confirmed["status"]) == ("likely", "found")
    assert confirmed["judge"]["verdict"].startswith("confirmed")
    rejected = twin("No.\nTwo editions.")
    assert (rejected["status"], rejected["judge"]["verdict"].split(":")[0]) == ("rejected", "rejected")


ACRONYMS = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        \begin{abstract}
        We model the heat release rate (HRR) of design fires.
        \end{abstract}
        \section{Wildland-Urban Interface (WUI)}
        The WUI matters. We ran FDS on it, and \ac{BEV} fires too.
        \input{sec}
        \end{document}
    """,
    "sec.tex": r"""
        The Fire Dynamics Simulator (FDS) predicts the heat release rate (HRR).
        Battery electric vehicles (BEVs) burn. Computational fluid dynamics (CFD) helps.
        At the peak of the fire (FP) it is hot. Later, computer fluid dynamics (CFD) again.
    """,
}


def test_long_form():
    """Schwartz and Hearst's rule: the short form's letters, last to first, in the words before it."""
    assert long_form("FDS", "We use the Fire Dynamics Simulator") == "Fire Dynamics Simulator"
    assert long_form("WFDS", "the Wildland-urban interface Fire Dynamics Simulator") == \
        "Wildland-urban interface Fire Dynamics Simulator"
    assert long_form("LiDAR", "scanned with Light Detection and Ranging") == "Light Detection and Ranging"
    assert long_form("FP", "At the peak of the fire") is None


def test_acronyms_used_before_their_definition_or_defined_twice(ctx):
    """FDS is used in main.tex before the \\input file that defines it. Not findings: HRR defined in the abstract and
    again in the body; WUI, defined only in a heading; \\ac{BEV}, which the acronym package defines; (FP), which no
    words before it spell."""
    tree = write_project(ctx.run_dir / "paper", ACRONYMS)
    found = [f for f in audit_text(scan(tree, tree / "main.tex"), tree) if f["check"].startswith("acronym")]
    assert [(f["check"], f["file"], f["line"], f["confidence"]) for f in found] == [
        ("acronym used before its definition", "main.tex", 7, "likely"),
        ("acronym defined twice", "sec.tex", 3, "likely")]
    used, twice = found
    assert "FDS" in used["evidence"] and "\"Fire Dynamics Simulator (FDS)\" at sec.tex:1" in used["why"]
    assert twice["why"].endswith("as \"Computational fluid dynamics\", and here as \"computer fluid dynamics\"")
    assert used["fix"] is None and twice["fix"] is None


HAND_NUMBERED = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        First \cite{a}, then \cite{b}, then \cite{c, d}. \nocite{e}
        \bibliographystyle{unsrt}
        \bibliography{refs}
        \end{document}
    """,
    "refs.bib": r"""
        @misc{a, title = {Alpha}, refnum = {1}}
        @misc{b, title = {Beta}, refnum = {3}}
        @misc{c, title = {Gamma}, refnum = {2}}
        @misc{d, title = {Delta}, refnum = {4}}
        @misc{e, title = {Epsilon}, refnum = {4}}
    """,
}


def bibitems(*numbers):
    """A thebibliography list numbered by hand, cited x, y, z in that order."""
    items = "\n".join(f"\\bibitem[{n}]{{{k}}} {k.upper()}." for n, k in zip(numbers, "xyz"))
    return {"main.tex": "\\documentclass{article}\n\\begin{document}\nSee \\cite{x} and \\cite{y} and \\cite{z}.\n"
                        f"\\begin{{thebibliography}}{{9}}\n{items}\n\\end{{thebibliography}}\n\\end{{document}}\n"}


def test_a_hand_numbered_list_against_the_order_of_citation(ctx):
    """refnum numbers in citation order run 1, 3, 2, 4: 'c' (2) is the one out of order; 'e' repeats 4."""
    assert increasing_run([1, 10, 2, 3, 4]) == {0, 2, 3, 4}  # 10 is out of order
    tree = write_project(ctx.run_dir / "paper", HAND_NUMBERED)
    found = {f["check"]: f for f in audit_references(scan(tree, tree / "main.tex"), tree, ctx)
             if "reference number" in f["check"]}
    order, twice = found["reference number out of citation order"], found["reference number given twice"]
    assert (order["file"], order["line"], order["confidence"]) == ("refs.bib", 3, "needs a person")
    assert "'c' is numbered 2, but it is the 3rd source the text cites (first at main.tex:3)" in order["why"]
    assert (twice["line"], twice["why"]) == (5, "'d', 'e' are all numbered 4")
    # \bibitem[n]{key}: the same check, on the .tex
    tree = write_project(ctx.run_dir / "items", bibitems(1, 3, 2))
    found = [f for f in audit_references(scan(tree, tree / "main.tex"), tree, ctx) if "reference number" in f["check"]]
    assert [(f["file"], f["line"], "'z' is numbered 2" in f["why"]) for f in found] == [("main.tex", 7, True)]
    # numbered in another order (3, 2, 1: by author, say): not a citation-order list, so nothing to say
    tree = write_project(ctx.run_dir / "reversed", bibitems(3, 2, 1))
    assert not [f for f in audit_references(scan(tree, tree / "main.tex"), tree, ctx) if "number" in f["check"]]


BROKEN_BIB = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        See \cite{a}, \cite{b}, \cite{c}, \cite{nope}.
        \bibliography{refs}
        \end{document}
    """,
    "refs.bib": r"""
        @article{a,
          author = {Doe, J.}, title = {A Study}, journal = {Fire J.}, year = {2020}
        }

        @article{a,
          author = {Roe, R.}, title = {Another}, journal = {Fire J.}, year = {2021}
        }

        @article{b,
          title = {No Author}, journaltitle = {Fire J.}, date = {2020-13-45}
        }

        @book{c,
          author = {Smith, A. and and Jones, B.}, title = {Fires}, year = {2001}, publisher = {P}, month = foo
        }

        @misc{d,
          title = {Broken} note = {x}
        }
    """,
}

# What BibTeX and biber wrote for BROKEN_BIB (TeX Live 2025)
BIBTEX_BLG = """This is BibTeX, Version 0.99d (TeX Live 2025)
Database file #1: refs.bib
Repeated entry---line 5 of file refs.bib
 : @article{a
 :           ,
I'm skipping whatever remains of this entry
Warning--string name "foo" is undefined
--line 14 of file refs.bib
I was expecting a `,' or a `}'---line 18 of file refs.bib
 :   title = {Broken}
 :                    note = {x}
I'm skipping whatever remains of this entry
Warning--I didn't find a database entry for "nope"
Warning--to sort, need author or key in b
Warning--empty author in b
Warning--empty journal in b
(There were 2 error messages)
"""
BIBER_BLG = r"""[0] Config.pm:307> INFO - This is Biber 2.19
[535] Utils.pm:479> ERROR - BibTeX subsystem: C:\Users\x\AppData\Local\Temp\biber_tmp_bVjT\8fe4c9_35940.utf8, line 18, syntax error: found "note", expected end of entry ("}" or ")") (skipping to next "@")
[535] Biber.pm:132> WARN - Duplicate entry key: 'a' in file 'refs.bib', skipping ...
[535] Biber.pm:132> WARN - article entry 'b' (refs.bib): Invalid format '2020-13-45' of date field 'date' - ignoring
[535] Biber.pm:132> WARN - Name in key 'c' is empty (probably consecutive 'and'): skipping entry 'c'
[536] Biber.pm:132> WARN - Entry with key 'c' in section '0' is cited and found but not created (likely due to sourcemap)
[536] Biber.pm:132> WARN - BibTeX subsystem: C:\Users\x\AppData\Local\Temp\biber_tmp_bVjT\8fe4c9_35940.utf8, line 14, warning: undefined macro "foo"
[536] Biber.pm:132> WARN - I didn't find a database entry for 'nope' (section 0)
[536] Biber.pm:134> INFO - WARNINGS: 6
"""


def test_what_bibtex_and_biber_said(ctx):
    """The .blg's warnings and errors at the entry or line they name, one finding per place; what the references
    auditor already says (no entry for 'nope', the key 'a' twice, b's missing author and journal) is left out."""
    tree = write_project(ctx.run_dir / "paper", BROKEN_BIB)
    found = scan(tree, tree / "main.tex")

    def said(blg):
        return [(f["check"], f["file"], f["line"], f["why"])
                for f in audit_references(found, tree, ctx, {"bibtex": read_blg(blg)})
                if f["check"].startswith(("BibTeX", "biber"))]

    assert said(BIBTEX_BLG) == [
        ("BibTeX warning", "refs.bib", 14, "BibTeX: string name \"foo\" is undefined --line 14 of file refs.bib"),
        ("BibTeX error", "refs.bib", 18, "BibTeX: I was expecting a `,' or a `}'---line 18 of file refs.bib")]
    biber = said(BIBER_BLG)
    assert [(check, file, line) for check, file, line, _ in biber] == [
        ("biber error", "refs.bib", 18), ("biber warning", "refs.bib", 9), ("biber warning", "refs.bib", 13),
        ("biber warning", "refs.bib", 14)]
    assert biber[0][3].startswith("biber: BibTeX subsystem: line 18, syntax error: found \"note\"")
    assert "Name in key 'c' is empty" in biber[2][3] and "cited and found but not created" in biber[2][3]
    assert not said("")  # no .blg (no bibliography), nothing to say


@needs_tex
@pytest.mark.skipif(shutil.which("biber") is None, reason="needs biber (TeX Live)")
def test_biber_warnings_reach_the_ledger(ctx):
    """A biblatex paper: latexmk runs biber, and the date biber can't read becomes a finding on its entry. Its
    journaltitle and date are biblatex's journal and year: no 'missing fields'."""
    tree = write_project(ctx.run_dir / "paper", {
        "main.tex": r"""
            \documentclass{article}
            \usepackage[backend=biber]{biblatex}
            \addbibresource{refs.bib}
            \begin{document}
            See \cite{b}.
            \printbibliography
            \end{document}
        """,
        "refs.bib": r"""
            @article{b,
              author = {Doe, John}, title = {A Study of Plumes}, journaltitle = {Fire J.}, date = {2020-13-45}
            }
        """})
    root, main = latex_project(str(tree), ctx)
    ledger, _, facts, _ = audit(root, main, ctx, checks=["references"], use_judge=False)
    assert facts["bibtex"]["program"] == "biber"
    assert [(f["check"], f["file"], f["line"]) for f in ledger["findings"] if f["category"] == "references"] == [
        ("biber warning", "refs.bib", 1)]
    assert "Invalid format '2020-13-45' of date field 'date'" in ledger["findings"][-1]["why"]


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
