"""
PowerPoint and Word tools. No model in either: the LLM decides WHAT to write, and these
functions put its text into the right place in the file's XML (python-pptx / python-docx).
"""

import difflib

from .base import Refusal, Tool
from .documents import accepted_text, heading_level, review_marks
from .files import check_written_text, rel, resolve, working_copy


def _replace_paragraphs(text_frame, lines):
    """
    Put new lines into a text box. Each new line is a copy of the old paragraph at its place (the last one for extra
    lines): its level, bullet and first run's font size and colour stay. A plain new paragraph would fall back to the
    template's own size, e.g. 32 pt in a deck the author had set at 24 pt.
    """
    import copy
    from pptx.oxml.ns import qn
    old = [p._p for p in text_frame.paragraphs]
    parent = old[0].getparent()
    for i, line in enumerate(lines):
        p = copy.deepcopy(old[min(i, len(old) - 1)])
        runs = p.findall(qn("a:r"))
        for child in list(p):  # keep the paragraph's properties, its first run and its end mark
            if child.tag not in (qn("a:pPr"), qn("a:endParaRPr")) and not (runs and child is runs[0]):
                p.remove(child)
        if runs:
            runs[0].find(qn("a:t")).text = line
        else:
            run = p.makeelement(qn("a:r"), {})
            run.append(p.makeelement(qn("a:t"), {}))
            run[0].text = line
            end = p.find(qn("a:endParaRPr"))
            (end.addprevious if end is not None else p.append)(run)
        old[-1].addnext(p) if i == 0 else parent.findall(qn("a:p"))[-1].addnext(p)
    for p in old:
        parent.remove(p)


def closest_heading(name, options):
    """Forgiving match for a heading the LLM wrote: exact, then 'Evaluation results' -> 'Results', then fuzzy."""
    norm = lambda s: " ".join(s.lower().split())
    exact = [o for o in options if norm(o) == norm(name)]
    inside = [o for o in options if norm(o) and (norm(o) in norm(name) or norm(name) in norm(o))]
    if exact or len(inside) == 1:
        return (exact or inside)[0]
    return next(iter(difflib.get_close_matches(name, options, n=1, cutoff=0.6)), None)


# =========================================================================================
class UpdatePowerPointTool(Tool):
    name = "update_powerpoint"
    description = ("Change one slide of a PowerPoint (.pptx) file: set its title and replace its bullet points "
                   "(and, if asked, its speaker notes). slide is the slide number, 1 for the first slide; 0 adds a "
                   "new slide at the end. The edited deck is saved as a copy in the run folder.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The .pptx file, e.g. workspace/project_update.pptx"},
            "slide": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Slide number (0 = add one)."},
            "title": {"type": "string", "description": "The slide title. Leave it out to keep the current title."},
            "bullets": {"type": "array", "items": {"type": "string"},
                        "description": "The new bullet points, one short sentence each."},
            "notes": {"type": "string", "description": "Speaker notes, only if the user asked for them."},
        },
        "required": ["path", "slide", "bullets"],
    }
    model_id = "python-pptx (no ML)"
    check_unused = False
    requires = ("search_references",)  # write from the references, not from memory
    how_it_works = ("No model here: python-pptx opens the deck (a ZIP of XML files), finds the slide's title and "
                    "body placeholders, swaps in the LLM's text while keeping the formatting, and saves a copy.")

    def run(self, ctx, path, slide, bullets, title=None, notes=None):
        from pptx import Presentation
        source = resolve(path, ctx)
        if source.suffix.lower() != ".pptx":
            raise ValueError(f"update_powerpoint only edits .pptx files, not {source.name}.")
        check_written_text(bullets, ctx)
        copy = working_copy(source, ctx)
        with ctx.tracer.stage("TOOL: update_powerpoint", "python", self.model_id, "title + bullets (text)",
                              ".pptx file") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            prs = Presentation(copy)
            count = len(prs.slides)
            if slide == 0:
                layout = next((l for l in prs.slide_layouts if l.name == "Title and Content"), prs.slide_layouts[1])
                target, slide = prs.slides.add_slide(layout), count + 1
            elif 1 <= slide <= count:
                target = prs.slides[slide - 1]
            else:
                raise ValueError(f"{source.name} has {count} slides. Use a slide number from 1 to {count}, "
                                 "or 0 to add a new slide.")
            title_shape = target.shapes.title
            body = next((s for s in target.placeholders if s.has_text_frame
                         and (title_shape is None or s.shape_id != title_shape.shape_id)), None)
            if body is None:
                raise ValueError(f"slide {slide} has no text placeholder for bullet points; use slide 0 to add one.")
            old_title = title_shape.text_frame.text if title_shape is not None else ""
            if title and title_shape is not None:
                _replace_paragraphs(title_shape.text_frame, [title])
            _replace_paragraphs(body.text_frame, bullets)
            if notes:  # added to what the author wrote, never over it
                frame = target.notes_slide.notes_text_frame
                if frame.text.strip():
                    frame.add_paragraph().text = notes
                else:
                    frame.text = notes
            prs.save(copy)
            ev.summary = (f"slide {slide} of {copy.name}: title \"{title or old_title}\", {len(bullets)} bullet(s)"
                          + (", speaker notes added" if notes else ""))
            ev.artifacts.append(str(copy))
        note = f"slide {slide} updated in a copy; the original {rel(source)} is unchanged"
        return {"artifacts": [{"type": "slides", "path": copy, "slide": slide, "note": note}]}


# =========================================================================================
class UpdateWordTool(Tool):
    name = "update_word_document"
    description = ("Edit a Word (.docx) document one section at a time. mode 'replace' (the default) replaces the "
                   "paragraphs under an existing heading, such as Results; mode 'add' adds a new section with this "
                   "heading at the end. The edited document is saved as a copy in the run folder.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The .docx file, e.g. workspace/report.docx"},
            "heading": {"type": "string", "description": "The section heading, e.g. Results"},
            "paragraphs": {"type": "array", "items": {"type": "string"},
                           "description": "The new text of the section, one paragraph per item."},
            "mode": {"type": "string", "enum": ["replace", "add"], "description": "replace or add (default replace)."},
        },
        "required": ["path", "heading", "paragraphs"],
    }
    model_id = "python-docx (no ML)"
    check_unused = False
    requires = ("search_references",)
    how_it_works = ("No model here: python-docx reads the document's paragraphs and their styles. Headings are "
                    "paragraphs styled 'Heading 1', 'Heading 2', ...; a section is everything up to the next heading "
                    "of the same level. The tool swaps that section's paragraphs for the LLM's and saves a copy.")

    def run(self, ctx, path, heading, paragraphs, mode="replace"):
        from docx import Document
        from docx.oxml import OxmlElement
        from docx.text.paragraph import Paragraph
        source = resolve(path, ctx)
        if source.suffix.lower() != ".docx":
            raise ValueError(f"update_word_document only edits .docx files, not {source.name}.")
        check_written_text(paragraphs, ctx)
        copy = working_copy(source, ctx)
        with ctx.tracer.stage("TOOL: update_word_document", "python", self.model_id, "heading + paragraphs (text)",
                              ".docx file") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            doc = Document(copy)
            if mode == "add":
                doc.add_heading(heading, level=1)
                for text in paragraphs:
                    doc.add_paragraph(text)
                removed = 0
            else:
                everything = doc.paragraphs
                headings = [i for i, p in enumerate(everything) if heading_level(p)]
                texts = {i: accepted_text(everything[i]._p) for i in headings}
                match = closest_heading(heading, list(texts.values()))
                if match is None:
                    raise ValueError(f"There is no heading \"{heading}\" in {source.name}. Its headings are: "
                                     f"{', '.join(texts.values())}. Use one of them, or mode \"add\".")
                start = next(i for i in headings if texts[i] == match)
                anchor = everything[start]
                level, body = heading_level(anchor), []
                for p in everything[start + 1:]:
                    other = heading_level(p)
                    if other is not None and other <= level:
                        break
                    body.append(p)
                # Replacing the section deletes its paragraphs, and with them any reviewer's tracked changes and the
                # anchors of their comments, without a trace. A reviewed section is edited with track changes instead
                marks = review_marks([p._p for p in body])
                if marks["revisions"] or marks["comments"]:
                    who = f" by {', '.join(marks['authors'])}" if marks["authors"] else ""
                    raise Refusal(f"The section \"{match}\" has {marks['revisions']} tracked change(s) and "
                                  f"{marks['comments']} comment(s){who}. Replacing it would delete them, so it was "
                                  "not changed. Edit it with track changes (redline_docx), or add a new section.")
                style = body[0].style if body else doc.styles["Normal"]
                for p in body:
                    p._p.getparent().remove(p._p)
                for text in paragraphs:  # insert each new paragraph right after the previous one
                    element = OxmlElement("w:p")
                    anchor._p.addnext(element)
                    anchor = Paragraph(element, anchor._parent)
                    anchor.text, anchor.style = text, style
                removed, heading = len(body), match
            doc.save(copy)
            words = sum(len(t.split()) for t in paragraphs)
            ev.summary = (f"section \"{heading}\" of {copy.name}: {removed} paragraph(s) out, "
                          f"{len(paragraphs)} in ({words} words)")
            ev.artifacts.append(str(copy))
        note = f"section \"{heading}\" {'added' if mode == 'add' else 'rewritten'} in a copy; the original is unchanged"
        return {"artifacts": [{"type": "document", "path": copy, "heading": heading, "note": note}]}
