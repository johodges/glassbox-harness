"""
The Word review tools (OFFICE_COLLAB_DESIGN.md, Part W): read_review, redline_docx, comment_docx, compare_docx and
accept_changes. Each one works on a copy and writes a NEW file in the run folder; the user's file is never written.

    VERSION CHECK    python  the input's SHA-256 against the version ledger: new, known, or edited by the user
    REVISION CENSUS  python  what the file holds already: tracked changes and comment threads by author (word_model.py)
    LOCATE EDITS     python  each text to change or comment on occurs once; otherwise the places it occurs are named
    APPROVAL         person  only for a text found after loosening spaces or quotes, and for accepting or rejecting
                             another author's changes (office_common.ask_person; run_agent.py --yes approves)
    APPLY EDITS      python  tracked changes and comments in the XML, under the review author (docx_markup.py)
    VERIFY           python  per edit: rejecting it gives the text (and formatting) back exactly, accepting it gives the
                             intended text; a failing edit is undone and the others stay. Per file: the structure rules,
                             closed comment anchors, the census. A file that fails is not saved
    WORD CHECK       COM     Word opens it without repairing it, and counts the same revisions and comments
    VERSION LEDGER   python  the new version, its changes, the run folder

What the LLM does: picks the tool and writes the edits, comment texts and replies. It never writes XML, never sets the
author (office_common.review_author: the model and the person it works for, never the person alone), and never says
whether an edit worked: VERIFY does. What it reads back is short (a census line, a few threads); the whole view goes
to review.md and review.json in the run folder.
"""

import ast
import copy
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from ..config import OFFICE, REVIEW_AUTHOR
from . import docx_markup as dm
from . import versions, word_com
from .base import Refusal, Tool
from .files import rel, resolve
from .office_common import ask_person, lock_file, output_path, review_author, review_initials
from .word_model import (ACCEPT, KEEP, REJECT, accepted_text, census_line, changes_as_word_counts, docx_view, lines,
                         paragraphs_of, revision_kind, section_paragraphs, style_names, texts, thread_line,
                         view_markdown, w)

SHOW = 8  # how many threads, changes or edits go back to the LLM; the rest are in review.md


# ---- shared steps ----------------------------------------------------------------------------------------------------
def open_docx(path, ctx):
    """The .docx the LLM named -> (the file, the copy to read). A file Word has open (a ~$ lock file next to it) is
    read from a copy in the run folder, and never written (nothing is ever written to an input)."""
    source = resolve(path, ctx)
    if source.suffix.lower() != ".docx":
        raise ValueError(f"{source.name} is not a Word .docx file")
    lock = lock_file(source)
    if lock is None:
        return source, source
    copy_ = ctx.run_dir / "inputs" / f"open_in_word_{source.name}"
    copy_.parent.mkdir(exist_ok=True)
    shutil.copy2(source, copy_)
    ctx.tracer.say(f"{source.name} is open in Word ({lock.name} next to it): reading a copy, {rel(copy_)}",
                   kind="python")
    return source, copy_


def census_stage(ctx, source, label="the .docx"):
    """REVISION CENSUS: the whole view of the file, and its counts in one line."""
    with ctx.tracer.stage("REVISION CENSUS", "python", "word_model.docx_view (lxml, no ML)", label,
                          "changes and threads by author") as ev:
        view = docx_view(source)
        ev.summary = census_line(view["census"]) + ("; track changes on" if view["settings"]["track_changes"] else "")
        ev.details = {"census": view["census"], "settings": view["settings"]}
    return view


def refuse_protected(view, source):
    protection = view["settings"]["protection"]
    if protection and protection != "trackedChanges":
        raise Refusal(f"{source.name} is protected ({protection}): the harness does not unlock documents. Remove the "
                      "protection in Word (Review > Restrict Editing) and ask again.")


def word_check(ctx, path, census, pdf=None):
    """WORD CHECK (optional, OFFICE['word_check']): Word opens the file read-only with repair off, and counts. ->
    (result or None, one line for the answer, broken: True if Word could not open it)"""
    if not OFFICE["word_check"]:
        return None, "WORD CHECK off (config OFFICE['word_check'])", False
    with ctx.tracer.stage("WORD CHECK", "server", "Word through COM, in its own process (word_com.py)", ".docx",
                          "revisions, comments, pages") as ev:
        result, why = word_com.open_check(path, pdf=pdf)
        if result is None:
            broken = why.startswith(word_com.OPEN_FAILED)
            ev.summary = why if broken else f"skipped: {why}"
            return None, (why if broken else f"Word check skipped: {why}"), broken
        same = result["revisions"] == census["changes"] and result["comments"] == census["comments"]
        line = (f"Word opened it without repair: {result['revisions']} revision(s), {result['comments']} comment(s), "
                f"{result['pages']} page(s)" + (" (= the census)" if same else
                                               f" (the census says {census['changes']} and {census['comments']})"))
        ev.summary = line
        ev.details = result
        if result.get("pdf"):
            ev.artifacts.append(result["pdf"])
    return result, line, False


def save_checked(ctx, pkg, source, tag, input_problems, census_after, pdf=True):
    """
    The last steps of every writing tool: the structure rules (only problems the input didn't have count), then the
    file in the run folder, then WORD CHECK on it. A file that breaks a rule, or that Word can't open without
    repairing, is not kept. -> (the file, the Word result or None, the WORD CHECK line, the PDF or None)
    """
    with ctx.tracer.stage("VERIFY: STRUCTURE", "python", "docx_markup.structure_problems (no ML)", "the edited XML",
                          "fine, or the problems") as ev:
        problems = [p for p in dm.structure_problems(pkg) if p not in input_problems]
        ev.summary = "ids unique, elements in order, deleted text as w:delText, comments anchored" if not problems \
            else "; ".join(problems[:4])
    if problems:
        raise ValueError(f"The result was not saved: it breaks the file's structure ({'; '.join(problems[:3])}).")
    out = output_path(ctx, source, tag)
    unchecked = out.with_name(f"{out.stem}.unchecked{out.suffix}")
    pkg.save(unchecked)
    pdf_path = out.with_suffix(".pdf") if pdf else None
    result, line, broken = word_check(ctx, unchecked, census_after, pdf=pdf_path)
    if broken:
        unchecked.unlink(missing_ok=True)
        raise ValueError(f"The result was not saved: {line}.")
    unchecked.replace(out)
    return out, result, line, (pdf_path if pdf_path and pdf_path.is_file() else None)


def write_review(ctx, view, title, extra=None):
    """review.md and review.json in the run folder: everything, for a person (the LLM gets a short version)."""
    n, md = 1, ctx.run_dir / "review.md"
    while md.exists():
        n += 1
        md = ctx.run_dir / f"review_{n}.md"
    text = view_markdown(view, title)
    if extra:
        text += "\n" + extra.rstrip() + "\n"
    md.write_text(text, encoding="utf-8")
    md.with_suffix(".json").write_text(json.dumps(view, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return md


def ledger_stage(ctx, ledger, out, tool, changes):
    with ctx.tracer.stage("VERSION LEDGER", "python", "versions.py (no ML)", rel(out), "a new harness version") as ev:
        entry = versions.record(ledger, out, ctx, tool, changes)
        ev.summary = f"v{entry['n']} of {ledger.key}, by {tool}"
    return entry


def short(text, limit=90):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def excerpt(marked, limit=150):
    """Marked text from a little before its first change: '...within {-5|GBH}{+3|GBH} minutes of ...'."""
    at = min([i for i in (marked.find("{+"), marked.find("{-"), marked.find("{~")) if i >= 0], default=0)
    start = max(0, at - 40)
    start = marked.rfind(" ", 0, start) + 1 if start else 0
    return ("..." if start else "") + short(marked[start:], limit)


def norm(text):
    return " ".join(str(text).replace("’", "'").replace("“", '"').replace("”", '"').split()).lower()


def loose_pattern(text):
    """'within 5 minutes' -> a regex that also matches other spaces, curly quotes and dashes ("needs approval")."""
    def one(word):
        out = []
        for ch in word:
            if ch in "'‘’":
                out.append("['‘’]")
            elif ch in '"“”':
                out.append('["“”]')
            elif ch in "-–—":
                out.append("[-–—]")
            else:
                out.append(re.escape(ch))
        return "".join(out)
    return re.compile(r"\s+".join(one(word) for word in str(text).split()))


def find_text(paragraphs, text):
    """-> ([(paragraph index, start, end)], exact?) for text in the paragraphs' accepted text."""
    hits = []
    for i, p in enumerate(paragraphs):
        body, at = accepted_text(p), 0
        while text and (at := body.find(text, at)) >= 0:
            hits.append((i, at, at + len(text)))
            at += len(text)
    if hits or not str(text).strip():
        return hits, True
    pattern = loose_pattern(text)
    for i, p in enumerate(paragraphs):
        hits += [(i, m.start(), m.end()) for m in pattern.finditer(accepted_text(p))]
    return hits, False


def describe_places(view, hits):
    """Where a text occurs, for the LLM: '¶4 (Scope), ¶9 (table 1, row 2, cell 1)'."""
    out = []
    for i, _, _ in hits[:5]:
        p = view["paragraphs"][i]
        out.append(f"¶{p['n']} ({p['where'] if p['where'] != 'body' else p['section'] or 'start'})")
    return ", ".join(out)


def closest(view, text):
    """The words in the document most like a text that was not found (a window of as many words), so the LLM can
    copy them exactly: '; the closest text is ¶39: "within 5 minutes of"'."""
    import difflib
    wanted = norm(text)
    n = max(1, len(wanted.split()))
    best, score = None, 0.0
    for p in view["paragraphs"]:
        words = p["accepted"].split()
        for size in {n - 1, n, n + 1} - {0}:
            for i in range(max(1, len(words) - size + 1)):
                window = " ".join(words[i:i + size])
                s = difflib.SequenceMatcher(None, wanted, norm(window)).ratio()
                if s > score:
                    best, score = (p["n"], window), s
    return f"; the closest text is ¶{best[0]}: \"{short(best[1], 120)}\"" if best and score > 0.5 else ""


def harness_authors():
    """A regex for every author the harness writes (any model, any person): REVIEW_AUTHOR with its fields open."""
    pattern = re.escape(REVIEW_AUTHOR).replace(r"\{model\}", ".+").replace(r"\{user\}", ".+")
    return re.compile(f"^{pattern}$")


def as_list(value):
    """A small LLM sends a list as a list, a JSON string, a Python-looking string, one object, or lines."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(text)
                return as_list(parsed) if not isinstance(parsed, str) else [parsed]
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                pass
        return [line.strip() for line in re.split(r"\n|;", text) if line.strip()]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip()[:1] in "{[":
                out += as_list(item)
            else:
                out.append(item)
        return out
    return [value]


def pick(item, *keys):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


ARROW = re.compile(r"\s*(?:->|=>|→|-->)\s*")
QUOTED_EDIT = re.compile(r"\b(?:change|replace|correct)\s+[\"“'](.+?)[\"”']\s+(?:to|with|by|into)\s+"
                         r"[\"“'](.+?)[\"”']", re.I | re.S)


# ---- read_review -----------------------------------------------------------------------------------------------------
class ReadReviewTool(Tool):
    name = "read_review"
    description = ("Read the review state of a Word .docx: who made which tracked changes, which comment threads are "
                   "open or resolved, and the marked text of a section. Use it for 'which comments are open', "
                   "'who changed what', 'what is still pending'.")
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string", "description": "The .docx, e.g. workspace/review_demo/"
                                                                           "test_plan.docx"},
                                 "section": {"type": "string",
                                             "description": "A heading whose marked text to show (optional)"}},
                  "required": ["path"]}
    model_id = "word_model.docx_view (lxml, no ML)"
    check_unused, repeatable = False, True
    how_it_works = ("No model here: Python reads the .docx's XML parts (document, comments, threads, people, "
                    "settings) and counts the tracked changes the way Word does. The LLM gets the counts and the open "
                    "threads; review.md has everything.")

    def run(self, ctx, path, section=""):
        t = ctx.tracer
        source, readable = open_docx(path, ctx)
        t.say(self.how_it_works, kind="python")
        versions.track(source, ctx)
        view = census_stage(ctx, readable)
        census = view["census"]
        comments = {c["id"]: c for c in view["comments"]}
        tops = [c for c in view["comments"] if not c["parent"]]
        open_threads = [thread_line(comments, c["id"]) for c in tops if not c["done"]]
        resolved = [thread_line(comments, c["id"]) for c in tops if c["done"]]
        root = _root(readable)
        paras = paragraphs_of(root)  # kept alive, so the ids below stay the elements' own
        index = {id(p): i for i, p in enumerate(paras)}
        changes = [change_line(g, view, index) for g in changes_as_word_counts(root)]
        wanted = section or next((p["section"] for p in view["paragraphs"] if p["heading"] is not None
                                  and p["section"] and re.search(rf"\b{re.escape(heading_words(p['section']))}\b",
                                                                 (ctx.request or "").lower())), "")
        shown, match = section_paragraphs(view, wanted) if wanted else ([], None)
        md = write_review(ctx, view, rel(source))
        line = census_line(census)
        track = "on" if view["settings"]["track_changes"] else "off"
        ctx.notes.append(f"read_review on {rel(source)}: {line}; track changes {track}. Every change and thread is in "
                         f"{rel(md)}.")
        pending = census["changes"] or census["open"]
        if pending and re.search(r"\b(?:i|we|you)\s+(?:have\s+|had\s+)?(?:accepted|resolved|cleared)\b|"
                                 r"\bno (?:more )?(?:changes|comments) (?:left|pending)\b", ctx.request or "", re.I):
            ctx.notes.append(f"The request assumes the changes were dealt with, but the file still has "
                             f"{census['changes']} tracked change(s) and {census['open']} open thread(s).")
        observations = {"file": rel(source), "census": line, "track_changes": track,
                        "authors": ", ".join(f"{k} = {a}" for a, k in view["authors"].items()),
                        "open_threads": open_threads[:SHOW], "resolved_threads": resolved[:4],
                        "changes": changes[:SHOW], "full_view": rel(md)}
        if len(open_threads) > SHOW or len(changes) > SHOW:
            observations["more"] = (f"{max(0, len(open_threads) - SHOW)} more open thread(s), "
                                    f"{max(0, len(changes) - SHOW)} more change(s) in {rel(md)}")
        if shown:
            observations["section"] = {"heading": match, "marked": [short(p["marked"], 300) for p in shown][:12],
                                       "key": "{+text|A} inserted by A, {-text|A} deleted, {~text|B} reformatted, "
                                              "[c4] comment c4"}
        return {"artifacts": [{"type": "word_review", "path": md, "note": "the census, every thread and change"}],
                "observations": observations}


def heading_words(heading):
    """'6 Procedure' -> 'procedure', '2.1 Test rig' -> 'test rig': how a request names a section."""
    return re.sub(r"^[\d.]+\s*", "", heading).strip().lower() or heading.lower()


def _root(path):
    return dm.Package(path).document


def earlier_version(path, ctx):
    """The harness's own last version of the file at this path, when the user has edited it since (its bytes match
    no version): 'what did I change since your version?'. The ledgers name a document by the file a request started
    from, so a redline the user saved over is found by its path. -> a version entry and its ledger, or (None, None)"""
    folder = Path(ctx.facts.get("versions_dir") or versions.VERSIONS_DIR)
    here, digest = Path(path).resolve(), versions.sha256(path)
    found = (None, None)
    for ledger_file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        ledger = versions.Versions(json.loads(ledger_file.read_text(encoding="utf-8"))["document"], folder)
        for entry in ledger.versions:
            if entry["origin"] == "harness" and Path(entry["file"]).resolve() == here and entry["sha256"] != digest:
                found = (entry, ledger)
    return found


def change_line(group, view, index):
    """One change as Word counts it -> 'A. Reviewer inserted "within 3 minutes" (¶12, Procedure)'. index: id of each
    paragraph element (kept alive by the caller) -> its position."""
    first = group[0]
    kind = revision_kind(first)
    verb = {"inserted": "inserted", "deleted": "deleted"}.get(
        "inserted" if "insert" in kind or kind == "moved to" else "deleted" if "delet" in kind or kind == "moved from"
        else "", "reformatted")
    if kind.startswith("row"):
        verb = "inserted a row" if "insert" in kind else "deleted a row"
    text = "".join(dm.run_text(r) for el in group for r in el.iter(w("r")))
    if kind == "formatting":
        r = first.getparent().getparent()
        text = dm.run_text(r) if r.tag == w("r") else ""
    p = next(first.iterancestors(w("p")), None)
    where = ""
    if p is not None:
        n = index.get(id(p))
        if n is not None and n < len(view["paragraphs"]):
            info = view["paragraphs"][n]
            where = f" (¶{info['n']}, {info['where'] if info['where'] != 'body' else info['section'] or 'start'})"
    return f"{first.get(w('author'), '?')} {verb}" + (f" \"{short(text, 60)}\"" if text.strip() else "") + where


# ---- redline_docx ----------------------------------------------------------------------------------------------------
def parse_edit(item):
    """One edit as a small LLM writes it -> {'op': ..., ...}, or None."""
    if isinstance(item, str):
        parts = ARROW.split(item.strip().strip("\"'"), maxsplit=1)
        if len(parts) == 2:
            return {"op": "replace", "find": parts[0].strip().strip("\"'"), "replace": parts[1].strip().strip("\"'"),
                    "comment": ""}
        return None
    if not isinstance(item, dict):
        return None
    item = {str(k).lower().strip(): v for k, v in item.items()}
    comment = str(pick(item, "comment", "note", "reason", "why", "explanation") or "")
    if (after := pick(item, "insert_after", "after", "insert_paragraph_after")) is not None:
        paragraphs = pick(item, "paragraphs", "paragraph", "new_paragraphs", "text", "content", "insert")
        paragraphs = [str(x) for x in as_list(paragraphs)] if not isinstance(paragraphs, str) else [paragraphs]
        return {"op": "insert", "after": str(after), "paragraphs": [p for p in paragraphs if p.strip()],
                "comment": comment}
    if (after := pick(item, "table_row_after", "row_after", "add_row_after", "insert_row_after")) is not None:
        cells = pick(item, "cells", "row", "values")
        cells = [str(c) for c in as_list(cells)] if not isinstance(cells, str) else [c.strip() for c in
                                                                                    cells.split("|")]
        return {"op": "insert_row", "after": str(after), "cells": cells, "comment": comment}
    if (text := pick(item, "delete_row", "remove_row")) is not None:
        return {"op": "delete_row", "text": str(text), "comment": comment}
    if (text := pick(item, "delete_paragraph", "remove_paragraph", "delete")) is not None:
        return {"op": "delete_paragraph", "text": str(text), "comment": comment}
    find = pick(item, "find", "old", "search", "original", "from", "text", "target")
    replace = next((item[k] for k in ("replace", "new", "with", "replacement", "to", "change_to") if k in item), None)
    if find is not None and replace is not None:
        return {"op": "replace", "find": str(find), "replace": "" if replace is None else str(replace),
                "comment": comment}
    return None


def edit_line(e):
    if e["op"] == "replace":
        return f"\"{short(e['find'], 50)}\" -> \"{short(e['replace'], 50)}\""
    if e["op"] == "insert":
        return f"{len(e['paragraphs'])} paragraph(s) after \"{short(e['after'], 40)}\""
    if e["op"] == "insert_row":
        return f"a row ({' | '.join(short(c, 20) for c in e['cells'])}) after the row with \"{short(e['after'], 30)}\""
    if e["op"] == "delete_row":
        return f"the row with \"{short(e['text'], 40)}\" deleted"
    return f"the paragraph with \"{short(e['text'], 40)}\" deleted"


def rows_of(root):
    return [tr for tr in root.iter(w("tr"))]


def locate(edit, root, view):
    """CHECK ARGS for one edit: where it applies -> (location dict, None) or (None, why it can't be placed)."""
    paras = paragraphs_of(root)
    if edit["op"] == "replace":
        if not edit["find"]:
            return None, "the text to change is empty"
        hits, exact = find_text(paras, edit["find"])
        if len(hits) != 1:
            return None, (f"\"{short(edit['find'], 60)}\" occurs {len(hits)} times ({describe_places(view, hits)}): "
                          "quote more of the sentence so it is unique" if hits else
                          f"\"{short(edit['find'], 60)}\" is not in the document's text (within one paragraph)"
                          + closest(view, edit["find"]))
        i, a, b = hits[0]
        return {"index": i, "start": a, "end": b, "exact": exact, "found": accepted_text(paras[i])[a:b]}, None
    if edit["op"] in ("insert", "delete_paragraph"):
        text = edit["after"] if edit["op"] == "insert" else edit["text"]
        hits, exact = find_text(paras, text.strip())
        places = sorted({h[0] for h in hits})
        if len(places) != 1:
            return None, (f"\"{short(text, 60)}\" is in {len(places)} paragraphs ({describe_places(view, hits)}): "
                          "quote more of it" if places else f"\"{short(text, 60)}\" is not in any paragraph"
                          + closest(view, text))
        return {"index": places[0], "start": 10 ** 9 if edit["op"] == "insert" else 0, "exact": exact,
                "found": accepted_text(paras[places[0]])}, None
    rows = rows_of(root)
    text = edit["after"] if edit["op"] == "insert_row" else edit["text"]
    matches = [i for i, tr in enumerate(rows)
               if any(accepted_text(p).strip() == text.strip() for p in tr.iter(w("p")))]
    if not matches:
        matches = [i for i, tr in enumerate(rows) if text.strip() and text.strip() in
                   " | ".join(accepted_text(p) for p in tr.iter(w("p")))]
    if len(matches) != 1:
        return None, (f"\"{short(text, 60)}\" is in {len(matches)} table rows: give the exact text of one cell"
                      if matches else f"no table row has a cell \"{short(text, 60)}\"")
    tr = rows[matches[0]]
    row_paras = [i for i, p in enumerate(paras) if any(a is tr for a in p.iterancestors(w("tr")))]
    index = row_paras[-1] if edit["op"] == "insert_row" else row_paras[0]
    return {"index": index, "row": matches[0], "start": 10 ** 9 if edit["op"] == "insert_row" else 0,
            "exact": True, "found": text}, None


def apply_edit(pkg, edit, where, writer, styles):
    """One edit on the package -> (its revision ids, the expected accepted text of every paragraph, the anchor for a
    comment: (first element, last element, the paragraph that holds the last))."""
    root = pkg.document
    paras = paragraphs_of(root)
    before = [accepted_text(p) for p in paras]
    i = where["index"]
    if edit["op"] == "replace":
        a, b = where["start"], where["end"]
        made, (first, last) = dm.tracked_replace(paras[i], a, b, edit["replace"], writer)
        expected = before[:i] + [before[i][:a] + edit["replace"] + before[i][b:]] + before[i + 1:]
        anchor = (first, last, paras[i])
    elif edit["op"] == "insert":
        if not edit["paragraphs"]:
            raise dm.Refused("no paragraphs to insert")
        made, new = dm.insert_paragraphs_after(paras[i], edit["paragraphs"], writer, styles)
        expected = before[:i + 1] + list(edit["paragraphs"]) + before[i + 1:]
        content = [c for q in new for c in q if c.tag != w("pPr")]
        anchor = (content[0], content[-1], new[-1]) if content else (None, None, None)
    elif edit["op"] == "delete_paragraph":
        made = dm.delete_paragraph(paras[i], writer)
        expected = before[:i] + [""] + before[i + 1:]
        content = [c for c in paras[i] if c.tag != w("pPr")]
        anchor = (content[0], content[-1], paras[i]) if content else (None, None, None)
    elif edit["op"] == "insert_row":
        tr = rows_of(root)[where["row"]]
        cells = len([c for c in tr if c.tag == w("tc")])
        made, new = dm.insert_row_after(tr, edit["cells"], writer)
        values = (list(edit["cells"]) + [""] * cells)[:cells]
        expected = before[:i + 1] + values + before[i + 1:]
        firsts = [c for q in new.iter(w("p")) for c in q if c.tag != w("pPr")]
        anchor = (firsts[0], firsts[-1], list(new.iter(w("p")))[-1]) if firsts else (None, None, None)
    else:
        tr = rows_of(root)[where["row"]]
        inside = [k for k, p in enumerate(paras) if any(a is tr for a in p.iterancestors(w("tr")))]
        made = dm.delete_row(tr, writer)
        expected = [("" if k in inside else text) for k, text in enumerate(before)]
        content = [c for q in tr.iter(w("p")) for c in q if c.tag != w("pPr")]
        anchor = (content[0], content[-1], list(tr.iter(w("p")))[-1]) if content else (None, None, None)
    return {el.get(w("id")) for el in made}, expected, anchor


def signature(root, decide):
    return [(where, text, sig) for where, text, sig in lines(root, decide)]


def first_difference(a, b):
    for k, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"paragraph {k + 1}: \"{short(x if isinstance(x, str) else x[1], 60)}\" vs " \
                   f"\"{short(y if isinstance(y, str) else y[1], 60)}\""
    return f"{len(a)} paragraphs vs {len(b)}"


def verify_edit(before_root, root, ids, expected):
    """W6, 2-4 for one edit -> None if it holds, else why. Rejecting the edit must give back the document as it was,
    text and formatting, paragraph by paragraph (so nothing changed outside its tracked spans); accepting everything
    must give the intended text."""
    rejected = signature(root, lambda el: "reject" if el.get(w("id")) in ids else "keep")
    was = signature(before_root, KEEP)
    if rejected != was:
        texts_ = ([x[1] for x in rejected], [x[1] for x in was])
        if texts_[0] != texts_[1]:
            return "rejecting it does not give back the text as it was: " + first_difference(*texts_)
        k = next(k for k, (x, y) in enumerate(zip(rejected, was)) if x != y)
        return f"rejecting it gives the text back but not the formatting or marks of paragraph {k + 1} (untracked " \
               "change)"
    accepted = [accepted_text(p) for p in paragraphs_of(root)]
    if accepted != expected:
        return "accepting it does not give the intended text: " + first_difference(accepted, expected)
    after, was = mark_counts(root), mark_counts(before_root)
    added = {a: after[a] - was[a] for a in set(after) | set(was) if after[a] != was[a]}
    reported = Counter(el.get(w("author"), "") for el in root.iter(*MARK_TAGS) if el.get(w("id")) in ids)
    if added != dict(reported):
        return f"the insertion and deletion marks changed by {added}, not by the {dict(reported)} it reported"
    return None


MARK_TAGS = tuple(dm.INSERTS | dm.DELETES)


def mark_counts(root):
    """W6.6, mark by mark: tracked insertions and deletions by author. (Splitting a run also splits a formatting change
    on it, which is the same change, so formatting marks are not counted.)"""
    return Counter(el.get(w("author"), "") for el in root.iter(*MARK_TAGS))


class RedlineDocxTool(Tool):
    name = "redline_docx"
    description = ("Edit a Word .docx WITH TRACK CHANGES: each edit becomes a tracked deletion and insertion, word by "
                   "word, under the harness's name, optionally with a comment. edits: a list such as "
                   "[{\"find\": \"exact text\", \"replace\": \"new text\", \"comment\": \"why (optional)\"}]; also "
                   "{\"insert_after\": \"text ending a paragraph\", \"paragraphs\": [\"new paragraph\"]}, "
                   "{\"delete_paragraph\": \"text in it\"}, {\"table_row_after\": \"cell text\", \"cells\": [...]}, "
                   "{\"delete_row\": \"cell text\"}. Writes a new copy; every edit is verified.")
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string", "description": "The .docx to edit"},
                                 "edits": {"type": "array", "items": {"type": "object"},
                                           "description": "The edits: find/replace (+ comment), insert_after, "
                                                          "delete_paragraph, table_row_after, delete_row"}},
                  "required": ["path", "edits"]}
    model_id = "docx_markup.py (lxml, no ML) + Word through COM for the check"
    check_unused = False
    how_it_works = ("No model here: Python finds each text (exactly once), splits the runs at the edit's word "
                    "boundaries, and writes w:del and w:ins around the words that change, under the review author. "
                    "Then it proves each edit: rejecting it gives the original back exactly, accepting it gives the "
                    "intended text. Word opens the result to check it.")

    def prepare(self, ctx):
        """W8: when the request is about the comments already in the file ('address the open comments'), read_review
        must run first (CHECK ORDER in agent.py), so the edits answer what the reviewers asked."""
        about_comments = re.search(r"\b(?:open|existing|their|reviewers?'?s?|address(?:ing)?|answer(?:ing)?|"
                                   r"respond(?:ing)? to)\s+(?:the\s+)?comments?\b|\bcomments?\s+(?:c?\d+)\b",
                                   ctx.request or "", re.I)
        self.requires = ("read_review",) if about_comments else ()

    def validate(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        if not args.get("path"):
            raise ValueError("missing required argument(s): path")
        items = as_list(args.get("edits"))
        top = {k: args[k] for k in args if k not in ("path", "edits")}
        edits = [e for e in (parse_edit(item) for item in items) if e is not None]
        single = parse_edit(top) if top else None
        if single is not None:  # one find/replace pair at the top level
            edits.append(single)
        elif top and len(edits) == 1 and not edits[0]["comment"]:  # a comment written next to the list
            edits[0]["comment"] = str(pick({k.lower(): v for k, v in top.items()}, "comment", "note", "reason") or "")
        if not edits:
            raise ValueError('give edits, e.g. edits=[{"find": "within 5 minutes", "replace": "within 3 minutes", '
                             '"comment": "why"}]')
        return {"path": str(args["path"]), "edits": edits}

    def check_request(self, args, ctx):
        """CHECK ARGS: 'change "X" to "Y"' in the request means exactly X and Y (a small model shortens or 'improves'
        quoted text). -> (args, summary, note for the LLM or None)"""
        quoted = QUOTED_EDIT.findall(ctx.request or "")
        if not quoted:
            return args, "the request quotes no 'change \"X\" to \"Y\"': the LLM's edits stand", None
        edits = [dict(e) for e in args["edits"]]
        fixed, used = [], set()
        for old, new in quoted:
            match = next((e for e in edits if e["op"] == "replace" and norm(e["find"]) == norm(old)), None)
            if match is None:
                match = next((e for e in edits if e["op"] == "replace" and id(e) not in used
                              and not any(norm(e["find"]) == norm(o) for o, _ in quoted)), None)
            if match is None:
                match = {"op": "replace", "find": old, "replace": new, "comment": ""}
                edits.append(match)
                fixed.append(f"added \"{old}\" -> \"{new}\"")
            elif match["find"] != old or match["replace"] != new:
                fixed.append(f"\"{match['find']}\" -> \"{match['replace']}\" became \"{old}\" -> \"{new}\"")
                match["find"], match["replace"] = old, new
            used.add(id(match))
        if not fixed:
            return args, "the edits use the user's quoted words", None
        note = "The request quotes the words to change, so the harness used them: " + "; ".join(fixed) + "."
        return {**args, "edits": edits}, "; ".join(fixed), note

    def run(self, ctx, path, edits):
        t = ctx.tracer
        source, readable = open_docx(path, ctx)
        t.say(self.how_it_works, kind="python")
        ledger, _, what = versions.track(source, ctx)
        view = census_stage(ctx, readable)
        refuse_protected(view, source)
        pkg = dm.Package(readable)
        input_problems = dm.structure_problems(pkg)
        styles = style_names(pkg.part("styles"))
        report = [{"edit": e, "status": "pending", "why": ""} for e in edits]
        with t.stage("LOCATE EDITS", "python", "word_review.locate (no ML)", f"{len(edits)} edit(s)",
                     "where each one applies") as ev:
            for item in report:
                where, why = locate(item["edit"], pkg.document, view)
                item["where"] = where
                if where is None:
                    item["status"], item["why"] = "refused", why
            ev.summary = (f"{sum(i['where'] is not None for i in report)} of {len(report)} edit(s) found once"
                          + "".join(f"; {short(i['why'], 100)}" for i in report if i["where"] is None))
        for item in report:  # a text found only after loosening spaces, quotes or dashes: a person says yes
            where = item["where"]
            if where and not where["exact"] and not ask_person(
                    ctx, f"\"{item['edit'].get('find') or item['edit'].get('after') or item['edit'].get('text')}\" "
                         f"was found as \"{short(where['found'], 80)}\" (spaces, quotes or dashes differ). Edit it?",
                    "an edit whose text was found loosely"):
                item["status"], item["why"] = "refused", "a person said no to the loosely matched text"
        author = review_author(ctx)
        writer = dm.Writer(pkg, author, review_initials(ctx))
        todo = sorted((i for i in report if i["status"] == "pending"),
                      key=lambda i: (i["where"]["index"], i["where"]["start"]), reverse=True)
        with t.stage("APPLY EDITS", "python", "docx_markup.py (lxml, no ML)", f"{len(todo)} edit(s)",
                     "tracked changes, verified one by one") as ev:
            for item in todo:  # from the end of the document up, so no pending edit's place moves
                edit, name = item["edit"], pkg.names["document"]
                snapshot = copy.deepcopy(pkg.document)
                try:
                    ids, expected, anchor = apply_edit(pkg, edit, item["where"], writer, styles)
                except dm.Refused as exc:
                    pkg.trees[name] = snapshot
                    why = str(exc).replace(f"by {author}", "by another edit in this call")
                    item["status"] = "needs a person" if exc.needs_person and "another edit" not in why else "refused"
                    item["why"] = why
                    continue
                why = verify_edit(snapshot, pkg.document, ids, expected)
                if why:  # W6: an edit that breaks 2-4 is undone, the rest stay
                    pkg.trees[name] = snapshot
                    item["status"], item["why"] = "failed", why
                    continue
                item["status"], item["ids"] = "verified", sorted(ids, key=int)
                if edit.get("comment") and anchor[0] is not None:
                    item["comment"] = "c" + dm.add_comment(pkg, anchor[0], anchor[1], anchor[2], edit["comment"],
                                                           writer)
            pkg.touch(pkg.names["document"])
            done = [i for i in report if i["status"] == "verified"]
            ev.summary = (f"{len(done)} verified, {sum(i['status'] == 'failed' for i in report)} failed and undone, "
                          f"{sum(i['status'] == 'needs a person' for i in report)} need a person, "
                          f"{sum(i['status'] == 'refused' for i in report)} refused")
            ev.details = {"edits": [{**i, "where": None} for i in report]}
        if not done:
            raise Refusal("No edit could be made, so no file was written: " + "; ".join(
                f"{edit_line(i['edit'])}: {i['why']}" for i in report)[:700])
        if OFFICE["track_changes_on"]:
            dm.set_track_changes(pkg, True)
        dm.renumber_duplicates(pkg, writer)
        ours = set(writer.revision_ids)
        with t.stage("VERIFY", "python", "word_model.lines (no ML)", "the redline + the input",
                     "reject-all = original, census") as ev:
            original = dm.Package(readable).document
            if signature(pkg.document, lambda el: "reject" if el.get(w("id")) in ours else "keep") != \
                    signature(original, KEEP):
                raise ValueError("The result was not saved: rejecting the harness's changes does not give back the "
                                 "original document.")
            after = docx_view(pkg)
            counts, was = mark_counts(pkg.document), mark_counts(original)  # W6.6: the census adds up
            if {a: n for a, n in counts.items() if a != author} != {a: n for a, n in was.items() if a != author} \
                    or counts[author] - was[author] != sum(1 for el in pkg.document.iter(*MARK_TAGS)
                                                           if el.get(w("id")) in ours):
                raise ValueError(f"The result was not saved: the tracked changes don't add up ({dict(was)} before, "
                                 f"{dict(counts)} after).")
            ev.summary = (f"rejecting all {len(ours)} revision mark(s) of the harness gives back the original, text "
                          f"and formatting; other authors' changes untouched; {census_line(after['census'])}")
        out, _, word_line, pdf = save_checked(ctx, pkg, source, "redline", input_problems, after["census"])
        ledger_stage(ctx, ledger, out, self.name,
                     [{"edit": edit_line(i["edit"]), "revision_ids": i.get("ids", []), "comment": i.get("comment")}
                      for i in done])
        lines_ = [f"{i['status']}: {edit_line(i['edit'])}" + (f" (+ comment {i['comment']})" if i.get("comment")
                                                               else "") + (f" -- {i['why']}" if i["why"] else "")
                  for i in report]
        md = write_review(ctx, after, rel(out), "## Edits in this request\n\n" + "\n".join(f"- {x}" for x in lines_))
        people = [i for i in report if i["status"] == "needs a person"]
        summary = (f"{len(done)} of {len(report)} edit(s) verified (rejecting them gives back the original, accepting "
                   f"them gives the intended text)" + (f"; {len(people)} need a person" if people else "")
                   + (f"; {sum(i['status'] in ('failed', 'refused') for i in report)} not made" if
                      any(i['status'] in ('failed', 'refused') for i in report) else ""))
        ctx.notes.append(f"redline_docx on {rel(source)}: {summary}. Before: {census_line(view['census'])}. After: "
                         f"{census_line(after['census'])}. {word_line}. The redline is {rel(out)}; the original is "
                         "unchanged.")
        artifacts = [{"type": "word_review", "path": out, "note": "the redlined copy (track changes on)"},
                     {"type": "word_review", "path": md, "note": "the census and every change"}]
        if pdf:
            artifacts.append({"type": "word_review", "path": pdf, "note": "the redline as a PDF, with markup"})
        return {"artifacts": artifacts,
                "observations": {"file": rel(source), "redline": rel(out), "summary": summary,
                                 "edits": [short(x, 200) for x in lines_][:SHOW],
                                 "census_before": census_line(view["census"]),
                                 "census_after": census_line(after["census"]), "word_check": word_line,
                                 "version": f"{what} input; the redline is the newest version in the ledger"}}


# ---- comment_docx ----------------------------------------------------------------------------------------------------
def comment_id(value, comments):
    """'c4', '4', 'comment 4', '#4', or the words of a comment -> its id, or None."""
    text = str(value).strip()
    m = re.fullmatch(r"(?:c|comment\s*|#)?\s*(\d+)", text, re.I)
    if m and m.group(1) in comments:
        return m.group(1)
    hits = [cid for cid, c in comments.items() if text and norm(text) in norm(c["text"])]
    return hits[0] if len(hits) == 1 else None


def label(c):
    """The comment an item is about, as 'c4' (its id once found, else what the LLM wrote)."""
    return f"c{c['id']}" if c.get("id") else str(c.get("to", ""))


def item_line(c):
    """One comment_docx item for the report: 'done: reply c18 to c4', 'refused: comment on "x" -- why'."""
    what = {"new": f"comment {c.get('made', '')} on \"{short(c.get('on'), 40)}\"",
            "reply": f"reply {c.get('made', '')} to {label(c)}", "resolve": f"resolve {label(c)}"}[c["op"]]
    return f"{c['status']}: {what}" + (f" -- {c['why']}" if c["why"] else "")


def squash(text):
    """'Now in Table 1.' and 'now in table 1' are the same words."""
    return re.sub(r"\W+", "", str(text)).lower()


def parse_comment(item):
    if isinstance(item, str):
        m = re.match(r"\s*(?:reply(?:\s+to)?|answer)\s+(?:comment\s+)?(c?\d+)\s*[:,-]\s*(.+)", item, re.I | re.S)
        if m:
            return {"op": "reply", "to": m.group(1), "text": m.group(2).strip(), "resolve": False}
        m = re.match(r"\s*resolve\s+(?:comment\s+)?(c?\d+)", item, re.I)
        if m:
            return {"op": "resolve", "to": m.group(1)}
        parts = ARROW.split(item, maxsplit=1)
        if len(parts) == 2:
            return {"op": "new", "on": parts[0].strip().strip("\"'"), "text": parts[1].strip()}
        return None
    if not isinstance(item, dict):
        return None
    item = {str(k).lower().strip(): v for k, v in item.items()}
    text = pick(item, "text", "comment", "reply", "body", "message", "note")
    flag = pick(item, "resolve", "resolved", "done", "mark_resolved", "close")
    to = pick(item, "reply_to", "parent", "in_reply_to", "comment_id", "id", "thread", "to")
    if isinstance(flag, str) and not text and not to and not re.fullmatch(r"(?i)true|yes|1", flag):
        return {"op": "resolve", "to": flag}
    if isinstance(flag, str) and re.fullmatch(r"(?i)c?\d+", flag.strip()) and text and to is None:
        # {"on": "duct sizes", "text": "now in Table 1", "resolve": "c4"} (Llama 3.2 3B): a reply to c4 that resolves it
        return {"op": "reply", "to": flag.strip(), "text": str(text), "resolve": True}
    resolve_too = flag is True or str(flag).lower() in ("true", "yes", "1")
    if to is not None and text:
        return {"op": "reply", "to": str(to), "text": str(text), "resolve": resolve_too}
    if to is not None and resolve_too:
        return {"op": "resolve", "to": str(to)}
    on = pick(item, "on", "anchor", "find", "quote", "at", "text_to_comment", "where")
    if on is not None and text:
        return {"op": "new", "on": str(on), "text": str(text)}
    return None


class CommentDocxTool(Tool):
    name = "comment_docx"
    description = ("Add comments to a Word .docx, reply to comment threads, and mark threads resolved, in a new copy. "
                   "comments: a list, each one of {\"on\": \"exact text to comment on\", \"text\": \"the comment\"}, "
                   "{\"reply_to\": \"c4\", \"text\": \"the reply\"}, {\"resolve\": \"c4\"}. The text is not changed.")
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string", "description": "The .docx"},
                                 "comments": {"type": "array", "items": {"type": "object"},
                                              "description": "New comments (on + text), replies (reply_to + text), "
                                                             "resolve (a comment id such as c4)"}},
                  "required": ["path", "comments"]}
    model_id = "docx_markup.py (lxml, no ML) + Word through COM for the check"
    check_unused = False
    how_it_works = ("No model here: Python anchors each new comment to exactly the quoted words, writes replies into "
                    "the thread (commentsExtended.xml) and the done flag, under the review author, then checks every "
                    "anchor, reference and parent, and that the text did not change.")

    def validate(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        if not args.get("path"):
            raise ValueError("missing required argument(s): path")
        items = as_list(args.get("comments"))
        top = {k: args[k] for k in args if k not in ("path", "comments")}
        if top:
            items.append(top)
        parsed = [c for c in (parse_comment(i) for i in items) if c is not None]
        if not parsed:
            raise ValueError('give comments, e.g. comments=[{"reply_to": "c4", "text": "Done."}, {"resolve": "c4"}]')
        return {"path": str(args["path"]), "comments": parsed}

    def check_request(self, args, ctx):
        """CHECK ARGS: 'reply to comment 4' means c4, and 'mark it resolved' means a resolve, whatever the LLM wrote."""
        request = ctx.request or ""
        items = [dict(c) for c in args["comments"]]
        fixed = []
        named = re.findall(r"\bcomments?\s+(?:c|#)?(\d+)\b|\bc(\d+)\b", request, re.I)
        named = sorted({a or b for a, b in named})
        replies = [c for c in items if c["op"] == "reply"]
        if len(named) == 1 and re.search(r"\b(?:reply|respond|answer)", request, re.I):
            new = next((c for c in items if c["op"] == "new"), None)
            if not replies and new is not None:  # a new comment where the user asked for a reply: it is the reply
                items.remove(new)
                replies = [{"op": "reply", "to": f"c{named[0]}", "text": new["text"], "resolve": False}]
                items.insert(0, replies[0])
                fixed.append(f"the new comment on \"{new['on']}\" became a reply to c{named[0]}")
            for c in replies:
                if re.sub(r"\D", "", c["to"]) != named[0]:
                    fixed.append(f"reply_to {c['to']} became c{named[0]}")
                    c["to"] = f"c{named[0]}"
            # "reply to comment 4 ... that the duct sizes are now in Table 1": the reply is in the user's own words
            said = re.search(r"\b(?:reply|respond|answer)\w*\b.*?\bthat\s+(.+?)(?:,?\s+and\s+(?:then\s+)?"
                             r"(?:mark|resolve)\b|[.?!]?\s*$)", request, re.I | re.S)
            words = said.group(1).strip() if said else ""
            if words and len(replies) == 1 and squash(replies[0]["text"]) != squash(words):
                replies[0]["text"] = words[0].upper() + words[1:] + ("" if words[-1] in ".!?" else ".")
                fixed.append(f"the reply is in the user's words: \"{replies[0]['text']}\"")
        resolve = re.search(r"\b(?:resolve[ds]?|mark (?:it|them|the thread|this) (?:as )?(?:resolved|done))\b",
                            request, re.I)
        if resolve and not any(c["op"] == "resolve" or c.get("resolve") for c in items):
            target = replies[0]["to"] if replies else (f"c{named[0]}" if len(named) == 1 else None)
            if target:
                items.append({"op": "resolve", "to": target})
                fixed.append(f"added resolve {target}")
        if not fixed:
            return args, "the comments stand as the LLM wrote them", None
        return {**args, "comments": items}, "; ".join(fixed), ("The request names the comment and asks for it to be "
                                                              "resolved, so the harness made it: " + "; ".join(fixed))

    def run(self, ctx, path, comments):
        t = ctx.tracer
        source, readable = open_docx(path, ctx)
        t.say(self.how_it_works, kind="python")
        ledger, _, _ = versions.track(source, ctx)
        view = census_stage(ctx, readable)
        refuse_protected(view, source)
        pkg = dm.Package(readable)
        input_problems = dm.structure_problems(pkg)
        known = {c["id"]: c for c in view["comments"]}
        items = []
        for c in comments:  # a reply with resolve: true is a reply and a resolve
            items.append(dict(c))
            if c["op"] == "reply" and c.get("resolve"):
                items.append({"op": "resolve", "to": c["to"]})
        with t.stage("LOCATE", "python", "word_review (no ML)", f"{len(items)} item(s)",
                     "anchors and threads found") as ev:
            paras = paragraphs_of(pkg.document)
            for c in items:
                c["status"], c["why"] = "pending", ""
                if c["op"] == "new":
                    hits, exact = find_text(paras, c["on"])
                    if len(hits) != 1:
                        c["status"] = "refused"
                        c["why"] = (f"\"{short(c['on'], 60)}\" occurs {len(hits)} times ({describe_places(view, hits)})"
                                    ": quote more of it" if hits else f"\"{short(c['on'], 60)}\" is not in the text"
                                    + closest(view, c["on"]))
                    else:
                        c["where"], c["exact"] = hits[0], exact
                else:
                    cid = comment_id(c["to"], known)
                    if cid is None:
                        c["status"] = "refused"
                        c["why"] = (f"there is no comment {c['to']}; the comments are "
                                    + ", ".join(f"c{k}" for k in known)[:200])
                    else:  # Word's threads are flat: a reply to a reply goes under the thread's first comment
                        root_id = cid
                        while known[root_id]["parent"]:
                            root_id = known[root_id]["parent"]
                        c["id"] = root_id
            ev.summary = f"{sum(c['status'] == 'pending' for c in items)} of {len(items)} found"
        for c in items:
            if c["status"] == "pending" and c["op"] == "new" and not c["exact"] and not ask_person(
                    ctx, f"\"{c['on']}\" was found with other spaces or quotes. Comment on it?", "a loose anchor"):
                c["status"], c["why"] = "refused", "a person said no to the loosely matched text"
        writer = dm.Writer(pkg, review_author(ctx), review_initials(ctx))
        with t.stage("APPLY COMMENTS", "python", "docx_markup.py (lxml, no ML)", "comments, replies, resolves",
                     "comments.xml, commentsExtended.xml, people.xml") as ev:
            for c in sorted((c for c in items if c["status"] == "pending" and c["op"] == "new"),
                            key=lambda c: (c["where"][0], c["where"][1]), reverse=True):
                i, a, b = c["where"]
                try:
                    c["made"] = "c" + dm.comment_on_text(pkg, paragraphs_of(pkg.document)[i], a, b, c["text"], writer)
                    c["status"] = "done"
                except dm.Refused as exc:
                    c["status"], c["why"] = "refused", str(exc)
            for c in items:
                if c["status"] != "pending":
                    continue
                if c["op"] == "reply":
                    c["made"] = "c" + dm.add_reply(pkg, c["id"], c["text"], writer)
                else:
                    dm.resolve_thread(pkg, c["id"], writer)
                    c["made"] = f"c{c['id']} resolved"
                c["status"] = "done"
            ev.summary = ", ".join(c.get("made", "") for c in items if c["status"] == "done") or "nothing"
        done = [c for c in items if c["status"] == "done"]
        if not done:
            raise Refusal("Nothing could be done, so no file was written: " + "; ".join(c["why"] for c in items)[:600])
        if OFFICE["track_changes_on"]:
            dm.set_track_changes(pkg, True)
        with t.stage("VERIFY", "python", "word_model (no ML)", "the commented copy + the input",
                     "text unchanged, threads as expected") as ev:
            original = dm.Package(readable).document
            if signature(pkg.document, KEEP) != signature(original, KEEP):
                raise ValueError("The result was not saved: adding the comments changed the text.")
            after = docx_view(pkg)
            new_top = sum(c["op"] == "new" for c in done)
            new_replies = sum(c["op"] == "reply" for c in done)
            expect = {"threads": view["census"]["threads"] + new_top,
                      "replies": view["census"]["replies"] + new_replies}
            got = {k: after["census"][k] for k in expect}
            mine = {c["id"]: c for c in after["comments"]}
            unanchored = [c["made"] for c in done if c["op"] in ("new", "reply") and not (
                mine[c["made"][1:]]["referenced"] and (mine[c["made"][1:]]["anchored"] or c["op"] == "reply"))]
            unresolved = [c["id"] for c in done if c["op"] == "resolve" and not mine[c["id"]]["done"]]
            if got != expect or unanchored or unresolved:
                raise ValueError(f"The result was not saved: the threads are not as expected ({got} vs {expect}"
                                 + (f"; not anchored: {unanchored}" if unanchored else "")
                                 + (f"; not resolved: {unresolved}" if unresolved else "") + ").")
            ev.summary = (f"the text is unchanged; every new comment anchored and referenced, every reply under its "
                          f"parent; {census_line(after['census'])}")
        out, _, word_line, pdf = save_checked(ctx, pkg, source, "commented", input_problems, after["census"])
        ledger_stage(ctx, ledger, out, self.name, [{k: c.get(k) for k in ("op", "to", "on", "text", "made")}
                                                   for c in done])
        report = [item_line(c) for c in items]
        md = write_review(ctx, after, rel(out), "## Comments in this request\n\n" + "\n".join(f"- {x}" for x in report))
        replies = [c for c in done if c["op"] == "reply"]
        resolved = [c for c in done if c["op"] == "resolve"]
        summary = "; ".join(x for x in (
            f"{sum(c['op'] == 'new' for c in done)} new comment(s)" if any(c["op"] == "new" for c in done) else "",
            f"{len(replies)} reply/replies added ({', '.join(c['made'] + ' to ' + label(c) for c in replies)})"
            if replies else "",
            f"{', '.join('c' + c['id'] for c in resolved)} resolved" if resolved else "",
            f"{sum(c['status'] == 'refused' for c in items)} not done" if any(c["status"] == "refused" for c in items)
            else "") if x)
        ctx.notes.append(f"comment_docx on {rel(source)}: {summary}. Before: {census_line(view['census'])}. After: "
                         f"{census_line(after['census'])}. {word_line}. The text is unchanged; the copy is {rel(out)}.")
        artifacts = [{"type": "word_review", "path": out, "note": "the commented copy"},
                     {"type": "word_review", "path": md, "note": "the census and every thread"}]
        if pdf:
            artifacts.append({"type": "word_review", "path": pdf, "note": "the copy as a PDF, with its comments"})
        return {"artifacts": artifacts,
                "observations": {"file": rel(source), "commented": rel(out), "summary": summary,
                                 "items": [short(x, 160) for x in report][:SHOW],
                                 "census_before": census_line(view["census"]),
                                 "census_after": census_line(after["census"]), "word_check": word_line}}


# ---- compare_docx ----------------------------------------------------------------------------------------------------
def version_number(path):
    """'test_plan_v2.docx' -> 2, 'proposal v5' -> 5, 'test_plan.docx' -> 1 (no number: the first)."""
    m = re.search(r"(?:^|[_\-\s.])v(?:ersion)?[_\-\s]?(\d+)(?=$|[_\-\s.])", Path(str(path)).stem, re.I)
    return int(m.group(1)) if m else 1


class CompareDocxTool(Tool):
    name = "compare_docx"
    description = ("Compare two versions of a Word .docx and write a redline: the original with every difference as "
                   "a tracked change (Word's own Compare, or a Python word diff without Word). original: the older "
                   "file; revised: the newer one. With only revised, the original is the last version the harness "
                   "knows (the version ledger).")
    parameters = {"type": "object",
                  "properties": {"original": {"type": "string", "description": "The older .docx (e.g. test_plan.docx)"},
                                 "revised": {"type": "string",
                                             "description": "The newer .docx (e.g. test_plan_v2.docx)"}},
                  "required": ["revised"]}
    model_id = "Word's Compare through COM, else difflib on words (no ML)"
    check_unused = False
    how_it_works = ("No model here: Word's own Compare (in its own process) marks every difference as a tracked "
                    "change; without Word, Python aligns the paragraphs and rows (difflib) and diffs them word by "
                    "word. Either way the redline is checked: accept all = the revised text, reject all = the "
                    "original.")
    ALIASES = {"original": ("original", "old", "base", "baseline", "v1", "first", "file1", "older", "from"),
               "revised": ("revised", "new", "newer", "latest", "v2", "second", "file2", "to", "path", "file")}

    def validate(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        args = {str(k).lower(): v for k, v in args.items()}
        out = {}
        for key, names in self.ALIASES.items():
            value = next((args[n] for n in names if args.get(n)), None)
            if value:
                out[key] = str(value)
        if "revised" not in out and "original" in out:
            raise ValueError("give the newer file as revised (and the older one as original)")
        if "revised" not in out:
            raise ValueError("missing required argument(s): revised")
        return out

    def check_request(self, args, ctx):
        """CHECK ARGS: the newer file is the revised one ('v2' after 'v1'), whichever slot the LLM put it in."""
        original, revised = args.get("original"), args.get("revised")
        if original and revised and version_number(original) > version_number(revised):
            return ({**args, "original": revised, "revised": original},
                    f"swapped: {Path(original).name} is the newer version, so it is the revised file",
                    f"{Path(original).name} is the newer version, so the harness compared it as the revised file.")
        return args, "original and revised stand", None

    def run(self, ctx, revised, original=""):
        t = ctx.tracer
        t.say(self.how_it_works, kind="python")
        new_source, new_read = open_docx(revised, ctx)
        saved_over, saved_ledger = (None, None) if original else earlier_version(new_source, ctx)
        ledger, entry, _ = versions.track(new_source, ctx)
        if original:
            old_source, old_read = open_docx(original, ctx)
            versions.track(old_source, ctx)
        else:  # from the version ledger: the harness's last version before the user's, or the baseline
            earlier = [v for v in ledger.versions if v["n"] < entry["n"]]
            base = next((v for v in reversed(earlier) if v["origin"] == "harness"), earlier[0] if earlier else None)
            if saved_over is not None:  # the user edited a file the harness wrote, in place
                base, ledger = saved_over, saved_ledger
            if base is None:
                raise Refusal(f"No earlier version of {rel(new_source)} is known to the harness: name the file to "
                              "compare it with (original).")
            old_source = old_read = ledger.stored_file(base)
            t.say(f"original: v{base['n']} ({base['origin']}) of {ledger.key}, from the version ledger", kind="python")
        if old_source.resolve() == new_source.resolve():
            raise ValueError("original and revised are the same file")
        old_view = census_stage(ctx, old_read, "the original")
        new_view = census_stage(ctx, new_read, "the revised file")
        author = review_author(ctx)
        work = ctx.run_dir / f"{new_source.stem}_compare_work"
        work.mkdir(exist_ok=True)
        want = [x[1] for x in lines(_root(new_read), ACCEPT, formatting=False)]
        was = [x[1] for x in lines(_root(old_read), ACCEPT, formatting=False)]
        method, made, why_not_word = None, None, ""

        def verified(path):
            root = _root(path)
            return texts(root, ACCEPT) == want and texts(root, REJECT) == was, root

        if OFFICE["compare"] == "word":
            with t.stage("COMPARE: WORD", "server", "Word's CompareDocuments through COM (word_com.py)",
                         "two .docx files", "a redline") as ev:
                result, why = word_com.compare(old_read, new_read, work / "word.docx", author)
                if result is None:
                    why_not_word = why
                    ev.summary = f"not used: {why}"
                else:
                    ok, _ = verified(work / "word.docx")
                    if ok:
                        method, made = "Word's Compare", work / "word.docx"
                        ev.summary = f"{result['revisions']} revision(s); accept all = revised, reject all = original"
                    else:
                        why_not_word = ("Word's redline failed the check (accept all != revised or reject all != "
                                        "original)")
                        ev.summary = why_not_word
        if made is None:
            with t.stage("COMPARE: PYTHON", "python", "docx_markup.python_compare (difflib, no ML)", "two .docx files",
                         "a redline") as ev:
                pkg, writer = dm.python_compare(old_read, new_read, author, review_initials(ctx))
                pkg.save(work / "python.docx")
                ok, _ = verified(work / "python.docx")
                if not ok:
                    raise ValueError("The redline was not saved: its accepted text is not the revised file's, or its "
                                     "rejected text is not the original's.")
                method, made = "a Python word diff" + (f" (Word: {why_not_word})" if why_not_word else ""), \
                    work / "python.docx"
                ev.summary = f"{len(writer.revision_ids)} revision mark(s); accept all = revised, reject all = original"
        with t.stage("VERIFY", "python", "word_model.lines (no ML)", "the redline", "accept all = revised, reject all "
                                                                                     "= original") as ev:
            pkg = dm.Package(made)
            input_problems = [] if method.startswith("a Python") else dm.structure_problems(pkg)
            if OFFICE["track_changes_on"]:
                dm.set_track_changes(pkg, True)
            after = docx_view(pkg)
            ev.summary = (f"accepting all {after['census']['changes']} change(s) gives {new_source.name}'s text, "
                          f"rejecting them gives {old_source.name}'s ({len(want)} and {len(was)} paragraphs)")
        out, _, word_line, pdf = save_checked(ctx, pkg, new_source, "compared", input_problems, after["census"])
        shutil.rmtree(work, ignore_errors=True)
        ledger_stage(ctx, ledger, out, self.name, [{"original": str(old_source), "revised": str(new_source),
                                                    "method": method}])
        changed = [p for p in after["paragraphs"] if "{+" in p["marked"] or "{-" in p["marked"]]
        changes = [f"¶{p['n']}: {excerpt(p['marked'])}" for p in changed]
        md = write_review(ctx, after, rel(out))
        summary = (f"{after['census']['changes']} change(s) from {rel(old_source)} to {rel(new_source)}, by "
                   f"{method}; verified: accepting them gives the revised text, rejecting them the original")
        ctx.notes.append(f"compare_docx: {summary}. {word_line}. The redline is {rel(out)}.")
        artifacts = [{"type": "word_review", "path": out, "note": "the redline (original + tracked changes)"},
                     {"type": "word_review", "path": md, "note": "every change"}]
        if pdf:
            artifacts.append({"type": "word_review", "path": pdf, "note": "the redline as a PDF"})
        return {"artifacts": artifacts,
                "observations": {"original": rel(old_source), "revised": rel(new_source), "redline": rel(out),
                                 "method": method, "summary": summary, "changes": changes[:SHOW],
                                 "census_original": census_line(old_view["census"]),
                                 "census_revised": census_line(new_view["census"]), "word_check": word_line}}


# ---- accept_changes --------------------------------------------------------------------------------------------------
class AcceptChangesTool(Tool):
    name = "accept_changes"
    description = ("Accept or reject tracked changes in a Word .docx, in a new copy. which: 'all', 'author:<name>' "
                   "(e.g. author:B. Checker), revision ids such as '12, 15', or 'mine' (the harness's own). action: "
                   "accept or reject. rest: what to do with the other changes: keep (default), accept or reject (a "
                   "clean copy). comments: keep or remove.")
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string", "description": "The .docx"},
                                 "which": {"type": "string", "description": "all, author:<name>, ids, or mine"},
                                 "action": {"type": "string", "enum": ["accept", "reject"]},
                                 "rest": {"type": "string", "enum": ["keep", "accept", "reject"],
                                          "description": "the changes not chosen: keep them pending (default), "
                                                         "accept or reject them"},
                                 "comments": {"type": "string", "enum": ["keep", "remove"]}},
                  "required": ["path"]}
    model_id = "docx_markup.resolve (lxml, no ML) + Word through COM for the check"
    check_unused = False
    how_it_works = ("No model here: Python accepts (unwraps w:ins, drops w:del) or rejects (the reverse) the chosen "
                    "revisions in a copy, joins paragraphs whose marks go, and checks the text against what the "
                    "reader predicted before anything was changed.")

    def validate(self, args):
        if isinstance(args, dict) and isinstance(args.get("which"), (list, tuple)):
            args = {**args, "which": ", ".join(str(x) for x in args["which"])}
        args = super().validate(args)
        args.setdefault("which", "all")
        return args

    def check_request(self, args, ctx):
        """CHECK ARGS: 'the rest rejected' or 'a clean copy' decide what happens to the changes not chosen, and 'B.
        Checker's changes' means author:B. Checker."""
        request, fixed = ctx.request or "", []
        args = dict(args)
        action = args.get("action", "accept")
        rest = args.get("rest", "keep")
        if re.search(r"\b(?:the )?(?:rest|others?|everything else|all other(?:s| changes)?)\b[^.]{0,20}\breject|"
                     r"\breject(?:ed|ing)? (?:the )?(?:rest|others?|everything else|all (?:the )?others?)\b",
                     request, re.I):
            new_rest = "reject"
        elif re.search(r"\baccept(?:ed|ing)? (?:the )?(?:rest|others?|everything else)\b|\b(?:the )?(?:rest|others?)"
                       r"\b[^.]{0,20}\baccept", request, re.I):
            new_rest = "accept"
        elif re.search(r"\bclean (?:copy|version)\b", request, re.I) and str(args.get("which", "all")) != "all":
            new_rest = "reject" if action == "accept" else "accept"
        else:
            new_rest = rest
        if new_rest != rest:
            fixed.append(f"rest {rest} -> {new_rest}")
            args["rest"] = new_rest
        m = re.search(r"\b([A-Z]\.\s?[A-Z][\w\-]+|[A-Z][\w\-]+ [A-Z][\w\-]+)(?:'s|’s) (?:tracked )?(?:changes|edits|"
                      r"revisions)", request)
        if m and str(args.get("which", "all")).strip().lower() in ("all", "") :
            args["which"] = f"author:{m.group(1)}"
            fixed.append(f"which all -> author:{m.group(1)}")
        if re.search(r"\b(?:remove|delete|without|no) (?:the |all )?comments\b", request, re.I) and \
                args.get("comments", "keep") != "remove":
            args["comments"] = "remove"
            fixed.append("comments -> remove")
        if not fixed:
            return args, "which, action and rest stand", None
        return args, "; ".join(fixed), "From the request's own words: " + "; ".join(fixed) + "."

    def choose(self, which, view, ctx):
        """which -> (a test for a revision element, a description) or a Refusal naming the authors."""
        text = str(which or "all").strip()
        authors = sorted({r["author"] for r in view["revisions"]})
        if text.lower() in ("all", "*", "everything", ""):
            return (lambda el: True), "all changes"
        if text.lower() in ("mine", "harness", "yours", "the harness's", "ours"):
            pattern = harness_authors()
            return (lambda el: bool(pattern.match(el.get(w("author"), "")))), "the harness's changes"
        ids = re.findall(r"\d+", text)
        if ids and not re.search(r"[A-Za-z]{3,}", re.sub(r"^\s*ids?\s*:?", "", text, flags=re.I)):
            wanted = set(ids)
            return (lambda el: el.get(w("id")) in wanted), f"revisions {', '.join(sorted(wanted, key=int))}"
        name = re.sub(r"^\s*(?:author|by)\s*[:=]?\s*", "", text, flags=re.I).strip().strip("'\"")
        match = [a for a in authors if a.lower() == name.lower()] or \
            [a for a in authors if name.lower() in a.lower() or a.lower() in name.lower()]
        if len(match) != 1:
            raise Refusal(f"No single author matches \"{name}\". The authors of tracked changes in this file are: "
                          f"{', '.join(authors) or 'none'}. Use which=\"author:<name>\".")
        return (lambda el: el.get(w("author")) == match[0]), f"{match[0]}'s changes"

    def run(self, ctx, path, which="all", action="accept", rest="keep", comments="keep"):
        t = ctx.tracer
        source, readable = open_docx(path, ctx)
        t.say(self.how_it_works, kind="python")
        ledger, _, _ = versions.track(source, ctx)
        view = census_stage(ctx, readable)
        refuse_protected(view, source)
        pkg = dm.Package(readable)
        input_problems = dm.structure_problems(pkg)
        chosen, described = self.choose(which, view, ctx)
        root = pkg.document

        def decide(el):
            return action if chosen(el) else (rest if rest != "keep" else "keep")

        marks = list(root.iter(*dm.REVISION_TAGS))
        picked = [el for el in marks if chosen(el)]
        if not picked and rest == "keep":
            raise Refusal(f"There are no tracked changes for which=\"{which}\" in {rel(source)} "
                          f"({census_line(view['census'])}): nothing to {action}.")
        mine = harness_authors()
        by = {}
        for el in marks:
            if decide(el) in ("accept", "reject"):
                by.setdefault((decide(el), el.get(w("author"), "")), 0)
                by[(decide(el), el.get(w("author"), ""))] += 1
        foreign = {k: n for k, n in by.items() if not mine.match(k[1])}
        if foreign:  # another author's changes: a person says yes (W4)
            plan = "; ".join(f"{d} {n} revision mark(s) by {a}" for (d, a), n in sorted(foreign.items()))
            if not ask_person(ctx, f"In a new copy of {source.name}: {plan}?",
                              "accepting or rejecting others' changes"):
                raise Refusal("A person said no, so no changes were accepted or rejected and no file was written.")
        with t.stage("PREDICT", "python", "word_model.lines (no ML)", "the input + the decisions",
                     "the text the copy must have") as ev:
            predicted = [(where, text, sig) for where, text, sig in lines(root, decide, formatting=False)]
            groups = changes_as_word_counts(root)
            ev.summary = f"{len(predicted)} paragraph(s) after: {action} {described}, the rest {rest}"
        decided_ids = {el.get(w("id")) for el in marks if decide(el) != "keep"}
        kept_ids = {el.get(w("id")) for el in marks if decide(el) == "keep"}
        with t.stage("APPLY", "python", "docx_markup.resolve (lxml, no ML)", f"{len(marks)} revision mark(s)",
                     "the copy") as ev:
            done = dm.resolve(root, decide)
            removed = dm.remove_comments(pkg) if comments == "remove" else 0
            pkg.touch(pkg.names["document"])
            ev.summary = (f"accepted {done.get('accepted', 0)}, rejected {done.get('rejected', 0)} revision mark(s)"
                          + (f"; {removed} comment(s) removed" if removed else ""))
        if OFFICE["track_changes_on"]:
            dm.set_track_changes(pkg, True)
        with t.stage("VERIFY", "python", "word_model (no ML)", "the copy + the prediction",
                     "the same text, the right revisions left") as ev:
            got = [(where, text, sig) for where, text, sig in lines(pkg.document, KEEP, formatting=False)]
            if got != predicted:
                raise ValueError("The copy was not saved: its text is not what the reader predicted ("
                                 + first_difference([g[1] for g in got], [p[1] for p in predicted]) + ").")
            left = {el.get(w("id")) for el in pkg.document.iter(*dm.REVISION_TAGS)}
            if left & decided_ids or not kept_ids <= left:
                raise ValueError("The copy was not saved: the revisions left are not the ones that should be.")
            after = docx_view(pkg)
            ev.summary = (f"text as predicted; {len(left)} revision mark(s) left, as expected; "
                          f"{census_line(after['census'])}")
        tag = "accepted" if action == "accept" else "rejected"
        word_before = None
        if OFFICE["word_check"]:
            word_before, _ = word_com.open_check(readable)
        out, word, word_line, pdf = save_checked(ctx, pkg, source, tag, input_problems, after["census"])
        if word and word_before:
            word_line += f"; pages before {word_before['pages']}, after {word['pages']}"
        ledger_stage(ctx, ledger, out, self.name, [{"which": which, "action": action, "rest": rest,
                                                    "revision_ids": sorted(decided_ids, key=int)}])
        who = {}
        for g in groups:
            d = decide(g[0])
            if d != "keep":
                who.setdefault(d, {}).setdefault(g[0].get(w("author"), "?"), 0)
                who[d][g[0].get(w("author"), "?")] += 1
        parts = [f"{d}ed {sum(n.values())} change(s) (" + ", ".join(f"{a} {k}" for a, k in n.items()) + ")"
                 for d, n in who.items()]
        summary = (f"{action}ed {described}" + (f"; the rest {rest}ed" if rest != "keep" else "") + ": "
                   + "; ".join(parts) + f"; {after['census']['changes']} still pending"
                   + (f"; {removed} comment(s) removed" if removed else
                      f"; comments kept ({after['census']['threads']} thread(s))"))
        md = write_review(ctx, after, rel(out))
        ctx.notes.append(f"accept_changes on {rel(source)}: {summary}. Before: {census_line(view['census'])}. After: "
                         f"{census_line(after['census'])}. {word_line}. The copy is {rel(out)}.")
        artifacts = [{"type": "word_review", "path": out, "note": f"the copy with {described} {action}ed"},
                     {"type": "word_review", "path": md, "note": "the census after"}]
        if pdf:
            artifacts.append({"type": "word_review", "path": pdf, "note": "the copy as a PDF"})
        return {"artifacts": artifacts,
                "observations": {"file": rel(source), "copy": rel(out), "summary": summary,
                                 "census_before": census_line(view["census"]),
                                 "census_after": census_line(after["census"]), "word_check": word_line}}
