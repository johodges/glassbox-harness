"""
Plain-Python readers: every kind of document -> text the LLM can read. No ML in here.

A .pptx or .docx is a ZIP of XML files and a PDF is a page-description language, so the LLM
can't read any of them directly. These functions are the "perception" for documents: cheap,
exact parsers instead of models.
"""

import re
from pathlib import Path

TEXT_TYPES = {".md", ".txt", ".tex", ".py", ".csv", ".json", ".bib"}
READABLE = TEXT_TYPES | {".pptx", ".docx", ".pdf", ".xlsx"}


def pptx_slides(path):
    """-> [{"slide": 1, "title": "...", "bullets": [...], "notes": "..."}, ...]"""
    from pptx import Presentation
    slides = []
    for number, slide in enumerate(Presentation(path).slides, 1):
        title = slide.shapes.title
        bullets = []
        for shape in slide.shapes:
            if shape.has_text_frame and (title is None or shape.shape_id != title.shape_id):
                bullets += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
        slides.append({"slide": number, "title": title.text_frame.text if title is not None else "",
                       "bullets": bullets, "notes": notes})
    return slides


def heading_level(paragraph):
    """Word marks headings with paragraph styles: 'Title' -> 0, 'Heading 2' -> 2, body text -> None."""
    name = paragraph.style.name if paragraph.style is not None else ""
    if name == "Title":
        return 0
    if name.startswith("Heading "):
        try:
            return int(name.split()[-1])
        except ValueError:
            return None
    return None


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REVISION_TAGS = (W + "ins", W + "del", W + "moveFrom", W + "moveTo", W + "rPrChange", W + "pPrChange")


def accepted_text(element):
    """
    A paragraph's text as it reads with every tracked change accepted: inserted text in, deleted text out.
    python-docx's paragraph.text reads only runs directly under the paragraph, so text inside a tracked insertion
    (w:ins) is invisible to it, and the LLM would never see a reviewer's additions.
    """
    out = []
    for el in element.iter(W + "t", W + "tab", W + "br", W + "cr"):
        if any(a.tag in (W + "del", W + "moveFrom") for a in el.iterancestors()):
            continue
        out.append((el.text or "") if el.tag == W + "t" else "\t" if el.tag == W + "tab" else "\n")
    return "".join(out)


def review_marks(elements):
    """Tracked changes and comment anchors under some paragraphs -> {"revisions": n, "comments": n, "authors": [...]}."""
    revisions, comments, authors = 0, set(), set()
    for element in elements:
        for el in element.iter(*REVISION_TAGS):
            revisions += 1
            if el.get(W + "author"):
                authors.add(el.get(W + "author"))
        for el in element.iter(W + "commentRangeStart", W + "commentReference"):
            comments.add(el.get(W + "id"))
    return {"revisions": revisions, "comments": len(comments), "authors": sorted(authors)}


def docx_sections(path):
    """-> [{"heading": "Results", "level": 1, "paragraphs": [...]}, ...], with tracked changes accepted."""
    from docx import Document
    sections = [{"heading": "", "level": 0, "paragraphs": []}]
    for p in Document(path).paragraphs:
        level, text = heading_level(p), accepted_text(p._p)
        if level is not None:
            sections.append({"heading": text, "level": level, "paragraphs": []})
        elif text.strip():
            sections[-1]["paragraphs"].append(text)
    return [s for s in sections if s["heading"] or s["paragraphs"]]


def docx_review_note(path):
    """'(12 tracked changes and 6 comments by A. Reviewer, B. Checker: the text above has the changes accepted)', or
    '' for a document nobody has reviewed."""
    from docx import Document
    doc = Document(path)
    marks = review_marks([doc.element.body])
    if not marks["revisions"] and not marks["comments"]:
        return ""
    authors = set(marks["authors"]) | {c.author for c in doc.comments if c.author}
    who = f" by {', '.join(sorted(authors))}" if authors else ""
    return (f"({marks['revisions']} tracked change(s) and {marks['comments']} comment(s){who}: the text here has the "
            "changes accepted; read_review shows each change and comment)")


def xlsx_text(path, max_rows=60):
    """Every sheet, row by row: each non-empty cell with its address, and for a formula the value Excel last
    calculated (None if no program has calculated it yet: openpyxl stores formulas, it doesn't evaluate them)."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    formulas, values = load_workbook(path), load_workbook(path, data_only=True)
    out = []
    for ws in formulas.worksheets:
        used = sorted({c.column for row in ws.iter_rows() for c in row if c.value not in (None, "")})
        free = next(i for i in range(1, len(used) + 2) if i not in used)  # a small LLM shouldn't guess this
        headers = [f"{c.value} ({c.column_letter})" for c in ws[1]
                   if isinstance(c.value, str) and re.fullmatch(r"[A-Za-z_]\w*", c.value.strip())]
        out.append(f"Sheet '{ws.title}': {ws.max_row} rows x {ws.max_column} columns; columns in use: "
                   f"{', '.join(get_column_letter(i) for i in used) or 'none'}; first empty column: "
                   f"{get_column_letter(free)}; column headers (use them in formulas): {', '.join(headers) or 'none'}")
        for row in ws.iter_rows(max_row=min(ws.max_row, max_rows)):
            cells = [f"{c.coordinate}={c.value}" + (f" -> {values[ws.title][c.coordinate].value}"
                                                     if isinstance(c.value, str) and c.value.startswith("=") else "")
                     for c in row if c.value not in (None, "")]
            if cells:
                out.append("  " + " | ".join(cells))
        if ws.max_row > max_rows:
            out.append(f"  ... rows {max_rows + 1} to {ws.max_row} not shown")
    names = [f"{name} = {dn.attr_text}" for name, dn in formulas.defined_names.items()]
    if names:  # a named cell can be used in formulas by its name, and doesn't move when a formula is filled down
        out.append("Named cells (use the name in formulas): " + "; ".join(names))
    return "\n".join(out)


def pdf_text(path):
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def as_text(path, numbered=False):
    """Any supported file -> one string. numbered=True puts line numbers on text files."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pptx":
        out = []
        for s in pptx_slides(path):
            out.append(f"Slide {s['slide']}: {s['title']}")
            out += [f"  - {b}" for b in s["bullets"]]
            if s["notes"]:
                out.append(f"  speaker notes: {s['notes']}")
        return "\n".join(out)
    if ext == ".docx":
        out = [note] if (note := docx_review_note(path)) else []
        for s in docx_sections(path):
            if s["heading"]:
                out.append("#" * max(1, s["level"]) + " " + s["heading"])
            out += s["paragraphs"]
        return "\n".join(out)
    if ext == ".pdf":
        return pdf_text(path)
    if ext == ".xlsx":
        return xlsx_text(path)
    if ext in TEXT_TYPES:
        text = path.read_text(encoding="utf-8", errors="replace")
        if numbered:
            return "\n".join(f"{i:>3}| {line}" for i, line in enumerate(text.splitlines(), 1))
        return text
    raise ValueError(f"cannot read {ext or 'this kind of'} file. Readable types: {', '.join(sorted(READABLE))}")


def read_text_keep_newlines(path):
    """-> (text with \\n line ends, the file's own line ending), so an edit doesn't rewrite every line."""
    raw = Path(path).read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def write_text_keep_newlines(path, text, newline):
    Path(path).write_bytes(text.replace("\n", newline).encode("utf-8"))


def split_passages(text, max_words=120, min_words=15):
    """
    Cut a document into passages of roughly paragraph size, so search can return just the part
    that answers a question. Plain string handling: blank lines, then sentences if a block is long.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    blocks = [re.sub(r"^#+ .*\n?", "", b).strip() for b in blocks]  # drop markdown heading lines
    passages, pending = [], ""
    for block in filter(None, blocks):
        block = " ".join(block.split())
        if pending:
            block, pending = pending + " " + block, ""
        if len(block.split()) < min_words:
            pending = block  # too short to stand alone: glue it to the next block
            continue
        if len(block.split()) <= max_words:
            passages.append(block)
            continue
        current = []
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            if current and len(" ".join(current + [sentence]).split()) > max_words:
                passages.append(" ".join(current))
                current = []
            current.append(sentence)
        if current:
            passages.append(" ".join(current))
    if pending:
        passages.append(pending)
    return passages
