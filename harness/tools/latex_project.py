"""
LaTeX projects: the copy, the build, and a map of the source. The audit, the fixes and the latexdiff
(latex_audit.py, latex_fix.py, latexdiff.py) all start here.

A LaTeX project is a folder: a main .tex with \\documentclass, the files it \\input's, a .bib, figures, perhaps a
.sty. Nothing in this file is AI:
  - latexmk (a Perl program that comes with TeX Live) runs pdfLaTeX or LuaLaTeX and BibTeX as often as needed;
  - plain Python reads what they wrote: the .log (errors, undefined references, overfull boxes), the .aux (the number
    each \\label printed as) and the .blg (BibTeX's warnings);
  - a balanced-brace scanner walks the source and lists every heading, label, reference, citation and caption.
Like run_tests' report, the LLM gets facts with a file and a line, never the 400-line log.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import LATEX, WORKSPACE_DIR
from .base import Tool
from .files import _key, rel, resolve

# Files a build writes: left out of every copy, so a stale .aux never fools a check
BUILD_PRODUCTS = ("*.aux", "*.log", "*.out", "*.toc", "*.lof", "*.lot", "*.bbl", "*.blg", "*.fls", "*.fdb_latexmk",
                  "*.synctex.gz", "*.run.xml", "*.bcf", "*.xdv", "__pycache__", "_minted*")
DOCUMENTCLASS = re.compile(r"^[^%\n]*\\documentclass", re.M)
HEADINGS = ("part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph")
REFS = ("ref", "eqref", "autoref", "pageref", "cref", "Cref", "vref", "nameref", "cpageref")
CITES = ("cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "parencite", "textcite",
         "autocite", "footcite", "supercite", "nocite", "Cite", "Citep", "Citet")
INPUTS = ("input", "include", "subfile")
# A figure, table, section or equation number typed as plain digits: \ref would have written it for you
TYPED_REFERENCE = re.compile(r"\b(Figures?|Fig\.|Tables?|Tab\.|Sections?|Sec\.|Chapters?|Ch\.|Equations?|Eqs?\.|"
                             r"Appendix|Appendices)[~\s]+(\d+(?:\.\d+)*)(?![\d.]*\d)")


# ---- finding the project ------------------------------------------------------------------------------------------
def read_tex(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def strip_comments(text):
    """Blank out % comments (not \\%), keeping every character's position, so line numbers stay right."""
    return re.sub(r"(?<!\\)%[^\n]*", lambda m: " " * len(m.group(0)), text)


def is_main(path):
    return DOCUMENTCLASS.search(strip_comments(read_tex(path))) is not None


def inside_fence(path, ctx):
    path = Path(path).resolve()
    return path.is_relative_to(WORKSPACE_DIR) or path.is_relative_to(Path(ctx.run_dir).resolve())


def latex_project(path, ctx):
    """
    What the LLM wrote (a folder, a main .tex, or any .tex in the project) -> (the project folder, its main file).
    The main file is the .tex with \\documentclass: main.tex if there are several.
    """
    target = resolve(path, ctx, allow_dir=True)
    # resolve() finds a bare file name anywhere in workspace/, which helps a small LLM; but "C:/papers/x/main.tex"
    # must not quietly become workspace/latex_demo/main.tex: the folder written has to be the folder found
    written = [p.lower() for p in Path(str(path).replace("\\", "/")).parts if p not in ("/", ".")]
    if len(written) >= 2 and _key(path) not in ctx.imports and [p.lower() for p in target.parts[-2:]] != written[-2:]:
        raise FileNotFoundError(f"There is no {path}: the nearest match, {rel(target)}, is another project.")
    folder = target if target.is_dir() else target.parent
    if target.is_file() and target.suffix.lower() == ".tex" and is_main(target):
        return folder, target
    for candidate in [folder, *folder.parents]:  # a section file: the project is the nearest folder with a main file
        if not inside_fence(candidate, ctx) or candidate == WORKSPACE_DIR:
            break
        mains = sorted(p for p in candidate.glob("*.tex") if is_main(p))
        if mains:
            return candidate, next((p for p in mains if p.name == "main.tex"), mains[0])
    mains = sorted(p for p in folder.rglob("*.tex") if is_main(p))  # a folder that holds the project one level down
    if mains:
        main = next((p for p in mains if p.name == "main.tex"), mains[0])
        return main.parent, main
    raise ValueError(f"No LaTeX project in {rel(folder)}: no .tex file there has \\documentclass.")


PATHLIKE = re.compile(r"[\w.~:-]*[/\\][\w./\\~:-]*\w")


def latex_project_named(text):
    """
    The first folder in the user's words that holds a LaTeX project (a .tex with \\documentclass, at most one folder
    down), as written, or None. For the ROUTER: "Audit workspace/latex_demo" names no .tex and no "LaTeX".
    """
    from ..config import PROJECT_DIR
    for m in PATHLIKE.finditer(text):
        token = m.group(0)
        p = Path(token)
        for folder in ([p] if p.is_absolute() else [PROJECT_DIR / p, WORKSPACE_DIR / p, Path.cwd() / p]):
            try:
                if folder.is_dir() and any(is_main(t) for t in [*folder.glob("*.tex"), *folder.glob("*/*.tex")]):
                    return token
            except OSError:
                continue
    return None


def copy_project(root, ctx, name):
    """The project copied into outputs/run_.../latex/<name>/, without old build products. The original never changes."""
    dest = Path(ctx.run_dir) / "latex" / name / root.name
    if dest.exists():
        shutil.rmtree(dest)
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1e6
    if size > LATEX["max_project_mb"]:
        raise ValueError(f"{rel(root)} is {size:.0f} MB; the limit is {LATEX['max_project_mb']} MB (config LATEX).")
    with ctx.tracer.stage("PROJECT COPY", "python", "shutil.copytree (no ML)", rel(root),
                          f"a copy in the run folder ({name})") as ev:
        shutil.copytree(root, dest, ignore=shutil.ignore_patterns(*BUILD_PRODUCTS))
        ev.summary = f"{rel(root)} -> {rel(dest)} ({size:.1f} MB; build products left out)"
    return dest


# ---- the build -------------------------------------------------------------------------------------------------------
def engine_of(main_text):
    """pdfLaTeX unless the file asks for another: a '% !TEX program =' line, or fontspec/unicode-math (LuaLaTeX)."""
    magic = re.search(r"%\s*!TEX\s+(?:TS-)?program\s*=\s*(\w+)", main_text, re.I)
    if magic and magic.group(1).lower() in ("lualatex", "xelatex", "pdflatex"):
        return magic.group(1).lower()
    if re.search(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b(?:fontspec|unicode-math)\b", strip_comments(main_text)):
        return "lualatex"
    return "pdflatex"


def build(tree, main, ctx, label="BUILD"):
    """latexmk on a copy -> facts: pages, errors, undefined references and citations, overfull boxes, BibTeX warnings."""
    engine = engine_of(read_tex(main))
    flag = {"pdflatex": "-pdf", "lualatex": "-lualatex", "xelatex": "-xelatex"}[engine]
    with ctx.tracer.stage(label, "server", f"latexmk + {engine} + BibTeX (TeX Live programs, no ML)",
                          f"{rel(main)} and the files it inputs", "PDF + .log, .aux, .blg") as ev:
        # TeX wraps its log at 79 characters, splitting paths and messages; a long line keeps each message whole
        env = {**os.environ, "max_print_line": "10000", "error_line": "254", "half_error_line": "238"}  # BibTeX refuses more
        start = time.perf_counter()
        try:
            proc = subprocess.run(["latexmk", flag, "-interaction=nonstopmode", "-file-line-error", main.name],
                                  cwd=main.parent, capture_output=True, text=True, errors="replace", env=env,
                                  timeout=LATEX["timeout"])
            timed_out = False
        except subprocess.TimeoutExpired:
            proc, timed_out = None, True
        except FileNotFoundError:
            raise RuntimeError("latexmk is not on the PATH: the LaTeX tools need TeX Live (https://tug.org/texlive/)")
        seconds = time.perf_counter() - start
        log_path, blg_path = main.with_suffix(".log"), main.with_suffix(".blg")
        facts = read_log(read_tex(log_path) if log_path.is_file() else "", tree)
        facts["bibtex"] = read_blg(read_tex(blg_path) if blg_path.is_file() else "")
        pdf = main.with_suffix(".pdf")
        facts.update(engine=engine, seconds=round(seconds, 1), pdf=str(pdf) if pdf.is_file() else None,
                     log=str(log_path), main=main.relative_to(tree).as_posix(), tree=str(tree))
        if timed_out:
            facts["errors"].insert(0, {"file": facts["main"], "line": None,
                                       "message": f"the build ran longer than {LATEX['timeout']} s and was stopped"})
        elif proc.returncode != 0 and not facts["errors"] and not facts["pdf"]:
            facts["errors"].append({"file": facts["main"], "line": None,
                                    "message": (proc.stdout + proc.stderr).strip().splitlines()[-1][:200]
                                    if (proc.stdout + proc.stderr).strip() else "latexmk failed"})
        facts["ok"] = bool(facts["pdf"]) and not facts["errors"]
        ev.summary = build_summary(facts)
        ev.details = {k: v for k, v in facts.items() if k != "tree"}
        if facts["pdf"]:
            ev.artifacts.append(facts["pdf"])
    return facts


def publish(pdf, ctx, root, kind):
    """A build's PDF copied to the run folder with a name that says what it is: latex_demo_original.pdf,
    latex_demo_corrected.pdf, latex_demo_latexdiff.pdf."""
    dest = Path(ctx.run_dir) / f"{Path(root).name}_{kind}.pdf"
    shutil.copyfile(pdf, dest)
    return dest


def build_summary(facts):
    parts = [f"{Path(facts['pdf']).name if facts['pdf'] else 'no PDF'}: {facts['pages'] or 0} page(s) in "
             f"{facts['seconds']} s ({facts['engine']})",
             f"{len(facts['errors'])} error(s)"]
    for key, what in (("undefined_refs", "undefined reference"), ("undefined_cites", "undefined citation"),
                      ("multiply_defined", "label defined twice"), ("overfull", "overfull box")):
        if facts[key]:
            names = ", ".join(sorted({x.get("label") or x.get("key") or x.get("file") or "" for x in facts[key]}
                                     - {""})[:4])  # a box in a .cls or .sty has no .tex file
            parts.append(f"{len(facts[key])} {what}(s)" + (f" ({names})" if names else ""))
    warnings = facts["bibtex"]["warnings"]
    if warnings:
        parts.append(f"BibTeX: {len(warnings)} warning(s)")
    return "; ".join(parts)


# "(./sections/a.tex" opens a file and ")" closes it. Any other "(" is text, as in "(1.2pt too wide)", but its ")"
# must close it, not the file
LOG_FILE = re.compile(r"\((\.{0,2}[/\\][^\s()]+|[A-Za-z]:[/\\][^\s()]+)?|\)")
ERROR = re.compile(r"^((?:[A-Za-z]:)?[^:\n]+\.(?:tex|sty|cls|bib|ltx|def)):(\d+): (.+)$", re.M)
UNDEFINED_REF = re.compile(r"LaTeX Warning: Reference [`']([^']+)' on page (\d+) undefined on input line (\d+)")
UNDEFINED_CITE = re.compile(r"(?:LaTeX|Package natbib|Package biblatex) Warning: Citation [`']([^']+)' on page "
                            r"(\d+) undefined on input line (\d+)")
MULTIPLY = re.compile(r"LaTeX Warning: Label [`']([^']+)' multiply defined")
OVERFULL = re.compile(r"Overfull \\hbox \(([\d.]+)pt too wide\) (?:in paragraph|in alignment|detected) at lines? "
                      r"(\d+)(?:--(\d+))?")


def read_log(text, tree=None):
    """The .log -> facts. A stack of the files TeX opened ('(./sections/a.tex' ... ')') says which file a box is in."""
    facts = {"errors": [], "undefined_refs": [], "undefined_cites": [], "multiply_defined": [], "overfull": [],
             "pages": None}
    stack, closed, events = [], None, []
    for m in LOG_FILE.finditer(text):  # where each file starts and ends, by character position
        if m.group(0).startswith("("):
            stack.append(m.group(1))  # None for a "(" that opens no file
        elif stack:
            gone = stack.pop()
            closed = gone if gone and gone.lower().endswith((".tex", ".ltx")) else closed
        events.append((m.start(), list(stack), closed))

    def file_at(pos, before=False):
        """The .tex file open at pos; before=True: the one closed last (a paragraph that ran to the end of a file
        is reported after the next file opened, as 'lines 14--1')."""
        current, last = [], None
        for start, files, gone in events:
            if start > pos:
                break
            current, last = files, gone
        tex = [f for f in current if f and f.lower().endswith((".tex", ".ltx"))]
        name = last if before and last else tex[-1] if tex else None
        return clean_path(name, tree) if name else None

    for m in ERROR.finditer(text):
        facts["errors"].append({"file": clean_path(m.group(1), tree), "line": int(m.group(2)),
                                "message": m.group(3).strip()[:200]})
    for m in re.finditer(r"^! (.+)$", text, re.M):  # an error without -file-line-error's prefix (fatal ones)
        if not any(e["message"].startswith(m.group(1)[:40]) for e in facts["errors"]):
            facts["errors"].append({"file": None, "line": None, "message": m.group(1).strip()[:200]})
    for m in UNDEFINED_REF.finditer(text):
        facts["undefined_refs"].append({"label": m.group(1), "page": int(m.group(2)), "line": int(m.group(3))})
    for m in UNDEFINED_CITE.finditer(text):
        facts["undefined_cites"].append({"key": m.group(1), "page": int(m.group(2)), "line": int(m.group(3))})
    facts["multiply_defined"] = [{"label": label} for label in sorted(set(MULTIPLY.findall(text)))]
    shipped = [(m.start(), int(m.group(1))) for m in re.finditer(r"\[(\d+)(?=[\s\]{<])", text)]
    for m in OVERFULL.finditer(text):
        if float(m.group(1)) < LATEX["overfull_pt"]:
            continue
        page = next((p for pos, p in reversed(shipped) if pos < m.start()), 0) + 1
        ran_on = m.group(3) is not None and int(m.group(3)) < int(m.group(2))
        facts["overfull"].append({"file": file_at(m.start(), before=ran_on), "line": int(m.group(2)),
                                  "pt": float(m.group(1)), "page": page})
    pages = re.search(r"Output written on .*?\((\d+) pages?", text)
    facts["pages"] = int(pages.group(1)) if pages else None
    return facts


def clean_path(path, tree=None):
    path = path.replace("\\", "/")
    if tree is not None:
        try:
            return Path(path).resolve().relative_to(Path(tree).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return re.sub(r"^\./", "", path)


def read_blg(text):
    """BibTeX's .blg (or biber's): its warnings and errors, one line each."""
    warnings = [w.strip() for w in re.findall(r"^Warning--(.+)$", text, re.M)]
    warnings += [w.strip() for w in re.findall(r"^\[\d+\] .*?WARN - (.+)$", text, re.M)]
    errors = [e.strip() for e in re.findall(r"^(I couldn't open .+|I found no .+|.*ERROR - .+)$", text, re.M)]
    return {"warnings": warnings, "errors": errors}


# ---- the .aux: what each label printed as ----------------------------------------------------------------------------
def group(text, i, open_="{", close="}"):
    """The balanced {...} (or [...]) starting at text[i] -> (its content, the index after it), or None."""
    if i >= len(text) or text[i] != open_:
        return None
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return None


def groups(text, i, n):
    """n balanced {...} groups in a row (spaces between allowed) -> ([contents], index after), or (None, i)."""
    out = []
    for _ in range(n):
        while i < len(text) and text[i] in " \t":
            i += 1
        g = group(text, i)
        if g is None:
            return None, i
        out.append(g[0])
        i = g[1]
    return out, i


def plain(text):
    """'{\\relax 2.1}' or '\\numberline {3}' -> '2.1' / '3': what a reader saw printed."""
    text = re.sub(r"\\(?:relax|ignorespaces|protect|nobreakspace)\s*", "", text)
    return re.sub(r"[{}]", "", text).strip()


def read_aux(main):
    """main.aux (and any .aux it \\@input's) -> labels, the bibliography numbers, the citation order, toc/lof/lot."""
    aux = {"labels": {}, "bibcite": {}, "citations": [], "toc": [], "lof": [], "lot": []}
    todo, seen = [main.with_suffix(".aux")], set()
    while todo:
        path = todo.pop(0)
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        text = read_tex(path)
        for m in re.finditer(r"\\@input\{([^}]+)\}", text):
            todo.append(main.parent / m.group(1))
        for m in re.finditer(r"\\newlabel\{([^}]+)\}", text):
            g, _ = groups(text, m.end(), 1)
            if not g:
                continue
            fields, _ = groups(g[0], 0, 2)
            if fields:
                more, _ = groups(g[0], 0, 4)  # hyperref adds a title and an anchor such as figure.2 or section.3
                kind = re.sub(r"[.*].*$", "", more[3]) if more else ""
                aux["labels"].setdefault(m.group(1), {"number": plain(fields[0]), "page": plain(fields[1]),
                                                      "kind": kind})
        for m in re.finditer(r"\\bibcite\{([^}]+)\}", text):
            g, _ = groups(text, m.end(), 1)
            if g:
                aux["bibcite"][m.group(1)] = plain(groups(g[0], 0, 1)[0][0]) if g[0].startswith("{") else plain(g[0])
        for m in re.finditer(r"\\citation\{([^}]+)\}", text):
            aux["citations"] += [k.strip() for k in m.group(1).split(",") if k.strip() not in aux["citations"]]
        for m in re.finditer(r"\\@writefile\{(toc|lof|lot)\}", text):
            g, _ = groups(text, m.end(), 1)
            if not g or "\\contentsline" not in g[0]:
                continue
            body = g[0][g[0].index("\\contentsline") + len("\\contentsline"):]
            fields, _ = groups(body, 0, 3)
            if not fields:
                continue
            number = re.search(r"\\numberline\s*\{([^}]*)\}", fields[1])
            title = re.sub(r"\\numberline\s*\{[^}]*\}", "", fields[1])
            aux[m.group(1)].append({"level": fields[0], "number": plain(number.group(1)) if number else "",
                                    "title": plain(title), "page": plain(fields[2])})
    return aux


# ---- the source: every heading, label, reference, citation and caption ------------------------------------------
class Source:
    """One .tex file of the project: its text, the text without comments, and line numbers for any position."""

    def __init__(self, path, tree):
        self.path, self.name = Path(path), Path(path).relative_to(tree).as_posix()
        self.text = read_tex(path)
        self.clean = strip_comments(self.text)
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", self.text)]

    def line(self, pos):
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def line_text(self, number):
        return self.text.splitlines()[number - 1] if 0 < number <= len(self.text.splitlines()) else ""


def command_args(clean, i, optional=1, required=1):
    """After a command name at clean[i]: up to `optional` [..] then `required` {..} -> ([opts], [reqs], end)."""
    opts, reqs = [], []
    while len(opts) < optional:
        j = i
        while j < len(clean) and clean[j] in " \t":
            j += 1
        g = group(clean, j, "[", "]")
        if g is None:
            break
        opts.append(g[0])
        i = g[1]
    for _ in range(required):
        j = i
        while j < len(clean) and clean[j] in " \t\n":
            j += 1
        g = group(clean, j)
        if g is None:
            return opts, reqs, i
        reqs.append(g[0])
        i = g[1]
    return opts, reqs, i


COMMAND = re.compile(r"\\([A-Za-z@]+)(\*?)")


def scan(tree, main):
    """
    Walk the project from its main file through \\input/\\include -> every heading, label, reference, citation,
    caption, bibliography and macro definition, each with its file and line; plus plain-text references
    ("Figure 3") and the files in reading order.
    """
    tree, main = Path(tree).resolve(), Path(main).resolve()
    base = main.parent  # LaTeX reads \input paths from the main file's folder
    found = {"files": [], "headings": [], "labels": [], "refs": [], "cites": [], "captions": [], "typed_refs": [],
             "bibliographies": [], "macros": set(), "sources": {}}
    seen = set()

    def visit(path):
        path = path.resolve()
        if path in seen or not path.is_file():
            return
        seen.add(path)
        src = Source(path, tree)
        found["sources"][src.name] = src
        found["files"].append(src.name)
        floats = []  # the float environments we are inside: a caption belongs to the innermost
        for m in COMMAND.finditer(src.clean):
            name, star, pos = m.group(1), m.group(2), m.start()
            line = src.line(pos)
            if name == "begin" or name == "end":
                _, reqs, _ = command_args(src.clean, m.end(), 0, 1)
                env = reqs[0].rstrip("*") if reqs else ""
                if env in ("figure", "table", "wrapfigure", "sidewaysfigure", "sidewaystable", "longtable", "tblr"):
                    if name == "begin":
                        floats.append(env)
                    elif floats:
                        floats.pop()
            elif name in INPUTS:
                _, reqs, _ = command_args(src.clean, m.end(), 0, 1)
                if reqs:
                    target = (base / reqs[0].strip())
                    visit(target if target.suffix else target.with_suffix(".tex"))
            elif name in HEADINGS:
                opts, reqs, end = command_args(src.clean, m.end(), 1, 1)
                if reqs:
                    found["headings"].append({"level": name, "star": bool(star), "short": opts[0] if opts else None,
                                              "title": reqs[0], "file": src.name, "line": line, "pos": pos,
                                              "end": end})
            elif name == "label":
                _, reqs, _ = command_args(src.clean, m.end(), 0, 1)
                if reqs:
                    found["labels"].append({"label": reqs[0].strip(), "file": src.name, "line": line, "pos": pos})
            elif name in REFS:
                _, reqs, end = command_args(src.clean, m.end(), 0, 1)
                if reqs:
                    found["refs"].append({"cmd": name, "labels": [x.strip() for x in reqs[0].split(",")],
                                          "file": src.name, "line": line, "pos": pos, "end": end})
            elif name in CITES:
                _, reqs, end = command_args(src.clean, m.end(), 2, 1)
                if reqs:
                    found["cites"].append({"cmd": name, "keys": [k.strip() for k in reqs[0].split(",") if k.strip()],
                                           "file": src.name, "line": line, "pos": pos, "end": end})
            elif name in ("caption", "captionof"):
                if name == "captionof":
                    _, kind, after = command_args(src.clean, m.end(), 0, 1)
                    opts, reqs, end = command_args(src.clean, after, 1, 1)
                    kind = kind[0] if kind else ""
                else:
                    opts, reqs, end = command_args(src.clean, m.end(), 1, 1)
                    kind = floats[-1] if floats else ""
                if reqs:
                    found["captions"].append({"kind": kind.replace("longtable", "table").replace("tblr", "table"),
                                              "short": opts[0] if opts else None, "text": reqs[0],
                                              "file": src.name, "line": line, "pos": pos, "star": bool(star),
                                              "end": end})
            elif name in ("bibliography", "addbibresource"):
                _, reqs, _ = command_args(src.clean, m.end(), 1 if name == "addbibresource" else 0, 1)
                for item in (reqs[0].split(",") if reqs else []):
                    bib = base / item.strip()
                    found["bibliographies"].append((bib if bib.suffix else bib.with_suffix(".bib")).relative_to(tree)
                                                   .as_posix())
            elif name in ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "def",
                          "DeclareMathOperator", "newenvironment"):
                after = src.clean[m.end():m.end() + 80]
                macro = re.match(r"\s*\{?\\?([A-Za-z@]+)", after)
                if macro:
                    found["macros"].add(macro.group(1))
        for m in TYPED_REFERENCE.finditer(src.clean):
            found["typed_refs"].append({"kind": m.group(1), "number": m.group(2), "text": m.group(0),
                                        "file": src.name, "line": src.line(m.start()), "pos": m.start()})

    visit(main)
    attach_labels(found)
    for sty in list(tree.rglob("*.sty")) + list(tree.rglob("*.cls")):  # the house style's own macros
        for macro in re.findall(r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\\"
                                r"([A-Za-z@]+)", strip_comments(read_tex(sty))):
            found["macros"].add(macro)
    found["macros"] = sorted(found["macros"])
    return found


def attach_labels(found):
    """A \\label belongs to the heading or caption just before it in the same file (within a few lines): that is
    how LaTeX numbers it. Each heading and caption gets its label's name."""
    anchors = sorted([("heading", h) for h in found["headings"]] + [("caption", c) for c in found["captions"]],
                     key=lambda a: (a[1]["file"], a[1]["pos"]))
    for lab in found["labels"]:
        before = [a for kind, a in anchors if a["file"] == lab["file"] and a["pos"] < lab["pos"]
                  and lab["line"] - a["line"] <= 3 and "label" not in a]
        if before:
            before[-1]["label"] = lab["label"]
            lab["of"] = "heading" if before[-1] in found["headings"] else "caption"


def scan_summary(found):
    return (f"{len(found['files'])} .tex file(s), {len(found['headings'])} heading(s), {len(found['labels'])} "
            f"label(s), {len(found['refs'])} reference(s), {sum(len(c['keys']) for c in found['cites'])} citation(s) "
            f"of {len({k for c in found['cites'] for k in c['keys']})} source(s), {len(found['captions'])} caption(s), "
            f"{len(found['typed_refs'])} number(s) typed as text")


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return path


def without_sources(found):
    return {k: v for k, v in found.items() if k != "sources"}


# ---- the tools ---------------------------------------------------------------------------------------------------------
PROJECT_PARAMETERS = {
    "type": "object",
    "properties": {"path": {"type": "string",
                            "description": "The LaTeX project: its folder or its main .tex, e.g. workspace/latex_demo"}},
    "required": ["path"],
}


class BuildLatexTool(Tool):
    name = "build_latex"
    description = ("Compile a LaTeX project (its folder or main .tex) with latexmk and report the errors, undefined "
                   "references and citations, and overfull boxes, each with its file and line. Works on a copy.")
    parameters = PROJECT_PARAMETERS
    model_id = "latexmk + pdfLaTeX/LuaLaTeX + BibTeX (no ML)"
    check_unused = False
    how_it_works = ("No model here: latexmk runs LaTeX and BibTeX until the cross-references settle, on a copy of the "
                    "project. Python reads the .log for errors, undefined references and overfull boxes.")

    def run(self, ctx, path):
        root, main = latex_project(path, ctx)
        tree = copy_project(root, ctx, "build")
        ctx.tracer.say(self.how_it_works, kind="python")
        facts = build(tree, tree / main.relative_to(root), ctx)
        observations = {"project": rel(root), "summary": build_summary(facts), "pages": facts["pages"],
                        "errors": facts["errors"][:6], "undefined_references": facts["undefined_refs"][:6],
                        "undefined_citations": facts["undefined_cites"][:6], "overfull_boxes": facts["overfull"][:6],
                        "bibtex_warnings": facts["bibtex"]["warnings"][:6]}
        artifacts = [{"type": "latex_pdf", "path": facts["pdf"], "note": "built from a copy"}] if facts["pdf"] else []
        return {"artifacts": artifacts, "observations": observations}


class ScanLatexTool(Tool):
    name = "scan_latex"
    description = ("Map a LaTeX project: its files, headings, labels, references, citations, captions, and figure "
                   "or section numbers typed as plain text. Use it to answer questions about a paper's structure.")
    parameters = PROJECT_PARAMETERS
    model_id = "a balanced-brace scanner + the .aux (no ML)"
    check_unused = False
    how_it_works = ("No model here: Python walks the main file and every file it \\input's, finds each command by "
                    "its name and reads its arguments by matching braces; the build's .aux says what number each "
                    "label printed as.")

    def run(self, ctx, path):
        root, main = latex_project(path, ctx)
        tree = copy_project(root, ctx, "scan")
        facts = build(tree, tree / main.relative_to(root), ctx)
        with ctx.tracer.stage("SCAN", "python", self.model_id, "the .tex files + .aux", "a map of the project") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            found = scan(tree, tree / main.relative_to(root))
            aux = read_aux(tree / main.relative_to(root))
            saved = save_json(Path(ctx.run_dir) / "scan.json", {**without_sources(found), "aux": aux})
            ev.summary = scan_summary(found)
            ev.artifacts.append(str(saved))
        cited = sorted({k for c in found["cites"] for k in c["keys"]})
        return {"artifacts": [{"type": "data", "path": saved, "note": "the full map"}],
                "observations": {
                    "project": rel(root), "summary": scan_summary(found), "build": build_summary(facts),
                    "files": found["files"][:12],
                    "headings": [f"{aux['labels'].get(h.get('label', ''), {}).get('number', '')} {h['title']}"
                                 f" ({h['file']}:{h['line']})".strip() for h in found["headings"]][:12],
                    "sources_cited": cited[:20],
                    "typed_numbers": [f"{t['text']} ({t['file']}:{t['line']})" for t in found["typed_refs"]][:8]}}
