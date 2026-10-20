"""
docx_view: everything a Word document holds for a review, read straight from its XML (OFFICE_COLLAB_DESIGN.md, F3).

A .docx is a ZIP of XML parts. python-docx's paragraph.text reads only the runs directly under a paragraph, so it misses
the text inside a tracked insertion, and it has no deletions, formatting changes, threads or done flags at all. This
reader uses lxml on the parts themselves:

  word/document.xml           paragraphs, tables and the tracked changes in them (w:ins, w:del, w:rPrChange, ...)
  word/comments.xml           each comment: author, initials, date, text
  word/commentsExtended.xml   the threads: a reply's parent (w15:paraIdParent) and whether it is done (w15:done)
  word/people.xml             the authors
  word/settings.xml           track changes on (w:trackRevisions), protection (w:documentProtection)

Each paragraph comes in three forms:

  marked    the fire {+grew quickly|A}{-spread|A} [c4]    every change shown, with its author's key (A = A. Reviewer)
  accepted  the fire grew quickly                          every change accepted
  original  the fire spread                                every change rejected

and the census counts the changes the way Word's Revisions.Count does: one change is a stretch of text one author
inserted (or deleted, or reformatted) with nothing else in between. It answers "what is still pending in this file?".

lines() is the same reading for a whole document, with each revision decided (accept, reject, or keep it pending). The
writer's checks use it (docx_markup.py, word_review.py): "reject the harness's changes and you get the input back" is
lines(output, reject ours) == lines(input). Plain Python: no model reads the XML. The LLM sees the census and a few
threads; the whole view goes to review.json and review.md in the run folder.
"""

import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16cex": "http://schemas.microsoft.com/office/word/2018/wordml/cex",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PARSER = etree.XMLParser(huge_tree=True, resolve_entities=False)


def qn(name):
    """'w:ins' -> '{http://...main}ins', the name lxml uses."""
    prefix, local = name.split(":")
    return f"{{{NS[prefix]}}}{local}"


def w(local):
    return f"{{{NS['w']}}}{local}"


W_ID = w("id")


# ---- the kinds of tracked change ------------------------------------------------------------------------------------
INSERTS = {w("ins"), w("moveTo")}
DELETES = {w("del"), w("moveFrom")}
PROPERTY_CHANGES = {w("rPrChange"): "formatting", w("pPrChange"): "paragraph formatting",
                    w("sectPrChange"): "section formatting", w("tblPrChange"): "table formatting",
                    w("tblPrExChange"): "table formatting", w("trPrChange"): "row formatting",
                    w("tcPrChange"): "cell formatting", w("tblGridChange"): "table grid",
                    w("numberingChange"): "numbering"}
CELL_CHANGES = {w("cellIns"): "cell inserted", w("cellDel"): "cell deleted", w("cellMerge"): "cell merged"}
REVISION_TAGS = tuple(INSERTS | DELETES | set(PROPERTY_CHANGES) | set(CELL_CHANGES))
# what may hold runs inside a paragraph: a reader looks inside; the writer refuses to edit inside the ones
# it doesn't know
RUN_CONTAINERS = {w("ins"), w("del"), w("moveFrom"), w("moveTo"), w("hyperlink"), w("smartTag"), w("sdt"),
                  w("sdtContent"), w("fldSimple"), w("customXml"), w("dir"), w("bdo")}
RANGE_MARKERS = {w("commentRangeStart"), w("commentRangeEnd"), w("bookmarkStart"), w("bookmarkEnd"),
                 w("moveFromRangeStart"), w("moveFromRangeEnd"), w("moveToRangeStart"), w("moveToRangeEnd"),
                 w("permStart"), w("permEnd"), w("proofErr")}
TEXT_ATOMS = {w("t"): None, w("delText"): None, w("tab"): "\t", w("br"): "\n", w("cr"): "\n"}


def revision_kind(el):
    """What a revision element changes, in words: 'insertion', 'paragraph mark deleted', 'row inserted', ..."""
    tag, parent = el.tag, el.getparent()
    if tag in PROPERTY_CHANGES:
        return PROPERTY_CHANGES[tag]
    if tag in CELL_CHANGES:
        return CELL_CHANGES[tag]
    inserted = tag in INSERTS
    where = parent.tag if parent is not None else None
    if where == w("rPr") and parent.getparent() is not None and parent.getparent().tag == w("pPr"):
        return "paragraph mark inserted" if inserted else "paragraph mark deleted"
    if where == w("trPr"):
        return "row inserted" if inserted else "row deleted"
    if where == w("numPr"):
        return "numbering"
    if tag == w("moveTo"):
        return "moved to"
    if tag == w("moveFrom"):
        return "moved from"
    return "insertion" if inserted else "deletion"


def change_class(kind):
    """Changes that Word counts as one when they touch: inserted text runs on into an inserted paragraph mark."""
    if kind in ("insertion", "paragraph mark inserted", "moved to", "row inserted"):
        return "inserted"
    if kind in ("deletion", "paragraph mark deleted", "moved from", "row deleted"):
        return "deleted"
    return kind


# ---- reading the parts ----------------------------------------------------------------------------------------------
def part_names(read):
    """read(name) -> bytes or None. The parts this reader needs, found through the relationships, as Word does:
    {'document': 'word/document.xml', 'comments': 'word/comments.xml', 'commentsExtended': ..., 'people': ...}."""
    rels = etree.fromstring(read("_rels/.rels"), PARSER)
    doc = next(r.get("Target") for r in rels if r.get("Type", "").endswith("/officeDocument")).lstrip("/")
    folder = posixpath.dirname(doc)
    names = {"document": doc, "document_rels": f"{folder}/_rels/{posixpath.basename(doc)}.rels"}
    data = read(names["document_rels"])
    for r in etree.fromstring(data, PARSER) if data else []:
        if r.get("TargetMode") == "External":
            continue
        target = r.get("Target", "")
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        names.setdefault(r.get("Type", "").rsplit("/", 1)[-1], target)
    return names


WANTED = ("document", "comments", "commentsExtended", "commentsIds", "people", "settings", "styles")


def read_parts(source):
    """A .docx path, or a docx_markup.Package (which may hold edited parts) -> {kind: parsed XML root or None}."""
    if hasattr(source, "xml"):  # a Package
        return {kind: source.xml(source.names[kind]) if kind in source.names and source.has(source.names[kind])
                else None for kind in WANTED}
    with zipfile.ZipFile(source) as z:
        files = set(z.namelist())
        read = lambda name: z.read(name) if name in files else None
        names = part_names(read)
        return {kind: etree.fromstring(read(names[kind]), PARSER) if names.get(kind) in files else None
                for kind in WANTED}


def style_names(styles):
    """styles.xml -> {styleId: name}: a paragraph says pStyle 'Heading1', a person reads 'heading 1'."""
    if styles is None:
        return {}
    return {s.get(w("styleId")): (s.find(w("name")).get(w("val")) if s.find(w("name")) is not None
                                  else s.get(w("styleId")))
            for s in styles.iter(w("style"))}


def heading_level(p, styles):
    """'Title' -> 0, 'heading 2' -> 2, an outline level -> it, body text -> None."""
    ppr = p.find(w("pPr"))
    style = ppr.find(w("pStyle")).get(w("val")) if ppr is not None and ppr.find(w("pStyle")) is not None else ""
    name = styles.get(style, style).lower()
    if name == "title":
        return 0
    if m := re.match(r"heading\s*(\d)", name):
        return int(m.group(1))
    level = ppr.find(w("outlineLvl")) if ppr is not None else None
    if level is not None and level.get(w("val"), "9").isdigit() and int(level.get(w("val"))) < 9:
        return int(level.get(w("val"))) + 1
    return None


# ---- the paragraphs, in reading order --------------------------------------------------------------------------------
def body_of(root):
    return root.find(w("body"))


def paragraphs_of(root):
    """Every paragraph of the body and its tables, in reading order. Not the paragraphs inside text boxes: they sit
    inside a run of another paragraph, and the review tools leave them alone (W1)."""
    out = []
    for p in body_of(root).iter(w("p")):
        if not any(a.tag in (w("p"), w("txbxContent")) for a in p.iterancestors()):
            out.append(p)
    return out


def walk(p):
    """
    The content of one paragraph, in order -> (kind, element, enclosing revisions, enclosing containers):
      ('run', w:r, [w:ins ...], [w:hyperlink ...])  a run, with the tracked changes and other containers around it
      ('start' | 'end', w:commentRangeStart/End, ...)  where a comment's anchor begins and ends
    Runs inside a text box or an equation are not the paragraph's own text, so they are not visited.
    """
    def visit(element, revisions, containers):
        for child in element:
            tag = child.tag
            if tag == w("r"):
                yield "run", child, revisions, containers
            elif tag in (w("commentRangeStart"), w("commentRangeEnd")):
                yield ("start" if tag == w("commentRangeStart") else "end"), child, revisions, containers
            elif tag in INSERTS or tag in DELETES:
                yield from visit(child, revisions + [child], containers)
            elif tag in RUN_CONTAINERS:
                yield from visit(child, revisions, containers + [child])
    yield from visit(p, [], [])


def run_text(r, deleted=True):
    """The characters of one run: w:t, w:delText (if deleted), tabs and breaks. Field codes are not text."""
    out = []
    for child in r:
        if child.tag == w("t") or (deleted and child.tag == w("delText")):
            out.append(child.text or "")
        elif child.tag in (w("tab"), w("br"), w("cr")):
            out.append(TEXT_ATOMS[child.tag])
    return "".join(out)


def mark_revision(p):
    """The tracked change on a paragraph's mark (the pilcrow), or None: inserted or deleted with the paragraph."""
    rpr = p.find(f"{w('pPr')}/{w('rPr')}")
    if rpr is None:
        return None
    return next((c for c in rpr if c.tag in INSERTS or c.tag in DELETES), None)


def row_revision(tr):
    trpr = tr.find(w("trPr"))
    return next((c for c in trpr if c.tag in INSERTS or c.tag in DELETES), None) if trpr is not None else None


def cell_revision(tc):
    tcpr = tc.find(w("tcPr"))
    return next((c for c in tcpr if c.tag in CELL_CHANGES), None) if tcpr is not None else None


# ---- deciding revisions: accept, reject, or keep pending -------------------------------------------------------------
def KEEP(el):
    return "keep"


def ACCEPT(el):
    return "accept"


def REJECT(el):
    return "reject"


def deciding(ids=None, authors=None, action="accept", others="keep"):
    """A decider: revisions with one of these ids (or by one of these authors) get `action`, the rest `others`."""
    ids = {str(i) for i in ids} if ids is not None else None
    authors = set(authors) if authors is not None else None

    def decide(el):
        chosen = ((ids is None or el.get(w("id")) in ids) and (authors is None or el.get(w("author")) in authors))
        return action if chosen else others
    return decide


def visible(revisions, decide):
    """Is text inside these (nested) revisions there, with them decided? -> (shown, the pending mark or None)."""
    mark = None
    for rev in revisions:
        d = decide(rev)
        if rev.tag in INSERTS:
            if d == "reject":
                return False, None
            if d == "keep":
                mark = ("+", rev.get(w("author"), ""))
        else:
            if d == "accept":
                return False, None
            if d == "keep":
                mark = ("-", rev.get(w("author"), ""))
    return True, mark


def canon(el, skip=()):
    """A canonical string for an element (formatting compared, not bytes): tag, sorted attributes, children.
    Bookkeeping (rsids, a tracked change's w:id, which a split run's copy gets anew) and the elements in `skip` don't
    count."""
    if el is None:
        return ""
    attrs = sorted((k, v) for k, v in el.attrib.items() if "rsid" not in k and k != W_ID)
    inner = "".join(canon(c, skip) for c in el if c.tag not in skip and isinstance(c.tag, str))
    return f"<{etree.QName(el).localname}{attrs}{inner}>"


def props(el, skip=()):
    """canon() of a properties element, where an empty one (the writer may add an empty w:pPr) is no properties."""
    if el is None or (not any(c.tag not in skip for c in el if isinstance(c.tag, str))
                      and not any("rsid" not in k for k in el.attrib)):
        return ""
    return canon(el, skip)


def run_format(r, decide):
    """A run's formatting with its tracked formatting change decided: rejected = the old formatting."""
    rpr = r.find(w("rPr"))
    if rpr is None:
        return ""
    change = rpr.find(w("rPrChange"))
    if change is None:
        return props(rpr)
    d = decide(change)
    if d == "reject":
        return props(change.find(w("rPr")))
    return props(rpr, skip=(w("rPrChange"),)) if d == "accept" else props(rpr)


def paragraph_format(p, decide):
    """A paragraph's own formatting (style, spacing, ...) with its pPrChange decided; the mark's revision is not part
    of it (that is the paragraph's existence, compared separately)."""
    ppr = p.find(w("pPr"))
    if ppr is None:
        return ""
    mark_skip = (w("ins"), w("del"), w("moveFrom"), w("moveTo"))
    mark_format = props(ppr.find(w("rPr")), skip=mark_skip)
    change = ppr.find(w("pPrChange"))
    own = props(ppr, skip=(w("rPr"), w("pPrChange"), w("sectPr")))
    if change is not None:
        d = decide(change)
        if d == "reject":
            own = props(change.find(w("pPr")))
        elif d == "keep":
            own += canon(change)
    return own + "|" + mark_format if own or mark_format else ""


def segments(p, decide=KEEP):
    """One paragraph, decided -> [(text, mark, author, formatting)]: mark '+' or '-' for a change still pending,
    '' for plain text. Neighbours with the same mark, author and formatting are joined."""
    out = []
    for kind, el, revisions, _ in walk(p):
        if kind != "run":
            continue
        shown, mark = visible(revisions, decide)
        if not shown:
            continue
        text = run_text(el)
        if not text:
            continue
        sign, author = mark or ("", "")
        fmt = run_format(el, decide)
        if out and out[-1][1:] == (sign, author, fmt):
            out[-1] = (out[-1][0] + text, sign, author, fmt)
        else:
            out.append((text, sign, author, fmt))
    return out


def text_of(p, decide=ACCEPT):
    return "".join(s[0] for s in segments(p, decide))


def accepted_text(p):
    return text_of(p, ACCEPT)


def original_text(p):
    return text_of(p, REJECT)


def mark_kept(rev, decide):
    """Is a paragraph mark (or row) with this revision there after deciding? -> (there, pending mark or None)."""
    if rev is None:
        return True, None
    shown, mark = visible([rev], decide)
    return shown, mark


def next_block(p):
    """The block after a paragraph (a paragraph, table or content control), past bookmarks and comment markers; None
    at the end of a body or cell (the section break doesn't count)."""
    for sibling in p.itersiblings():
        if not isinstance(sibling.tag, str) or sibling.tag in RANGE_MARKERS:
            continue
        return sibling if sibling.tag in (w("p"), w("tbl"), w("sdt"), w("customXml")) else None
    return None


def next_paragraph(p):
    """The paragraph a paragraph joins when its mark goes: the next block, if it is a paragraph."""
    block = next_block(p)
    return block if block is not None and block.tag == w("p") else None


def mark_goes(p):
    """What happens to a paragraph whose mark is accepted away (or rejected away), as Word does it (checked through
    COM): 'join' the next paragraph; 'drop' it, if a table follows and nothing of it is left; else 'stay' (the last
    paragraph of a body or cell keeps its mark)."""
    block = next_block(p)
    if block is None:
        return "stay"
    return "join" if block.tag == w("p") else "drop"


def where_of(p, tables):
    """'body' or 'table 2, row 3, cell 1', for a person."""
    tc = next((a for a in p.iterancestors(w("tc"))), None)
    if tc is None:
        return "body"
    tr = tc.getparent()
    tbl = tr.getparent()
    row = [c for c in tbl if c.tag == w("tr")].index(tr) + 1
    cell = [c for c in tr if c.tag == w("tc")].index(tc) + 1
    return f"table {tables.get(tbl, '?')}, row {row}, cell {cell}"


def table_numbers(root):
    """Top-level and nested tables numbered in reading order: {w:tbl element: 1, ...}."""
    return {t: i for i, t in enumerate(body_of(root).iter(w("tbl")), 1)}


def lines(root, decide=KEEP, formatting=True):
    """
    The whole document, every revision decided -> [(where, text, signature)], one item per paragraph that is there.
    A paragraph whose mark goes (an inserted mark rejected, a deleted one accepted) joins the next paragraph, as in
    Word; a row or cell that goes takes its paragraphs with it. signature holds each stretch's pending mark, author
    and formatting, and the paragraph's own formatting, so two documents with the same signature differ in nothing a
    reader could see (formatting=False: text and pending marks only).
    """
    tables = table_numbers(root)
    out, carry = [], []

    def gone(element_rev):
        return element_rev is not None and not mark_kept(element_rev, decide)[0]

    for p in paragraphs_of(root):
        tr = next(p.iterancestors(w("tr")), None)
        tc = next(p.iterancestors(w("tc")), None)
        if (tr is not None and any(gone(row_revision(r)) for r in [tr] + list(tr.iterancestors(w("tr"))))) or \
                (tc is not None and gone(cell_revision(tc))):
            continue
        segs = carry + segments(p, decide)
        carry = []
        rev = mark_revision(p)
        kept, pending = mark_kept(rev, decide)
        if not kept and mark_goes(p) == "join":
            carry = segs  # joins the next paragraph, which keeps its own formatting
            continue
        if not kept and mark_goes(p) == "drop" and not segs:
            continue  # an emptied paragraph before a table goes
        text = "".join(s[0] for s in segs)
        signature = tuple((s[0], s[1], s[2]) + ((s[3],) if formatting else ()) for s in segs)
        signature += (("mark", pending or ("", "")),)
        if formatting:
            signature += (("format", paragraph_format(p, decide)),)
        out.append((where_of(p, tables), text, signature))
    return out


def texts(root, decide=ACCEPT):
    """Just the text of each paragraph that is there, decided (accept all: what the document says when every change
    is accepted)."""
    return [text for _, text, _ in lines(root, decide, formatting=False)]


# ---- comments ------------------------------------------------------------------------------------------------------
def comment_text(c):
    return "\n".join("".join(run_text(r, deleted=False) for r in p.iter(w("r"))) for p in c.iter(w("p"))).strip()


def comment_para_id(c):
    """The w14:paraId of a comment's last paragraph: commentsExtended.xml names the comment by it."""
    paras = list(c.iter(w("p")))
    return paras[-1].get(qn("w14:paraId")) if paras else None


def read_comments(parts):
    """comments.xml + commentsExtended.xml -> {id: {...}} with each reply's parent and each thread's done flag."""
    comments = {}
    if parts.get("comments") is None:
        return comments
    extended = {}
    if parts.get("commentsExtended") is not None:
        for ex in parts["commentsExtended"].iter(qn("w15:commentEx")):
            extended[ex.get(qn("w15:paraId"))] = ex
    by_para = {}
    for c in parts["comments"].iter(w("comment")):
        cid = c.get(w("id"))
        para = comment_para_id(c)
        ex = extended.get(para)
        comments[cid] = {"id": cid, "label": f"c{cid}", "author": c.get(w("author"), ""),
                         "initials": c.get(w("initials"), ""), "date": c.get(w("date"), ""), "text": comment_text(c),
                         "para_id": para, "parent_para": ex.get(qn("w15:paraIdParent")) if ex is not None else None,
                         "done": ex is not None and ex.get(qn("w15:done")) in ("1", "true"), "parent": None,
                         "replies": [], "anchor": "", "anchored": False, "referenced": False}
        if para:
            by_para[para] = cid
    for c in comments.values():
        if c["parent_para"] and c["parent_para"] in by_para:
            c["parent"] = by_para[c["parent_para"]]
            comments[c["parent"]]["replies"].append(c["id"])
    return comments


def thread_root(comments, cid):
    seen = set()
    while comments[cid]["parent"] and cid not in seen:
        seen.add(cid)
        cid = comments[cid]["parent"]
    return cid


def thread_line(comments, cid, limit=70):
    """'c4 A. Reviewer, 2026-10-02: "Which duct sizes?" on "the exhaust duct" (2 replies, open)'"""
    c = comments[cid]
    replies = len(c["replies"])
    text = c["text"] if len(c["text"]) <= limit else c["text"][:limit - 3] + "..."
    anchor = c["anchor"] if len(c["anchor"]) <= 50 else c["anchor"][:47] + "..."
    return (f"{c['label']} {c['author']}, {c['date'][:10]}: \"{text}\"" + (f" on \"{anchor}\"" if anchor else "")
            + f" ({replies} repl{'y' if replies == 1 else 'ies'}, {'resolved' if c['done'] else 'open'})")


# ---- the view ------------------------------------------------------------------------------------------------------
def initials_of(author, comments):
    """'A. Reviewer' -> 'AR': the initials its comments carry, else the capitals of its name (the part before a
    comma or bracket); the harness's own author -> 'GBH'."""
    if "glass-box harness" in author.lower():
        return "GBH"
    given = next((c["initials"] for c in comments.values() if c["author"] == author and c["initials"]), "")
    if given:
        return given
    name = re.split(r"[,(]", author)[0]
    return "".join(ch for ch in name if ch.isupper())[:3] or name[:2] or "?"


def author_keys(root, comments):
    """Initials for each author, unique, in the order they first appear: the marked text says {+grew|AR}."""
    keys = {}
    authors = [el.get(w("author")) for el in body_of(root).iter(*REVISION_TAGS)] + [c["author"] for c in
                                                                                    comments.values()]
    for author in authors:
        if author and author not in keys:
            key = initials_of(author, comments)
            n = 2
            while key in keys.values():
                key = f"{initials_of(author, comments)}{n}"
                n += 1
            keys[author] = key
    return keys


def marked_text(p, keys, decide=KEEP):
    """'the fire {+grew quickly|A}{-spread|A} [c4]': pending changes with their author's key, formatting changes as
    {~text|B}, the paragraph's own mark when it is inserted or deleted, comments where their reference is."""
    out = []
    for kind, el, revisions, _ in walk(p):
        if kind != "run":
            continue
        shown, mark = visible(revisions, decide)
        if not shown:
            continue
        for ref in el.iter(w("commentReference")):
            out.append(f" [c{ref.get(w('id'))}]")
        text = run_text(el)
        if not text:
            continue
        change = el.find(f"{w('rPr')}/{w('rPrChange')}")
        if mark:
            out.append(f"{{{mark[0]}{text}|{keys.get(mark[1], '?')}}}")
        elif change is not None and decide(change) == "keep":
            out.append(f"{{~{text}|{keys.get(change.get(w('author')), '?')}}}")
        else:
            out.append(text)
    rev = mark_revision(p)
    if rev is not None and decide(rev) == "keep":
        out.append(f"{{{'+' if rev.tag in INSERTS else '-'}¶|{keys.get(rev.get(w('author')), '?')}}}")
    return re.sub(r"\|(\w+)\}\{([+\-~])", lambda m: f"|{m.group(1)}}}{{{m.group(2)}", "".join(out))


def revisions_of(root, tables):
    """Every revision element -> [{id, author, date, kind, paragraph, where, text, element}] in document order."""
    numbers = {p: i for i, p in enumerate(paragraphs_of(root), 1)}
    out = []
    for el in body_of(root).iter(*REVISION_TAGS):
        kind = revision_kind(el)
        p = el if el.tag == w("p") else next(el.iterancestors(w("p")), None)
        if kind.startswith("row"):
            tr = el.getparent().getparent()
            text = " | ".join(accepted_text(q) or original_text(q) for q in tr.iter(w("p")))
            first = next(tr.iter(w("p")), None)
            number = numbers.get(first)
        elif kind.startswith("cell"):
            tc = el.getparent().getparent()
            text = " ".join(original_text(q) for q in tc.iter(w("p")))
            number = numbers.get(next(tc.iter(w("p")), None))
        elif kind.startswith("paragraph mark"):
            text, number = "¶", numbers.get(p)
        elif kind == "formatting":
            r = el.getparent().getparent()
            text = run_text(r) if r.tag == w("r") else "¶"
            number = numbers.get(p)
        elif kind in ("paragraph formatting",):
            text, number = accepted_text(p) if p is not None else "", numbers.get(p)
        elif el.tag in INSERTS or el.tag in DELETES:
            text = "".join(run_text(r) for r in el.iter(w("r")))
            number = numbers.get(p)
        else:
            text, number = "", numbers.get(p)
        out.append({"id": el.get(w("id")), "author": el.get(w("author"), ""), "date": el.get(w("date"), ""),
                    "kind": kind, "paragraph": number,
                    "where": where_of(p, tables) if p is not None else "", "text": text, "element": el})
    return out


def change_key(el):
    kind = revision_kind(el)
    return (change_class(kind) if el.tag not in PROPERTY_CHANGES else "formatting", el.get(w("author")),
            el.get(w("date")))


def changes_as_word_counts(root):
    """
    Group the revision elements into changes as Word's Revisions collection counts them (checked against Word through
    COM): text one author inserted that runs on into an inserted paragraph mark and the next paragraph's inserted text
    is one change; a deletion next to an insertion is two; an inserted table row, or several in a row, is one; an
    insertion with another author's deletion inside it is one insertion and one deletion. A stretch ends at text that
    is not part of it, at a table cell, and at another kind, author or date. -> [[revision element, ...], ...]
    """
    groups, open_ = [], {}  # open_: (kind, author, date) -> the group still growing
    # the text and marks inside a tracked row, counted with the row: id -> element (an lxml element's id() only
    # stays the same while a reference to it is alive, so the dict keeps one)
    taken = {}

    def close_all():
        open_.clear()

    def touch(elements):
        """One stretch of the document carries these revisions: their groups go on, every other one ends."""
        keys = {}
        for el in elements:
            if id(el) not in taken:
                keys.setdefault(change_key(el), []).append(el)
        for key in [k for k in open_ if k not in keys]:
            del open_[key]
        for key, els in keys.items():
            if key not in open_:
                open_[key] = []
                groups.append(open_[key])
            for el in els:
                if not any(el is x for x in open_[key]):
                    open_[key].append(el)

    def visit_paragraph(p):
        for kind, el, revisions, _ in walk(p):
            if kind != "run":
                continue
            fmt = el.find(f"{w('rPr')}/{w('rPrChange')}")
            marks = list(revisions) + ([fmt] if fmt is not None else [])
            if marks:
                touch(marks)
            elif run_text(el):
                close_all()
        ppr = p.find(w("pPr"))
        change = ppr.find(w("pPrChange")) if ppr is not None else None
        if change is not None:
            groups.append([change])
        rev = mark_revision(p)
        mark_format = p.find(f"{w('pPr')}/{w('rPr')}/{w('rPrChange')}")
        marks = ([rev] if rev is not None else []) + ([mark_format] if mark_format is not None else [])
        if marks:
            touch(marks)
        else:
            close_all()

    def visit(element):
        for child in element:
            if child.tag == w("p"):
                visit_paragraph(child)
            elif child.tag == w("tbl"):
                close_all()
                groups.extend([el] for el in child.iter(w("tblPrChange"), w("tblGridChange")))
                row_group, row_key = None, None
                for tr in (c for c in child if c.tag == w("tr")):
                    rev = row_revision(tr)
                    if rev is not None:  # the row, its cells' text and marks by the same author: one change
                        key = change_key(rev)
                        taken.update((id(el), el) for el in tr.iter(*INSERTS, *DELETES)
                                     if change_key(el)[:2] == key[:2])
                        if row_key == key:  # the next inserted row runs on: still one change
                            row_group.append(rev)
                        else:
                            row_group, row_key = [rev], key
                            groups.append(row_group)
                    else:
                        row_group, row_key = None, None
                    groups.extend([el] for el in tr.iter(w("trPrChange"), w("tblPrExChange")))
                    for tc in (c for c in tr if c.tag == w("tc")):
                        close_all()
                        groups.extend([el] for el in tc.iter(w("tcPrChange"), *CELL_CHANGES)
                                      if next(el.iterancestors(w("tc"))) is tc)
                        visit(tc)
                        close_all()
            elif child.tag in (w("sdt"), w("sdtContent"), w("customXml")):
                visit(child)
            elif child.tag == w("sectPr"):
                close_all()
                groups.extend([el] for el in child.iter(w("sectPrChange")))
        close_all()

    visit(body_of(root))
    for p in paragraphs_of(root):  # section breaks inside paragraphs
        groups.extend([el] for el in p.iter(w("sectPrChange")))
    return [g for g in groups if g]


def census_of(root, comments, keys=None):
    """The counts a person (and the WORD CHECK) can check: changes by author and kind, threads open and resolved."""
    groups = changes_as_word_counts(root)
    by_author, by_kind, by_author_kind = Counter(), Counter(), {}
    for group in groups:
        first = group[0]
        author = first.get(w("author"), "")
        kind = revision_kind(first)
        if kind.startswith("row"):
            pass
        elif change_class(kind) == "inserted":
            kind = "insertion"
        elif change_class(kind) == "deleted":
            kind = "deletion"
        by_author[author] += 1
        by_kind[kind] += 1
        by_author_kind.setdefault(author, Counter())[kind] += 1
    marks = list(body_of(root).iter(*REVISION_TAGS))
    tops = [c for c in comments.values() if not c["parent"]]
    paragraphs_with_changes = sum(1 for p in paragraphs_of(root)
                                  if any(True for _ in p.iter(*REVISION_TAGS)))
    return {"changes": len(groups), "by_author": dict(by_author), "by_kind": dict(by_kind),
            "by_author_kind": {a: dict(c) for a, c in by_author_kind.items()},
            "marks": len(marks), "paragraphs_with_changes": paragraphs_with_changes,
            "comments": len(comments), "threads": len(tops), "open": sum(not c["done"] for c in tops),
            "resolved": sum(c["done"] for c in tops), "replies": len(comments) - len(tops),
            "comments_by_author": dict(Counter(c["author"] for c in comments.values())),
            "authors": sorted(set(by_author) | {c["author"] for c in comments.values()})}


def census_line(census):
    """'12 tracked changes by 2 authors (A. Reviewer 9, B. Checker 3); 6 comment threads (4 open, 2 resolved), 3
    replies' -- the whole census in one line, in the tool's words."""
    by = ", ".join(f"{a} {n}" for a, n in sorted(census["by_author"].items(), key=lambda kv: -kv[1]))
    authors = len(census["by_author"])
    changes = (f"{census['changes']} tracked change(s) by {authors} author(s) ({by})" if census["changes"]
               else "no tracked changes")
    threads = (f"{census['threads']} comment thread(s) ({census['open']} open, {census['resolved']} resolved)"
               + (f" with {census['replies']} repl{'y' if census['replies'] == 1 else 'ies'}" if census["replies"]
                  else "") if census["threads"] else "no comments")
    return f"{changes}; {threads}"


def settings_of(settings):
    """Track changes on? Protected? (a protection other than tracked changes is refused, not unlocked: W10)"""
    if settings is None:
        return {"track_changes": False, "protection": None}
    protect = settings.find(w("documentProtection"))
    enforced = protect is not None and protect.get(w("enforcement"), "0") in ("1", "true", "on")
    return {"track_changes": settings.find(w("trackRevisions")) is not None and
            settings.find(w("trackRevisions")).get(w("val"), "true") not in ("0", "false", "off"),
            "protection": protect.get(w("edit"), "unknown") if enforced else None}


def anchors(root, comments):
    """Fill in each comment's anchor text (accepted text from commentRangeStart to commentRangeEnd), and whether its
    range and reference are in the document."""
    open_ = {}
    for p in paragraphs_of(root):
        for kind, el, revisions, _ in walk(p):
            cid = el.get(w("id"))
            if kind == "start":
                open_[cid] = []
                if cid in comments:
                    comments[cid]["anchored"] = True
            elif kind == "end":
                if cid in comments and cid in open_:
                    comments[cid]["anchor"] = " ".join("".join(open_.pop(cid)).split())
                open_.pop(cid, None)
            else:
                for ref in el.iter(w("commentReference")):
                    if ref.get(w("id")) in comments:
                        comments[ref.get(w("id"))]["referenced"] = True
                shown, _ = visible(revisions, ACCEPT)
                if shown:
                    text = run_text(el)
                    for buffer in open_.values():
                        buffer.append(text)
        for buffer in open_.values():
            buffer.append(" ")
    for cid, c in comments.items():  # a reply sits on its parent's anchor
        if not c["anchor"] and c["parent"]:
            c["anchor"] = comments[thread_root(comments, cid)]["anchor"]


def docx_view(source):
    """
    Everything a review needs from a .docx (a path, or a docx_markup.Package) -> {
      'paragraphs': [{n, where, style, section, marked, accepted, original}],
      'tables': [{n, rows: [[{marked, accepted, original}, ...], ...]}],
      'revisions': [{id, author, date, kind, paragraph, where, text}],
      'comments': [{id, label, author, initials, date, text, anchor, parent, done, replies}],
      'people': [...], 'authors': {name: key}, 'census': {...}, 'settings': {...}}
    """
    parts = read_parts(source)
    root = parts["document"]
    styles = style_names(parts.get("styles"))
    comments = read_comments(parts)
    anchors(root, comments)
    keys = author_keys(root, comments)
    tables = table_numbers(root)
    paragraphs, section = [], ""
    for n, p in enumerate(paragraphs_of(root), 1):
        level = heading_level(p, styles)
        accepted = accepted_text(p)
        if level is not None and accepted.strip() and next(p.iterancestors(w("tc")), None) is None:
            section = accepted.strip()
        ppr = p.find(w("pPr"))
        style = ppr.find(w("pStyle")).get(w("val")) if ppr is not None and ppr.find(w("pStyle")) is not None else ""
        paragraphs.append({"n": n, "where": where_of(p, tables), "style": styles.get(style, style or "Normal"),
                           "section": section, "heading": level, "marked": marked_text(p, keys),
                           "accepted": accepted, "original": original_text(p)})
    table_view = []
    for tbl, number in tables.items():
        rows = []
        for tr in (c for c in tbl if c.tag == w("tr")):
            cells = []
            for tc in (c for c in tr if c.tag == w("tc")):
                ps = [p for p in tc if p.tag == w("p")]
                cells.append({"marked": "\n".join(marked_text(p, keys) for p in ps),
                              "accepted": "\n".join(accepted_text(p) for p in ps),
                              "original": "\n".join(original_text(p) for p in ps)})
            rows.append(cells)
        table_view.append({"n": number, "rows": rows})
    revisions = [{k: v for k, v in r.items() if k != "element"} for r in revisions_of(root, tables)]
    people = ([p.get(qn("w15:author")) for p in parts["people"].iter(qn("w15:person"))]
              if parts.get("people") is not None else [])
    return {"paragraphs": paragraphs, "tables": table_view, "revisions": revisions,
            "comments": list(comments.values()), "people": people, "authors": keys,
            "census": census_of(root, comments, keys), "settings": settings_of(parts.get("settings"))}


def view_markdown(view, title):
    """review.md: the census, every thread, every change and the marked text, for a person."""
    keys = view["authors"]
    comments = {c["id"]: c for c in view["comments"]}
    out = [f"# Review of {title}", "", census_line(view["census"]), "",
           "Track changes is " + ("on" if view["settings"]["track_changes"] else "off") + "."
           + (f" The document is protected ({view['settings']['protection']})." if view["settings"]["protection"]
              else ""), "", "## Authors", ""]
    out += [f"- {key} = {name}" for name, key in keys.items()] + ["", "## Comment threads", ""]
    for c in view["comments"]:
        if c["parent"]:
            continue
        out.append(f"- {thread_line(comments, c['id'], limit=400)}")
        for reply in c["replies"]:
            r = comments[reply]
            out.append(f"  - {r['label']} {r['author']}, {r['date'][:10]}: \"{r['text']}\"")
    out += ["", "## Tracked changes", "", "| id | author | date | kind | paragraph | text |",
            "|---|---|---|---|---|---|"]
    out += [f"| {r['id']} | {r['author']} | {r['date'][:10]} | {r['kind']} | {r['paragraph']} | "
            f"{r['text'][:80].replace('|', '/')} |" for r in view["revisions"]]
    out += ["", "## Marked text", "", "`{+text|A}` inserted by A, `{-text|A}` deleted by A, `{~text|B}` formatting "
            "changed by B, `[c4]` comment c4.", ""]
    section = None
    for p in view["paragraphs"]:
        if p["section"] != section and p["where"] == "body":
            section = p["section"]
        prefix = f"{p['n']:>3} " + (f"({p['where']}) " if p["where"] != "body" else "")
        out.append(f"    {prefix}{p['marked']}")
    return "\n".join(out) + "\n"


def section_paragraphs(view, name):
    """The paragraphs of the section whose heading best matches `name` (for "show me the Procedure section")."""
    name = " ".join(str(name).lower().split())
    sections = [p["section"] for p in view["paragraphs"]]
    match = next((s for s in sections if s.lower() == name), None) or \
        next((s for s in sections if name and (name in s.lower() or s.lower() in name) and s), None)
    return [p for p in view["paragraphs"] if match and p["section"] == match], match


def load(path):
    """docx_view for a path, with the path checked first (a .docx, not a .doc or a renamed file)."""
    path = Path(path)
    if path.suffix.lower() != ".docx":
        raise ValueError(f"{path.name} is not a Word .docx file")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"{path.name} is not a .docx (a ZIP of XML parts): it may be an old .doc renamed")
    return docx_view(path)
