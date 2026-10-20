"""
audit_latex: three auditors read a LaTeX project and write every problem they find into a ledger.

  BUILD + SCAN        latex_project.py: the PDF, the .log, the .aux, and a map of every heading, label and citation
  AUDIT: REFERENCES   bibtexparser reads the .bib: citations with no entry, entries never cited, the same work twice,
                      missing fields, DOIs written as links
  AUDIT: CROSS-REFS   the .aux says what each \\label printed as: references to labels that don't exist, numbers typed
                      by hand ("Figure 3") that name the wrong figure or none, table-of-contents titles that differ
                      from the heading
  AUDIT: TEXT         pylatexenc parses the LaTeX; the words between the commands become plain text, each character
                      remembering its place in the file: doubled words, misspellings (pyspellchecker), a name spelled
                      two ways, "..", dates, quotes, a citation run into the next word
  JUDGE               the LLM, one short question per "likely" finding that has a fix: is it an error?
  LEDGER              ledger.json, audit_report.md and ledger.csv in the run folder (and outputs/ledgers/)

Every finding has a file, a line, the words it is about (evidence), why, a fix if one follows from the facts, and a
confidence:
  certain         the fix follows from facts ("in in" -> "in"; one dictionary suggestion; exactly one figure fits)
  likely          a checker suspects it; JUDGE may confirm or reject it
  needs a person  no fix can be derived (a source missing from the .bib, a section that doesn't exist)
The LLM never makes a finding certain on its own say-so, and never writes a file.
"""

import csv
import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..config import LATEX, OUTPUT_DIR
from .base import Tool
from .files import rel
from .latex_project import (CITES, HEADINGS, PROJECT_PARAMETERS, REFS, Source, build, build_summary, copy_project,
                            latex_project, publish, read_aux, read_tex, save_json, scan, scan_summary,
                            strip_comments)

CHECKS = ("references", "crossrefs", "text")
CATEGORIES = ("build", "references", "crossrefs", "text", "requested")  # the ledger's order; edit_tex adds "requested"
LEDGERS_DIR = OUTPUT_DIR / "ledgers"  # the latest ledger of each project: fix_latex finds "the last audit" here
STOPWORDS = set("about after also and are been before but can for from had has have into its more not of off one "
                "our over shows shown show such than that the their them then there these they this those through "
                "under was were what when where which while who will with would".split())


# ---- findings and their fixes ------------------------------------------------------------------------------------------
def paragraph(text, line):
    """The paragraph (lines between blank lines) that holds a 1-based line -> its (start, end) in the text."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return 0, 0
    first = last = max(1, min(line, len(lines))) - 1
    while first > 0 and lines[first - 1].strip():
        first -= 1
    while last < len(lines) - 1 and lines[last + 1].strip():
        last += 1
    start = sum(len(x) for x in lines[:first])
    return start, start + sum(len(x) for x in lines[first:last + 1])


def word_before(text, i, low=0):
    while i > low and text[i - 1].isspace():
        i -= 1
    while i > low and not text[i - 1].isspace():
        i -= 1
    return i


def word_after(text, i, high=None):
    high = len(text) if high is None else high
    while i < high and text[i].isspace():
        i += 1
    while i < high and not text[i].isspace():
        i += 1
    return i


def snippet(text, start, end, words=4):
    """The words around text[start:end], on one line: enough for a person to find the place."""
    low, high = paragraph(text, text.count("\n", 0, start) + 1)
    a, b = start, end
    for _ in range(words):
        a, b = word_before(text, a, low), word_after(text, b, high)
    return " ".join(text[a:b].split())


def exact_fix(text, start, end, new):
    """
    text[start:end] -> new, written as an exact-match edit {"old", "new"}: the old text grows a word at a time on each
    side until it occurs exactly once in its paragraph, so latex_fix.py can find it again without line numbers.
    None if it never becomes unique.
    """
    low, high = paragraph(text, text.count("\n", 0, start) + 1)
    a, b = start, end
    for _ in range(8):
        if text[low:high].count(text[a:b]) == 1:
            return {"old": text[a:b], "new": text[a:start] + new + text[end:b]}
        wider = word_before(text, a, low), word_after(text, b, high)
        if wider == (a, b):
            break
        a, b = wider
    return None


def finding(category, check, src, start, end, why, confidence, new=None, words=4):
    """One row of the ledger, about src.text[start:end]. A fix is made when `new` is given; a 'certain' finding
    whose fix can't be made exact becomes 'needs a person' (no guessing)."""
    fix = exact_fix(src.text, start, end, new) if new is not None else None
    if confidence == "certain" and fix is None:
        confidence = "needs a person"
    return {"id": None, "category": category, "check": check, "file": src.name, "line": src.line(start),
            "evidence": snippet(src.text, start, end, words), "why": why, "fix": fix, "confidence": confidence,
            "source": f"audit_{category}", "judge": None, "status": "found"}


def content_words(text):
    """'Heat release rate of the four t-squared design fires' -> {'heat', 'release', 'rate', 'four', 'squared', ...}:
    lower case, no LaTeX commands or maths, no short or common words, a plural 's' dropped."""
    text = re.sub(r"\$[^$]*\$|\\[A-Za-z@]+\*?", " ", text)
    return {w.rstrip("s") if len(w) > 4 else w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOPWORDS}


def sentence_at(text, pos):
    """The sentence of text that holds pos (between full stops followed by a space, within its paragraph)."""
    low, high = paragraph(text, text.count("\n", 0, pos) + 1)
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", text[low:high])]
    start = max([low + e for e in ends if low + e <= pos] or [low])
    stop = min([low + e for e in ends if low + e > pos] or [high])
    return text[start:stop]


# ---- AUDIT: REFERENCES -------------------------------------------------------------------------------------------------
REQUIRED = {  # the fields BibTeX's standard styles need for each entry type ("a|b": either will do)
    "article": ("author", "title", "journal", "year"),
    "book": ("author|editor", "title", "publisher", "year"),
    "inproceedings": ("author", "title", "booktitle", "year"),
    "conference": ("author", "title", "booktitle", "year"),
    "incollection": ("author", "title", "booktitle", "publisher", "year"),
    "techreport": ("author", "title", "institution", "year"),
    "phdthesis": ("author", "title", "school", "year"),
    "mastersthesis": ("author", "title", "school", "year"),
    "manual": ("title",), "misc": ("title",), "unpublished": ("author", "title", "note"),
}
DOI = re.compile(r"^10\.\d{4,9}/\S+$")
DOI_LINK = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)


def read_bib(path, tree):
    """A .bib -> (its Source, its entries). bibtexparser reads the fields; a regex finds where each entry starts."""
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    src = Source(path, tree)
    parser = BibTexParser(common_strings=True, ignore_nonstandard_types=False)
    parsed = bibtexparser.loads(src.text, parser=parser).entries
    starts = {}
    for m in re.finditer(r"@(\w+)\s*[{(]\s*([^,\s]+)\s*,", src.text):
        if m.group(1).lower() not in ("string", "preamble", "comment"):
            starts.setdefault(m.group(2), []).append(m)
    entries = []
    for e in parsed:
        where = starts.get(e["ID"], [])
        m = where.pop(0) if where else None
        entries.append({"key": e["ID"], "type": e["ENTRYTYPE"].lower(),
                        "fields": {k.lower(): v for k, v in e.items() if k not in ("ID", "ENTRYTYPE")},
                        "pos": m.start() if m else 0, "end": m.end() if m else 0})
    for key, left in starts.items():  # a key entered twice: bibtexparser keeps one, the regex sees both
        for m in left:
            entries.append({"key": key, "type": m.group(1).lower(), "fields": {}, "pos": m.start(), "end": m.end(),
                            "duplicate_key": True})
    return src, sorted(entries, key=lambda e: e["pos"])


def normal_title(title):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\\[A-Za-z]+", "", title.lower()))


def audit_references(found, tree, ctx):
    findings, entries, bibs = [], {}, {}
    for name in found["bibliographies"]:
        path = Path(tree) / name
        if not path.is_file():
            main = found["sources"][found["files"][0]]
            m = re.search(r"\\(?:bibliography|addbibresource)\b", main.clean)
            findings.append(finding("references", "bibliography file missing", main, m.start() if m else 0,
                                    m.end() if m else 0, f"{name} is not in the project", "needs a person"))
            continue
        src, bib = read_bib(path, tree)
        bibs[src.name] = (src, bib)
        for e in bib:
            entries.setdefault(e["key"], (src, e))
    bibitems = {m.group(1).strip() for src in found["sources"].values()  # a thebibliography list in the .tex itself
                for m in re.finditer(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", src.clean)}
    cited = Counter(k for c in found["cites"] for k in c["keys"])

    # a \cite with no entry
    for c in found["cites"]:
        src = found["sources"][c["file"]]
        for key in c["keys"]:
            if key != "*" and key not in entries and key not in bibitems:
                findings.append(finding("references", "citation with no entry", src, c["pos"], c["end"],
                                        f"no entry '{key}' in {', '.join(bibs) or 'any .bib'}: the PDF prints [?]",
                                        "needs a person"))
    nocite_all = "*" in cited
    titles, dois = {}, {}
    for bib_name, (src, bib) in bibs.items():
        for e in bib:
            header = (e["pos"], e["end"])
            if e.get("duplicate_key"):
                findings.append(finding("references", "key entered twice", src, *header,
                                        f"the key '{e['key']}' is used by two entries; BibTeX keeps the first",
                                        "needs a person"))
                continue
            if not nocite_all and e["key"] not in cited:
                findings.append(finding("references", "entry never cited", src, *header,
                                        f"'{e['key']}' is in {bib_name} but nothing cites it, so the reference list "
                                        f"leaves it out", "likely"))
            fields = e["fields"]
            # the same work twice: the same DOI, or the same title and year
            doi = DOI_LINK.sub("", fields.get("doi", "")).strip().lower()
            title = (normal_title(fields.get("title", "")), fields.get("year", ""))
            twin = dois.get(doi) if doi else None
            twin = twin or (titles.get(title) if len(title[0]) > 12 else None)
            if twin:
                same = "DOI" if doi and dois.get(doi) else "title and year"
                findings.append(finding("references", "the same work twice", src, *header,
                                        f"'{e['key']}' has the same {same} as '{twin['key']}' (line "
                                        f"{src.line(twin['pos'])}): one work entered under two keys", "likely"))
            if doi:
                dois.setdefault(doi, e)
            titles.setdefault(title, e)
            missing = [need for need in REQUIRED.get(e["type"], ())
                       if not any(fields.get(f, "").strip() for f in need.split("|"))]
            if missing:
                findings.append(finding("references", "missing fields", src, *header,
                                        f"a {e['type']} needs {', '.join(m.replace('|', ' or ') for m in missing)}",
                                        "likely"))
            if fields.get("doi"):
                raw = fields["doi"].strip()
                value = re.search(r"\bdoi\s*=\s*[{\"]\s*" + re.escape(raw), src.text[e["pos"]:], re.I)
                if value and DOI_LINK.match(raw):
                    start = e["pos"] + value.end() - len(raw)
                    findings.append(finding("references", "DOI written as a link", src, start, start + len(raw),
                                            "a doi field holds the DOI alone; the style adds the link",
                                            "certain", DOI_LINK.sub("", raw)))
                elif not DOI.match(DOI_LINK.sub("", raw)):
                    findings.append(finding("references", "malformed DOI", src, *header,
                                            f"'{raw}' is not a DOI (10.<number>/<suffix>)", "needs a person"))
    return findings


# ---- AUDIT: CROSS-REFERENCES -------------------------------------------------------------------------------------------
KINDS = {"figure": ("Figure", "Figures", "Fig."), "table": ("Table", "Tables", "Tab."),
         "equation": ("Equation", "Equations", "Eq.", "Eqs."),
         "section": ("Section", "Sections", "Sec.", "Chapter", "Chapters", "Ch.", "Appendix", "Appendices")}
PREFIXES = {"fig": "figure", "tab": "table", "eq": "equation", "sec": "section", "ch": "section", "chap": "section",
            "app": "section", "subsec": "section"}


def label_kinds(found, aux):
    """label -> (kind, the number it printed as), for every label the build numbered."""
    of = {}
    for h in found["headings"]:
        if "label" in h:
            of[h["label"]] = "section"
    for c in found["captions"]:
        if "label" in c:
            of[c["label"]] = "figure" if "figure" in c["kind"] else "table" if "table" in c["kind"] else c["kind"]
    kinds = {}
    for label, info in aux["labels"].items():
        anchor = info.get("kind", "")  # hyperref's anchor: figure.2, table.1, section.3, equation.1
        kind = ("section" if anchor in HEADINGS or anchor == "appendix" else anchor if anchor in KINDS else
                of.get(label) or PREFIXES.get(re.split(r"[:._-]", label)[0].lower(), ""))
        if kind:
            kinds[label] = (kind, info["number"])
    return kinds


def short_title_differs(short, title):
    """A short title is a shortening if most of its words are in the long one; else the TOC says something else."""
    words = content_words(short)
    return bool(words) and len(words & content_words(title)) < max(1, len(words) / 2)


def audit_crossrefs(found, aux, facts):
    findings, sources = [], found["sources"]
    defined = Counter(lab["label"] for lab in found["labels"])
    kinds = label_kinds(found, aux)
    numbers = {k: {n for (kk, n) in kinds.values() if kk == k} for k in KINDS}
    numbers["section"] |= {e["number"] for e in aux["toc"] if e["number"]}
    numbers["figure"] |= {e["number"] for e in aux["lof"] if e["number"]}
    numbers["table"] |= {e["number"] for e in aux["lot"] if e["number"]}

    # \ref to a label that doesn't exist; a label defined twice
    for r in found["refs"]:
        for label in r["labels"]:
            if label not in defined and label not in aux["labels"]:
                findings.append(finding("crossrefs", "reference to an undefined label", sources[r["file"]], r["pos"],
                                        r["end"], f"no \\label{{{label}}} in the project: the PDF prints ??",
                                        "needs a person"))
    seen = Counter()
    for lab in found["labels"]:
        seen[lab["label"]] += 1
        if seen[lab["label"]] == 2:
            findings.append(finding("crossrefs", "label defined twice", sources[lab["file"]], lab["pos"],
                                    lab["pos"] + len(lab["label"]) + 8,
                                    f"\\label{{{lab['label']}}} is defined {defined[lab['label']]} times; every "
                                    "\\ref to it gets the last one", "needs a person"))

    # numbers typed by hand: "Figure 3", "Section 4.0"
    captions = [c for c in found["captions"] if "label" in c and c["label"] in kinds]
    for t in found["typed_refs"]:
        kind = next(k for k, words in KINDS.items() if t["kind"] in words)
        number = re.sub(r"(\.0)+$", "", t["number"])
        src, start, end = sources[t["file"]], t["pos"], t["pos"] + len(t["text"])
        printed = sorted(numbers[kind], key=lambda n: [int(x) if x.isdigit() else 0 for x in n.split(".")])
        listed = ", ".join(printed) or "none"
        # which caption does the sentence describe? (figures and tables: their captions have words to compare)
        ranked = []
        if kind in ("figure", "table"):
            words = content_words(sentence_at(src.clean, start))
            ranked = sorted(((len(words & content_words(c["text"])), c) for c in captions
                             if kinds[c["label"]][0] == kind), key=lambda x: -x[0])
        best = ranked[0] if ranked and ranked[0][0] >= 2 and (len(ranked) == 1 or ranked[0][0] >= 2 * ranked[1][0]) \
            else None
        if number in numbers[kind]:
            same = [label for label, (k, n) in kinds.items() if k == kind and n == number]
            if best and best[1]["label"] not in same:
                label = best[1]["label"]
                findings.append(finding("crossrefs", "typed number names another " + kind, src, start, end,
                                        f"{t['kind']} {number} is printed, but the sentence matches the caption of "
                                        f"{label} (\"{best[1]['text'][:60]}\"), {t['kind']} {kinds[label][1]}",
                                        "likely", f"{t['kind']}~\\ref{{{label}}}"))
            elif len(same) == 1:
                findings.append(finding("crossrefs", "number typed by hand", src, start, end,
                                        f"{t['kind']} {number} is right today; \\ref{{{same[0]}}} keeps it right if "
                                        f"the {kind}s move", "likely", f"{t['kind']}~\\ref{{{same[0]}}}"))
        elif best:
            label = best[1]["label"]
            findings.append(finding("crossrefs", f"wrong {kind} number", src, start, end,
                                    f"there is no {t['kind']} {t['number']} ({kind}s: {listed}); the sentence "
                                    f"matches the caption of {label} (\"{best[1]['text'][:60]}\"), printed as "
                                    f"{t['kind']} {kinds[label][1]}", "certain", f"{t['kind']}~\\ref{{{label}}}"))
        else:
            findings.append(finding("crossrefs", f"{kind} that does not exist", src, start, end,
                                    f"there is no {t['kind']} {t['number']}: the {kind}s are numbered {listed}",
                                    "needs a person"))

    # \section[short]{long} and \caption[short]{long}: the table of contents (or list of figures) says something else
    for h in found["headings"] + found["captions"]:
        if h.get("short") and short_title_differs(h["short"], h.get("title", h.get("text", ""))):
            src, long_ = sources[h["file"]], h.get("title", h.get("text", ""))
            command = src.text[h["pos"]:h["end"]]
            name = re.match(r"\\[A-Za-z]+\*?", command).group(0)
            where = "table of contents" if "title" in h else "list of " + (h["kind"] + "s" if h["kind"] else "figures")
            findings.append(finding("crossrefs", f"{where} title differs", src, h["pos"], h["end"],
                                    f"the {where} says \"{h['short']}\", the page says \"{long_}\"", "likely",
                                    f"{name}{{{long_}}}", words=1))

    # numbers forced by hand
    for name, src in sources.items():
        for m in re.finditer(r"\\(?:setcounter|addtocounter)\s*\{(figure|table|section|chapter|equation)\}", src.clean):
            findings.append(finding("crossrefs", "counter set by hand", src, m.start(), m.end(),
                                    f"the {m.group(1)} numbers are forced: a reference typed against them breaks when "
                                    "the document changes", "likely"))
    return findings


def audit_build(facts, found):
    """The build's own complaints: errors (missing files, undefined commands) and overfull boxes."""
    findings, sources = [], found["sources"]
    for e in facts["errors"]:
        src = sources.get(e["file"]) or sources[found["files"][0]]
        pos = src.starts[e["line"] - 1] if e["line"] and e["file"] in sources and e["line"] <= len(src.starts) else 0
        row = finding("build", "build error", src, pos, pos, e["message"], "needs a person", words=6)
        if not e["line"]:
            row["line"], row["evidence"] = None, e["message"]
        findings.append(row)
    for box in facts["overfull"]:
        src = sources.get(box["file"])
        if src and box["line"] <= len(src.starts):
            pos = src.starts[box["line"] - 1]
            findings.append(finding("build", "overfull box", src, pos, pos,
                                    f"a line sticks {box['pt']:.1f} pt into the margin on page {box['page']}",
                                    "likely", words=8))
    return findings


# ---- AUDIT: TEXT -------------------------------------------------------------------------------------------------------
BREAK = "\x01"  # in the plain text: where maths, a citation or another command stood. No word runs across it.
TRANSPARENT = {"emph", "textbf", "textit", "textsl", "textsc", "textup", "textrm", "textsf", "texttt", "underline",
               "uline", "mbox", "text", "textnormal"}  # their argument is part of the sentence around them
OWN_TEXT = set(HEADINGS) | {"caption", "captionof", "footnote", "title", "author", "item", "thanks", "date"}
MATH_ENVS = {"equation", "align", "gather", "multline", "eqnarray", "displaymath", "math", "flalign", "alignat",
             "split", "dmath"}
SKIP_ENVS = {"verbatim", "lstlisting", "minted", "comment", "tikzpicture", "filecontents", "thebibliography",
             "ganttchart", "axis", "forest", "circuitikz"}  # code, drawings and charts: not prose
# Environments whose [options] and {arguments} are not text ("\begin{ganttchart}[hgrid, vgrid]")
ENVIRONMENT_ARGS = {"ganttchart": "[{{", "tikzpicture": "[", "axis": "[", "tabularx": "{{", "tabulary": "{{",
                    "longtable": "[{", "enumerate": "[", "itemize": "[", "description": "[", "wrapfigure": "[{[{",
                    "minipage": "[[[{", "tcolorbox": "[", "subfigure": "[{", "multicols": "{", "figure": "[",
                    "table": "[", "figure*": "[", "table*": "[", "lstlisting": "["}
SPECIALS = {"~": " ", "``": "\u201c", "''": "\u201d", "--": "\u2013", "---": "\u2014"}
LITERAL = {"%": "%", "&": "&", "_": "_", "#": "#", "$": "$", "{": "{", "}": "}", ",": " ", ";": " ", ":": " ",
           " ": " ", "!": "", "-": "", "/": "", "@": "", "ldots": "\u2026", "dots": "\u2026", "textellipsis": "\u2026"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
LIKELY_DOUBLED = {"that", "had", "is"}  # "that that" and "had had" can be right


def latex_context(found, tree):
    """pylatexenc's table of commands and their arguments, plus the ones it doesn't know (\\caption, natbib's
    \\citep, \\cref) and the project's own \\newcommand's, so their arguments aren't read as words."""
    from pylatexenc.latexwalker import get_default_latex_context_db
    from pylatexenc.macrospec import EnvironmentSpec, MacroSpec
    specs = {"caption": "*[{", "captionof": "*{[{", "graphicspath": "{", "url": "{", "href": "{{", "SI": "[{{",
             "si": "[{", "num": "[{", "qty": "[{{", "hyperref": "[{", "setcounter": "{{", "addtocounter": "{{",
             "addbibresource": "[{", "bibliography": "{", "bibliographystyle": "{", "subfile": "{"}
    specs.update({c: "*[[{" for c in CITES})
    specs.update({r: "*{" for r in REFS})
    texts = [s.clean for s in found["sources"].values()] + [strip_comments(read_tex(p)) for p in Path(tree).rglob("*.sty")]
    for text in texts:  # \newcommand{\units}[1]{...} -> \units takes one argument
        for m in re.finditer(r"\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z@]+)\}?\s*(?:\[(\d)\])?\s*(\[[^\]]*\])?", text):
            n = int(m.group(2) or 0)
            specs.setdefault(m.group(1), ("[" if m.group(3) else "") + "{" * (n - (1 if m.group(3) else 0)))
    db = get_default_latex_context_db().filter_context()
    db.add_context_category("harness", prepend=True, macros=[MacroSpec(name, spec) for name, spec in specs.items()],
                            environments=[EnvironmentSpec(name, spec) for name, spec in ENVIRONMENT_ARGS.items()])
    return db


def to_plain(src, context):
    """
    A .tex file -> (plain text, where): the words between the commands, and for each character of the plain text
    its position in the file. Maths and commands such as \\cite become BREAK; the argument of \\emph stays part of the
    sentence; "~" is a space. Every finding made on the plain text is mapped back to the file through `where`.
    """
    from pylatexenc.latex2text import LatexNodes2Text
    from pylatexenc.latexwalker import (LatexCharsNode, LatexEnvironmentNode, LatexGroupNode, LatexMacroNode,
                                        LatexMathNode, LatexSpecialsNode, LatexWalker)
    chars, where, accents = [], [], LatexNodes2Text()

    def put(text, pos, step=1):
        for i, c in enumerate(text):
            chars.append(c)
            where.append(pos + i * step)

    def walk(nodes):
        for n in nodes or []:
            if n is None:
                continue
            if isinstance(n, LatexCharsNode):
                put(n.chars, n.pos)
            elif isinstance(n, LatexGroupNode):
                walk(n.nodelist)
            elif isinstance(n, LatexSpecialsNode):
                put(SPECIALS.get(n.specials_chars, BREAK), n.pos, 0)
            elif isinstance(n, LatexMathNode):
                put(BREAK, n.pos)
            elif isinstance(n, LatexEnvironmentNode):
                name = n.environmentname.rstrip("*")
                put(BREAK, n.pos)
                if name not in MATH_ENVS and name not in SKIP_ENVS:
                    walk(n.nodelist)
                    put(BREAK, n.pos + n.len - 1)
            elif isinstance(n, LatexMacroNode):
                name, args = n.macroname, (n.nodeargd.argnlist if n.nodeargd else []) or []
                if name in LITERAL:
                    put(LITERAL[name], n.pos, 0)
                elif len(name) == 1 or (name in "uvHckrbdt" and args):  # \'e, \"o, \c{c}: one accented letter
                    put(accents.nodelist_to_text([n]) or BREAK, n.pos, 0)
                elif name in TRANSPARENT:
                    walk([a for a in args if isinstance(a, LatexGroupNode)])
                elif name in OWN_TEXT:
                    for a in args:
                        if isinstance(a, LatexGroupNode):
                            put(BREAK, a.pos)
                            walk(a.nodelist)
                    put(BREAK, n.pos + n.len - 1)
                else:
                    put(BREAK, n.pos)

    nodes, _, _ = LatexWalker(src.clean, latex_context=context, tolerant_parsing=True).get_latex_nodes(pos=0)
    walk(nodes)
    return "".join(chars), where


def span(where, text, start, end):
    """A match in the plain text -> its (start, end) in the file."""
    return where[start], where[end - 1] + 1


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def project_words(found, tree, bibs_text):
    """Words the spell checker must accept: every word in the .bib, the macro names, and any .wordlist file."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]{2,}", bibs_text)}
    words |= {m.lower() for m in found["macros"]}
    for path in list(Path(tree).rglob("*.wordlist")) + list(Path(tree).rglob(".wordlist")):
        words |= {w.strip().lower() for w in read_tex(path).split() if w.strip()}
    return words


COMMON = 1000  # times a word occurs in pyspellchecker's word counts: "rail" 7,405, "strut" 1,847, "subproblem" 50
SUFFIXES = (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"), ("ly", ""),
            ("ers", ""), ("er", ""), ("ized", "ize"), ("izes", "ize"), ("izing", "ize"), ("ization", "ize"),
            ("ize", ""), ("ized", ""), ("able", ""), ("ability", "able"))
WORD_PREFIXES = ("re", "un", "sub", "pre", "non", "over", "under", "multi", "co", "inter", "post", "semi", "anti", "de",
            "mis", "out", "cross", "micro", "macro", "meta", "self", "super", "auto")


def is_a_word(word, spell, depth=0):
    """
    A word the dictionary lacks but builds from words it has: an inflection or a prefix of a known word
    ("deliverables", "chemistries", "repurposed", "submodels") or two common words joined ("railcar", "workflow",
    "wildland", "runtime"). "struture" is none of these.
    """
    if depth and spell.known([word]):
        return True
    if depth > 1:
        return False
    roots = {word[:-len(s)] + r for s, r in SUFFIXES if word.endswith(s) and len(word) - len(s) >= 3}
    roots |= {word[len(p):] for p in WORD_PREFIXES if word.startswith(p) and len(word) - len(p) >= 5}  # not un+till
    if any(is_a_word(root, spell, depth + 1) for root in roots):
        return True
    freq = spell.word_frequency
    return any(freq[word[:i]] >= COMMON and freq[word[i:]] >= COMMON for i in range(3, len(word) - 2)
               if word[:i] in freq and word[i:] in freq)


def audit_text(found, tree):
    from spellchecker import SpellChecker
    findings, sources = [], found["sources"]
    bibs_text = " ".join(read_tex(Path(tree) / b) for b in found["bibliographies"] if (Path(tree) / b).is_file())
    known = project_words(found, tree, bibs_text)
    spell = SpellChecker(language=LATEX["language"])
    spell.word_frequency.load_words(known)
    context = latex_context(found, tree)
    plains = {name: to_plain(src, context) for name, src in sources.items()}
    uses = Counter(w for plain, _ in plains.values() for w in re.findall(r"[a-z]{4,}", plain))

    def add(check, src, plain, where, m, why, confidence, new=None, group=0):
        start, end = span(where, plain, m.start(group), m.end(group))
        findings.append(finding("text", check, src, start, end, why, confidence, new))

    for name, src in sources.items():
        plain, where = plains[name]
        # doubled words, across line breaks and \emph{}
        for m in re.finditer(r"\b([A-Za-z]+)\s+(\1)\b", plain, re.I):
            # the second word goes, with the space before it; a closing brace between them stays:
            # "\emph{very} very hot" -> "\emph{very} hot". Across an opening command, a person decides.
            start, end = where[m.start(1)], where[m.end(2) - 1] + 1
            first_end, second = where[m.end(1) - 1] + 1, where[m.start(2)]
            between = src.text[first_end:second]
            new = src.text[start:first_end] + between.strip() if re.fullmatch(r"[\s}]*", between) else None
            confidence = "likely" if m.group(1).lower() in LIKELY_DOUBLED or new is None else "certain"
            findings.append(finding("text", "doubled word", src, start, end, f"\"{m.group(1)}\" twice in a row",
                                    confidence, new))
        # misspellings: lower-case words only (a capitalised word may be a name), 4 letters or more. A dictionary
        # misses many real words ("deliverables", "railcar", "workflow"), so a word it lacks is a misspelling only
        # if it is used once and isn't made of words it has (is_a_word). The rest are listed once, for a person.
        unknown = {}
        for m in re.finditer(r"(?<![\w'\x01\\])([a-z]{4,})(?![\w'\x01])", plain):
            word = m.group(1)
            if word in known or not spell.unknown([word]):
                continue
            candidates = sorted(c for c in spell.candidates(word) or [] if "'" not in c)
            if uses[word] == 1 and len(candidates) == 1 and spell.word_frequency[candidates[0]] >= COMMON:
                # one common word one letter away, for a word used once: "struture", "occured"
                add("misspelling", src, plain, where, m, f"\"{word}\" is not in the dictionary; the only word one "
                    f"letter away is \"{candidates[0]}\"", "certain", candidates[0])
            elif is_a_word(word, spell):
                continue
            elif uses[word] > 1 or not candidates:
                unknown.setdefault(word, m)
            else:
                best = spell.correction(word) if len(candidates) > 1 else candidates[0]
                add("misspelling", src, plain, where, m, f"\"{word}\" is not in the dictionary; perhaps "
                    f"\"{best}\" ({', '.join(candidates[:4])})", "likely", best)
        if unknown:
            first = min(unknown.values(), key=lambda m: m.start())
            add("words not in the dictionary", src, plain, where, first,
                ", ".join(f"{w} ({uses[w]}x)" for w in sorted(unknown)) + ": not in the dictionary, but used more "
                "than once or with no word near them, so probably right (add them to .wordlist)", "likely")
        # punctuation
        for pattern, check, new in ((r"(?<![.\u2026])\.\.(?![.])", "double full stop", "."),
                                    (r",,", "double comma", ","),
                                    (r"(?<=\w) +(?=[,;:](?!\d))", "space before punctuation", "")):
            for m in re.finditer(pattern, plain):
                add(check, src, plain, where, m, f"\"{m.group(0)}\" in running text", "certain", new)
        # dates: "January 8. 2025", "January 8,2025", "1//2025"
        for m in re.finditer(rf"\b({'|'.join(MONTHS)})\s+(\d{{1,2}})(\.\s*|,(?=\d))(\d{{4}})\b", plain):
            add("date", src, plain, where, m, "a date is written 'Month day, year'", "certain",
                f"{m.group(1)} {m.group(2)}, {m.group(4)}")
        for m in re.finditer(r"\b\d{1,4}//\d{1,4}\b", plain):
            add("date", src, plain, where, m, f"\"{m.group(0)}\" is not a date anyone can read for sure",
                "needs a person")
        # quotes: "word" types two closing quotes in LaTeX
        for m in re.finditer(r'"([^"\n\x01]{1,80})"', plain):
            start, end = span(where, plain, m.start(), m.end())
            if src.text[start] == '"' and src.text[end - 1] == '"':
                findings.append(finding("text", "straight quotes", src, start, end,
                                        "LaTeX prints \" as a closing quote: write ``...''", "certain",
                                        f"``{src.text[start + 1:end - 1]}''"))
    # a citation or reference run into the next word: \cite{x}can
    for c in found["cites"] + found["refs"]:
        src = sources[c["file"]]
        m = re.match(r"[A-Za-z]+", src.clean[c["end"]:c["end"] + 30])
        if m:
            command = src.text[c["pos"]:c["end"]]
            findings.append(finding("text", "no space after a citation" if c in found["cites"] else
                                    "no space after a reference", src, c["pos"], c["end"] + m.end(),
                                    f"\"{command}{m.group(0)}\": the next word runs into the citation", "certain",
                                    f"{command} {m.group(0)}"))
    findings += variant_spellings(sources, plains, spell)
    return findings


def variant_spellings(sources, plains, spell):
    """The same name spelled two ways ("Castellamare" once, "Castellammare" twice): capitalised words one or two
    letters apart that the dictionary doesn't know. The rarer form is likely the mistake."""
    seen, places = Counter(), {}
    for name, (plain, where) in plains.items():
        for m in re.finditer(r"(?<![\w\x01])([A-Z][a-z]{4,})(?![\w\x01])", plain):
            seen[m.group(1)] += 1
            places.setdefault(m.group(1), []).append((name, m, where))
    findings = []
    for rare in seen:
        for common in seen:
            if rare == common or seen[rare] >= seen[common] or common.startswith(rare) or rare.startswith(common):
                continue  # plurals and possessives start with the other
            if spell.known([rare.lower()]) and spell.known([common.lower()]):
                continue  # two real words ("Model", "Modes")
            if edit_distance(rare, common) <= (1 if len(common) < 8 else 2):
                for name, m, where in places[rare]:
                    src = sources[name]
                    start, end = where[m.start(1)], where[m.end(1) - 1] + 1
                    findings.append(finding("text", "name spelled two ways", src, start, end,
                                            f"\"{rare}\" here, \"{common}\" {seen[common]} time(s) elsewhere",
                                            "likely", common))
    return findings


# ---- JUDGE: the LLM looks at each likely finding once -------------------------------------------------------------
JUDGE_SYSTEM = ("You check one suspected error in a scientific paper written in LaTeX. Reply with exactly two lines. "
                "Line 1: yes if it is an error, or no if it is not. Line 2: the passage as it should read.")


def squash(text):
    return re.sub(r"\s+", "", text).lower()


def judge(findings, ctx):
    """
    JUDGE: one short prompt per 'likely' finding that has a fix: the passage, the suspicion, the proposed correction.
    Plain Python reads the reply. A 'yes' makes the finding certain only if the LLM's corrected passage is the
    proposed one; a 'no' rejects it (it stays in the ledger, marked rejected). The LLM can't add a finding or a fix.
    """
    agent = getattr(ctx, "agent", None)
    asked = [f for f in findings if f["confidence"] == "likely" and f["fix"]]
    if not asked or agent is None:
        return {"asked": 0}
    t, counts = ctx.tracer, Counter()
    t.banner(f"JUDGE: the LLM looks at {len(asked)} likely finding(s), one short prompt each")
    for k, f in enumerate(asked, 1):
        before = f["evidence"]
        old, new = " ".join(f["fix"]["old"].split()), " ".join(f["fix"]["new"].split())
        after = before.replace(old, new, 1) if old in before else new
        question = (f"Passage: {before}\nSuspected error: {f['why']}\nProposed correction: {after}\n"
                    "Is it an error?")
        start = time.perf_counter()
        with t.nested(f"JUDGE {k}/{len(asked)}"):
            reply = agent.llm.generate([{"role": "system", "content": JUDGE_SYSTEM},
                                        {"role": "user", "content": question}], None, 120, 0.0)
        with t.stage("JUDGE", "python", "plain Python reads the LLM's yes/no (no ML)", "the LLM's reply",
                     "the finding's confidence") as ev:
            lines = [x.strip() for x in reply.strip().splitlines() if x.strip()]
            answer = re.match(r"\W*(yes|no)\b", lines[0], re.I) if lines else None
            answer = answer.group(1).lower() if answer else "unclear"
            wrote = re.sub(r"^(?:line 2:|corrected:|correction:)\s*", "", lines[1], flags=re.I) if len(lines) > 1 else ""
            if answer == "yes" and squash(wrote.strip("\"'`")) == squash(after):
                f["confidence"], verdict = "certain", "confirmed: yes, and the same correction"
            elif answer == "no":
                f["status"], verdict = "rejected", "rejected: the LLM says it is not an error"
            else:
                verdict = ("still likely: yes, but another correction" if answer == "yes"
                           else "still likely: no clear yes or no")
            counts[verdict.split(":")[0]] += 1
            f["judge"] = {"answer": answer, "wrote": wrote[:200], "verdict": verdict,
                          "seconds": round(time.perf_counter() - start, 1)}
            ev.summary = f"{f['file']}:{f['line']} {f['check']}: {verdict}"
            ev.details = {"question": question, "reply": reply}
    return {"asked": len(asked), **counts}


# ---- LEDGER ----------------------------------------------------------------------------------------------------------
def number_findings(findings, order):
    rank = {name: i for i, name in enumerate(order)}
    findings.sort(key=lambda f: (CATEGORIES.index(f["category"]), rank.get(f["file"], len(rank)), f["line"] or 0))
    for i, f in enumerate(findings, 1):
        f["id"] = f"A{i:03d}"
    return findings


def one_line(f):
    """A007 crossrefs sections/results.tex:16 "as Figure 3 shows" -> Figure~\\ref{fig:hrr} (certain)"""
    fix = f" -> {f['fix']['new']}" if f["fix"] else ""
    status = f", {f['status']}" if f["status"] not in ("found",) else ""
    return (f"{f['id']} {f['category']} {f['file']}:{f['line']} \"{f['evidence'][:70]}\"{fix} "
            f"({f['confidence']}{status}): {f['why'][:110]}")


def counts_of(findings):
    live = [f for f in findings if f["status"] != "rejected"]
    by = Counter(f["confidence"] for f in live)
    rejected = len(findings) - len(live)
    return (f"{len(live)} finding(s): {by['certain']} certain, {by['likely']} likely, {by['needs a person']} need a "
            f"person" + (f"; {rejected} rejected by JUDGE" if rejected else ""))


def ledger_slug(project):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(project)).strip("_") or "project"


def write_ledger(ledger, run_dir):
    """ledger.json (for the tools), audit_report.md (for a person), ledger.csv (for a spreadsheet), and the latest
    ledger of the project in outputs/ledgers/, where fix_latex finds it in a later request."""
    run_dir = Path(run_dir)
    paths = {"ledger": save_json(run_dir / "ledger.json", ledger),
             "report": run_dir / "audit_report.md", "csv": run_dir / "ledger.csv"}
    save_json(LEDGERS_DIR / f"{ledger_slug(ledger['project'])}.json", ledger)
    findings = ledger["findings"]
    lines = [f"# Audit of {ledger['project']}", "",
             f"Audited {ledger['audited']} by the glass-box harness. Build: {ledger['build']}.", "",
             f"**{counts_of(findings)}.**", "",
             "| Category | certain | likely | needs a person | rejected |", "|---|---|---|---|---|"]
    for cat in CATEGORIES:
        rows = [f for f in findings if f["category"] == cat]
        if rows:
            live = Counter(f["confidence"] for f in rows if f["status"] != "rejected")
            lines.append(f"| {cat} | {live['certain']} | {live['likely']} | {live['needs a person']} | "
                         f"{sum(f['status'] == 'rejected' for f in rows)} |")
    for cat in CATEGORIES:
        rows = [f for f in findings if f["category"] == cat]
        if not rows:
            continue
        lines += ["", f"## {cat}", ""]
        for f in rows:
            fix = f" Fix: `{f['fix']['old']}` -> `{f['fix']['new']}`." if f["fix"] else ""
            judged = f" JUDGE: {f['judge']['verdict']}." if f["judge"] else ""
            status = "" if f["status"] == "found" else f" **{f['status']}**"
            lines.append(f"- **{f['id']}** `{f['file']}:{f['line']}` ({f['confidence']}){status} {f['check']}: "
                         f"\"{f['evidence']}\". {f['why']}.{fix}{judged}".replace("..", "."))
    paths["report"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(paths["csv"], "w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(["id", "category", "check", "file", "line", "confidence", "status", "evidence", "why", "old",
                      "new", "judge"])
        for f in findings:
            out.writerow([f["id"], f["category"], f["check"], f["file"], f["line"], f["confidence"], f["status"],
                          f["evidence"], f["why"], (f["fix"] or {}).get("old", ""), (f["fix"] or {}).get("new", ""),
                          (f["judge"] or {}).get("verdict", "")])
    return paths


def audit(root, main, ctx, checks=CHECKS, use_judge=None, name="original", save=True):
    """The whole audit on a copy of the project -> (ledger, paths, build facts, the scan). fix_latex re-audits with
    it (save=False: nothing is written, and no JUDGE)."""
    tree = copy_project(root, ctx, name)
    copy_main = tree / main.relative_to(root)
    facts = build(tree, copy_main, ctx)
    with ctx.tracer.stage("SCAN", "python", "a balanced-brace scanner + the .aux (no ML)", "the .tex files + .aux",
                          "a map of the project") as ev:
        found = scan(tree, copy_main)
        aux = read_aux(copy_main)
        ev.summary = scan_summary(found)
    findings = audit_build(facts, found)
    for check, (stage, component, auditor) in {
            "references": ("AUDIT: REFERENCES", "bibtexparser + the scan (no ML)",
                           lambda: audit_references(found, tree, ctx)),
            "crossrefs": ("AUDIT: CROSS-REFERENCES", "the .aux + the scan (no ML)",
                          lambda: audit_crossrefs(found, aux, facts)),
            "text": ("AUDIT: TEXT", "pylatexenc + pyspellchecker + regular expressions (no ML)",
                     lambda: audit_text(found, tree))}.items():
        if check not in checks:
            continue
        with ctx.tracer.stage(stage, "python", component, "the scan", "findings") as ev:
            rows = auditor()
            by = Counter(f["confidence"] for f in rows)
            ev.summary = f"{len(rows)} finding(s): " + ", ".join(f"{n} {c}" for c, n in by.items()) if rows else "none"
            ev.details = {"findings": [f"{f['file']}:{f['line']} {f['check']}: {f['evidence']}" for f in rows]}
            findings += rows
    number_findings(findings, found["files"] + found["bibliographies"])
    use_judge = save and (LATEX["judge"] if use_judge is None else use_judge)
    judged = judge(findings, ctx) if use_judge else {"asked": 0}
    ledger = {"project": rel(root), "root": str(root), "main": main.relative_to(root).as_posix(),
              "audited": datetime.now().isoformat(timespec="minutes"), "run_dir": str(ctx.run_dir),
              "copy": str(tree), "engine": facts["engine"], "pages": facts["pages"], "build": build_summary(facts),
              "pdf": facts["pdf"], "checks": list(checks), "judge": judged, "findings": findings}
    if not save:
        return ledger, {}, facts, found
    with ctx.tracer.stage("LEDGER", "python", "json + csv (no ML)", f"{len(findings)} findings",
                          "ledger.json, audit_report.md, ledger.csv") as ev:
        paths = write_ledger(ledger, ctx.run_dir)
        ctx.facts.setdefault("latex_ledgers", {})[str(root)] = ledger  # fix_latex and make_latexdiff use it
        ev.summary = counts_of(findings) + f" -> {rel(paths['ledger'])}"
        ev.artifacts += [str(p) for p in paths.values()]
    return ledger, paths, facts, found


def last_ledger(root, ctx):
    """The project's ledger: this request's, else the latest saved one (outputs/ledgers/<project>.json), or None."""
    if str(root) in ctx.facts.get("latex_ledgers", {}):
        return ctx.facts["latex_ledgers"][str(root)]
    path = LEDGERS_DIR / f"{ledger_slug(rel(root))}.json"
    if path.is_file():
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ctx.facts.setdefault("latex_ledgers", {})[str(root)] = ledger
        return ledger
    return None


class AuditLatexTool(Tool):
    name = "audit_latex"
    description = ("Audit a LaTeX paper (its folder or main .tex): references, cross-references and figure numbers, "
                   "spelling and punctuation. Writes a ledger of findings, each with a file, line, evidence and a fix "
                   "where one follows from the facts. Works on a copy.")
    parameters = {"type": "object",
                  "properties": {**PROJECT_PARAMETERS["properties"],
                                 "checks": {"type": "array", "items": {"type": "string", "enum": list(CHECKS)},
                                            "description": "Which audits to run (default: all three)"}},
                  "required": ["path"]}
    model_id = "latexmk + bibtexparser + pylatexenc + pyspellchecker (no ML); the LLM as JUDGE"
    check_unused = False
    how_it_works = ("Almost no AI: latexmk builds a copy; Python reads the .log, the .aux and the .bib, scans the "
                    "source, and runs the three auditors. The LLM is asked only about the 'likely' findings, one "
                    "short question each (JUDGE), and can only confirm or reject them.")

    def validate(self, args):
        args = super().validate(args)
        checks = [c for c in args.get("checks") or [] if c in CHECKS]
        args["checks"] = checks or list(CHECKS)
        return args

    def run(self, ctx, path, checks=CHECKS):
        root, main = latex_project(path, ctx)
        ctx.tracer.say(self.how_it_works, kind="python")
        ledger, paths, facts, _ = audit(root, main, ctx, checks)
        findings = ledger["findings"]
        live = [f for f in findings if f["status"] != "rejected"]
        # the counts in the tool's own words: a 3B model summarizing 14 findings gets some of them wrong
        ctx.notes.append(f"audit_latex on {rel(root)}: {counts_of(findings)}. Build: {build_summary(facts)}. "
                         f"Every finding is in {rel(paths['report'])}.")
        artifacts = [{"type": "ledger", "path": paths["report"], "note": "the audit, grouped by category"},
                     {"type": "ledger", "path": paths["ledger"], "note": "every finding (fix_latex reads it)"}]
        if facts["pdf"]:
            artifacts.append({"type": "latex_pdf", "path": publish(facts["pdf"], ctx, root, "original"),
                              "note": "the original, as built"})
        return {"artifacts": artifacts,
                "observations": {"project": rel(root), "build": build_summary(facts), "summary": counts_of(findings),
                                 "ledger": [one_line(f) for f in live][:14],
                                 "more": max(0, len(live) - 14),
                                 "next": "fix_latex applies the certain fixes; make_latexdiff then marks them up"}}
