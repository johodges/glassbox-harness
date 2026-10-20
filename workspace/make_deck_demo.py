"""
Builds workspace/deck_demo/: three decks for read_deck, diff_decks, edit_deck and check_deck
(harness/tools/deck_edit.py, OFFICE_COLLAB_DESIGN.md P8). Synthetic content: a briefing on a series of compartment
fire tests.

    briefing.pptx          8 slides on templates/workshop_template.pptx, with speaker notes, a picture, a table, and a
                           logo and footer rule on its slide master. Two problems are planted for check_deck: the text
                           on slide 5 is too long for its box, and the picture on slide 6 covers the logo
    briefing_edited.pptx   briefing.pptx as its author saved it after six edits, for diff_decks: a slide moved, a slide
                           deleted, a bullet reworded with a bold word, new speaker notes, a picture swapped, a box
                           resized
    results.pptx           3 slides in the plain Office look (4:3): a title, bullets, and a native chart, to import

The answer key is deck_demo_answers.json at the project root, outside workspace/, so no tool can show it to the LLM;
compare_decks.py scores the tools against it. The harness never changes these files: every edit is a new file in
outputs/run_.../. Run this again any time to get fresh copies:

    python workspace/make_deck_demo.py
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.util import Cm, Pt

HERE = Path(__file__).resolve().parent
OUT = HERE / "deck_demo"
TEMPLATE = HERE / "templates" / "workshop_template.pptx"
PLOT = HERE / "images" / "hrr_curves.png"
TEAL = "0F4C5C"
LOGO = (29.4, 17.5, 2.8, 1.2)  # cm: inside the logo zone of template_profiles/workshop_template.json
RULE_Y = 17.25                 # cm: the footer rule; content stays above it


def _png(image):
    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    return stream


def _font(size, bold=False):
    try:
        return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def logo_png():
    """A made-up logo: 'FIRE LAB' in white on a teal badge."""
    image = Image.new("RGBA", (336, 144), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, 335, 143], radius=24, fill="#" + TEAL)
    draw.polygon([(40, 112), (58, 40), (70, 76), (80, 30), (100, 112)], fill="#f28c28")  # a flame
    draw.text((214, 72), "FIRE\nLAB", fill="white", font=_font(46, True), anchor="mm", align="center")
    return image


def neutral_author(prs):
    """python-pptx's built-in template names its developer as every deck's author; a demo in a public repository
    names no one."""
    prs.core_properties.author = prs.core_properties.last_modified_by = "Glass-box harness demo"


def emu(value_cm):
    return int(Cm(value_cm))


def add_logo_and_rule(prs):
    """The logo and the footer rule go on the slide master, so every slide shows them (python-pptx can add pictures
    to slides only, so the two shapes are written as XML)."""
    master = prs.slide_master
    tree = master.shapes._spTree
    next_id = max(int(i) for i in tree.xpath(".//p:cNvPr/@id")) + 1
    _, rid = master.part.get_or_add_image_part(_png(logo_png()))
    x, y, w, h = (emu(v) for v in LOGO)
    left, right = emu(1.69), emu(32.17)
    tree.append(parse_xml(
        f'<p:cxnSp {nsdecls("p", "a")}><p:nvCxnSpPr><p:cNvPr id="{next_id}" name="Footer rule"/><p:cNvCxnSpPr/>'
        f'<p:nvPr userDrawn="1"/></p:nvCxnSpPr><p:spPr><a:xfrm><a:off x="{left}" y="{emu(RULE_Y)}"/>'
        f'<a:ext cx="{right - left}" cy="0"/></a:xfrm><a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
        f'<a:ln w="12700"><a:solidFill><a:srgbClr val="{TEAL}"/></a:solidFill></a:ln></p:spPr></p:cxnSp>'))
    tree.append(parse_xml(
        f'<p:pic {nsdecls("p", "a", "r")}><p:nvPicPr><p:cNvPr id="{next_id + 1}" name="Workshop logo" '
        f'descr="Fire lab logo"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr userDrawn="1"/>'
        f'</p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm><a:prstGeom prst="rect">'
        f'<a:avLst/></a:prstGeom></p:spPr></p:pic>'))


def bullets(frame, items, sizes=(24, 20)):
    """items: text, or (level, text), where text is a string or [(words, bold), ...]. Sized down from the template's
    32 pt so the slides fit, as people do by hand."""
    for i, item in enumerate(items):
        level, runs = item if isinstance(item, tuple) else (0, item)
        runs = [(runs, False)] if isinstance(runs, str) else runs
        p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        p.level = level
        for words, bold in runs:
            run = p.add_run()
            run.text = words
            run.font.size = Pt(sizes[min(level, 1)])
            if bold:
                run.font.bold = True


def content_slide(prs, title, items, notes=None):
    slide = prs.slides.add_slide(layout(prs, "Title and Content"))
    slide.shapes.title.text = title
    bullets(slide.placeholders[1].text_frame, items)
    if notes:
        slide.notes_slide.notes_text_frame.text = notes
    return slide


def layout(prs, name):
    return next(item for item in prs.slide_layouts if item.name == name)


def body(slide):
    return next(ph for ph in slide.placeholders if ph.placeholder_format.idx == 1)


def make_briefing(path):
    prs = Presentation(TEMPLATE)
    add_logo_and_rule(prs)

    s = prs.slides.add_slide(layout(prs, "Title Slide"))
    s.shapes.title.text = "Compartment fire tests"
    s.placeholders[1].text_frame.text = "Briefing for the fire safety workshop"
    s.placeholders[1].text_frame.add_paragraph().text = "October 2026"
    s.notes_slide.notes_text_frame.text = ("Welcome. Fifteen minutes on the test series: the set-up, what we saw, and "
                                           "what comes next.")

    content_slide(prs, "Test programme", [
        "Six tests in a 3.6 m x 2.4 m x 2.4 m compartment",
        "Fuel: wood cribs (tests 1-4) and a sofa mock-up (tests 5-6)",
        "Door opening 0.8 m x 2.0 m, the only vent",
        "Measured: heat release rate, gas temperatures and heat flux"],
        "The compartment follows the ISO 9705 room geometry.")
    content_slide(prs, "Instrumentation", [
        "Oxygen consumption calorimetry in the exhaust hood",
        "Three thermocouple trees, ten heights each",
        "Heat flux gauges on the floor, 1 m and 2 m from the door"],
        "Slide 7 has the full table of peak values.")  # stale once slide 7 is deleted or moved
    content_slide(prs, "Results so far", [
        [("We have completed ", False), ("12 tests", True), (" of the 16 planned", False)],
        "Flashover in every wood crib test, after 4 to 7 minutes",
        "Peak heat release rate up to 2.4 MW"],
        "Two tests were repeated after a leak in the hood.")
    # PLANTED: seven long bullets at 24 pt, in a box that holds about five of them
    content_slide(prs, "Observations", [
        "The hot layer reached the floor of the doorway about 40 seconds before flashover in every crib test",
        "Flames came out of the door 10 to 20 seconds after the gas temperature under the ceiling passed 600 °C",
        "The sofa mock-up grew faster than any crib, close to the ultrafast t-squared curve for its first two minutes",
        "Heat flux at the floor 1 m from the door was about twice the flux at 2 m throughout the growth phase",
        "Smoke filled the room to the door soffit within a minute in the sofa tests, long before the cribs did",
        "Repeat tests agreed within 10 % on peak heat release rate and within 30 seconds on the time to flashover",
        "The exhaust hood captured all of the smoke at every stage, so the calorimetry needs no correction"])

    s = prs.slides.add_slide(layout(prs, "Title Only"))
    s.shapes.title.text = "Design fire curves"
    height = 13.4  # PLANTED: the picture's bottom-right corner covers the logo (it reaches 31.0 cm x 18.0 cm)
    width = height * 960 / 600
    s.shapes.add_picture(str(PLOT), Cm(31.0 - width), Cm(4.6), Cm(width), Cm(height))
    s.notes_slide.notes_text_frame.text = "The ultrafast curve is closest to the sofa mock-up."

    s = prs.slides.add_slide(layout(prs, "Title Only"))
    s.shapes.title.text = "Peak values by test"
    rows = [("Test", "Fuel", "Peak heat release rate (MW)", "Time to flashover (s)"),
            ("1", "Wood crib", "1.8", "290"), ("2", "Wood crib", "2.1", "265"), ("3", "Wood crib", "1.9", "310"),
            ("4", "Wood crib", "2.0", "275"), ("5", "Sofa mock-up", "2.4", "150"), ("6", "Sofa mock-up", "2.3", "165")]
    table = s.shapes.add_table(len(rows), 4, Cm(1.69), Cm(4.6), Cm(30.48), Cm(0.9 * len(rows))).table
    for col, w in enumerate((3.5, 8.0, 10.0, 8.98)):
        table.columns[col].width = Cm(w)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
            for run in table.cell(r, c).text_frame.paragraphs[0].runs:
                run.font.size = Pt(16)
    s.notes_slide.notes_text_frame.text = "Values are one-second averages."

    content_slide(prs, "Next steps", [
        "Four more tests with a 0.4 m door",
        "Compare the results with FDS models of the room",
        "Report to the workshop in December 2026"],
        "Questions and discussion.")
    neutral_author(prs)
    prs.save(path)


def grey(path):
    """The same plot in grey, as an author might swap in for printing: same size, other bytes."""
    return _png(Image.open(path).convert("L").convert("RGB"))


def make_edited(source, path):
    """The six edits a person made in PowerPoint, made here with python-pptx (which renumbers the slide parts on save,
    as PowerPoint does)."""
    prs = Presentation(source)
    slides = list(prs.slides)
    by_title = {s.shapes.title.text_frame.text: s for s in slides}

    # 1. a reworded bullet with a bold word
    p = body(by_title["Test programme"]).text_frame.paragraphs[2]
    for r in list(p.runs):
        r._r.getparent().remove(r._r)
    for words, bold in (("One door, 0.8 m wide and 2.0 m high, as the ", False), ("only", True), (" vent", False)):
        run = p.add_run()
        run.text = words
        run.font.size = Pt(24)
        if bold:
            run.font.bold = True
    # 2. new speaker notes where there were none
    by_title["Observations"].notes_slide.notes_text_frame.text = ("Read the smoke layer height from the video, not "
                                                                   "from the thermocouples.")
    # 3. the picture swapped for another file
    picture = next(sh for sh in by_title["Design fire curves"].shapes if sh.shape_type == 13)
    blip = picture._element.blipFill.find(qn("a:blip"))
    old = blip.get(qn("r:embed"))
    _, rid = by_title["Design fire curves"].part.get_or_add_image_part(grey(PLOT))
    blip.set(qn("r:embed"), rid)
    by_title["Design fire curves"].part.drop_rel(old)
    # 4. a box resized (narrower, to leave room on the right)
    box = body(by_title["Results so far"])
    left, top, height = box.left, box.top, box.height
    box.left, box.top, box.width, box.height = left, top, Cm(22.0), height
    # 5. a slide moved: Instrumentation after Observations
    ids = prs.slides._sldIdLst
    entries = {int(e.get("id")): e for e in ids}
    moved = entries[by_title["Instrumentation"].slide_id]
    ids.remove(moved)
    entries[by_title["Observations"].slide_id].addnext(moved)
    # 6. a slide deleted: Peak values by test
    gone = entries[by_title["Peak values by test"].slide_id]
    ids.remove(gone)
    prs.part.drop_rel(gone.get(qn("r:id")))
    neutral_author(prs)
    prs.save(path)


def make_results(path):
    prs = Presentation()  # python-pptx's built-in template: 4:3, the plain Office look
    s = prs.slides.add_slide(layout(prs, "Title Slide"))
    s.shapes.title.text = "Test series B"
    s.placeholders[1].text_frame.text = "Results with a 0.4 m door"

    s = prs.slides.add_slide(layout(prs, "Title and Content"))
    s.shapes.title.text = "Peak heat release rates, series B"
    bullets(s.placeholders[1].text_frame, [
        [("Test B1, wood crib: ", False), ("1.1 MW", True)],
        "Test B2, wood crib: 1.3 MW",
        "Test B3, sofa mock-up: 1.9 MW",
        "The narrow door slowed the growth to flashover"])
    s.notes_slide.notes_text_frame.text = "Peak values are one-second averages, as in series A."

    s = prs.slides.add_slide(layout(prs, "Title Only"))
    s.shapes.title.text = "Time to flashover, series B"
    data = CategoryChartData()
    data.categories = ["B1", "B2", "B3"]
    data.add_series("Time to flashover (s)", (412, 388, 245))
    s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Cm(2.5), Cm(4.5), Cm(20.4), Cm(12.5), data)
    s.notes_slide.notes_text_frame.text = "Test B3 reached flashover first: the sofa burns faster than the cribs."
    neutral_author(prs)
    prs.save(path)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    make_briefing(OUT / "briefing.pptx")
    make_edited(OUT / "briefing.pptx", OUT / "briefing_edited.pptx")
    make_results(OUT / "results.pptx")
    print(f"wrote briefing.pptx, briefing_edited.pptx and results.pptx in {OUT}")
