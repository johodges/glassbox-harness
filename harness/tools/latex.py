"""
The LaTeX tool: edit one \\section of a .tex file, then compile it to check the result.

Compiling is this tool's "perception": pdflatex either makes a PDF or prints errors with line
numbers, and either way the harness turns that into text for the LLM. If the LLM's LaTeX is
broken, the error goes back to it as the tool result, and it can try again.
"""

import re
import subprocess

from .base import Tool
from .documents import read_text_keep_newlines, write_text_keep_newlines
from .files import check_written_text, rel, resolve, working_copy
from .office import closest_heading

SECTION = re.compile(r"\\section\*?\{([^}]*)\}")
END = re.compile(r"\\section\*?\{|\\end\{document\}")


def escape_specials(text):
    """Small LLMs write '50%' and 'R&D' in LaTeX, where % starts a comment and & breaks tables. Fix those."""
    pattern = r"(?<!\\)([%&#])" if "$" in text else r"(?<!\\)([%&#_])"  # leave _ alone if there is maths
    return re.subn(pattern, r"\\\1", text)


class UpdateLatexTool(Tool):
    name = "update_latex"
    description = ("Edit a LaTeX (.tex) file: replace the text of one \\section{...}, such as Results, then compile "
                   "the file with pdflatex and report the result or the errors. Plain sentences are fine; the tool "
                   "escapes % & # _ for you. The edited .tex file and its PDF are saved in the run folder.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The .tex file, e.g. workspace/report.tex"},
            "section": {"type": "string", "description": "The section title, e.g. Results"},
            "text": {"type": "string", "description": "The new text of the section (sentences or LaTeX)."},
        },
        "required": ["path", "section", "text"],
    }
    model_id = "regular expressions + pdflatex (no ML)"
    check_unused = False
    requires = ("search_references",)
    how_it_works = ("No model here: a regular expression finds \\section{...} and the text up to the next section, "
                    "swaps in the LLM's text, then pdflatex (TeX Live, a separate program) compiles a PDF. Its log "
                    "says how many pages it wrote, or which line is wrong.")

    def run(self, ctx, path, section, text):
        source = resolve(path, ctx)
        if source.suffix.lower() != ".tex":
            raise ValueError(f"update_latex only edits .tex files, not {source.name}.")
        check_written_text([text], ctx)
        copy = working_copy(source, ctx)
        with ctx.tracer.stage("TOOL: update_latex", "python", "re (regular expressions, no ML)", "section text",
                              ".tex file") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            tex, newline = read_text_keep_newlines(copy)
            titles = [m.group(1) for m in SECTION.finditer(tex)]
            wanted = closest_heading(section, titles)
            match = next((m for m in SECTION.finditer(tex) if m.group(1) == wanted), None)
            if match is None:
                raise ValueError(f"There is no \\section{{{section}}} in {source.name}. Its sections are: "
                                 f"{', '.join(titles)}.")
            end = END.search(tex, match.end())
            body, escaped = escape_specials(text.strip())
            tex = tex[:match.end()] + "\n" + body + "\n\n" + tex[end.start():]
            write_text_keep_newlines(copy, tex, newline)
            ev.summary = (f"\\section{{{match.group(1)}}} of {copy.name}: {len(text.split())} words"
                          + (f", {escaped} special character(s) escaped" if escaped else ""))
        with ctx.tracer.stage("COMPILE", "python", "pdflatex (TeX Live, no ML)", ".tex file", "PDF + log") as ev:
            proc = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", copy.name],
                                  cwd=copy.parent, capture_output=True, text=True, timeout=120)
            log = copy.with_suffix(".log").read_text(encoding="utf-8", errors="replace")
            pdf = copy.with_suffix(".pdf")
            for leftover in (copy.with_suffix(".aux"), copy.with_suffix(".out")):
                leftover.unlink(missing_ok=True)
            if proc.returncode != 0:
                lines = log.splitlines()
                errors = [" ".join([lines[i].lstrip("! ")] + [l.strip() for l in lines[i + 1:i + 3] if l.startswith("l.")])
                          for i, l in enumerate(lines) if l.startswith("!")]
                ev.summary = "FAILED: " + ("; ".join(errors[:2]) or "see the .log file")
                raise ValueError(f"pdflatex could not compile {copy.name}: {'; '.join(errors[:2])}. "
                                 "Fix the text and call update_latex again.")
            pages = re.search(r"Output written on .*?\((\d+) pages?", log, re.S)
            ev.summary = f"compiled {pdf.name}: {pages.group(1) if pages else '?'} page(s), no errors"
            ev.artifacts.append(str(pdf))
        note = f"section \"{match.group(1)}\" rewritten in a copy and compiled; the original {rel(source)} is unchanged"
        return {"artifacts": [{"type": "latex", "path": copy, "note": note},
                              {"type": "pdf", "path": pdf, "section": match.group(1)}]}
