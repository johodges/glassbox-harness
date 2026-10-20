"""report.py for a LaTeX audit: audit_report.md as HTML, and links to the original, corrected and latexdiff PDFs."""

import json
from types import SimpleNamespace

from harness.report import markdown_html, write_report
from harness.tools.latex_audit import write_ledger

FINDING = {"id": "A001", "category": "text", "check": "straight quotes", "file": "main.tex", "line": 3,
           "evidence": "we said \"hello\" there", "why": "LaTeX prints \" as a closing quote: write ``...''",
           "fix": {"old": "\"hello\"", "new": "``hello''"}, "confidence": "certain", "source": "audit_text",
           "judge": None, "status": "found"}


def run_result(prompt="Audit workspace/paper"):
    return SimpleNamespace(prompt=prompt, answer="Done.", events=[], files=[], log=[], messages=[])


def test_markdown_html():
    html = markdown_html("# Audit of x\n\n**2 finding(s)**.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n## text\n\n"
                         "- **A001** `main.tex:3` one\n  more\n- ``` ``hello'' ``` <b>\n")
    assert "<h3>Audit of x</h3>" in html and "<h4>text</h4>" in html  # two levels below the page's own headings
    assert "<p><strong>2 finding(s)</strong>.</p>" in html
    assert "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>" in html
    assert "<li><strong>A001</strong> <code>main.tex:3</code> one more</li>" in html  # an indented line continues it
    assert "<li><code>``hello&#x27;&#x27;</code> &lt;b&gt;</li>" in html  # backticks inside code, and HTML escaped


def test_report_page_shows_the_audit_and_links_the_pdfs(tmp_path):
    """The run audited and diffed; the corrected PDF came from an earlier run, which the ledger names."""
    run, earlier = tmp_path / "run_2", tmp_path / "run_1"
    run.mkdir(), earlier.mkdir()
    ledger = {"project": "workspace/paper", "root": str(tmp_path / "workspace" / "paper"),
              "audited": "2026-10-20T14:02", "build": "main.pdf: 1 page(s)", "run_dir": str(run), "findings": [FINDING],
              "corrected": {"run_dir": str(earlier)}}
    write_ledger(ledger, run)
    for pdf in (run / "paper_original.pdf", run / "paper_latexdiff.pdf", earlier / "paper_corrected.pdf"):
        pdf.write_bytes(b"%PDF-1.5\n")
    page = write_report(run_result(), run / "report.html").read_text(encoding="utf-8")
    assert "The audit: audit_report.md and the PDFs" in page
    for href in ("paper_original.pdf", "../run_1/paper_corrected.pdf", "paper_latexdiff.pdf"):
        assert f"<a href='{href}'>" in page
    assert "(from run_1)" in page
    assert "<h3>Audit of workspace/paper</h3>" in page and "<table>" in page
    # the fix's own backticks, and the ones in the prose before it (escaped in the .md, so they open no code span)
    assert ("write ``...&#x27;&#x27;. Fix: <code>&quot;hello&quot;</code> -&gt; <code>``hello&#x27;&#x27;</code>."
            in page)
    assert json.loads((run / "ledger.json").read_text(encoding="utf-8"))["project"] == "workspace/paper"


def test_report_page_without_an_audit(tmp_path):
    page = write_report(run_result("Draw a robot"), tmp_path / "report.html").read_text(encoding="utf-8")
    assert "audit_report.md" not in page
