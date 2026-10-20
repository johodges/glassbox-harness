"""
deck_view: everything on every slide of a .pptx, read with lxml straight from the file (OFFICE_COLLAB_DESIGN.md, F3).

python-pptx hides the parts that matter when two people take turns on a deck: shapes inside mc:AlternateContent
(equations), positions inside groups, the text of SmartArt, pictures whose file is missing. So this module reads the
ZIP itself:

    Package      the .pptx as what it is: parts (XML files and media), the relationships between them, a content type
                 for each. The package checks of VERIFY (deck_edit.py) use it too
    deck_view    per slide: its id (p:sldId, which stays the same when slides move), position, layout, every shape
                 (name, kind, position and size in cm, text as runs with bold / italic / superscript / link marks, a
                 picture's image hash, a table's cells), the notes, and a FINGERPRINT
    fingerprint  a hash of the slide's canonical XML: attributes sorted, namespace prefixes ignored, and every r:id
                 replaced by what it points to (a picture by its bytes' hash, a link by its URL, another slide by its
                 id). Two slides with the same fingerprint look the same, whatever their part names: PowerPoint
                 renumbers slide1.xml, slide2.xml ... on every save, so part names prove nothing

No model anywhere: the LLM reads a few lines made from this view (read_deck), never the XML.
"""

import hashlib
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"p": P_NS, "a": A_NS, "r": R_NS, "mc": MC_NS,
      "a14": "http://schemas.microsoft.com/office/drawing/2010/main",
      "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
      "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
      "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"}
EMU_PER_CM = 360000
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
GRAPHIC_KINDS = {"/table": "table", "/chart": "chart", "/diagram": "SmartArt", "/ole": "OLE", "chartex": "chart"}
# Relationships that name another part of the deck's design, not content: described by name in a fingerprint
BY_NAME = {"slideLayout", "slideMaster", "notesMaster", "handoutMaster", "theme", "notesSlide", "presProps",
           "viewProps", "tableStyles"}
# Run attributes PowerPoint rewrites on every save (spelling flags, language): not formatting a person chose
VOLATILE_RPR = {"lang", "altLang", "dirty", "err", "smtClean", "smtId", "bmk", "noProof", "kumimoji"}
# Elements that say which slide this is, not what is on it: a duplicate gets its own creationId and looks the same
IDENTITY = {f"{{{NS['p14']}}}creationId"}


def q(prefix_name):
    """'a:t' -> '{http://...drawingml...}t' (lxml's Clark notation)."""
    prefix, name = prefix_name.split(":")
    return f"{{{NS[prefix]}}}{name}"


def local(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def short_hash(data, n=12):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()[:n]


# ---- the package ----------------------------------------------------------------------------------------------------
class Package:
    """A .pptx as a ZIP of parts. Part names start with '/', as in the relationships ('/ppt/slides/slide3.xml')."""

    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.blobs = {"/" + i.filename: z.read(i.filename) for i in z.infolist() if not i.is_dir()}
        self._xml, self._rels, self._digest = {}, {}, {}
        types = self.xml("/[Content_Types].xml")
        self.defaults = {d.get("Extension", "").lower(): d.get("ContentType") for d in types.findall(q("ct:Default"))}
        self.overrides = {o.get("PartName", "").lower(): o.get("ContentType") for o in types.findall(q("ct:Override"))}

    def xml(self, name):
        if name not in self._xml:
            self._xml[name] = etree.fromstring(self.blobs[name], PARSER)
        return self._xml[name]

    def content_type(self, name):
        return self.overrides.get(name.lower()) or self.defaults.get(name.rsplit(".", 1)[-1].lower())

    @staticmethod
    def rels_name(name):
        folder, base = posixpath.split(name)
        return posixpath.join(folder, "_rels", base + ".rels") if name != "/" else "/_rels/.rels"

    def rels(self, name):
        """{rId: {"type": 'slideLayout', "target": '/ppt/slideLayouts/slideLayout2.xml' or a URL, "external": bool}}"""
        if name not in self._rels:
            found = {}
            rels_name = self.rels_name(name)
            if rels_name in self.blobs:
                base = posixpath.dirname(name) if name != "/" else "/"
                for rel in self.xml(rels_name).findall(q("pr:Relationship")):
                    target, external = rel.get("Target", ""), rel.get("TargetMode") == "External"
                    if not external:
                        target = unquote(target)
                        target = posixpath.normpath(target if target.startswith("/") else posixpath.join(base, target))
                    found[rel.get("Id")] = {"type": rel.get("Type", "").rsplit("/", 1)[-1], "target": target,
                                            "external": external}
            self._rels[name] = found
        return self._rels[name]

    def related(self, name, reltype):
        """The first part `name` relates to with this type ('slideLayout'), or None."""
        return next((r["target"] for r in self.rels(name).values() if r["type"] == reltype and not r["external"]), None)

    def digest(self, name, context=None):
        """What a part holds, as a short hash: media by their bytes; XML parts by their canonical XML, with their own
        relationships resolved the same way (a chart and its embedded workbook). Part names never enter it."""
        if name not in self.blobs:
            return "missing"
        if name not in self._digest:
            self._digest[name] = "cycle"  # a part that leads back to itself
            if self.is_xml(name):
                self._digest[name] = short_hash(canonical(self.xml(name), lambda rid: self.describe_rel(name, rid,
                                                                                                         context)))
            else:
                self._digest[name] = short_hash(self.blobs[name])
        return self._digest[name]

    def is_xml(self, name):
        kind = self.content_type(name) or ""
        return kind.endswith("+xml") or kind.endswith("/xml") or name.endswith(".xml")

    def describe_rel(self, source, rid, context=None):
        """An r:id in `source`'s XML -> what it points to, independent of part names: 'image:3f2a...',
        'hyperlink:https://...', 'slide:257' (another slide, by its stable id), 'slideLayout:Title Only'."""
        rel = self.rels(source).get(rid)
        if rel is None:
            return "missing-relationship"
        if rel["external"]:
            return f"{rel['type']}:{rel['target']}"
        if rel["type"] == "slide":
            return f"slide:{(context or {}).get('slide_ids', {}).get(rel['target'], '?')}"
        if rel["type"] in BY_NAME:
            return f"{rel['type']}:{part_name_of(self, rel['target'])}"
        return f"{rel['type']}:{self.digest(rel['target'], context)}"

    def reachable(self):
        """Every part reachable from the package's own relationships: what a save keeps."""
        seen, todo = set(), ["/"]
        while todo:
            name = todo.pop()
            for rel in self.rels(name).values():
                if not rel["external"] and rel["target"] not in seen and rel["target"] in self.blobs:
                    seen.add(rel["target"])
                    todo.append(rel["target"])
        return seen


def part_name_of(package, name):
    """A layout's or master's own name (p:cSld/@name), a theme's name; else the file name."""
    if name not in package.blobs or not package.is_xml(name):
        return posixpath.basename(name)
    root = package.xml(name)
    csld = root.find(q("p:cSld"))
    if csld is not None and csld.get("name"):
        return csld.get("name").strip()
    return (root.get("name") or posixpath.basename(name)).strip()


def canonical(root, resolve=None):
    """
    One string per XML tree that stays the same when the tree means the same: namespace prefixes ignored (Clark
    names), attributes sorted, whitespace between elements dropped (text in a:t kept exactly), comments dropped, and
    every attribute in the relationships namespace (r:id, r:embed, r:link ...) replaced by resolve(value).
    """
    out = []
    keep_text = {q("a:t")}
    ignorable = f"{{{MC_NS}}}Ignorable"

    def walk(el):
        if not isinstance(el.tag, str) or el.tag in IDENTITY:  # a comment; a slide's identity, not its content
            return
        attrs = sorted((k, (resolve(v) if resolve and k.startswith(f"{{{R_NS}}}") else v))
                       for k, v in el.attrib.items() if k != ignorable)
        out.append("<" + el.tag + "".join(f' {k}="{v}"' for k, v in attrs) + ">")
        if el.text and (el.tag in keep_text or el.text.strip()):
            out.append(el.text.replace("&", "&amp;").replace("<", "&lt;"))
        for child in el:
            walk(child)
            if child.tail and child.tail.strip():
                out.append(child.tail.strip())
        out.append("</" + el.tag + ">")

    walk(root)
    return "".join(out)


def rpr_digest(rpr):
    """A run's formatting as a short hash, without the attributes PowerPoint rewrites on every save."""
    if rpr is None:
        return "-"
    copy = etree.fromstring(etree.tostring(rpr))
    for attr in VOLATILE_RPR:
        copy.attrib.pop(attr, None)
    return short_hash(canonical(copy), 8)


# ---- text ------------------------------------------------------------------------------------------------------------
def paragraph_runs(p, package=None, part=None):
    """
    One a:p -> [{"t": text, "b": True, "i": True, "sup": True, "link": url, "fmt": hash}, ...] in reading order.
    Marks are the run's OWN attributes (bold from a style the run inherits is not a mark); a line break is "\\v", a
    field (slide number, date) its current text, an equation "[equation]".
    """
    runs = []
    for child in p:
        tag = local(child)
        if tag in ("r", "fld"):
            rpr = child.find(q("a:rPr"))
            run = {"t": "".join(t.text or "" for t in child.findall(q("a:t"))), "fmt": rpr_digest(rpr)}
            if rpr is not None:
                if rpr.get("b") in ("1", "true"):
                    run["b"] = True
                if rpr.get("i") in ("1", "true"):
                    run["i"] = True
                baseline = int(rpr.get("baseline", "0") or 0)
                if baseline > 0:
                    run["sup"] = True
                elif baseline < 0:
                    run["sub"] = True
                link = rpr.find(q("a:hlinkClick"))
                if link is not None:
                    rid = link.get(f"{{{R_NS}}}id")
                    rel = package.rels(part).get(rid) if package and part and rid else None
                    run["link"] = (rel["target"] if rel and rel["external"] else
                                   link.get("action", "") or "link")
            if tag == "fld":
                run["field"] = child.get("type", "field")
            runs.append(run)
        elif tag == "br":
            runs.append({"t": "\v", "fmt": "br"})
        elif tag == "AlternateContent":  # an equation (a14:m) inside the paragraph
            runs.append({"t": "[equation]", "fmt": "math", "math": True})
    return runs


def plain(runs):
    return "".join(r["t"] for r in runs)


def marked(runs):
    """Runs -> one string with marks: **bold**, *italic*, ^superscript^, [text](link)."""
    out = []
    for r in runs:
        text = r["t"].replace("\v", " / ")
        if not text.strip():
            out.append(text)
            continue
        lead, core, trail = re.match(r"^(\s*)(.*?)(\s*)$", text, re.S).groups()
        if r.get("sup"):
            core = f"^{core}^"
        if r.get("i"):
            core = f"*{core}*"
        if r.get("b"):
            core = f"**{core}**"
        if r.get("link"):
            core = f"[{core}]({r['link']})"
        out.append(lead + core + trail)
    return re.sub(r"\*\*\*\*", "", "".join(out))  # **a****b** -> **ab**


def text_body(body, package=None, part=None):
    """A txBody -> [{"level": 0, "runs": [...]}, ...]"""
    paragraphs = []
    for p in body.findall(q("a:p")):
        ppr = p.find(q("a:pPr"))
        paragraphs.append({"level": int(ppr.get("lvl", "0")) if ppr is not None else 0,
                           "runs": paragraph_runs(p, package, part)})
    return paragraphs


def body_text(paragraphs):
    return "\n".join(plain(p["runs"]) for p in paragraphs).strip()


# ---- geometry --------------------------------------------------------------------------------------------------------
def xfrm_of(el):
    tag = local(el)
    if tag == "graphicFrame":
        return el.find(q("p:xfrm"))
    holder = el.find(q("p:grpSpPr") if tag == "grpSp" else q("p:spPr"))
    return holder.find(q("a:xfrm")) if holder is not None else None


def box_of(xfrm):
    """a:xfrm -> (x, y, w, h) in EMU, or None."""
    if xfrm is None:
        return None
    off, ext = xfrm.find(q("a:off")), xfrm.find(q("a:ext"))
    if off is None or ext is None:
        return None
    return (int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)), int(ext.get("cy", 0)))


def _ph_key(ph):
    """A placeholder's type, as the master knows it: titles to 'title', content and subtitles to 'body'."""
    kind = ph.get("type", "obj")
    return {"ctrTitle": "title", "subTitle": "body", "obj": "body"}.get(kind, kind)


def inherited_box(ph, layout, master):
    """A placeholder with no position of its own sits where its layout placeholder (same idx, else same type) or the
    master placeholder (same kind) puts it."""
    idx, kind = ph.get("idx", "0"), ph.get("type", "obj")
    for root, by_idx in ((layout, True), (master, False)):
        if root is None:
            continue
        candidates = root.findall(".//p:sp", NS)
        found = None
        for sp in candidates:
            other = sp.find("./p:nvSpPr/p:nvPr/p:ph", NS)
            if other is None:
                continue
            if by_idx and other.get("idx", "0") == idx and (idx != "0" or other.get("type", "obj") == kind):
                found = sp
                break
        if found is None:
            found = next((sp for sp in candidates if sp.find("./p:nvSpPr/p:nvPr/p:ph", NS) is not None
                          and _ph_key(sp.find("./p:nvSpPr/p:nvPr/p:ph", NS)) == _ph_key(ph)), None)
        if found is not None:
            box = box_of(xfrm_of(found))
            if box:
                return box
    return None


def _compose(transform, group):
    """A group's children are drawn in its own coordinates (chOff, chExt), mapped onto its box (off, ext)."""
    xfrm = xfrm_of(group)
    if xfrm is None:
        return transform
    off, ext = xfrm.find(q("a:off")), xfrm.find(q("a:ext"))
    ch_off, ch_ext = xfrm.find(q("a:chOff")), xfrm.find(q("a:chExt"))
    if None in (off, ext, ch_off, ch_ext):
        return transform
    sx = int(ext.get("cx")) / int(ch_ext.get("cx")) if int(ch_ext.get("cx", 0)) else 1.0
    sy = int(ext.get("cy")) / int(ch_ext.get("cy")) if int(ch_ext.get("cy", 0)) else 1.0
    ox, oy = int(off.get("x")) - int(ch_off.get("x")) * sx, int(off.get("y")) - int(ch_off.get("y")) * sy
    return lambda x, y, w, h: transform(ox + x * sx, oy + y * sy, w * sx, h * sy)


def cm(box):
    return [round(v / EMU_PER_CM, 2) for v in box] if box else None


# ---- shapes ----------------------------------------------------------------------------------------------------------
def _graphic_kind(el):
    data = el.find("./a:graphic/a:graphicData", NS)
    uri = data.get("uri", "") if data is not None else ""
    return next((kind for key, kind in GRAPHIC_KINDS.items() if uri.endswith(key) or key in uri), "graphic"), data


def _shape(el, view, transform, group, alt):
    """One shape element -> its record in the view."""
    pkg, part, layout, master = view["pkg"], view["part"], view["layout"], view["master"]
    tag = local(el)
    nv = el.find("./*/p:cNvPr", NS)
    ph = el.find("./*/p:nvPr/p:ph", NS)
    shape = {"id": int(nv.get("id", 0)) if nv is not None else 0, "name": nv.get("name", "") if nv is not None else "",
             "kind": "shape"}
    if ph is not None:
        shape["ph"] = ph.get("type", "obj")
        shape["idx"] = int(ph.get("idx", "0"))
    if nv is not None and nv.get("hidden") in ("1", "true"):
        shape["hidden"] = True
    if group:
        shape["group"] = group
    if alt:
        shape["alternate"] = True  # inside mc:AlternateContent: read, never edited
    box = box_of(xfrm_of(el))
    if box is None and ph is not None:
        box = inherited_box(ph, layout, master)
        shape["inherited_box"] = box is not None
    if box is not None:
        shape["box"] = cm(transform(*box))
    xfrm = xfrm_of(el)
    if xfrm is not None and int(xfrm.get("rot", "0")):
        shape["rotation"] = round(int(xfrm.get("rot")) / 60000, 1)
    body = el.find(q("p:txBody"))
    if tag == "sp":
        if el.find(".//a14:m", NS) is not None:
            shape["kind"] = "equation"
        elif body is not None and body_text(text_body(body)):
            shape["kind"] = "text"
        elif ph is not None:
            shape["kind"] = "placeholder"
        if el.find("./p:spPr/a:blipFill", NS) is not None:
            shape["image"] = _image(el.find("./p:spPr/a:blipFill/a:blip", NS), pkg, part)
    elif tag == "cxnSp":
        shape["kind"] = "connector"
    elif tag == "pic":
        nvpr = el.find("./p:nvPicPr/p:nvPr", NS)
        media = nvpr is not None and (nvpr.find(q("a:videoFile")) is not None or nvpr.find(".//p14:media", NS)
                                      is not None or nvpr.find(q("a:audioFile")) is not None)
        shape["kind"] = ("audio" if nvpr is not None and nvpr.find(q("a:audioFile")) is not None else "video") \
            if media else "picture"
        shape["image"] = _image(el.find("./p:blipFill/a:blip", NS), pkg, part)
    elif tag == "graphicFrame":
        kind, data = _graphic_kind(el)
        shape["kind"] = kind
        if kind == "table":
            shape["table"] = [[body_text(text_body(tc.find(q("a:txBody")), pkg, part)) if tc.find(q("a:txBody"))
                               is not None else "" for tc in tr.findall(q("a:tc"))]
                              for tr in data.findall("./a:tbl/a:tr", NS)]
        elif kind == "chart":
            chart = next((v for k, v in data.find("./*", NS).attrib.items() if k.startswith(f"{{{R_NS}}}")), None) \
                if data is not None and data.find("./*", NS) is not None else None
            target = pkg.rels(part).get(chart, {}).get("target") if chart else None
            shape["chart"] = pkg.digest(target, view["context"]) if target else "missing"
            if target and target in pkg.blobs:  # what the chart shows: PowerPoint rewrites its XML on every save
                root = pkg.xml(target)
                shape["chart_text"] = [t for t in (root.xpath(".//c:title//a:t/text()", namespaces=NS)
                                                   + root.xpath(".//c:ser/c:tx//c:v/text()", namespaces=NS))][:8]
                values = root.xpath(".//c:ser/c:cat//c:pt/c:v/text() | .//c:ser/c:val//c:pt/c:v/text()",
                                    namespaces=NS)
                shape["chart_data"] = short_hash("\n".join(shape["chart_text"] + values))
        elif kind == "SmartArt":
            ids = data.find("./dgm:relIds", NS) if data is not None else None
            target = pkg.rels(part).get(ids.get(f"{{{R_NS}}}dm"), {}).get("target") if ids is not None else None
            if target and target in pkg.blobs:
                shape["smartart_text"] = [t for t in pkg.xml(target).xpath(".//a:t/text()", namespaces=NS) if t.strip()]
        elif kind == "OLE":
            ole = data.find(".//p:oleObj", NS) if data is not None else None
            shape["program"] = ole.get("progId", "") if ole is not None else ""
    elif tag == "contentPart":
        shape["kind"] = "ink"
    if body is not None and shape["kind"] in ("text", "equation", "placeholder"):
        shape["paragraphs"] = text_body(body, pkg, part)
    return shape


def _image(blip, pkg, part):
    """A picture's file: 'sha:<hash>', 'linked:<url>', or 'broken' (PowerPoint sometimes saves an empty target)."""
    if blip is None:
        return "none"
    embed, link = blip.get(f"{{{R_NS}}}embed"), blip.get(f"{{{R_NS}}}link")
    if link:
        rel = pkg.rels(part).get(link)
        return f"linked:{rel['target']}" if rel else "broken"
    rel = pkg.rels(part).get(embed) if embed else None
    if rel is None or rel["external"] or rel["target"] not in pkg.blobs:
        return "broken"
    return short_hash(pkg.blobs[rel["target"]])


def _walk(tree, view, transform, group=None, alt=False):
    shapes = []
    for el in tree:
        tag = local(el)
        if tag == "AlternateContent":
            choice = el.find(q("mc:Choice"))
            branch = choice if choice is not None else el.find(q("mc:Fallback"))
            if branch is not None:
                shapes += _walk(branch, view, transform, group, True)
        elif tag == "grpSp":
            record = _shape(el, view, transform, group, alt)
            record["kind"] = "group"
            shapes.append(record)
            shapes += _walk(el, view, _compose(transform, el), record["name"], alt)
        elif tag in ("sp", "pic", "graphicFrame", "cxnSp", "contentPart"):
            shapes.append(_shape(el, view, transform, group, alt))
    return shapes


# ---- the deck --------------------------------------------------------------------------------------------------------
def notes_of(pkg, slide_part):
    notes = pkg.related(slide_part, "notesSlide")
    if not notes or notes not in pkg.blobs:
        return ""
    texts = []
    for sp in pkg.xml(notes).findall(".//p:sp", NS):
        ph = sp.find("./p:nvSpPr/p:nvPr/p:ph", NS)
        body = sp.find(q("p:txBody"))
        if ph is not None and ph.get("type") == "body" and body is not None:
            texts.append(body_text(text_body(body)))
    return "\n".join(t for t in texts if t).strip()


def presentation_part(pkg):
    return next(r["target"] for r in pkg.rels("/").values() if r["type"] == "officeDocument")


def slide_list(pkg):
    """[(slide id, slide part name)] in the deck's order (p:sldIdLst)."""
    pres = presentation_part(pkg)
    rels = pkg.rels(pres)
    out = []
    for sld in pkg.xml(pres).findall("./p:sldIdLst/p:sldId", NS):
        rel = rels.get(sld.get(f"{{{R_NS}}}id"))
        out.append((int(sld.get("id")), rel["target"] if rel else None))
    return out


def fingerprint(pkg, part, layout_name, notes, context):
    """The slide's canonical XML with every r:id resolved, its layout's name and its notes' text, hashed."""
    xml = canonical(pkg.xml(part), lambda rid: pkg.describe_rel(part, rid, context))
    return short_hash(f"{xml}\nlayout:{layout_name}\nnotes:{notes}", 16)


def template_info(pkg):
    """What a template profile can be matched by: the theme and master names, the layouts, docProps' Template."""
    pres = presentation_part(pkg)
    masters = [r["target"] for r in pkg.rels(pres).values() if r["type"] == "slideMaster"]
    layouts, themes, names = [], [], []
    for m in masters:
        names.append(part_name_of(pkg, m))
        theme = pkg.related(m, "theme")
        if theme:
            themes.append(part_name_of(pkg, theme))
        layouts += [part_name_of(pkg, r["target"]) for r in pkg.rels(m).values() if r["type"] == "slideLayout"]
    template = ""
    if "/docProps/app.xml" in pkg.blobs:
        found = pkg.xml("/docProps/app.xml").find(q("ep:Template"))
        template = (found.text or "").strip() if found is not None else ""
    size = pkg.xml(pres).find(q("p:sldSz"))
    return {"masters": names, "themes": themes, "layouts": layouts, "app_template": template,
            "slide_size_cm": cm((0, 0, int(size.get("cx")), int(size.get("cy"))))[2:] if size is not None else None}


def deck_view(path):
    """
    The whole deck as plain data (a dict that json.dumps can write): the template's names, and per slide its id,
    position, layout, title, shapes, notes and fingerprint. See the module docstring.
    """
    pkg = Package(path)
    order = slide_list(pkg)
    context = {"slide_ids": {part: sid for sid, part in order if part}}
    slides = []
    for position, (sid, part) in enumerate(order, 1):
        if part is None or part not in pkg.blobs:
            slides.append({"id": sid, "position": position, "missing": True, "layout": "", "title": "", "shapes": [],
                           "notes": "", "fingerprint": "missing"})
            continue
        layout = pkg.related(part, "slideLayout")
        master = pkg.related(layout, "slideMaster") if layout else None
        view = {"pkg": pkg, "part": part, "context": context,
                "layout": pkg.xml(layout) if layout in pkg.blobs else None,
                "master": pkg.xml(master) if master in pkg.blobs else None}
        root = pkg.xml(part)
        tree = root.find("./p:cSld/p:spTree", NS)
        shapes = _walk(tree, view, lambda x, y, w, h: (x, y, w, h)) if tree is not None else []
        layout_name = part_name_of(pkg, layout) if layout else ""
        title = next((body_text(s["paragraphs"]).replace("\n", " ") for s in shapes
                      if s.get("ph") in ("title", "ctrTitle") and s.get("paragraphs")), "")
        notes = notes_of(pkg, part)
        slides.append({"id": sid, "position": position, "part": part, "layout": layout_name, "title": title,
                       "hidden": root.get("show") == "0", "shapes": shapes, "notes": notes,
                       "fingerprint": fingerprint(pkg, part, layout_name, notes, context)})
    return {"file": str(path), "slide_count": len(slides), "template": template_info(pkg), "slides": slides}


# ---- short forms for the LLM -----------------------------------------------------------------------------------------
def shape_line(shape, limit=160):
    """'"Content Placeholder 2" (text, 3 paragraphs): "We have completed **12 tests** / ..."'"""
    kind = shape["kind"]
    if shape.get("paragraphs") and kind in ("text", "equation"):
        paragraphs = [marked(p["runs"]) for p in shape["paragraphs"] if plain(p["runs"]).strip()]
        text = " / ".join(paragraphs)
        text = text if len(text) <= limit else text[:limit] + "..."
        what = f"{kind}, {len(paragraphs)} paragraph{'s' if len(paragraphs) != 1 else ''}"
        return f"\"{shape['name']}\" ({what}): \"{text}\""
    if kind == "table":
        rows = shape.get("table") or []
        first = " | ".join(rows[0]) if rows else ""
        return f"\"{shape['name']}\" (table, {len(rows)} rows x {len(rows[0]) if rows else 0}): \"{first[:limit]}\""
    if kind in ("picture", "video", "audio"):
        return f"\"{shape['name']}\" ({kind}, image {shape.get('image', '?')}, {fmt_box(shape.get('box'))})"
    if kind == "chart":
        return f"\"{shape['name']}\" (chart: {', '.join(shape.get('chart_text', [])[:4]) or 'no title'})"
    if kind == "SmartArt":
        return f"\"{shape['name']}\" (SmartArt, read only: \"{' / '.join(shape.get('smartart_text', []))[:limit]}\")"
    return f"\"{shape['name']}\" ({kind})"


def fmt_box(box):
    return f"{box[2]:.1f} x {box[3]:.1f} cm at ({box[0]:.1f}, {box[1]:.1f})" if box else "no size"


def slide_line(slide):
    """'3 (Title and Content) "Instrumentation": 2 shapes, notes'"""
    content = [s for s in slide["shapes"] if s.get("ph") not in ("title", "ctrTitle", "sldNum", "dt", "ftr")
               and not s.get("group")]
    kinds = {}
    for s in content:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    parts = [f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items()]
    return (f"{slide['position']} ({slide['layout']}) \"{slide['title'] or '(no title)'}\": "
            + (", ".join(parts) or "empty") + (", notes" if slide["notes"] else "") + (", hidden" if slide.get("hidden")
                                                                                       else ""))


def slide_detail(slide):
    """Every shape and the notes of one slide, a line each."""
    lines = [shape_line(s) for s in slide["shapes"]
             if s["kind"] != "group" and s.get("ph") not in ("sldNum", "dt", "ftr")]
    if slide["notes"]:
        notes = slide["notes"].replace("\n", " / ")
        lines.append(f"notes: \"{notes[:300]}{'...' if len(notes) > 300 else ''}\"")
    return lines


def deck_markdown(view):
    """deck.md: the whole view, for a person (the LLM gets a few lines)."""
    lines = [f"# {Path(view['file']).name}", "", f"{view['slide_count']} slides; template: "
             f"{', '.join(view['template']['masters'] + view['template']['themes']) or '?'}", ""]
    for s in view["slides"]:
        lines += [f"## Slide {s['position']}: {s['title'] or '(no title)'}", "",
                  f"- id {s['id']}, layout *{s['layout']}*, fingerprint `{s['fingerprint']}`"]
        for shape in s["shapes"]:
            indent = "  " if shape.get("group") else ""
            lines.append(f"{indent}- {shape_line(shape, 400)}; {fmt_box(shape.get('box'))}")
            if shape["kind"] == "table":
                lines += [f"{indent}  - {' | '.join(row)}" for row in shape.get("table", [])]
        if s["notes"]:
            lines += ["", "Notes:", "", "> " + s["notes"].replace("\n", "\n> ")]
        lines.append("")
    return "\n".join(lines)


