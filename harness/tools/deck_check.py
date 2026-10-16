"""
Checking a deck without eyes. Llama 3.2 3B can't see a slide, so the harness checks the layout in code
and tells the LLM what it found, as text. Two ways to ask "does this text fit its box?":

    estimate   plain Python: follow PowerPoint's style inheritance (paragraph -> shape -> layout ->
               master) to find each paragraph's font and size, measure every word with that font's real
               letter widths (PIL reads the .ttf file), wrap the lines, add up their heights and compare
               the total with the box. Needs nothing but the file, and it is only an estimate.
    measure    PowerPoint itself, through COM (Windows only): it lays the text out and reports its height
               (TextRange.BoundHeight). Exact. The same connection draws every slide as a PNG and saves a
               deck as PDF. Without PowerPoint, LibreOffice (soffice --headless) can still make the PDF.

No neural network anywhere in this file.
"""

import functools
import math
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

EMU_PER_PT = 12700
LINE = 1.2  # PowerPoint's single line spacing is about 1.2 x the font size
FONT_FILES = {"calibri light": "calibril", "arial narrow": "arialn", "segoe ui": "segoeui",
              "times new roman": "times", "courier new": "cour"}  # family -> .ttf name, where it isn't obvious


# ---- the estimate ----------------------------------------------------------------------------------
def _chain(shape):
    """The XML elements a shape's text styles come from: the shape, the layout placeholder it inherits from,
    and the master placeholder above that (a text box that is not a placeholder inherits from nothing)."""
    elements, current = [shape._element], shape
    for _ in range(2):
        try:
            current = current._base_placeholder
        except (AttributeError, NotImplementedError):
            break
        if current is None:
            break
        elements.append(current._element)
    return elements


def _master(shape):
    return shape.part.slide_layout.slide_master


def _text_style(shape):
    """Which of the master's three text styles applies: titles, other placeholders, or plain text boxes."""
    if not shape.is_placeholder:
        return "otherStyle"
    return "titleStyle" if shape.placeholder_format.type in (1, 3) else "bodyStyle"  # 1 TITLE, 3 CENTER_TITLE


def style_value(shape, level, path, paragraph=None):
    """
    The first value of `path` (an XPath below <a:lvlNpPr>) on the way up the inheritance chain, in the
    order PowerPoint looks: the paragraph itself, the shape, its layout placeholder, the master placeholder,
    then the master's text styles. None if nobody sets it.
    """
    tag = f"a:lvl{level + 1}pPr"
    if paragraph is not None:
        found = paragraph._p.xpath(f"./a:pPr/{path}")
        if found:
            return found[0]
    for element in _chain(shape):
        found = element.xpath(f"./p:txBody/a:lstStyle/{tag}/{path}")
        if found:
            return found[0]
    found = _master(shape)._element.xpath(f"./p:txStyles/p:{_text_style(shape)}/{tag}/{path}")
    return found[0] if found else None


BULLETS = "*[self::a:buNone or self::a:buChar or self::a:buAutoNum or self::a:buBlip]"


def bulleted(shape, level, paragraph=None):
    """Does this level of this shape show a bullet? Templates disagree: the Office theme bullets every level,
    ULRI's first level is a plain paragraph."""
    found = style_value(shape, level, BULLETS, paragraph)
    return found is not None and not found.tag.endswith("buNone")


@functools.lru_cache(maxsize=None)
def _theme_fonts(master_part):
    from lxml import etree
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    theme = etree.fromstring(master_part.part_related_by(RT.THEME).blob)
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    major = theme.xpath("//a:majorFont/a:latin/@typeface", namespaces=ns)
    minor = theme.xpath("//a:minorFont/a:latin/@typeface", namespaces=ns)
    return {"+mj-lt": major[0] if major else "Arial", "+mn-lt": minor[0] if minor else "Arial"}


@functools.lru_cache(maxsize=None)
def _ttf(face, bold):
    """The font file behind a family name, loaded at 100 pt so widths scale to any size. None if not installed."""
    base = FONT_FILES.get(face.lower(), face.lower().replace(" ", ""))
    for name in ([base + "bd.ttf", base + "b.ttf"] if bold else []) + [base + ".ttf"]:
        try:
            return ImageFont.truetype(name, 100)
        except OSError:
            continue
    return None


def _wrapped_lines(text, width, measure):
    """Greedy word wrap, as PowerPoint does it: how many lines does this text take in this width (points)?"""
    lines, current, space = 1, 0.0, measure(" ")
    for word in text.split():
        w = measure(word)
        if current and current + space + w > width:
            lines, current = lines + 1, w
        else:
            current += (space if current else 0) + w
        if w > width:  # one word wider than the box breaks over several lines
            lines += math.ceil(w / width) - 1
    return lines


def _spacing(shape, level, tag, paragraph, size):
    points = style_value(shape, level, f"a:{tag}/a:spcPts/@val", paragraph)
    if points is not None:
        return int(points) / 100
    percent = style_value(shape, level, f"a:{tag}/a:spcPct/@val", paragraph)
    return int(percent) / 100000 * size if percent is not None else 0.0


def _inset(shape, side, default):
    for element in _chain(shape):
        found = element.xpath(f"./p:txBody/a:bodyPr/@{side}")
        if found:
            return int(found[0])
    return default


def text_fit(shape, font_scale=None, line_reduction=None):
    """
    How full is this text box? 1.0 = exactly full, 1.25 = the text needs 25% more height than the box has.
    font_scale / line_reduction try out a shrink (PowerPoint's "shrink text on overflow") before applying it.
    None if the shape has no text or no size.
    """
    if not shape.has_text_frame or not shape.text_frame.text.strip() or not shape.width or not shape.height:
        return None
    room_w = (shape.width - _inset(shape, "lIns", 91440) - _inset(shape, "rIns", 91440)) / EMU_PER_PT
    room_h = (shape.height - _inset(shape, "tIns", 45720) - _inset(shape, "bIns", 45720)) / EMU_PER_PT
    autofit = shape.text_frame._txBody.bodyPr.xpath("./a:normAutofit")
    if font_scale is None:
        font_scale = int(autofit[0].get("fontScale", "100000")) / 100000 if autofit else 1.0
    if line_reduction is None:
        line_reduction = int(autofit[0].get("lnSpcReduction", "0")) / 100000 if autofit else 0.0
    fonts = _theme_fonts(_master(shape).part)
    default_face = "+mj-lt" if _text_style(shape) == "titleStyle" else "+mn-lt"
    needed = 0.0
    for i, p in enumerate(shape.text_frame.paragraphs):
        level = p.level
        sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
        size = (max(sizes) if sizes else int(style_value(shape, level, "a:defRPr/@sz") or 1800) / 100) * font_scale
        face = next((r.font.name for r in p.runs if r.font.name), None) \
            or style_value(shape, level, "a:defRPr/a:latin/@typeface") or default_face
        face = fonts.get(face, face)
        bold = any(r.font.bold for r in p.runs) or style_value(shape, level, "a:defRPr/@b") in ("1", "true")
        font = _ttf(face, bold)
        measure = (lambda s, f=font, z=size: f.getlength(s) * z / 100) if font else (lambda s, z=size: len(s) * z * 0.5)
        indent = int(style_value(shape, level, "@marL", p) or 0) / EMU_PER_PT
        line_pct = int(style_value(shape, level, "a:lnSpc/a:spcPct/@val", p) or 100000) / 100000
        lines = sum(_wrapped_lines(part, room_w - indent, measure) for part in p.text.split("\v"))
        before = _spacing(shape, level, "spcBef", p, size) if i else 0.0  # no space above the first paragraph
        needed += before + lines * size * LINE * line_pct * (1 - line_reduction) + _spacing(shape, level, "spcAft", p, size)
    return needed / room_h if room_h > 0 else None


def check_deck(path):
    """Read a saved deck back: each slide's layout, title and text, and how full each text box looks."""
    from pptx import Presentation
    from pptx.shapes.picture import Picture
    slides = []
    for number, slide in enumerate(Presentation(path).slides, 1):
        title = slide.shapes.title
        text, fit, pictures, tables = [], {}, 0, 0
        for shape in slide.shapes:
            pictures += isinstance(shape, Picture)
            tables += bool(getattr(shape, "has_table", False))
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            if shape.is_placeholder and shape.placeholder_format.type == 13:  # SLIDE_NUMBER
                continue
            if title is None or shape.shape_id != title.shape_id:
                text += [p.text.replace("\v", " ") for p in shape.text_frame.paragraphs if p.text.strip()]
            ratio = text_fit(shape)
            if ratio is not None:
                fit[shape.shape_id] = (shape.name, round(ratio, 2))
        slides.append({"slide": number, "layout": slide.slide_layout.name.strip(),
                       "title": title.text_frame.text.replace("\v", " ") if title is not None else "",
                       "text": text, "pictures": pictures, "tables": tables, "fit": fit})
    return slides


# ---- PowerPoint and LibreOffice --------------------------------------------------------------------
def powerpoint_available():
    """Windows, pywin32 installed, and PowerPoint registered as a COM server."""
    if os.name != "nt":
        return False
    try:
        import win32com.client  # noqa: F401  (pywin32)
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"))
        return True
    except (ImportError, OSError):
        return False


@contextmanager
def powerpoint():
    """PowerPoint as a COM server: another program on this PC that Python drives through an API. If it was
    not running before, it is closed again afterwards (a PowerPoint you have open is left alone)."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        win32com.client.GetActiveObject("PowerPoint.Application")
        was_running = True
    except Exception:
        was_running = False
    app = win32com.client.Dispatch("PowerPoint.Application")
    try:
        yield app
    finally:
        if not was_running and app.Presentations.Count == 0:
            app.Quit()


def render_deck(path, folder, width=1280):
    """
    PowerPoint opens the deck read-only and without a window, draws every slide as a PNG, and measures every
    text box: the height of its laid-out text / the height inside the box.
    -> ([png paths], {(slide number, shape id): ratio})
    """
    folder.mkdir(exist_ok=True)
    pngs, measured = [], {}
    with powerpoint() as app:
        pres = app.Presentations.Open(str(path), True, False, False)  # read-only, not untitled, no window
        try:
            height = round(width * pres.PageSetup.SlideHeight / pres.PageSetup.SlideWidth)
            for slide in pres.Slides:
                png = folder / f"slide_{slide.SlideIndex:02d}.png"
                slide.Export(str(png), "PNG", width, height)
                pngs.append(png)
                for shape in slide.Shapes:
                    if shape.HasTextFrame and shape.TextFrame.HasText:
                        frame = shape.TextFrame
                        room = shape.Height - frame.MarginTop - frame.MarginBottom
                        if room > 0:
                            measured[(slide.SlideIndex, shape.Id)] = round(frame.TextRange.BoundHeight / room, 2)
        finally:
            pres.Close()
    return pngs, measured


def find_soffice():
    found = shutil.which("soffice") or shutil.which("libreoffice")
    for folder in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        candidate = Path(folder) / "LibreOffice" / "program" / "soffice.exe"
        if not found and folder and candidate.exists():
            found = str(candidate)
    return found


def save_as_pdf(deck, pdf):
    """Turn a deck into a PDF with whichever program is installed. -> the name of the program that did it."""
    if powerpoint_available():
        with powerpoint() as app:
            pres = app.Presentations.Open(str(deck), True, False, False)
            try:
                pres.SaveAs(str(pdf), 32)  # 32 = ppSaveAsPDF
            finally:
                pres.Close()
        return "PowerPoint (COM)"
    soffice = find_soffice()
    if soffice:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(pdf.parent), str(deck)],
                       check=True, capture_output=True, timeout=180)
        made = pdf.parent / (Path(deck).stem + ".pdf")
        if made != pdf:
            made.replace(pdf)
        return "LibreOffice (soffice --headless)"
    raise RuntimeError("Neither PowerPoint nor LibreOffice is installed here, so the deck cannot be made into a PDF.")


def contact_sheet(pngs, path, columns=3, thumb=420):
    """Every slide in one picture, for report.html (plain PIL)."""
    images = [Image.open(p).convert("RGB") for p in pngs]
    height = round(thumb * images[0].height / images[0].width)
    gap, label = 12, 22
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (gap + columns * (thumb + gap), gap + rows * (label + height + gap)), "#d9dce1")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    for i, image in enumerate(images):
        x, y = gap + (i % columns) * (thumb + gap), gap + (i // columns) * (label + height + gap)
        draw.text((x, y + 2), f"slide {i + 1}", fill="#1d1f23", font=font)
        sheet.paste(image.resize((thumb, height), Image.LANCZOS), (x, y + label))
    sheet.save(path)
    return Path(path)
