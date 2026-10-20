"""latex_project.py: the .log and .aux readers, the source scanner, the engine, and finding a project's main file."""

import textwrap

from conftest import needs_tex, write_project

from harness.tools.latex_project import (build, copy_project, engine_of, latex_project, latex_project_named,
                                         read_aux, read_log, scan)

LOG = textwrap.dedent(r"""
    This is pdfTeX, Version 3.141592653-2.6-1.40.27 (TeX Live 2025) (preloaded format=pdflatex)
    (./main.tex
    (./sections/a.tex
    ./sections/a.tex:12: Undefined control sequence.
    l.12 \foo

    LaTeX Warning: Reference `fig:x' on page 1 undefined on input line 7.

    ) (./sections/b.tex
    Overfull \hbox (3.5pt too wide) in paragraph at lines 14--1
    []\OT1/cmr/m/n/10 a paragraph that ran to the end of a.tex
     []

    [1]
    LaTeX Warning: Citation `nist2026' on page 2 undefined on input line 5.

    Overfull \hbox (0.2pt too wide) in paragraph at lines 3--4
    )
    LaTeX Warning: Label `sec:a' multiply defined.

    Output written on main.pdf (2 pages, 12345 bytes).
""")

AUX = textwrap.dedent(r"""
    \relax
    \citation{b}
    \bibcite{b}{1}
    \newlabel{sec:intro}{{1}{1}{Introduction}{section.1}{}}
    \newlabel{fig:x}{{2}{3}{Caption}{figure.2}{}}
    \newlabel{eq:a}{{1}{1}}
    \@writefile{toc}{\contentsline {section}{\numberline {1}Introduction}{1}{section.1}\protected@file@percent }
    \@writefile{lof}{\contentsline {figure}{\numberline {2}{\ignorespaces Caption}}{3}{figure.2}\protected@file@percent }
""")

PROJECT = {
    "main.tex": r"""
        \documentclass{article}
        \begin{document}
        \input{sections/sec}
        \bibliography{refs}
        \end{document}
    """,
    "sections/sec.tex": r"""
        \section[Short]{A Much Longer Title}
        \label{sec:a}
        See Figure 3 and Section~\ref{sec:a}, as in~\cite{k1, k2}.
        % Figure 9 in a comment is not text
        \begin{figure}
          \caption[LoF]{Heat release rate}
          \label{fig:h}
        \end{figure}
    """,
}


def test_log_errors_warnings_and_pages():
    facts = read_log(LOG)
    assert facts["errors"][0] == {"file": "sections/a.tex", "line": 12, "message": "Undefined control sequence."}
    assert facts["undefined_refs"] == [{"label": "fig:x", "page": 1, "line": 7}]
    assert [c["key"] for c in facts["undefined_cites"]] == ["nist2026"]
    assert facts["multiply_defined"] == [{"label": "sec:a"}]
    assert facts["pages"] == 2


def test_log_overfull_boxes():
    """Boxes under the threshold are dropped. 'lines 14--1': the paragraph began in the file that just closed."""
    boxes = read_log(LOG)["overfull"]
    assert boxes == [{"file": "sections/a.tex", "line": 14, "pt": 3.5, "page": 1}]


def test_aux_numbers_kinds_and_lists(tmp_path):
    (tmp_path / "main.aux").write_text(AUX, encoding="utf-8")
    aux = read_aux(tmp_path / "main.tex")
    assert aux["labels"]["fig:x"] == {"number": "2", "page": "3", "kind": "figure"}
    assert aux["labels"]["eq:a"] == {"number": "1", "page": "1", "kind": ""}  # no hyperref: no anchor
    assert aux["bibcite"] == {"b": "1"} and aux["citations"] == ["b"]
    assert aux["toc"][0]["number"] == "1" and aux["toc"][0]["title"] == "Introduction"
    assert aux["lof"][0]["number"] == "2"


def test_scan_finds_commands_and_typed_numbers(ctx):
    tree = write_project(ctx.run_dir / "paper", PROJECT)
    found = scan(tree, tree / "main.tex")
    assert found["files"] == ["main.tex", "sections/sec.tex"]
    heading = found["headings"][0]
    assert (heading["short"], heading["title"], heading["label"]) == ("Short", "A Much Longer Title", "sec:a")
    caption = found["captions"][0]
    assert (caption["kind"], caption["short"], caption["label"]) == ("figure", "LoF", "fig:h")
    assert [t["text"] for t in found["typed_refs"]] == ["Figure 3"]  # not the one in the comment
    assert found["cites"][0]["keys"] == ["k1", "k2"] and found["refs"][0]["labels"] == ["sec:a"]
    assert found["bibliographies"] == ["refs.bib"]


def test_engine():
    assert engine_of("% !TEX program = lualatex\n\\documentclass{article}") == "lualatex"
    assert engine_of("\\documentclass{article}\n\\usepackage{fontspec}") == "lualatex"
    assert engine_of("\\documentclass{article}\n% \\usepackage{fontspec}") == "pdflatex"


def test_main_file_from_a_section_file(ctx):
    tree = write_project(ctx.run_dir / "paper", PROJECT)
    root, main = latex_project(str(tree / "sections" / "sec.tex"), ctx)
    assert (root, main) == (tree.resolve(), (tree / "main.tex").resolve())


def test_project_named_in_a_request():
    assert latex_project_named("Audit workspace/latex_demo, then fix it") == "workspace/latex_demo"
    assert latex_project_named("Run the tests in workspace/code_demo") is None
    assert latex_project_named("a rate in kW/s") is None


@needs_tex
def test_build_reads_the_demo(ctx):
    root, _ = latex_project("workspace/latex_demo", ctx)
    tree = copy_project(root, ctx, "test")  # never build in workspace/: on a copy, as the tools do
    facts = build(tree, tree / "main.tex", ctx)
    assert facts["pdf"] and facts["pages"] == 3 and not facts["errors"]
    assert [r["label"] for r in facts["undefined_refs"]] == ["fig:smoke"]
    assert [c["key"] for c in facts["undefined_cites"]] == ["nist2026"]
