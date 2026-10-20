"""
make_latexdiff: a PDF that marks every change between the original paper and its corrected copy, for peer review.

  CHANGED FILES  plain Python compares the original folder with the corrected copy (fix_latex or edit_tex made it)
  LATEXDIFF      latexdiff (a Perl program that comes with TeX Live) on each changed .tex file, one at a time (on a big
                 project, --flatten fails), and always on the main file: latexdiff writes the markup's definitions
                 into its preamble, and its \\input's pick up the other diffed files
  BUILD DIFF     latexmk on the diff. A file whose diff won't compile is diffed again with --math-markup=0, then the
                 whole diff with -t CFONT (a change of font instead of underlining); failing that, the file goes in
                 unmarked, and is named. Never silently.
  NOT SHOWN      changes a marked-up text can't show: a .bib field, the preamble, a table-of-contents title

Deleted text is struck through in red, added text underlined in blue. Nothing here is AI.
"""

import shutil
import subprocess
from pathlib import Path

from ..config import LATEX
from .base import Tool
from .files import rel
from .latex_audit import last_ledger, squash
from .latex_project import (BUILD_PRODUCTS, PROJECT_PARAMETERS, build, build_summary, copy_project, latex_project,
                            publish, read_tex)

# Whole words: by default latexdiff marks a one-letter change inside a word (Castellam[m]are), which a reviewer misses
WORDS = "--config=MAXCHANGESLETTER=0"
ATTEMPTS = (  # (what, the files it applies to, latexdiff options)
    ("underline", "all", ["--type=UNDERLINE", WORDS]),
    ("no maths markup", "failing", ["--type=UNDERLINE", WORDS, "--math-markup=0"]),
    ("font change (CFONT)", "all", ["--type=CFONT", WORDS, "--math-markup=0"]),
)
COMPARED = (".tex", ".bib", ".sty", ".cls", ".bst")


def project_files(tree):
    """The files of a project that a diff can compare, by relative path (build products left out)."""
    skip = [p for pattern in BUILD_PRODUCTS for p in Path(tree).rglob(pattern)]
    return {p.relative_to(tree).as_posix(): p for p in Path(tree).rglob("*")
            if p.is_file() and p.suffix.lower() in COMPARED and p not in skip}


def same_text(a, b):
    return read_tex(a).replace("\r\n", "\n") == read_tex(b).replace("\r\n", "\n")


def latexdiff(old, new, out, options):
    """latexdiff old new > out -> how many pieces it marked as added and deleted."""
    try:
        proc = subprocess.run(["latexdiff", "--encoding=utf8", *options, str(old), str(new)], capture_output=True,
                              timeout=LATEX["timeout"])
    except FileNotFoundError:
        raise RuntimeError("latexdiff is not on the PATH: it comes with TeX Live (https://tug.org/texlive/)")
    if proc.returncode != 0 or not proc.stdout:
        message = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError(f"latexdiff failed on {Path(new).name}: {message[-1] if message else 'no output'}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(proc.stdout)
    text = proc.stdout.decode("utf-8", errors="replace")
    return {"added": text.count("\\DIFadd{") + text.count("\\DIFaddFL{"),
            "deleted": text.count("\\DIFdel{") + text.count("\\DIFdelFL{")}


def not_shown(ledger, changed_other, main_name, corrected):
    """Changes the marked-up PDF can't show, so a reviewer is told about them in words."""
    notes = [f"{name} changed: a diff marks text, so the change shows only in its result (e.g. the reference list)"
             for name in changed_other]
    begin = None
    main = Path(corrected) / main_name
    if main.is_file():
        begin = read_tex(main).count("\n", 0, max(0, read_tex(main).find("\\begin{document}"))) + 1
    for f in ledger.get("findings", []):
        if f["status"] != "verified":
            continue
        if not f["file"].endswith(".tex"):
            notes.append(f"{f['id']} {f['file']}:{f['line']}: {f['fix']['old']} -> {f['fix']['new']} (not a .tex file)")
        elif f["file"] == main_name and begin and f["line"] < begin:
            notes.append(f"{f['id']} {f['file']}:{f['line']}: a change in the preamble")
        elif squash(f["fix"]["old"]) == squash(f["fix"]["new"]):
            notes.append(f"{f['id']} {f['file']}:{f['line']}: only spacing changes ({f['check']}), which a diff "
                         "can't mark")
        elif "title differs" in f["check"]:
            notes.append(f"{f['id']} {f['file']}:{f['line']}: only the {f['check'].replace(' title differs', '')} "
                         "changes; the heading on the page is the same")
    return notes


class MakeLatexdiffTool(Tool):
    name = "make_latexdiff"
    description = ("Make a latexdiff PDF of a LaTeX project for review: the original against its corrected copy "
                   "(made by fix_latex or edit_tex), deletions struck through in red, additions underlined in blue.")
    parameters = PROJECT_PARAMETERS
    model_id = "latexdiff + latexmk (no ML)"
    requires = ("fix_latex", "edit_tex")  # whichever of them the router offered makes the corrected copy first
    how_it_works = ("No model here: latexdiff compares each changed .tex file with the original and writes a copy "
                    "with the changes marked; latexmk builds it. If a marked-up file won't compile, simpler markup is "
                    "tried, and as a last resort the file goes in unmarked, and is named.")

    def run(self, ctx, path):
        t = ctx.tracer
        root, main = latex_project(path, ctx)
        ledger = last_ledger(root, ctx) or {}
        corrected = ctx.facts.get("latex_corrected", {}).get(str(root)) or (ledger.get("corrected") or {}).get("tree")
        if not corrected or not Path(corrected).is_dir():
            raise ValueError(f"There is no corrected copy of {rel(root)} yet: call fix_latex (or edit_tex) first, "
                             "then make_latexdiff.")
        corrected, main_name = Path(corrected), main.relative_to(root).as_posix()
        t.say(self.how_it_works, kind="python")
        with t.stage("CHANGED FILES", "python", "filecmp of two folders (no ML)", "the original + the corrected copy",
                     "the files that differ") as ev:
            old, new = project_files(root), project_files(corrected)
            changed = sorted(n for n in new if n not in old or not same_text(old[n], new[n]))
            removed = sorted(n for n in old if n not in new)
            changed_tex = [n for n in changed if n.endswith(".tex")]
            changed_other = [n for n in changed if not n.endswith(".tex")] + [f"{n} (removed)" for n in removed]
            ev.summary = (f"{len(changed)} changed file(s): {', '.join(changed) or 'none'}"
                          + (f"; removed: {', '.join(removed)}" if removed else ""))
        if not changed:
            raise ValueError(f"The corrected copy of {rel(root)} is the same as the original: nothing to mark up.")
        diff_tree = copy_project(corrected, ctx, "diff")
        diffed = sorted(set(changed_tex) | {main_name})  # the main file always: its preamble defines the markup
        options = {name: ATTEMPTS[0][2] for name in diffed}
        counts, tried, unmarked, failing = {}, [], [], []
        empty = Path(ctx.run_dir) / "latex" / "empty.tex"
        empty.write_text("", encoding="utf-8")
        for attempt, (what, scope, opts) in enumerate(ATTEMPTS):
            if attempt:
                for name in (failing if scope == "failing" else diffed):
                    options[name] = opts
            with t.stage("LATEXDIFF", "server", "latexdiff (Perl, from TeX Live; no ML)",
                         f"{len(diffed)} file(s), {what}", "marked-up .tex files") as ev:
                for name in diffed if attempt == 0 or scope == "all" else failing:
                    counts[name] = latexdiff(old.get(name, empty), new[name], diff_tree / name, options[name])
                ev.summary = "; ".join(f"{n}: +{c['added']} -{c['deleted']}" for n, c in counts.items())
            facts = build(diff_tree, diff_tree / main_name, ctx, label="BUILD DIFF")
            tried.append(what)
            failing = sorted({e["file"] for e in facts["errors"] if e["file"] in diffed})
            if facts["pdf"] and not failing:
                break
            if not failing:  # no PDF, and no error names a file: any diffed file may be to blame
                failing = diffed
        else:
            with t.stage("UNMARKED", "python", "shutil.copy (no ML)", f"{len(failing)} file(s) whose diff won't build",
                         "the corrected file, without markup") as ev:
                if main_name in failing:  # the main file unmarked, but still with the markup's definitions
                    latexdiff(new[main_name], new[main_name], diff_tree / main_name, ATTEMPTS[0][2])
                for name in failing:
                    if name != main_name:
                        shutil.copyfile(new[name], diff_tree / name)
                unmarked = failing
                ev.summary = f"included without markup: {', '.join(unmarked)}"
            facts = build(diff_tree, diff_tree / main_name, ctx, label="BUILD DIFF")
            tried.append("unmarked")
        if not facts["pdf"]:
            raise RuntimeError(f"The diff of {rel(root)} did not build: {build_summary(facts)}")
        pdf = publish(facts["pdf"], ctx, root, "latexdiff")
        notes = not_shown(ledger, changed_other, main_name, corrected)
        ctx.facts.setdefault("latex_diffs", {})[str(root)] = str(pdf)
        ctx.notes.append(f"make_latexdiff: {rel(pdf)} marks {len(changed_tex)} changed .tex file(s)"
                         + (f"; shown without markup: {', '.join(unmarked)}" if unmarked else "")
                         + (f"; not visible in it: {len(notes)} change(s), listed in the tool's result" if notes
                            else "") + ".")
        return {"artifacts": [{"type": "latex_pdf", "path": pdf,
                               "note": "deleted text struck through in red, added text underlined in blue"}],
                "observations": {
                    "project": rel(root), "diff_pdf": rel(pdf),
                    "changed_files": {n: f"{counts[n]['added']} added, {counts[n]['deleted']} deleted"
                                      for n in changed_tex if n in counts},
                    "markup": tried[-1], "attempts": tried,
                    "unmarked": [f"{n}: its marked-up version would not build, so it is shown without markup"
                                 for n in unmarked],
                    "not_shown": notes[:10], "build": build_summary(facts)}}
