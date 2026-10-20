"""
The OOXML writer for Word review (OFFICE_COLLAB_DESIGN.md, W5): tracked changes and comments written into a .docx's
XML, accept and reject, and the structural checks every output must pass. Plain Python and lxml; no model, no Word.

A tracked change is XML around the text. "within 5 minutes" -> "within 3 minutes" becomes

    <w:r><w:t xml:space="preserve">within </w:t></w:r>
    <w:del w:id="12" w:author="..." w:date="2026-10-20T09:00:00Z"><w:r><w:delText>5</w:delText></w:r></w:del>
    <w:ins w:id="13" w:author="..." w:date="2026-10-20T09:00:00Z"><w:r><w:t>3</w:t></w:r></w:ins>
    <w:r><w:t xml:space="preserve"> minutes</w:t></w:r>

The runs are split at the edit's word boundaries first (split_run), so the unchanged words keep their own runs and
formatting. The rules below each come from a file Word refused, or repaired without saying, in a past job:

  ids         w:id unique across insertions, deletions, comments and bookmarks (Ids: new ids start above every id in
              the file); w14:paraId and w14:textId 8 hexadecimal digits below 0x80000000, unique
  order       w:rPr first in a run; in a paragraph's w:pPr, w:rPr after the other properties (the tracked paragraph
              mark's w:ins/w:del go first inside it); in w:trPr, w:ins/w:del after the other row properties
  deletions   w:delText, never w:t, inside w:del; xml:space="preserve" on text with spaces at either end
  copies      a copied paragraph or row is cleaned of its revisions, comments and bookmarks first (clean_copy)
  one call    one author and one ISO 8601 UTC timestamp for everything written in one tool call (Writer)
  zip         [Content_Types].xml first; every part not changed copied byte for byte (sensitivity labels included);
              relationships and content types added for new parts

Nothing here decides WHAT to change: the tools in word_review.py pass the edits the LLM proposed, and check the result
with the readers in word_model.py.
"""

import copy
import difflib
import random
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .word_model import (ACCEPT, CELL_CHANGES, DELETES, INSERTS, NS, PARSER, PROPERTY_CHANGES, RANGE_MARKERS,
                         REVISION_TAGS, XML_SPACE, body_of, mark_goes, mark_revision, next_block, next_paragraph,
                         part_names, qn, read_comments, revision_kind, run_text, visible, w, walk)

CONTENT_TYPES = {
    "comments": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    "commentsExtended": "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
    "commentsIds": "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
    "commentsExtensible": "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml",
    "people": "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml",
}
RELATIONSHIP_TYPES = {
    "comments": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    "commentsExtended": "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    "commentsIds": "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    "commentsExtensible": "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
    "people": "http://schemas.microsoft.com/office/2011/relationships/people",
}
ROOTS = {"comments": "w:comments", "commentsExtended": "w15:commentsEx", "commentsIds": "w16cid:commentsIds",
         "commentsExtensible": "w16cex:commentsExtensible", "people": "w15:people"}

# Schema order (ECMA-376) of the children the writer adds to. Word refuses or repairs a file with them out of order.
RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
             "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w",
             "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl",
             "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath", "rPrChange"]
MARK_RPR_ORDER = ["ins", "del", "moveFrom", "moveTo"] + RPR_ORDER  # a paragraph mark's w:rPr: its revision first
PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
             "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
             "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid",
             "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
             "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"]
TRPR_ORDER = ["cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter", "cantSplit", "trHeight",
              "tblHeader", "tblCellSpacing", "jc", "hidden", "ins", "del", "trPrChange"]
TCPR_ORDER = ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
              "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge",
              "tcPrChange"]
SETTINGS_ORDER = ["writeProtection", "view", "zoom", "removePersonalInformation", "removeDateAndTime",
                  "doNotDisplayPageBoundaries", "displayBackgroundShape", "printPostScriptOverText",
                  "printFractionalCharacterWidth", "printFormsData", "embedTrueTypeFonts", "embedSystemFonts",
                  "saveSubsetFonts", "saveFormsData", "mirrorMargins", "alignBordersAndEdges",
                  "bordersDoNotSurroundHeader", "bordersDoNotSurroundFooter", "gutterAtTop", "hideSpellingErrors",
                  "hideGrammaticalErrors", "activeWritingStyle", "proofState", "formsDesign", "attachedTemplate",
                  "linkStyles", "stylePaneFormatFilter", "stylePaneSortMethod", "documentType", "mailMerge",
                  "revisionView", "trackRevisions", "doNotTrackMoves", "doNotTrackFormatting", "documentProtection",
                  "autoFormatOverride", "styleLockTheme", "styleLockQFSet", "defaultTabStop"]
TOKENS = re.compile(r"\w+|\s+|[^\w\s]")  # the units of a word diff: words, spaces, punctuation marks


class Refused(Exception):
    """An edit the writer will not make, and why: someone else's pending change ('needs a person'), a field, ..."""

    def __init__(self, why, needs_person=False):
        super().__init__(why)
        self.needs_person = needs_person


def now_utc():
    """One timestamp per tool call, ISO 8601 in UTC, as Word writes them: 2026-10-20T09:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- the package: the .docx's parts, read once, written back with only the changed ones re-serialized ------------
class Package:
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(path) as z:
            self.infos = z.infolist()
            self.raw = {i.filename: z.read(i.filename) for i in self.infos}
        self.trees, self.changed = {}, set()
        self.names = part_names(self.raw.get)

    def has(self, name):
        return name in self.raw or name in self.trees

    def xml(self, name):
        if name not in self.trees:
            self.trees[name] = etree.fromstring(self.raw[name], PARSER)
        return self.trees[name]

    @property
    def document(self):
        return self.xml(self.names["document"])

    def touch(self, name):
        self.xml(name)  # parsed, so what is saved is the tree
        self.changed.add(name)

    def part(self, kind):
        """A part by its kind ('comments', 'people', ...) or None."""
        name = self.names.get(kind)
        return self.xml(name) if name and self.has(name) else None

    def bytes_of(self, name):
        if name in self.changed:
            root = self.trees[name]
            if NS["w14"] not in root.nsmap.values() and root.find(f".//*[@{{{NS['w14']}}}paraId]") is not None:
                etree.cleanup_namespaces(root, top_nsmap={"w14": NS["w14"]})  # declared once, on the root
            return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        return self.raw[name]

    def new_part(self, kind, root):
        """A part the document doesn't have yet (comments.xml, people.xml, ...), with its relationship and content
        type."""
        folder = self.names["document"].rsplit("/", 1)[0]
        name = f"{folder}/{kind}.xml"
        self.trees[name] = root
        self.names[kind] = name
        self.touch(name)
        rels = self.xml(self.names["document_rels"])
        used = {r.get("Id") for r in rels}
        rid = next(f"rId{n}" for n in range(1, 10000) if f"rId{n}" not in used)
        etree.SubElement(rels, f"{{{NS['rel']}}}Relationship", Id=rid, Type=RELATIONSHIP_TYPES[kind],
                         Target=f"{kind}.xml")
        self.touch(self.names["document_rels"])
        types = self.xml("[Content_Types].xml")
        if not any(o.get("PartName") == f"/{name}" for o in types.iter(f"{{{NS['ct']}}}Override")):
            etree.SubElement(types, f"{{{NS['ct']}}}Override", PartName=f"/{name}", ContentType=CONTENT_TYPES[kind])
            self.touch("[Content_Types].xml")
        return root

    def save(self, out):
        """[Content_Types].xml first, then every part in its old order; unchanged parts byte for byte."""
        names = [i.filename for i in self.infos]
        names += [n for n in self.trees if n not in self.raw and n not in names]
        names = ["[Content_Types].xml"] + [n for n in names if n != "[Content_Types].xml"]
        infos = {i.filename: i for i in self.infos}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in names:
                old = infos.get(name)
                info = zipfile.ZipInfo(name, date_time=old.date_time if old else datetime.now().timetuple()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = old.external_attr if old else 0o600 << 16
                z.writestr(info, self.bytes_of(name))
        return Path(out)


# ---- ids -----------------------------------------------------------------------------------------------------------
class Ids:
    """New annotation ids and paragraph ids, never one already used anywhere in the file."""

    def __init__(self, pkg, seed=0x5EED):
        numbers, hexes = {0}, set()
        for name in pkg.raw:
            if name.endswith(".xml") and not name.startswith("customXml"):
                data = pkg.bytes_of(name) if name in pkg.changed else pkg.raw[name]
                numbers.update(int(n) for n in re.findall(rb'\bw:id="(-?\d+)"', data) if int(n) >= 0)
                hexes.update(int(h, 16) for h in re.findall(rb'(?:paraId|textId|durableId)="([0-9A-Fa-f]{1,8})"',
                                                            data))
        hex_attrs = (qn("w14:paraId"), qn("w14:textId"), qn("w16cid:durableId"), qn("w16cex:durableId"))
        for root in pkg.trees.values():  # parts edited in memory (another Writer on the same package)
            for el in root.iter():
                value = el.get(w("id"))
                if value and value.lstrip("-").isdigit() and int(value) >= 0:
                    numbers.add(int(value))
                for attr in hex_attrs:
                    if el.get(attr) and re.fullmatch(r"[0-9A-Fa-f]{1,8}", el.get(attr)):
                        hexes.add(int(el.get(attr), 16))
        self.next = max(numbers) + 1
        self.hexes = hexes
        self.rng = random.Random(seed)

    def new(self):
        self.next += 1
        return str(self.next - 1)

    def hex8(self):
        """8 hexadecimal digits below 0x80000000 (Word's limit for paraId, textId and durableId), unique."""
        while True:
            value = self.rng.randrange(0x00100000, 0x7FFFFFFF)
            if value not in self.hexes:
                self.hexes.add(value)
                return f"{value:08X}"


class Writer:
    """Everything one tool call writes: one author, one timestamp, and the ids it gave out."""

    def __init__(self, pkg, author, initials="", date=None):
        self.pkg, self.author, self.initials = pkg, author, initials
        self.date = date or now_utc()
        if getattr(pkg, "ids", None) is None:  # one counter per package, shared by every Writer on it
            pkg.ids = Ids(pkg)
        self.ids = pkg.ids
        self.revision_ids, self.comment_ids = [], []

    def revision(self, tag):
        el = etree.Element(w(tag))
        el.set(w("id"), self.ids.new())
        el.set(w("author"), self.author)
        el.set(w("date"), self.date)
        self.revision_ids.append(el.get(w("id")))
        return el

    def paragraph_ids(self, p):
        p.set(qn("w14:paraId"), self.ids.hex8())
        p.set(qn("w14:textId"), self.ids.hex8())


# ---- small XML helpers ---------------------------------------------------------------------------------------------
def local(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def insert_ordered(parent, child, order):
    """Put child where the schema wants it among parent's children (order: local names; others go last)."""
    rank = {name: i for i, name in enumerate(order)}
    mine = rank.get(local(child), len(order))
    for i, existing in enumerate(parent):
        if not isinstance(existing.tag, str):
            continue
        theirs = rank.get(local(existing), len(order)) if existing.tag.startswith(f"{{{NS['w']}}}") else len(order)
        if theirs > mine:
            parent.insert(i, child)
            return child
    parent.append(child)
    return child


def set_text(t, text):
    t.text = text
    if text != text.strip() or "  " in text:
        t.set(XML_SPACE, "preserve")
    elif XML_SPACE in t.attrib and text == text.strip():
        pass  # Word's own files keep it; harmless
    return t


def clean_rpr(rpr):
    """A run's formatting for new text: a copy without the tracked changes it may carry."""
    if rpr is None:
        return None
    rpr = copy.deepcopy(rpr)
    rpr.tag = w("rPr")
    for child in list(rpr):
        if child.tag in (w("rPrChange"), w("ins"), w("del"), w("moveFrom"), w("moveTo")):
            rpr.remove(child)
    return rpr if len(rpr) else None


def new_run(text, rpr=None, deleted=False):
    """A run holding `text` (tabs and line breaks as w:tab and w:br), w:delText if it is deleted text."""
    r = etree.Element(w("r"))
    rpr = clean_rpr(rpr)
    if rpr is not None:
        r.append(rpr)
    for piece in re.split(r"(\t|\n)", text):
        if piece == "\t":
            etree.SubElement(r, w("tab"))
        elif piece == "\n":
            etree.SubElement(r, w("br"))
        elif piece:
            set_text(etree.SubElement(r, w("delText" if deleted else "t")), piece)
    return r


def top_level(el, p):
    """The child of paragraph p that holds el (el itself if it is one)."""
    while el.getparent() is not p:
        el = el.getparent()
    return el


# ---- runs and offsets ------------------------------------------------------------------------------------------------
def visible_runs(p):
    """The runs of p that are in its accepted text -> [(run, start, end, revisions, containers)]: offsets in the
    accepted text, the tracked changes around the run, the other containers (hyperlink, field, ...) around it."""
    out, pos = [], 0
    for kind, r, revisions, containers in walk(p):
        if kind != "run":
            continue
        if not visible(revisions, ACCEPT)[0]:
            continue
        n = len(run_text(r))
        out.append((r, pos, pos + n, revisions, containers))
        pos += n
    return out


def split_run(r, k, ids=None):
    """Cut a run in two after its k-th character; both halves keep the formatting (and a tracked formatting change,
    whose copy gets a new id: ids, the package's Ids). -> the new second run."""
    right = etree.Element(r.tag, attrib=dict(r.attrib))
    rpr = r.find(w("rPr"))
    if rpr is not None:
        right.append(copy.deepcopy(rpr))
        change = right.find(f"{w('rPr')}/{w('rPrChange')}")
        if change is not None and ids is not None:
            change.set(w("id"), ids.new())
    pos, moving = 0, False
    for child in list(r):
        if child.tag == w("rPr"):
            continue
        if moving:
            right.append(child)
            continue
        size = (len(child.text or "") if child.tag in (w("t"), w("delText"))
                else 1 if child.tag in (w("tab"), w("br"), w("cr")) else 0)
        if pos + size <= k:
            pos += size
            moving = pos == k and size > 0
            continue
        cut = k - pos
        tail = etree.SubElement(right, child.tag)
        set_text(tail, (child.text or "")[cut:])
        set_text(child, (child.text or "")[:cut])
        moving = True
    r.addnext(right)
    return right


def split_at(p, offset, ids=None):
    """Make `offset` (in p's accepted text) a run boundary."""
    for r, start, end, _, _ in visible_runs(p):
        if start < offset < end:
            split_run(r, offset - start, ids)
            return


def region(p, a, b, ids=None):
    """The runs covering [a, b) of p's accepted text, after splitting at a and b. Zero-length runs strictly inside
    (a picture, a field mark, a comment reference) count too."""
    split_at(p, b, ids)
    split_at(p, a, ids)
    return [(r, s, e, revs, conts) for r, s, e, revs, conts in visible_runs(p)
            if (a <= s and e <= b and e > s) or (s == e and a < s < b)]


NOT_TEXT = {w("drawing"): "a picture", w("pict"): "a picture", w("object"): "an embedded object",
            w("footnoteReference"): "a footnote reference", w("endnoteReference"): "an endnote reference",
            w("commentReference"): "a comment reference", w("fldChar"): "a field", w("instrText"): "a field code",
            w("sym"): "a symbol", w("ptab"): "an alignment tab"}
CONTAINER_NAMES = {w("hyperlink"): "a hyperlink", w("fldSimple"): "a field", w("sdt"): "a content control",
                   w("sdtContent"): "a content control", w("smartTag"): "a smart tag", w("customXml"): "custom XML",
                   w("dir"): "a bidirectional span", w("bdo"): "a bidirectional span"}


def field_depth(p):
    """{run: True} for runs inside a complex field (between fldChar begin and end): its code or its result."""
    inside, depth = {}, 0
    for kind, r, _, _ in walk(p):
        if kind != "run":
            continue
        chars = [c.get(w("fldCharType")) for c in r.iter(w("fldChar"))]
        if "begin" in chars:
            depth += chars.count("begin")
        inside[r] = depth > 0
        depth -= chars.count("end")
        depth = max(depth, 0)
    return inside


def check_region(p, a, b, runs):
    """
    May the writer change [a, b) of p? Raises Refused with the reason. Not: text inside someone's pending insertion or
    deletion (Word's rules for that are subtle: a person decides), nor inside a field, hyperlink, content control or
    equation, nor across a picture or a comment reference.
    """
    if not runs:
        raise Refused("the text to change is not in the paragraph's runs")
    for r, _, _, revisions, containers in runs:
        if revisions:
            who = revisions[-1].get(w("author"), "someone")
            what = "insertion" if revisions[-1].tag in INSERTS else "deletion"
            raise Refused(f"it touches a pending {what} by {who}: accept or reject that first (needs a person)",
                          needs_person=True)
        if containers:
            raise Refused(f"the text is inside {CONTAINER_NAMES.get(containers[0].tag, 'a container')}, which the "
                          "writer does not edit")
        for child in r:
            if child.tag in NOT_TEXT:
                raise Refused(f"the text to change contains {NOT_TEXT[child.tag]}")
    fields = field_depth(p)
    if any(fields.get(r) for r, *_ in runs):
        raise Refused("the text is the result of a field (a cross-reference, date, page number, ...), which Word "
                      "would overwrite on the next update")
    items = [(kind, el, revs) for kind, el, revs, _ in walk(p) if kind == "run"]
    first = next(i for i, (_, el, _) in enumerate(items) if el is runs[0][0])
    last = next(i for i, (_, el, _) in enumerate(items) if el is runs[-1][0])
    for _, _, revs in items[first:last + 1]:
        if revs:
            who = revs[-1].get(w("author"), "someone")
            raise Refused(f"it spans a pending change by {who}: accept or reject that first (needs a person)",
                          needs_person=True)


def wrap(elements, wrapper):
    """Put consecutive siblings (and the range markers between them) inside wrapper, where the first one was."""
    first, last = elements[0], elements[-1]
    first.addprevious(wrapper)
    node = first
    while node is not None:
        following = node.getnext()
        wrapper.append(node)
        if node is last:
            break
        node = following
    return wrapper


def to_deleted(el):
    """Text inside a deletion is w:delText (and field codes w:delInstrText), never w:t."""
    for t in list(el.iter(w("t"))):
        t.tag = w("delText")
        if (t.text or "") != (t.text or "").strip():
            t.set(XML_SPACE, "preserve")
    for t in list(el.iter(w("instrText"))):
        t.tag = w("delInstrText")


def to_kept(el):
    for t in list(el.iter(w("delText"))):
        t.tag = w("t")
    for t in list(el.iter(w("delInstrText"))):
        t.tag = w("instrText")


def delete_runs(runs, writer):
    """Wrap runs in w:del, one w:del per stretch of sibling runs (runs in a hyperlink get theirs inside it)."""
    groups = []
    for r in runs:
        if groups and groups[-1][-1].getparent() is r.getparent() and _adjacent(groups[-1][-1], r):
            groups[-1].append(r)
        else:
            groups.append([r])
    made = []
    for group in groups:
        dele = wrap(group, writer.revision("del"))
        to_deleted(dele)
        made.append(dele)
    return made


def _adjacent(a, b):
    """Is b the next run after a, with only range markers between?"""
    node = a.getnext()
    while node is not None and node is not b:
        if isinstance(node.tag, str) and node.tag not in RANGE_MARKERS:
            return False
        node = node.getnext()
    return node is b


# ---- tracked text edits --------------------------------------------------------------------------------------------
def tokens(text):
    return TOKENS.findall(text)


def tracked_replace(p, a, b, new, writer, rpr_for=None):
    """
    Change [a, b) of paragraph p's accepted text into `new`, as tracked changes, word by word: unchanged words stay as
    they are (with their formatting), removed words go into w:del with their own formatting, added words into w:ins
    with the formatting of the word they replace or follow. rpr_for(j1, j2) -> [(text, rPr)], when given, formats new
    text instead (compare_docx: the revised file's own formatting). Raises Refused before changing anything.
    -> (the revision elements made, the first and last element of the edit, for a comment's anchor)
    """
    runs = region(p, a, b, writer.ids)
    check_region(p, a, b, runs)
    old = "".join(run_text(r) for r, *_ in runs)
    old_tokens, new_tokens = tokens(old), tokens(new)
    starts, new_starts = offsets(old_tokens), offsets(new_tokens)
    ops = [op for op in difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes()
           if op[0] != "equal"]
    for _, i1, i2, _, _ in ops:  # every boundary of a change becomes a run boundary
        split_at(p, a + starts[i2], writer.ids)
        split_at(p, a + starts[i1], writer.ids)
    # the region's runs with their offsets in the text as it was: positions don't move while the edit goes on
    spans = [(s, e, r) for r, s, e, _, _ in visible_runs(p) if a <= s and e <= b]
    made, created = [], []
    for _, i1, i2, j1, j2 in reversed(ops):  # from the end of the region back, as F6 says
        s, e = a + starts[i1], a + starts[i2]
        doomed = [r for r0, e0, r in spans if (s <= r0 and e0 <= e and e0 > r0) or (e0 == r0 and s < r0 < e)]
        text = "".join(new_tokens[j1:j2])
        last_del = None
        if doomed:
            dels = delete_runs(doomed, writer)
            made += dels
            last_del = dels[-1]
        if not text:
            continue
        pieces = rpr_for(new_starts[j1], new_starts[j2]) if rpr_for is not None else None
        if not pieces:
            source = doomed[0] if doomed else next((r for r0, e0, r in reversed(spans) if e0 == s and e0 > r0), None)
            if source is None:
                source = run_ending_at(p, s)
            if source is None:
                source = next((r for r0, e0, r in spans if r0 >= s and e0 > r0), None)
            pieces = [(text, source.find(w("rPr")) if source is not None else None)]
        ins = writer.revision("ins")
        for piece, rpr in pieces:
            if piece:
                ins.append(new_run(piece, rpr))
        if last_del is not None:  # a replacement: the new words right after the old ones, as Word writes it
            last_del.addnext(ins)
        elif s < b:  # an insertion inside the region: before the word that follows it
            top_level(next(r for r0, e0, r in spans if r0 == s and e0 > r0), p).addprevious(ins)
        else:  # at the region's end: after its last word
            top_level([r for r0, e0, r in spans if e0 == b and e0 > r0][-1], p).addnext(ins)
        made.append(ins)
        created.append(ins)
    tops = [top_level(x, p) for x in created + [r for _, _, r in spans]]
    if not tops:
        return made, (None, None)
    return made, (min(tops, key=p.index), max(tops, key=p.index))


def offsets(toks):
    out = [0]
    for tok in toks:
        out.append(out[-1] + len(tok))
    return out


def run_ending_at(p, s):
    """The visible run whose text ends at offset s (the word before new text at the start of a region)."""
    return next((r for r, rs, re_, _, _ in reversed(visible_runs(p)) if re_ == s and re_ > rs), None)


def mark_paragraph(p, tag, writer):
    """Track the paragraph's mark (the pilcrow) as inserted ('ins') or deleted ('del'): w:pPr/w:rPr/w:ins."""
    ppr = p.find(w("pPr"))
    if ppr is None:
        ppr = etree.Element(w("pPr"))
        p.insert(0, ppr)
    rpr = ppr.find(w("rPr"))
    if rpr is None:
        rpr = insert_ordered(ppr, etree.Element(w("rPr")), PPR_ORDER)
    rev = writer.revision(tag)
    insert_ordered(rpr, rev, MARK_RPR_ORDER)
    return rev


def mark_row(tr, tag, writer):
    trpr = tr.find(w("trPr"))
    if trpr is None:
        trpr = etree.Element(w("trPr"))
        tblprex = tr.find(w("tblPrEx"))
        (tblprex.addnext(trpr) if tblprex is not None else tr.insert(0, trpr))
    rev = writer.revision(tag)
    insert_ordered(trpr, rev, TRPR_ORDER)
    return rev


def clean_copy(el, writer):
    """
    A copied paragraph, row or table for new content: its tracked changes accepted, its comments, bookmarks and
    proofing marks taken out, and new paragraph ids. In a past job copied rows brought old tracked changes with them.
    """
    el = copy.deepcopy(el)
    resolve(el, ACCEPT)
    for marker in list(el.iter(*RANGE_MARKERS)):
        marker.getparent().remove(marker)
    for ref in list(el.iter(w("commentReference"), w("annotationRef"))):
        r = ref.getparent()
        if r is not None and r.getparent() is not None:
            r.getparent().remove(r)
    for sect in list(el.iter(w("sectPr"))):
        if sect.getparent().tag == w("pPr"):
            sect.getparent().remove(sect)
    for p in el.iter(w("p")):
        writer.paragraph_ids(p)
    return el


def insert_runs(p, text, rpr, writer):
    """New text at the end of an (empty) paragraph, as a tracked insertion."""
    ins = writer.revision("ins")
    ins.append(new_run(text, rpr))
    p.append(ins)
    return ins


def is_heading(p, styles):
    from .word_model import heading_level
    return heading_level(p, styles) is not None


def insert_paragraphs_after(p, texts, writer, styles=None, model=None):
    """
    New paragraphs after p, tracked: their text in w:ins and their marks inserted. Formatting from p (from the next
    paragraph if p is a heading). After the last paragraph of a body, cell or before a table, p's own mark becomes the
    inserted one and the last new paragraph keeps an ordinary mark, as Word does when you press Enter at the end: the
    file's last paragraph mark can't be tracked. -> (revision elements, new paragraphs)
    """
    nxt = next_paragraph(p)
    pattern = model if model is not None else (nxt if nxt is not None and styles is not None and is_heading(p, styles)
                                               and not is_heading(nxt, styles) else p)
    runs = [r for r, s, e, _, _ in visible_runs(pattern) if e > s]
    rpr = runs[-1].find(w("rPr")) if runs else None
    made, new = [], []
    at_end = nxt is None
    if at_end:
        made.append(mark_paragraph(p, "ins", writer))
    anchor = p
    for i, text in enumerate(texts):
        q = etree.Element(w("p"))
        writer.paragraph_ids(q)
        ppr = pattern.find(w("pPr"))
        if ppr is not None:
            ppr = copy.deepcopy(ppr)
            for child in list(ppr):
                if child.tag in (w("pPrChange"), w("sectPr")):
                    ppr.remove(child)
            mark = ppr.find(w("rPr"))
            if mark is not None:
                for child in list(mark):
                    if child.tag in INSERTS | DELETES or child.tag == w("rPrChange"):
                        mark.remove(child)
            q.append(ppr)
        if text:
            made.append(insert_runs(q, text, rpr, writer))
        if not (at_end and i == len(texts) - 1):
            made.append(mark_paragraph(q, "ins", writer))
        anchor.addnext(q)
        anchor = q
        new.append(q)
    return made, new


def delete_paragraph(p, writer):
    """
    p deleted, tracked: every run of it in w:del, and its mark deleted, so accepting joins nothing to anything. The
    last paragraph of a body or cell keeps its mark (Word can't delete it) and the paragraph before it loses its mark
    instead; a cell's only paragraph keeps its mark, and an empty paragraph stays. -> revision elements made
    """
    for kind, el, revisions, _ in walk(p):
        if revisions:
            raise Refused(f"the paragraph has a pending change by {revisions[-1].get(w('author'), 'someone')}: accept "
                          "or reject it first (needs a person)", needs_person=True)
        if kind in ("start", "end") or (kind == "run" and el.find(w("commentReference")) is not None):
            raise Refused("the paragraph carries a comment: deleting it would delete the comment's anchor (needs a "
                          "person)", needs_person=True)
    if mark_revision(p) is not None:
        raise Refused("the paragraph's mark has a pending change: accept or reject it first (needs a person)",
                      needs_person=True)
    runs = [el for kind, el, _, _ in walk(p) if kind == "run"]
    made = delete_runs(runs, writer) if runs else []
    return made + delete_mark(p, writer)


def delete_mark(p, writer):
    """The mark that goes with a deleted paragraph: its own, if a paragraph or table follows; at the end of a body or
    cell, the mark of the paragraph before it (Word can't delete the last mark)."""
    if next_block(p) is not None:
        return [mark_paragraph(p, "del", writer)]
    before = p.getprevious()
    while before is not None and before.tag in RANGE_MARKERS:
        before = before.getprevious()
    if before is not None and before.tag == w("p") and mark_revision(before) is None:
        return [mark_paragraph(before, "del", writer)]
    return []


def fill_row(tr, cells, writer):
    """A cleaned copy of a row, made into a new tracked row: one paragraph per cell holding its new text."""
    tcs = [c for c in tr if c.tag == w("tc")]
    if len(cells) > len(tcs):
        raise Refused(f"the row has {len(tcs)} cells, but {len(cells)} were given")
    cells = list(cells) + [""] * (len(tcs) - len(cells))
    made = [mark_row(tr, "ins", writer)]
    trpr = tr.find(w("trPr"))
    for header in list(trpr.iter(w("tblHeader"))):
        trpr.remove(header)
    for tc, text in zip(tcs, cells):
        tcpr = tc.find(w("tcPr"))
        if tcpr is not None:
            for merge in list(tcpr.iter(w("vMerge"))):
                tcpr.remove(merge)
        paras = [c for c in tc if c.tag == w("p")]
        first = paras[0] if paras else etree.SubElement(tc, w("p"))
        runs = [r for r, s, e, _, _ in visible_runs(first) if e > s]
        rpr = copy.deepcopy(runs[0].find(w("rPr"))) if runs and runs[0].find(w("rPr")) is not None else None
        for child in list(tc):
            if child.tag not in (w("tcPr"),) and child is not first:
                tc.remove(child)
        for child in list(first):
            if child.tag != w("pPr"):
                first.remove(child)
        if text:
            made.append(insert_runs(first, str(text), rpr, writer))
        made.append(mark_paragraph(first, "ins", writer))
    return made


def insert_row_after(tr, cells, writer):
    new = clean_copy(tr, writer)
    made = fill_row(new, cells, writer)
    tr.addnext(new)
    return made, new


def delete_row(tr, writer):
    for el in tr.iter(*REVISION_TAGS):
        raise Refused(f"the row has a pending change by {el.get(w('author'), 'someone')}: accept or reject it first "
                      "(needs a person)", needs_person=True)
    if next(tr.iter(w("commentRangeStart"), w("commentReference")), None) is not None:
        raise Refused("the row carries a comment: deleting it would delete the comment's anchor (needs a person)",
                      needs_person=True)
    made = [mark_row(tr, "del", writer)]
    for p in tr.iter(w("p")):
        runs = [el for kind, el, _, _ in walk(p) if kind == "run"]
        if runs:
            made += delete_runs(runs, writer)
    return made


def track_formatting(p, a, b, props, writer):
    """A tracked formatting change over [a, b) (the demo's reviewer bolds a word): props like ['b'] are added and the
    old formatting goes into w:rPrChange. Not offered to the LLM (W1: formatting-only edits are out of scope)."""
    runs = region(p, a, b, writer.ids)
    check_region(p, a, b, runs)
    made = []
    for r, *_ in runs:
        rpr = r.find(w("rPr"))
        if rpr is None:
            rpr = etree.Element(w("rPr"))
            r.insert(0, rpr)
        old = copy.deepcopy(rpr)
        for prop in props:
            if rpr.find(w(prop)) is None:
                insert_ordered(rpr, etree.Element(w(prop)), RPR_ORDER)
        change = writer.revision("rPrChange")
        change.append(old)
        insert_ordered(rpr, change, RPR_ORDER)
        made.append(change)
    return made


# ---- comments ------------------------------------------------------------------------------------------------------
def new_root(kind):
    prefix = ROOTS[kind].split(":")[0]
    nsmap = {k: NS[k] for k in ("w", "w14", "w15", "w16cid", "w16cex", "mc", "r")}
    root = etree.Element(qn(ROOTS[kind]), nsmap=nsmap)
    root.set(qn("mc:Ignorable"), " ".join(p for p in ("w14", "w15", "w16cid", "w16cex") if p in nsmap and
                                          (p == prefix or kind == "comments")))
    return root


def comment_part(pkg, kind):
    return pkg.part(kind) if pkg.part(kind) is not None else pkg.new_part(kind, new_root(kind))


def style_exists(pkg, style_id):
    styles = pkg.part("styles")
    return styles is not None and any(s.get(w("styleId")) == style_id for s in styles.iter(w("style")))


def reference_run(pkg, cid):
    r = etree.Element(w("r"))
    if style_exists(pkg, "CommentReference"):
        rpr = etree.SubElement(r, w("rPr"))
        etree.SubElement(rpr, w("rStyle")).set(w("val"), "CommentReference")
    etree.SubElement(r, w("commentReference")).set(w("id"), cid)
    return r


def ensure_person(pkg, author):
    """people.xml lists the review author, so Word shows the name rather than guessing from the comments."""
    people = comment_part(pkg, "people")
    if any(p.get(qn("w15:author")) == author for p in people.iter(qn("w15:person"))):
        return
    person = etree.SubElement(people, qn("w15:person"))
    person.set(qn("w15:author"), author)
    info = etree.SubElement(person, qn("w15:presenceInfo"))
    info.set(qn("w15:providerId"), "None")
    info.set(qn("w15:userId"), author)
    pkg.touch(pkg.names["people"])


def comment_body(pkg, cid, text, writer, author=None, initials=None, date=None):
    """The w:comment element: its paragraphs (one per line of text), the annotation mark first."""
    c = etree.Element(w("comment"))
    c.set(w("id"), cid)
    c.set(w("author"), author or writer.author)
    c.set(w("date"), date or writer.date)
    c.set(w("initials"), initials if initials is not None else writer.initials)
    lines_ = str(text).split("\n") or [""]
    for i, line in enumerate(lines_):
        p = etree.SubElement(c, w("p"))
        writer.paragraph_ids(p)
        if style_exists(pkg, "CommentText"):
            etree.SubElement(etree.SubElement(p, w("pPr")), w("pStyle")).set(w("val"), "CommentText")
        if i == 0:
            r = etree.SubElement(p, w("r"))
            if style_exists(pkg, "CommentReference"):
                etree.SubElement(etree.SubElement(r, w("rPr")), w("rStyle")).set(w("val"), "CommentReference")
            etree.SubElement(r, w("annotationRef"))
        if line:
            p.append(new_run(line))
    return c


def register_comment(pkg, c, writer, parent_para=None, done=False):
    """comments.xml gets the comment; commentsExtended.xml its thread parent and done flag; commentsIds.xml a durable
    id; commentsExtensible.xml (when the file has one) its UTC date; people.xml its author."""
    comments = comment_part(pkg, "comments")
    comments.append(c)
    pkg.touch(pkg.names["comments"])
    para = c.findall(w("p"))[-1].get(qn("w14:paraId"))
    extended = comment_part(pkg, "commentsExtended")
    ex = etree.SubElement(extended, qn("w15:commentEx"))
    ex.set(qn("w15:paraId"), para)
    if parent_para:
        ex.set(qn("w15:paraIdParent"), parent_para)
    ex.set(qn("w15:done"), "1" if done else "0")
    pkg.touch(pkg.names["commentsExtended"])
    ids = comment_part(pkg, "commentsIds")
    durable = writer.ids.hex8()
    cid = etree.SubElement(ids, qn("w16cid:commentId"))
    cid.set(qn("w16cid:paraId"), para)
    cid.set(qn("w16cid:durableId"), durable)
    pkg.touch(pkg.names["commentsIds"])
    if pkg.part("commentsExtensible") is not None:
        cex = etree.SubElement(pkg.part("commentsExtensible"), qn("w16cex:commentExtensible"))
        cex.set(qn("w16cex:durableId"), durable)
        cex.set(qn("w16cex:dateUtc"), c.get(w("date")))
        pkg.touch(pkg.names["commentsExtensible"])
    ensure_person(pkg, c.get(w("author")))
    writer.comment_ids.append(c.get(w("id")))


def add_comment(pkg, first, last, p_last, text, writer, author=None, initials=None, date=None):
    """
    A new comment anchored from element `first` to element `last` (siblings or not): commentRangeStart before first,
    commentRangeEnd after last, and the reference run after the end, outside any pending change. -> its id
    """
    cid = writer.ids.new()
    start = etree.Element(w("commentRangeStart"))
    start.set(w("id"), cid)
    end = etree.Element(w("commentRangeEnd"))
    end.set(w("id"), cid)
    first.addprevious(start)
    last.addnext(end)
    holder = end
    while holder.getparent() is not None and holder.getparent() is not p_last and holder.getparent().tag != w("p"):
        holder = holder.getparent()
    holder.addnext(reference_run(pkg, cid))
    register_comment(pkg, comment_body(pkg, cid, text, writer, author, initials, date), writer)
    pkg.touch(pkg.names["document"])
    return cid


def comment_on_text(pkg, p, a, b, text, writer, author=None, initials=None, date=None):
    """A new comment on exactly [a, b) of p's accepted text: the runs are split so it covers just those words."""
    runs = region(p, a, b, writer.ids)
    if not runs:
        raise Refused("the text to comment on is not in the paragraph's runs")
    for _, _, _, _, containers in runs:
        if any(c.tag in (w("fldSimple"),) for c in containers):
            raise Refused("the text is inside a field")
    return add_comment(pkg, runs[0][0], runs[-1][0], p, text, writer, author, initials, date)


def comment_markers(root, cid):
    found = {"start": None, "end": None, "ref": None}
    for el in root.iter(w("commentRangeStart"), w("commentRangeEnd"), w("commentReference")):
        if el.get(w("id")) == cid:
            key = {w("commentRangeStart"): "start", w("commentRangeEnd"): "end",
                   w("commentReference"): "ref"}[el.tag]
            found[key] = found[key] if found[key] is not None else el
    return found


def comment_element(pkg, cid):
    comments = pkg.part("comments")
    return next((c for c in comments.iter(w("comment")) if c.get(w("id")) == cid), None) if comments is not None \
        else None


def ensure_para_id(pkg, c, writer):
    """A comment written by an old tool may have no w14:paraId: a reply or a done flag needs one."""
    last = c.findall(w("p"))[-1] if c.findall(w("p")) else etree.SubElement(c, w("p"))
    if not last.get(qn("w14:paraId")):
        writer.paragraph_ids(last)
        pkg.touch(pkg.names["comments"])
    return last.get(qn("w14:paraId"))


def comment_ex(pkg, para, create_with=None):
    extended = comment_part(pkg, "commentsExtended")
    ex = next((e for e in extended.iter(qn("w15:commentEx")) if e.get(qn("w15:paraId")) == para), None)
    if ex is None and create_with is not None:
        ex = etree.SubElement(extended, qn("w15:commentEx"))
        ex.set(qn("w15:paraId"), para)
        ex.set(qn("w15:done"), "0")
        pkg.touch(pkg.names["commentsExtended"])
    return ex


def add_reply(pkg, parent_id, text, writer, author=None, initials=None, date=None):
    """A reply in parent's thread: its own anchor markers right next to the parent's, and w15:paraIdParent."""
    parent = comment_element(pkg, parent_id)
    if parent is None:
        raise Refused(f"there is no comment c{parent_id}")
    root = pkg.document
    marks = comment_markers(root, parent_id)
    thread = {parent_id} | {c["id"] for c in read_comments({"comments": pkg.part("comments"),
                                                             "commentsExtended": pkg.part("commentsExtended")}).values()
                            if c["parent"] == parent_id}
    cid = writer.ids.new()
    if marks["start"] is not None and marks["end"] is not None:
        start = etree.Element(w("commentRangeStart"))
        start.set(w("id"), cid)
        _after_thread(marks["start"], thread).addnext(start)
        end = etree.Element(w("commentRangeEnd"))
        end.set(w("id"), cid)
        _after_thread(marks["end"], thread).addnext(end)
    if marks["ref"] is not None:
        _after_thread(marks["ref"].getparent(), thread).addnext(reference_run(pkg, cid))
    elif marks["end"] is not None:
        _after_thread(marks["end"], thread).addnext(reference_run(pkg, cid))
    else:
        raise Refused(f"comment c{parent_id} has no anchor in the document to reply at")
    parent_para = ensure_para_id(pkg, parent, writer)
    comment_ex(pkg, parent_para, create_with=True)
    register_comment(pkg, comment_body(pkg, cid, text, writer, author, initials, date), writer,
                     parent_para=parent_para)
    pkg.touch(pkg.names["document"])
    return cid


def _after_thread(el, thread):
    """The last of the thread's markers (or reference runs) that follow el, so a new reply comes after the earlier
    ones: Word lists replies in the order of their markers."""
    def ours(node):
        if node is None or not isinstance(node.tag, str):
            return False
        if node.tag in (w("commentRangeStart"), w("commentRangeEnd")):
            return node.get(w("id")) in thread and node.tag == el.tag
        ref = node.find(w("commentReference")) if node.tag == w("r") else None
        return el.tag == w("r") and ref is not None and ref.get(w("id")) in thread
    while ours(el.getnext()):
        el = el.getnext()
    return el


def resolve_thread(pkg, cid, writer, done=True):
    """Mark a thread resolved (w15:done="1" on its first comment), or open again."""
    c = comment_element(pkg, cid)
    if c is None:
        raise Refused(f"there is no comment c{cid}")
    para = ensure_para_id(pkg, c, writer)
    ex = comment_ex(pkg, para, create_with=True)
    ex.set(qn("w15:done"), "1" if done else "0")
    pkg.touch(pkg.names["commentsExtended"])


def remove_comments(pkg, ids=None):
    """Take comments out (a clean copy): their markers in the document and their entries in every comments part."""
    root = pkg.document
    comments = read_comments({"comments": pkg.part("comments"), "commentsExtended": pkg.part("commentsExtended")})
    ids = set(comments) if ids is None else {str(i) for i in ids}
    paras = {comments[i]["para_id"] for i in ids if i in comments}
    for el in list(root.iter(w("commentRangeStart"), w("commentRangeEnd"))):
        if el.get(w("id")) in ids:
            el.getparent().remove(el)
    for ref in list(root.iter(w("commentReference"))):
        if ref.get(w("id")) in ids:
            r = ref.getparent()
            r.remove(ref)
            if not [c for c in r if c.tag != w("rPr")]:
                r.getparent().remove(r)
    pkg.touch(pkg.names["document"])
    if pkg.part("comments") is not None:
        for c in list(pkg.part("comments").iter(w("comment"))):
            if c.get(w("id")) in ids:
                c.getparent().remove(c)
        pkg.touch(pkg.names["comments"])
    durable = set()
    for kind, tag, attr in (("commentsExtended", "w15:commentEx", "w15:paraId"),
                            ("commentsIds", "w16cid:commentId", "w16cid:paraId")):
        part = pkg.part(kind)
        if part is None:
            continue
        for el in list(part.iter(qn(tag))):
            if el.get(qn(attr)) in paras:
                if kind == "commentsIds":
                    durable.add(el.get(qn("w16cid:durableId")))
                el.getparent().remove(el)
        pkg.touch(pkg.names[kind])
    if pkg.part("commentsExtensible") is not None:
        for el in list(pkg.part("commentsExtensible").iter(qn("w16cex:commentExtensible"))):
            if el.get(qn("w16cex:durableId")) in durable:
                el.getparent().remove(el)
        pkg.touch(pkg.names["commentsExtensible"])
    return len(ids & set(comments))


# ---- accept and reject ---------------------------------------------------------------------------------------------
def _keep_markers(el):
    """Before removing an element: move the range markers inside it (comment anchors, bookmarks) out, in place."""
    for marker in [m for m in el.iter(*RANGE_MARKERS) if m is not el]:
        el.addprevious(marker)
    for ref in [r for r in el.iter(w("r")) if r.find(w("commentReference")) is not None]:
        el.addprevious(ref)


def _remove(el):
    _keep_markers(el)
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _unwrap(el):
    parent = el.getparent()
    for child in list(el):
        el.addprevious(child)
    parent.remove(el)


def merge_with_next(p):
    """A paragraph whose mark goes joins the next paragraph: its content goes in front of the next one's, which keeps
    its own formatting. Before a table an emptied paragraph goes; at the end of a body or cell it stays (Word)."""
    how = mark_goes(p)
    if how == "drop":
        if not any(run_text(r) for r in p.iter(w("r"))) and not list(p.iter(w("drawing"), w("pict"), w("object"))):
            _keep_markers(p)
            p.getparent().remove(p)
            return True
        return False
    nxt = next_paragraph(p)
    if nxt is None:
        return False
    content = [c for c in p if c.tag != w("pPr")]
    anchor = nxt.find(w("pPr"))
    for child in content:
        if anchor is None:
            nxt.insert(0, child)
        else:
            anchor.addnext(child)
        anchor = child
    p.getparent().remove(p)
    return True


def _restore_properties(owner, change):
    """Reject a formatting change: the owner (w:rPr, w:pPr, w:tblPr, ...) gets back the properties stored in it."""
    if change.tag == w("numberingChange"):  # an old attribute-only record: nothing to restore from
        owner.remove(change)
        return
    old = change[0] if len(change) else None
    keep = {w("rPr"), w("sectPr"), w("pPrChange")} if owner.tag == w("pPr") else set()
    keep |= {w("ins"), w("del"), w("moveFrom"), w("moveTo")} | set(CELL_CHANGES)
    keep |= {w("headerReference"), w("footerReference")} if owner.tag == w("sectPr") else set()
    for child in list(owner):
        if child is change or child.tag in keep:
            continue
        owner.remove(child)
    if old is not None:
        anchor = next((c for c in owner if c.tag in (w("rPr"), w("sectPr"))), None) if owner.tag == w("pPr") else None
        for child in list(old):
            if anchor is not None:
                anchor.addprevious(child)
            else:
                change.addprevious(child)
    owner.remove(change)


def resolve(root, decide):
    """
    Accept or reject tracked changes in place (root: a document or any part of one). decide(element) -> 'accept',
    'reject' or 'keep'. Accept: insertions stay (unwrapped), deletions go; reject: the reverse. A paragraph mark that
    goes joins its paragraph to the next; a row that goes takes its cells with it; a formatting change rejected gets
    its old formatting back. Comment anchors and bookmarks inside removed text are kept. -> {'accepted': n,
    'rejected': n}
    """
    done = Counter()
    merges, rows, cells = [], [], []
    for el in list(root.iter(*REVISION_TAGS)):
        if el.getparent() is None or not _attached(el, root):
            continue
        d = decide(el)
        if d not in ("accept", "reject"):
            continue
        done["accepted" if d == "accept" else "rejected"] += 1
        kind = revision_kind(el)
        parent = el.getparent()
        if el.tag in PROPERTY_CHANGES:
            if d == "accept":
                parent.remove(el)
            else:
                _restore_properties(parent, el)
        elif el.tag in CELL_CHANGES:
            tc = parent.getparent()
            parent.remove(el)
            if (el.tag == w("cellIns") and d == "reject") or (el.tag == w("cellDel") and d == "accept"):
                cells.append(tc)
        elif kind.startswith("paragraph mark"):
            p = parent.getparent().getparent()
            parent.remove(el)
            if (el.tag in INSERTS and d == "reject") or (el.tag in DELETES and d == "accept"):
                merges.append(p)
        elif kind.startswith("row"):
            tr = parent.getparent()
            parent.remove(el)
            if (el.tag in INSERTS and d == "reject") or (el.tag in DELETES and d == "accept"):
                rows.append(tr)
        elif kind == "numbering":
            parent.remove(el)
        elif el.tag in INSERTS:
            if d == "accept":
                _unwrap(el)
            else:
                _remove(el)
        else:
            if d == "accept":
                _remove(el)
            else:
                to_kept(el)
                _unwrap(el)
    for p in merges:
        if _attached(p, root):
            merge_with_next(p)
    for tr in rows:
        if _attached(tr, root):
            _keep_markers(tr)
            tr.getparent().remove(tr)
    for tc in cells:
        if _attached(tc, root):
            tc.getparent().remove(tc)
    for marker in list(root.iter(w("moveFromRangeStart"), w("moveFromRangeEnd"), w("moveToRangeStart"),
                                 w("moveToRangeEnd"))):
        if decide(marker) in ("accept", "reject") or not any(True for _ in root.iter(w("moveFrom"), w("moveTo"))):
            marker.getparent().remove(marker)
    for empty in [e for e in root.iter(w("ins"), w("del"), w("moveFrom"), w("moveTo")) if len(e) == 0
                  and e.getparent() is not None and e.getparent().tag not in (w("rPr"), w("trPr"), w("numPr"))]:
        empty.getparent().remove(empty)
    for table in list(root.iter(w("tbl"))):
        if not any(c.tag == w("tr") for c in table) and table.getparent() is not None:
            table.getparent().remove(table)
    return dict(done)


def _attached(el, root):
    node = el
    while node is not None:
        if node is root:
            return True
        node = node.getparent()
    return False


# ---- settings ------------------------------------------------------------------------------------------------------
def set_track_changes(pkg, on=True):
    """w:trackRevisions in settings.xml: the user's next edits in Word are tracked too (OFFICE['track_changes_on'])."""
    settings = pkg.part("settings")
    if settings is None:
        return False
    current = settings.find(w("trackRevisions"))
    if on and current is None:
        insert_ordered(settings, etree.Element(w("trackRevisions")), SETTINGS_ORDER)
    elif on and current.get(w("val")) in ("0", "false", "off"):
        del current.attrib[w("val")]
    elif not on and current is not None:
        settings.remove(current)
    else:
        return False
    pkg.touch(pkg.names["settings"])
    return True


def renumber_duplicates(pkg, writer):
    """Revision ids used twice (26 of them in a past job's charter) get new ids before saving. -> how many"""
    seen, fixed = set(), 0
    reserved = {el.get(w("id")) for el in pkg.document.iter(w("commentRangeStart"), w("commentReference"),
                                                              w("bookmarkStart"))}
    for el in pkg.document.iter(*REVISION_TAGS):
        value = el.get(w("id"))
        if value in seen or value in reserved:
            el.set(w("id"), writer.ids.new())
            fixed += 1
        seen.add(el.get(w("id")))
    if fixed:
        pkg.touch(pkg.names["document"])
    return fixed


# ---- the structural checks (W6, 1 and 5) ---------------------------------------------------------------------------
def _order_problems(parent, order, what):
    rank = {name: i for i, name in enumerate(order)}
    last, problems = -1, []
    for child in parent:
        if not isinstance(child.tag, str) or not child.tag.startswith(f"{{{NS['w']}}}"):
            continue
        r = rank.get(local(child))
        if r is None:
            continue
        if r < last:
            problems.append(f"{what}: w:{local(child)} is out of order")
            break
        last = r
    return problems


def structure_problems(pkg):
    """
    The checks every output must pass before it is saved -> a list of problems (empty: fine). Well-formed parts, the
    order rules, unique ids, valid paragraph ids, deleted text as w:delText, and closed comment anchors with a
    reference, a comment and (for a reply) a parent. Not a full schema validator: the elements the writer touches.
    """
    problems = []
    for name in pkg.changed:
        try:
            etree.fromstring(pkg.bytes_of(name), PARSER)
        except etree.XMLSyntaxError as exc:
            problems.append(f"{name} is not well-formed XML: {exc}")
    if problems:
        return problems
    root = pkg.document
    for r in root.iter(w("r")):
        rpr = r.find(w("rPr"))
        if rpr is not None and r.index(rpr) != 0:
            problems.append("a run's w:rPr is not its first child")
        if rpr is not None:
            problems += _order_problems(rpr, RPR_ORDER, "run properties")
    for p in root.iter(w("p")):
        ppr = p.find(w("pPr"))
        if ppr is not None and p.index(ppr) != 0:
            problems.append("a paragraph's w:pPr is not its first child")
        if ppr is not None:
            problems += _order_problems(ppr, PPR_ORDER, "paragraph properties")
            mark = ppr.find(w("rPr"))
            if mark is not None:
                problems += _order_problems(mark, MARK_RPR_ORDER, "paragraph mark properties")
    for trpr in root.iter(w("trPr")):
        problems += _order_problems(trpr, TRPR_ORDER, "row properties")
    for tcpr in root.iter(w("tcPr")):
        problems += _order_problems(tcpr, TCPR_ORDER, "cell properties")
    ids = Counter(el.get(w("id")) for el in root.iter(*REVISION_TAGS))
    problems += [f"revision id {i} is used {n} times" for i, n in ids.items() if n > 1]
    others = {el.get(w("id")) for el in root.iter(w("commentRangeStart"), w("commentReference"), w("bookmarkStart"))}
    problems += [f"revision id {i} is also a comment or bookmark id" for i in set(ids) & others]
    seen = Counter()
    for part in (root, pkg.part("comments")):
        if part is None:
            continue
        for el in part.iter(w("p")):
            for attr in ("w14:paraId", "w14:textId"):
                value = el.get(qn(attr))
                if value is None:
                    continue
                if not re.fullmatch(r"[0-9A-Fa-f]{8}", value) or int(value, 16) >= 0x80000000:
                    problems.append(f"{attr} {value} is not 8 hexadecimal digits below 80000000")
                if attr == "w14:paraId":
                    seen[value.upper()] += 1
    problems += [f"w14:paraId {v} is used {n} times" for v, n in seen.items() if n > 1]
    for t in root.iter(w("t")):
        if any(a.tag in DELETES for a in t.iterancestors()):
            problems.append("w:t inside a deletion (it must be w:delText)")
            break
    for t in root.iter(w("delText")):
        if not any(a.tag in DELETES for a in t.iterancestors()):
            problems.append("w:delText outside a deletion")
            break
    for t in root.iter(w("t"), w("delText")):
        if t.text and t.text != t.text.strip() and t.get(XML_SPACE) != "preserve":
            problems.append(f"text with spaces at its ends has no xml:space=\"preserve\": \"{t.text[:30]}\"")
            break
    problems += comment_problems(pkg)
    return problems


def comment_problems(pkg):
    """Every comment anchor closed, every comment with a reference and an entry, every reply with a parent."""
    root, problems = pkg.document, []
    starts = Counter(el.get(w("id")) for el in root.iter(w("commentRangeStart")))
    ends = Counter(el.get(w("id")) for el in root.iter(w("commentRangeEnd")))
    refs = Counter(el.get(w("id")) for el in root.iter(w("commentReference")))
    part = pkg.part("comments")
    known = {c.get(w("id")) for c in part.iter(w("comment"))} if part is not None else set()
    for cid in set(starts) | set(ends):
        if starts[cid] != 1 or ends[cid] != 1:
            problems.append(f"comment {cid}: its anchor is not one start and one end")
    for cid in set(starts) | set(refs):
        if cid not in known:
            problems.append(f"comment {cid} is in the text but not in comments.xml")
    for cid in known:
        if refs[cid] != 1:
            problems.append(f"comment {cid} has {refs[cid]} references in the text (1 expected)")
    if pkg.part("commentsExtended") is not None and part is not None:
        paras = {c.findall(w("p"))[-1].get(qn("w14:paraId")) for c in part.iter(w("comment")) if c.findall(w("p"))}
        for ex in pkg.part("commentsExtended").iter(qn("w15:commentEx")):
            parent = ex.get(qn("w15:paraIdParent"))
            if parent and parent not in paras:
                problems.append(f"a reply's parent {parent} is not a comment")
    return problems


# ---- the Python compare (compare_docx without Word) ----------------------------------------------------------------
def similarity(a, b):
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, tokens(a), tokens(b), autojunk=False).ratio()


def pair_up(old, new, key, score, threshold=0.45):
    """
    Align two lists (paragraphs, rows) -> [(old item or None, new item or None)] in order. difflib on the keys finds
    the stretches that are the same; inside a changed stretch, items are paired by similarity (a dynamic program that
    keeps the order), so a deleted paragraph doesn't shift the ones after it, and a reworded one is diffed word by word.
    """
    out = []
    matcher = difflib.SequenceMatcher(None, [key(x) for x in old], [key(x) for x in new], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out += list(zip(old[i1:i2], new[j1:j2]))
            continue
        a, b = old[i1:i2], new[j1:j2]
        n, m = len(a), len(b)
        if n * m > 40000:  # too big a changed stretch to pair by similarity: pair in order, the rest in or out
            out += [(a[k] if k < n else None, b[k] if k < m else None) for k in range(max(n, m))]
            continue
        scores = [[score(x, y) for y in b] for x in a]
        sc = lambda i, j, scores=scores: scores[i][j]  # noqa: E731  (bound now, not to the loop's next stretch)
        best = [[0.0] * (m + 1) for _ in range(n + 1)]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                s = sc(i, j)
                best[i][j] = max(best[i + 1][j], best[i][j + 1], best[i + 1][j + 1] + s if s >= threshold else -1)
        i = j = 0
        while i < n or j < m:
            if i < n and j < m and sc(i, j) >= threshold and \
                    abs(best[i][j] - (best[i + 1][j + 1] + sc(i, j))) < 1e-9:
                out.append((a[i], b[j]))
                i, j = i + 1, j + 1
            elif i < n and (j >= m or best[i][j] == best[i + 1][j]):
                out.append((a[i], None))
                i += 1
            else:
                out.append((None, b[j]))
                j += 1
    return out


def _formats(new_p):
    """For new text taken from the revised paragraph: (start, end) in its text -> [(text, rPr)] by its own runs."""
    runs = [(r, s, e) for r, s, e, _, _ in visible_runs(new_p) if e > s]

    def pieces(start, end):
        out = []
        for r, s, e in runs:
            lo, hi = max(s, start), min(e, end)
            if lo < hi:
                out.append((run_text(r)[lo - s:hi - s], r.find(w("rPr"))))
        return out
    return pieces


def block_key(el):
    from .word_model import accepted_text
    if el.tag == w("p"):
        return "p:" + accepted_text(el)
    rows = [c for c in el if c.tag == w("tr")]
    return "t:" + (" | ".join(accepted_text(p) for p in rows[0].iter(w("p"))) if rows else "")


def block_score(a, b):
    if a.tag != b.tag:
        return 0.0
    if a.tag == w("tbl"):
        return 1.0
    return similarity(block_key(a)[2:], block_key(b)[2:])


def insert_copy(new_el, cursor, container, writer):
    """A block of the revised file that the original lacks, copied in after cursor as a tracked insertion."""
    el = clean_copy(new_el, writer)
    made = []
    if el.tag == w("p"):
        runs = [x for kind, x, _, _ in walk(el) if kind == "run"]
        made += _insert_wrapped(runs, writer)
        made.append(mark_paragraph(el, "ins", writer))
    else:
        for tr in (c for c in el if c.tag == w("tr")):
            made.append(mark_row(tr, "ins", writer))
            for p in tr.iter(w("p")):
                made += _insert_wrapped([x for kind, x, _, _ in walk(p) if kind == "run"], writer)
                made.append(mark_paragraph(p, "ins", writer))
    if cursor is None:
        first = next((c for c in container if c.tag in (w("p"), w("tbl"))), None)
        if first is not None:
            first.addprevious(el)
        else:
            _before_section(container, el)
    else:
        cursor.addnext(el)
    if el.tag == w("p") and next_paragraph(el) is None:
        # the container's last paragraph: the mark before it is the tracked one (see insert_paragraphs_after)
        rev = mark_revision(el)
        rev.getparent().remove(rev)
        writer.revision_ids.remove(rev.get(w("id")))
        made.remove(rev)
        before = el.getprevious()
        if before is not None and before.tag == w("p") and mark_revision(before) is None:
            made.append(mark_paragraph(before, "ins", writer))
    return made, el


def _before_section(container, el):
    sect = container.find(w("sectPr"))
    (sect.addprevious(el) if sect is not None else container.append(el))


def _insert_wrapped(runs, writer):
    groups, made = [], []
    for r in runs:
        if groups and groups[-1][-1].getparent() is r.getparent() and _adjacent(groups[-1][-1], r):
            groups[-1].append(r)
        else:
            groups.append([r])
    for group in groups:
        made.append(wrap(group, writer.revision("ins")))
    return made


def delete_block(el, writer):
    if el.tag == w("p"):
        runs = [x for kind, x, _, _ in walk(el) if kind == "run"]
        made = delete_runs(runs, writer) if runs else []
        return made + (delete_mark(el, writer) if mark_revision(el) is None else [])
    made = []
    for tr in (c for c in el if c.tag == w("tr")):
        made.append(mark_row(tr, "del", writer))
        for p in tr.iter(w("p")):
            runs = [x for kind, x, _, _ in walk(p) if kind == "run"]
            made += delete_runs(runs, writer) if runs else []
    return made


def compare_paragraph(old_p, new_p, writer):
    from .word_model import accepted_text
    old_text, new_text = accepted_text(old_p), accepted_text(new_p)
    if old_text == new_text:
        return []
    if not old_text:
        made = []
        pieces = _formats(new_p)(0, len(new_text))
        ins = writer.revision("ins")
        for piece, rpr in pieces:
            ins.append(new_run(piece, rpr))
        old_p.append(ins)
        return [ins]
    made, _ = tracked_replace(old_p, 0, len(old_text), new_text, writer, rpr_for=_formats(new_p))
    return made


def compare_containers(old, new, writer):
    """Two bodies or two cells: their paragraphs and tables aligned, then diffed. -> revision elements made"""
    olds = [c for c in old if c.tag in (w("p"), w("tbl"))]
    news = [c for c in new if c.tag in (w("p"), w("tbl"))]
    made, cursor = [], None
    for a, b in pair_up(olds, news, block_key, block_score):
        if a is not None and b is not None and a.tag == b.tag:
            try:
                made += compare_paragraph(a, b, writer) if a.tag == w("p") else compare_tables(a, b, writer)
                cursor = a
                continue
            except Refused:  # a field or hyperlink changed: the whole paragraph out, the new one in
                pass
        if a is not None:
            made += delete_block(a, writer)
            cursor = a
        if b is not None:
            more, cursor = insert_copy(b, cursor, old, writer)
            made += more
    return made


def row_key(tr):
    from .word_model import accepted_text
    return " | ".join(accepted_text(p) for p in tr.iter(w("p")))


def compare_tables(old, new, writer):
    olds = [c for c in old if c.tag == w("tr")]
    news = [c for c in new if c.tag == w("tr")]
    made, cursor = [], None
    pairs = pair_up(olds, news, row_key, lambda a, b: similarity(row_key(a), row_key(b)), threshold=0.5)
    for a, b in pairs:
        if a is not None and b is not None and len([c for c in a if c.tag == w("tc")]) == \
                len([c for c in b if c.tag == w("tc")]):
            for tc_a, tc_b in zip((c for c in a if c.tag == w("tc")), (c for c in b if c.tag == w("tc"))):
                made += compare_containers(tc_a, tc_b, writer)
            cursor = a
        else:
            if a is not None:
                made += delete_block_row(a, writer)
                cursor = a
            if b is not None:
                row = clean_copy(b, writer)
                made.append(mark_row(row, "ins", writer))
                for p in row.iter(w("p")):
                    made += _insert_wrapped([x for kind, x, _, _ in walk(p) if kind == "run"], writer)
                    made.append(mark_paragraph(p, "ins", writer))
                if cursor is None:
                    first = next((c for c in old if c.tag == w("tr")), None)
                    first.addprevious(row)
                else:
                    cursor.addnext(row)
                cursor = row
    return made


def delete_block_row(tr, writer):
    made = [mark_row(tr, "del", writer)]
    for p in tr.iter(w("p")):
        runs = [x for kind, x, _, _ in walk(p) if kind == "run"]
        made += delete_runs(runs, writer) if runs else []
    return made


def python_compare(original, revised, author, initials="", date=None):
    """
    A redline of two versions without Word: the original's file (styles, comments, headers kept), with every change
    that turns its text into the revised file's as a tracked change by `author`. Both files are read with their own
    tracked changes accepted, as Word's Compare does. Paragraph formatting, headers, footers, footnotes and text boxes
    are not compared; new text takes the revised file's run formatting. -> (the package, to save; the writer: its ids)
    """
    pkg = Package(original)
    other = Package(revised)
    resolve(pkg.document, ACCEPT)
    resolve(other.document, ACCEPT)
    writer = Writer(pkg, author, initials, date)
    compare_containers(body_of(pkg.document), body_of(other.document), writer)
    pkg.touch(pkg.names["document"])
    return pkg, writer
