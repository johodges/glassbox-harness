"""
Editing a deck with its author (OFFICE_COLLAB_DESIGN.md, Part P). Four tools around YOUR deck as the master: the
harness edits a copy of the deck itself, and never rebuilds it from a script.

    read_deck    deck_view (deck_model.py): every slide, shape, run and note. The LLM gets one line per slide (all of
                 a slide it asks about); the whole view goes to <deck>_deck.json and <deck>_deck.md in the run folder
    diff_decks   what changed between two versions. Slides are matched by CONTENT (their words and picture hashes),
                 keeping their order where possible (a weighted longest common subsequence), never by part names:
                 PowerPoint renumbers slide1.xml, slide2.xml ... on every save. Then shape by shape: text run by run
                 (wording, bold, italic), geometry beyond 0.1 cm, pictures by hash, tables cell by cell, notes, layout
    edit_deck    a batch of operations, each proven not to touch any other slide:
                    VERSION CHECK  the version ledger (versions.py): first seen, written by the harness, or
                                   edited by you
                    DECK VIEW      slide ids, fingerprints, shapes
                    CHECK OPS      the slides exist; the words to change occur once on their slide; positions -> ids
                    APPROVAL       a person says yes to a delete, or to replacing a box's whole text
                    APPLY OPS      python-pptx and lxml on a copy, by slide id, so one op can't shift the next
                    VERIFY         every slide no op named keeps its fingerprint; each op shows in the new view; every
                                   relationship resolves and every part has a content type. An op that fails is undone
                    VERSION LEDGER the new version and its operations
    check_deck   PowerPoint draws the changed slides (in a child process, deck_check.py) and measures the text; the
                 template profile's keep-out zones (logo, footer rule); references a move or delete may have made stale

Every result is a new file in the run folder (office_common.output_path): the user's deck is never written, so no
request can lose their work. The LLM chooses the operations and writes the words; it never sees XML.
"""

import ast
import difflib
import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path

from lxml import etree

from . import versions
from .base import Refusal, Tool
from .deck_check import (PowerPointUnavailable, contact_sheet, estimated_fit, keep_out_problems, profile_for,
                         render_slides, stale_references, term_hits)
from .deck_model import (NS, R_NS, Package, body_text, deck_markdown, deck_view, local, marked, plain, q,
                         rpr_digest, slide_detail, slide_line, slide_list)
from .files import current_version, named_paths, rel, resolve
from .office_common import ask_person, lock_file, output_path

MATCH = 0.5           # two slides with at least this similarity are the same slide (diff_decks)
GEOMETRY_CM = 0.1     # moves and resizes smaller than this are not reported
MAX_LINES = 12        # list items the LLM sees; the .md files have all of them
OPS = ("text", "notes", "insert", "move", "delete", "duplicate", "import")
STRUCTURAL = {"move", "delete", "import", "insert", "duplicate"}  # ops after which "slide 7" may mean another slide


class OpFailed(Exception):
    """One operation could not be applied; the others go on."""


# ---- the files -------------------------------------------------------------------------------------------------------
def deck_file(path, ctx, edited=True):
    """
    The deck the user means -> (the file they named, the file to read): the edited copy if this request already
    changed it (unless edited=False: diff_decks compares the files as named); a copy in the run folder when
    PowerPoint has it open (a ~$ lock file next to it).
    """
    original = resolve(path, ctx)
    if original.suffix.lower() != ".pptx":
        raise ValueError(f"{rel(original)} is not a PowerPoint deck (.pptx)")
    current = Path(current_version(original, ctx) if edited else original)
    if lock_file(current):
        copy = ctx.run_dir / "inputs" / current.name
        if not copy.exists():
            copy.parent.mkdir(exist_ok=True)
            shutil.copy2(current, copy)
        ctx.facts.setdefault("imported_from", {})[str(copy.resolve())] = str(versions.original_of(current, ctx))
        ctx.tracer.say(f"{rel(current)} is open in PowerPoint (a ~$ lock file next to it): reading a copy, "
                       f"{rel(copy)}; the file itself is never written", kind="python")
        current = copy
    return original, current


def numbers(value):
    """'2-3' -> [2, 3]; '2, 4' -> [2, 4]; [2, '3'] -> [2, 3]; 'slide 5' -> [5]; 5 -> [5]."""
    if value is None or value == "":
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(n for v in value for n in numbers(v)))
    text, out = str(value), []
    for a, b in re.findall(r"(\d+)\s*(?:-|–|to|through)\s*(\d+)", text):
        out += range(int(a), int(b) + 1) if int(b) >= int(a) else [int(a), int(b)]
    text = re.sub(r"(\d+)\s*(?:-|–|to|through)\s*(\d+)", " ", text)
    out += [int(n) for n in re.findall(r"\d+", text)]
    return list(dict.fromkeys(out))


def number(value):
    found = numbers(value)
    return found[0] if found else None


def write_json(path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def new_folder(ctx, source, tag):
    """outputs/run_.../<deck>_<tag>/, numbered if taken: a folder of results, never the source's own."""
    folder = Path(ctx.run_dir) / f"{Path(source).stem}_{tag}"
    n = 2
    while folder.exists():
        folder = Path(ctx.run_dir) / f"{Path(source).stem}_{tag}_{n}"
        n += 1
    folder.mkdir(parents=True)
    return folder


# ---- diff_decks: which slide is which, then what changed -------------------------------------------------------------
def _words(slide):
    words = []
    for s in slide["shapes"]:
        for p in s.get("paragraphs", []):
            words += re.findall(r"\w+", plain(p["runs"]).lower())
        for row in s.get("table", []):
            words += [w for cell in row for w in re.findall(r"\w+", cell.lower())]
        for text in s.get("chart_text", []) + s.get("smartart_text", []):
            words += re.findall(r"\w+", text.lower())
    return words


def _pictures(slide):
    return {s["image"] for s in slide["shapes"] if s.get("image") not in (None, "none")} | \
        {f"chart:{s.get('chart_data', s['chart'])}" for s in slide["shapes"] if s.get("chart")}


def similarity(a, b):
    """How alike two slides are, 0..1: their words in order, their pictures, a shared title and a shared slide id."""
    if a["fingerprint"] == b["fingerprint"]:
        return 1.0
    wa, wb = _words(a), _words(b)
    text = difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio() if (wa or wb) else 1.0
    pa, pb = _pictures(a), _pictures(b)
    if pa or pb:
        pictures = len(pa & pb) / len(pa | pb)
        score = 0.7 * text + 0.3 * pictures if (wa or wb) else pictures
    else:
        score = text
    if a["title"] and a["title"].strip().lower() == b["title"].strip().lower():
        score += 0.1
    if a["id"] == b["id"] and score >= 0.3:  # PowerPoint keeps a slide's id when it saves: evidence, not proof
        score += 0.15
    return min(1.0, score)


def align(old, new):
    """
    Which old slide is which new slide -> (pairs in order [(i, j)], moved [(i, j)], deleted [i], added [j]).
    A weighted longest common subsequence keeps as many slides in order as it can; a slide matched only outside that
    order has moved. So a deleted slide shows as deleted, not as every later slide changed (the RTM job's align_diff).
    """
    n, m = len(old), len(new)
    sim = [[similarity(o, w) for w in new] for o in old]
    # Slides on one template look alike (the same title box, similar bullets): weighted by sim ** 8, one near-identical
    # pair outweighs several look-alikes, so the order is kept by the slides that really are the same
    weight = [[s ** 8 for s in row] for row in sim]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            take = best[i - 1][j - 1] + weight[i - 1][j - 1] if sim[i - 1][j - 1] >= MATCH else -1.0
            best[i][j] = max(best[i - 1][j], best[i][j - 1], take)
    pairs, i, j = [], n, m
    while i and j:
        if sim[i - 1][j - 1] >= MATCH and best[i][j] == best[i - 1][j - 1] + weight[i - 1][j - 1]:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif best[i - 1][j] >= best[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    left_old, left_new = set(range(n)) - {a for a, _ in pairs}, set(range(m)) - {b for _, b in pairs}
    moved = []
    for score, a, b in sorted(((sim[a][b], a, b) for a in left_old for b in left_new if sim[a][b] >= MATCH),
                              reverse=True):
        if a in left_old and b in left_new:
            moved.append((a, b))
            left_old.discard(a)
            left_new.discard(b)
    return pairs, sorted(moved), sorted(left_old), sorted(left_new)


def _bold_words(runs, mark="b"):
    return {w for r in runs if r.get(mark) for w in re.findall(r"\w[\w.,%-]*", r["t"])}


def _marks_note(a_runs, b_runs):
    """Words that kept their place but gained or lost bold, italic or superscript ('**12**' -> '**14**' is a new
    word, not bold added)."""
    notes = []
    both = set(re.findall(r"\w[\w.,%-]*", plain(a_runs))) & set(re.findall(r"\w[\w.,%-]*", plain(b_runs)))
    for mark, name in (("b", "bold"), ("i", "italic"), ("sup", "superscript")):
        gained = sorted((_bold_words(b_runs, mark) - _bold_words(a_runs, mark)) & both)
        lost = sorted((_bold_words(a_runs, mark) - _bold_words(b_runs, mark)) & both)
        if gained:
            notes.append(f"{name} added: {', '.join(gained[:4])}")
        if lost:
            notes.append(f"{name} removed: {', '.join(lost[:4])}")
    links_a = {r.get("link") for r in a_runs if r.get("link")}
    links_b = {r.get("link") for r in b_runs if r.get("link")}
    if links_a != links_b:
        notes.append("link changed")
    return "; ".join(notes)


def _clip(text, limit=110):
    return text if len(text) <= limit else text[:limit - 3] + "..."


def compare_paragraphs(pa, pb, label):
    """Two text boxes' paragraphs -> [(kind, text)]: reworded, re-formatted, added or removed paragraphs."""
    a = [p for p in pa if plain(p["runs"]).strip()]
    b = [p for p in pb if plain(p["runs"]).strip()]
    norm = lambda p: " ".join(plain(p["runs"]).split())
    title = label == "title"
    items = []
    matcher = difflib.SequenceMatcher(None, [norm(p) for p in a], [norm(p) for p in b], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for p, r in zip(a[i1:i2], b[j1:j2]):
                where = label if title else f"{label} bullet {b.index(r) + 1}"
                if marked(p["runs"]) != marked(r["runs"]):
                    items.append(("format", f"{where} \"{_clip(marked(p['runs']))}\" -> \"{_clip(marked(r['runs']))}\""
                                  f" ({_marks_note(p['runs'], r['runs']) or 'formatting changed'})"))
                elif p["level"] != r["level"]:
                    items.append(("format", f"{where} indent level {p['level']} -> {r['level']}: "
                                            f"\"{_clip(norm(r))}\""))
            continue
        old, new = a[i1:i2], b[j1:j2]
        for p, r in zip(old, new):
            where = label if title else f"{label} bullet {b.index(r) + 1}"
            note = _marks_note(p["runs"], r["runs"])
            items.append(("text", f"{where} \"{_clip(marked(p['runs']))}\" -> \"{_clip(marked(r['runs']))}\""
                          + (f" ({note})" if note else "")))
        for p in old[len(new):]:
            items.append(("text", f"{label} paragraph removed: \"{_clip(marked(p['runs']))}\""))
        for r in new[len(old):]:
            items.append(("text", f"{label} bullet {b.index(r) + 1} added: \"{_clip(marked(r['runs']))}\""))
    return items


def _label(shape, shapes):
    if shape.get("ph") in ("title", "ctrTitle"):
        return "title"
    bodies = [s for s in shapes if s.get("ph") in ("body", "obj", "subTitle") and s.get("paragraphs")]
    if len(bodies) == 1 and bodies[0] is shape:
        return "body"
    return f"\"{shape['name']}\""


def compare_shapes(a, b, label):
    items = []
    if not a.get("group") and a.get("box") and b.get("box"):
        (x0, y0, w0, h0), (x1, y1, w1, h1) = a["box"], b["box"]
        moved = abs(x0 - x1) > GEOMETRY_CM or abs(y0 - y1) > GEOMETRY_CM
        if abs(w0 - w1) > GEOMETRY_CM or abs(h0 - h1) > GEOMETRY_CM:
            items.append(("geometry", f"{label} resized from {w0:.1f} x {h0:.1f} cm to {w1:.1f} x {h1:.1f} cm"
                          + (f", and moved to ({x1:.1f}, {y1:.1f})" if moved else "")))
        elif moved:
            items.append(("geometry", f"{label} moved from ({x0:.1f}, {y0:.1f}) to ({x1:.1f}, {y1:.1f}) cm"))
    if a.get("rotation", 0) != b.get("rotation", 0):
        items.append(("geometry", f"{label} rotated {a.get('rotation', 0)} -> {b.get('rotation', 0)} degrees"))
    if a.get("image") and b.get("image") and a["image"] != b["image"]:
        items.append(("picture", f"picture {label} swapped for another image"
                      + (" (its file is missing: a broken picture)" if b["image"] == "broken" else "")))
    if a.get("paragraphs") or b.get("paragraphs"):
        items += compare_paragraphs(a.get("paragraphs", []), b.get("paragraphs", []), label)
    if a.get("table") is not None and b.get("table") is not None and a["table"] != b["table"]:
        ta, tb = a["table"], b["table"]
        cells = [f"row {r + 1} col {c + 1} \"{_clip(x, 30)}\" -> \"{_clip(y, 30)}\""
                 for r, (ra, rb) in enumerate(zip(ta, tb)) for c, (x, y) in enumerate(zip(ra, rb)) if x != y]
        shape = (f"; {len(ta)} x {len(ta[0]) if ta else 0} -> {len(tb)} x {len(tb[0]) if tb else 0} cells"
                 if (len(ta), len(ta[0]) if ta else 0) != (len(tb), len(tb[0]) if tb else 0) else "")
        items.append(("table", f"table {label}: " + ("; ".join(cells[:4]) or "rows changed") + shape
                      + (f" (+{len(cells) - 4} more cells)" if len(cells) > 4 else "")))
    if a.get("chart") and a.get("chart_data") != b.get("chart_data"):
        items.append(("chart", f"chart {label} changed (its numbers, categories or titles)"))
    if a.get("smartart_text") != b.get("smartart_text"):
        items.append(("text", f"SmartArt {label}: \"{_clip(' / '.join(b.get('smartart_text') or []))}\""))
    return items


def match_shapes(sa, sb):
    """Shapes of two versions of a slide -> (pairs, removed, added): by name first, then by kind and position."""
    key = lambda s: (s.get("group"), s["name"])
    kinds = lambda s: "text" if s["kind"] in ("text", "placeholder", "equation") else s["kind"]
    count_a, count_b = {}, {}
    for s in sa:
        count_a[key(s)] = count_a.get(key(s), 0) + 1
    for s in sb:
        count_b[key(s)] = count_b.get(key(s), 0) + 1
    pairs, used = [], set()
    for i, s in enumerate(sa):
        if count_a[key(s)] == 1 and count_b.get(key(s)) == 1:
            j = next(j for j, t in enumerate(sb) if key(t) == key(s))
            if kinds(sb[j]) == kinds(s):
                pairs.append((i, j))
                used.add(j)
    done = {i for i, _ in pairs}

    def closeness(s, t):
        if kinds(s) != kinds(t):
            return 0.0
        text = difflib.SequenceMatcher(None, body_text(s.get("paragraphs", [])), body_text(t.get("paragraphs", []))
                                       ).ratio() if s.get("paragraphs") or t.get("paragraphs") else 0.5
        if s.get("box") and t.get("box"):
            far = abs(s["box"][0] - t["box"][0]) + abs(s["box"][1] - t["box"][1])
            text += 0.5 * max(0.0, 1 - far / 5)
        return text

    for i, s in enumerate(sa):
        if i in done:
            continue
        options = sorted(((closeness(s, t), j) for j, t in enumerate(sb) if j not in used), reverse=True)
        if options and options[0][0] >= 0.5:
            pairs.append((i, options[0][1]))
            used.add(options[0][1])
            done.add(i)
    return pairs, [s for i, s in enumerate(sa) if i not in done], [t for j, t in enumerate(sb) if j not in used]


def compare_slides(a, b):
    """Two versions of one slide -> [(kind, text)]"""
    items = []
    if a["layout"] != b["layout"]:
        items.append(("layout", f"layout {a['layout']} -> {b['layout']}"))
    if a.get("hidden") != b.get("hidden"):
        items.append(("hidden", "hidden in the slide show" if b.get("hidden") else "no longer hidden"))
    pairs, removed, added = match_shapes(a["shapes"], b["shapes"])
    for i, j in pairs:
        items += compare_shapes(a["shapes"][i], b["shapes"][j], _label(b["shapes"][j], b["shapes"]))
    for s in removed:
        if s.get("ph") not in ("sldNum", "dt", "ftr"):
            items.append(("shape", f"{s['kind']} \"{s['name']}\" removed"))
    for s in added:
        if s.get("ph") not in ("sldNum", "dt", "ftr"):
            items.append(("shape", f"{s['kind']} \"{s['name']}\" added"))
    na, nb = " ".join(a["notes"].split()), " ".join(b["notes"].split())
    if na != nb:
        if not na:
            items.append(("notes", f"notes added ({len(nb.split())} words): \"{_clip(nb, 90)}\""))
        elif not nb:
            items.append(("notes", f"notes removed ({len(na.split())} words)"))
        else:
            items.append(("notes", f"notes rewritten ({len(nb.split()) - len(na.split()):+d} words): "
                                   f"\"{_clip(nb, 90)}\""))
    return items


def diff_views(old, new):
    """Two deck_views -> {"items": [{"kind", "old", "new", "title", "text"}], "moved", "deleted", "added", "changed",
    "summary"}. old/new in an item are slide positions (None where the slide is not in that version)."""
    so, sn = old["slides"], new["slides"]
    pairs, moved, deleted, added = align(so, sn)
    items = []
    for i, j in moved:
        before = f" (after \"{sn[j - 1]['title']}\")" if j else " (now first)"
        items.append({"kind": "moved", "old": i + 1, "new": j + 1, "title": sn[j]["title"],
                      "text": f"slide {i + 1} \"{sn[j]['title']}\" moved: now slide {j + 1}{before}"})
    for i in deleted:
        items.append({"kind": "deleted", "old": i + 1, "new": None, "title": so[i]["title"],
                      "text": f"slide {i + 1} \"{so[i]['title']}\" deleted"})
    for j in added:
        items.append({"kind": "added", "old": None, "new": j + 1, "title": sn[j]["title"],
                      "text": f"slide {j + 1} \"{sn[j]['title']}\" added ({slide_line(sn[j]).split(': ', 1)[-1]})"})
    changed = set()
    for i, j in sorted(pairs + moved, key=lambda p: p[1]):
        for kind, text in compare_slides(so[i], sn[j]):
            changed.add(j)
            items.append({"kind": kind, "old": i + 1, "new": j + 1, "title": sn[j]["title"],
                          "text": f"slide {j + 1} \"{sn[j]['title']}\": {text}"})
    unchanged = len(pairs) + len(moved) - len(changed)
    summary = (f"{len(moved)} slide(s) moved, {len(deleted)} deleted, {len(added)} added, {len(changed)} changed "
               f"({sum(it['kind'] not in ('moved', 'deleted', 'added') for it in items)} change(s)); "
               f"{unchanged} unchanged")
    pick = lambda kind: [it["text"] for it in items if it["kind"] == kind]
    return {"items": items, "summary": summary, "moved": pick("moved"), "deleted": pick("deleted"),
            "added": pick("added"),
            "changed": [it["text"] for it in items if it["kind"] not in ("moved", "deleted", "added")],
            "slide_count": [len(so), len(sn)]}


def diff_markdown(diff, old_name, new_name):
    lines = [f"# What changed: {old_name} -> {new_name}", "", diff["summary"], "",
             "Slides are matched by their words and pictures (keeping their order where possible), not by part names.",
             ""]
    for kind, heading in (("moved", "Moved"), ("deleted", "Deleted"), ("added", "Added")):
        if diff[kind]:
            lines += [f"## {heading}", ""] + [f"- {t}" for t in diff[kind]] + [""]
    if diff["changed"]:
        lines += ["## Changed", ""] + [f"- {t}" for t in diff["changed"]] + [""]
    return "\n".join(lines)


def ledger_pair(path, ctx):
    """
    diff_decks with one deck: the version ledger says what to compare it with. A version you edited is compared with
    the harness's version before it; a version the harness wrote, with the newest version you edited after it.
    -> (old file, new file, how they were chosen)
    """
    ledger, entry, what = versions.track(path, ctx)
    earlier = [v for v in ledger.versions if v["n"] < entry["n"] and v["origin"] in ("harness", "baseline")]
    later = [v for v in ledger.versions if v["n"] > entry["n"] and v["origin"] == "user"]
    if entry["origin"] == "user" and earlier:
        return ledger.stored_file(earlier[-1]), Path(path), \
            f"your version v{entry['n']} against the harness's v{earlier[-1]['n']} (the version ledger)"
    if later:
        return Path(path), ledger.stored_file(later[-1]), \
            f"v{entry['n']} against your newest version, v{later[-1]['n']} (the version ledger)"
    raise Refusal(f"The version ledger has nothing to compare {rel(path)} with (it is v{entry['n']}, "
                  f"{entry['origin']}, of {ledger.key}). Name both decks: old and new.")


# ---- edit_deck: reading the LLM's operations -------------------------------------------------------------------------
OP_WORDS = {"text": ("text", "replace", "change", "edit", "reword", "set_text", "change_text", "replace_text"),
            "notes": ("notes", "note", "speaker_notes", "add_notes", "set_notes", "speaker_note"),
            "insert": ("insert", "add", "add_slide", "new_slide", "insert_slide"),
            "move": ("move", "reorder", "move_slide"),
            "delete": ("delete", "remove", "delete_slide", "remove_slide"),
            "duplicate": ("duplicate", "copy", "clone", "duplicate_slide"),
            "import": ("import", "import_slides", "merge", "insert_from")}
FIELDS = {"slide": ("slide", "slides", "slide_number", "number", "index", "page", "on_slide", "slide_no"),
          "after": ("after", "after_slide", "insert_after", "to_after"),
          "before": ("before", "before_slide"),
          "find": ("find", "old", "search", "old_text", "text_to_find", "original", "from_text", "replace_text"),
          "replace": ("replace", "new", "with", "new_text", "replacement", "by", "to_text", "replace_with"),
          "shape": ("shape", "in", "box", "placeholder", "shape_name", "target"),
          "mode": ("mode", "how"),
          "layout": ("layout", "layout_name"),
          "title": ("title", "heading"),
          "bullets": ("bullets", "body", "points", "bullet_points", "items", "content", "lines"),
          "from": ("from", "source", "deck", "file", "path", "pptx", "from_deck", "source_deck"),
          "restyle": ("restyle", "restyled", "use_layouts", "keep_style")}


def op_name(word):
    word = str(word).strip().lower().replace(" ", "_").replace("-", "_")
    return next((op for op, words in OP_WORDS.items() if word in words), None)


def _field(d, name, default=None):
    for key in FIELDS[name]:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def _after(value, count=None):
    """'after' as the LLM writes it: 2, 'slide 2', 'start' (0), 'end' (the last slide)."""
    if isinstance(value, str) and value.strip().lower() in ("start", "beginning", "first", "top", "0"):
        return 0
    if isinstance(value, str) and value.strip().lower() in ("end", "last", "bottom") and count:
        return count
    return number(value)


def normalize_op(raw):
    """One op as a small LLM writes it -> {"op": ..., fields}. Raises ValueError with the form it expects."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            for parse in (json.loads, ast.literal_eval):
                try:
                    return normalize_op(parse(text))
                except (ValueError, SyntaxError):
                    continue
        return parse_op_text(text)
    if not isinstance(raw, dict):
        raise ValueError(f"an op must be an object such as {{\"op\": \"move\", \"slide\": 5, \"after\": 2}}, "
                         f"not {raw!r}")
    d = {str(k).strip().lower().replace(" ", "_"): v for k, v in raw.items()}
    name = None
    for key in ("op", "action", "type", "operation", "kind", "do", "command"):
        if key in d and op_name(d[key]):
            name = op_name(d.pop(key))
            break
    if name is None:  # {"move": 5, "after": 2}, {"notes": 3, "text": "..."}, {"import": "results.pptx", ...}
        fields = {word for words in FIELDS.values() for word in words}  # "new": 14 is a replacement, not an op
        numbered = [k for k in d if op_name(k) and k not in fields and number(d[k]) is not None
                    and not isinstance(d[k], bool)
                    and not (op_name(k) == "text" and "find" not in d and "replace" not in d and "notes" in d)]
        if numbered:
            name = op_name(numbered[0])
            if name != "import":  # {"move": 5}: the number is the slide
                d.setdefault("slide", d.pop(numbered[0]))
        elif any(op_name(k) == "import" for k in d):
            key = next(k for k in d if op_name(k) == "import")
            d.setdefault("from", d.pop(key))
            name = "import"
        elif any(op_name(k) == "notes" for k in d):
            key = next(k for k in d if op_name(k) == "notes")
            d.setdefault("text", d.pop(key))
            name = "notes"
        elif _field(d, "find") is not None or _field(d, "replace") is not None:
            name = "text"
        elif any(str(v).lower().endswith(".pptx") for v in d.values()):
            name = "import"
        elif _field(d, "title") is not None or _field(d, "bullets") is not None:
            name = "insert"
        elif _field(d, "slide") is not None and (_field(d, "after") is not None or _field(d, "before") is not None):
            name = "move"
    if name is None:
        raise ValueError(f"could not tell which operation {raw!r} is. Write \"op\": one of {', '.join(OPS)}")
    op = {"op": name}
    slide = _field(d, "slide")
    after, before = _field(d, "after"), _field(d, "before")
    if name == "text":
        op.update(slide=number(slide), find=str(_field(d, "find", "") or ""), shape=_field(d, "shape"))
        replace = _field(d, "replace")
        if replace is None and "text" in d and op["find"]:
            replace = d["text"]
        op["replace"] = "" if replace is None else str(replace)
    elif name == "notes":
        text = d.get("text", d.get("notes", d.get("note", d.get("content", d.get("value")))))
        if isinstance(text, list):
            text = "\n".join(str(t) for t in text)
        mode = str(_field(d, "mode", "append")).lower()
        op.update(slide=number(slide), text=str(text or ""),
                  mode="replace" if mode in ("replace", "set", "overwrite", "rewrite", "new") else "append")
    elif name == "insert":
        items = _field(d, "bullets", [])
        if isinstance(items, str):
            items = [line for line in re.split(r"\n|;", items) if line.strip()]
        op.update(after=_after(after) if after is not None else (number(before) - 1 if number(before) else None),
                  layout=_field(d, "layout"), title=str(_field(d, "title", "") or ""),
                  bullets=[str(b) for b in items])
    elif name in ("move", "duplicate"):
        op["slide"] = number(slide)
        op["after"] = _after(after) if after is not None else (number(before) - 1 if number(before) else None)
        if op["after"] is None and "to" in d and number(d["to"]) and op["slide"]:  # "to 3": becomes slide 3
            to = number(d["to"])
            op["after"] = to - 1 if to <= op["slide"] else to
        if op["after"] is None and name == "duplicate":
            op["after"] = op["slide"]  # a copy goes right after its slide
    elif name == "delete":
        op["slide"] = number(slide)
    elif name == "import":
        restyle = _field(d, "restyle", True)
        op.update(**{"from": str(_field(d, "from", "") or ""), "slides": numbers(_field(d, "slide", "")),
                     "after": _after(after) if after is not None else None,
                     "restyle": str(restyle).strip().lower() not in ("false", "no", "0", "off")})
    return op


QUOTE = r"[\"“”'‘’]"
TEXT_FORMS = [
    (re.compile(r"^\s*move\s+(?:slide\s+)?(\d+)\s+(after|before)\s+(?:slide\s+)?(\d+)", re.I),
     lambda m: {"op": "move", "slide": int(m[1]), "after": int(m[3]) - (m[2].lower() == "before")}),
    (re.compile(r"^\s*(?:delete|remove)\s+(?:slide\s+)?(\d+)", re.I), lambda m: {"op": "delete", "slide": int(m[1])}),
    (re.compile(r"^\s*(?:duplicate|copy)\s+(?:slide\s+)?(\d+)(?:\s+after\s+(?:slide\s+)?(\d+))?", re.I),
     lambda m: {"op": "duplicate", "slide": int(m[1]), "after": int(m[2] or m[1])}),
    (re.compile(r"^\s*(?:add\s+)?(?:speaker\s+)?notes?\s+(?:to\s+|on\s+|for\s+)?(?:slide\s+)?(\d+)\s*[:=-]\s*(.+)$",
                re.I | re.S),
     lambda m: {"op": "notes", "slide": int(m[1]), "text": m[2].strip().strip("\"“”'"), "mode": "append"}),
    (re.compile(r"^\s*import\s+(?:slides?\s+)?([\d,\s\-–to]+?)\s+(?:of|from)\s+(\S+\.pptx)"
                r"(?:.*?after\s+(?:slide\s+)?(\d+))?", re.I),
     lambda m: {"op": "import", "from": m[2], "slides": numbers(m[1]), "after": int(m[3]) if m[3] else None,
                "restyle": True}),
    (re.compile(r"slide\s+(\d+).*?" + QUOTE + r"(.+?)" + QUOTE + r"\s*(?:->|→|to|with|by|into)\s*" + QUOTE + r"(.*?)"
                + QUOTE, re.I | re.S),
     lambda m: {"op": "text", "slide": int(m[1]), "find": m[2], "replace": m[3]}),
    (re.compile(QUOTE + r"(.+?)" + QUOTE + r"\s*(?:->|→|to|with|by|into)\s*" + QUOTE + r"(.*?)" + QUOTE
                + r".*?slide\s+(\d+)", re.I | re.S),
     lambda m: {"op": "text", "slide": int(m[3]), "find": m[1], "replace": m[2]}),
]


def parse_op_text(text):
    """'move 5 after 2', 'delete slide 7', 'notes 3: ...', 'slide 4: "12 tests" -> "14 tests"', 'import 2-3 from
    x.pptx after 6' -> an op."""
    for pattern, make in TEXT_FORMS:
        m = pattern.search(text)
        if m:
            return normalize_op(make(m))
    raise ValueError(f"could not read the op \"{text[:80]}\": write ops as objects, e.g. "
                     "{\"op\": \"move\", \"slide\": 5, \"after\": 2}")


def parse_ops(value):
    """The ops argument in any shape a small model sends: a list, one object, a JSON string, op sentences."""
    if isinstance(value, str):
        text = value.strip()
        parsed = None
        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(text)
                break
            except (ValueError, SyntaxError):
                continue
        value = parsed if parsed is not None else [s for s in re.split(r"\n|;", text) if s.strip()]
    if isinstance(value, dict):
        value = value.get("ops", value.get("operations", [value]))
    if not isinstance(value, list):
        value = [value]
    ops = []
    for raw in value:
        try:
            ops.append(normalize_op(raw))
        except ValueError as exc:
            ops.append({"op": "unreadable", "error": str(exc), "raw": str(raw)[:120]})
    return ops


def describe(op):
    """An op in a few words, for the trace, the ledger and the LLM."""
    kind = op["op"]
    if kind == "text":
        if not op.get("find"):
            return (f"slide {op.get('slide')} \"{op.get('shape') or 'body'}\": all text -> "
                    f"\"{_clip(op['replace'], 60)}\"")
        return f"slide {op.get('slide')}: \"{_clip(op['find'], 50)}\" -> \"{_clip(op['replace'], 50)}\""
    if kind == "notes":
        return f"notes on slide {op.get('slide')} ({op['mode']}): \"{_clip(op['text'], 60)}\""
    if kind == "insert":
        return f"insert a slide after slide {op.get('after')}: \"{op.get('title', '')}\""
    if kind == "move":
        return f"move slide {op.get('slide')} after slide {op.get('after')}"
    if kind == "delete":
        return f"delete slide {op.get('slide')}"
    if kind == "duplicate":
        return f"duplicate slide {op.get('slide')} after slide {op.get('after')}"
    if kind == "import":
        return (f"import slide(s) {', '.join(map(str, op.get('slides', [])))} of {Path(op['from']).name} after slide "
                f"{op.get('after')}" + ("" if op.get("restyle", True) else " (kept as drawn)"))
    return f"unreadable op: {op.get('raw', '')}"


QUOTED_EDIT = re.compile(r"\b(?:change|replace|correct)\s+[\"“'](.+?)[\"”']\s+(?:to|with|by|into)\s+[\"“'](.+?)[\"”']",
                         re.I | re.S)


# ---- edit_deck: finding words on a slide -----------------------------------------------------------------------------
QUOTES = {"’": "'", "‘": "'", "“": '"', "”": '"', " ": " ", "–": "-"}


def strip_marks(text):
    """'**12 tests**' -> '12 tests': read_deck shows bold as **...**, and the LLM may copy it."""
    return re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", text))


def segments(text):
    """'the **only** vent' -> [('the ', {}), ('only', {'b': True}), (' vent', {})], and whether marks were used."""
    parts, pos, used = [], 0, False
    for m in re.finditer(r"\*\*(.+?)\*\*|(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", text):
        if m.start() > pos:
            parts.append((text[pos:m.start()], {}))
        parts.append((m.group(1) or m.group(2), {"b": True} if m.group(1) else {"i": True}))
        pos, used = m.end(), True
    if pos < len(text):
        parts.append((text[pos:], {}))
    return parts, used


def paragraph_pieces(p):
    """The children of an a:p that hold text -> [(element, start, end, kind)]: runs, fields, breaks, equations."""
    pieces, pos = [], 0
    for child in p:
        tag = local(child)
        if tag in ("r", "fld"):
            n = len("".join(t.text or "" for t in child.findall(q("a:t"))))
            pieces.append((child, pos, pos + n, tag))
            pos += n
        elif tag in ("br", "AlternateContent"):
            pieces.append((child, pos, pos + 1, "br" if tag == "br" else "math"))
            pos += 1
    return pieces


def paragraph_text(p):
    out = []
    for el, s, e, kind in paragraph_pieces(p):
        out.append("".join(t.text or "" for t in el.findall(q("a:t"))) if kind in ("r", "fld")
                   else "\v" if kind == "br" else "￼")
    return "".join(out)


def char_formats(p):
    """Every character of a paragraph with its run's formatting hash, as deck_view sees it."""
    out = []
    for el, s, e, kind in paragraph_pieces(p):
        if kind in ("r", "fld"):
            fmt = rpr_digest(el.find(q("a:rPr")))
            out += [(ch, fmt) for ch in "".join(t.text or "" for t in el.findall(q("a:t")))]
        else:
            out.append(("\v" if kind == "br" else "￼", "br" if kind == "br" else "math"))
    return out


def text_targets(slide_el):
    """Every editable text on a slide: shapes (in groups too) and table cells. Shapes inside mc:AlternateContent
    (equations) are left out on purpose: read, never edited."""
    out = []

    def walk(parent):
        for el in parent:
            tag = local(el)
            if tag == "grpSp":
                walk(el)
            elif tag == "sp" and el.find(q("p:txBody")) is not None:
                nv, ph = el.find("./p:nvSpPr/p:cNvPr", NS), el.find("./p:nvSpPr/p:nvPr/p:ph", NS)
                out.append({"name": nv.get("name", ""), "id": int(nv.get("id", 0)), "cell": None,
                            "ph": ph.get("type", "obj") if ph is not None else None, "body": el.find(q("p:txBody"))})
            elif tag == "graphicFrame" and el.find(".//a:tbl", NS) is not None:
                nv = el.find("./p:nvGraphicFramePr/p:cNvPr", NS)
                for r, tr in enumerate(el.find(".//a:tbl", NS).findall(q("a:tr")), 1):
                    for c, tc in enumerate(tr.findall(q("a:tc")), 1):
                        if tc.find(q("a:txBody")) is not None:
                            out.append({"name": nv.get("name", ""), "id": int(nv.get("id", 0)), "cell": (r, c),
                                        "ph": None, "body": tc.find(q("a:txBody"))})

    walk(slide_el.find("./p:cSld/p:spTree", NS))
    return out


def shape_matches(target, wanted):
    wanted = str(wanted).strip().lower()
    if not wanted:
        return True
    if wanted in ("title", "heading", "the title"):
        return target["ph"] in ("title", "ctrTitle")
    if wanted in ("body", "content", "text", "bullets", "bullet points", "subtitle", "main text"):
        return target["ph"] in ("body", "obj", "subTitle")
    name = target["name"].lower()
    return name == wanted or wanted in name


def loose_hits(text, find):
    """Where `find` occurs ignoring case, curly quotes and runs of spaces -> [(start, end)] in the original text."""
    norm, index, space = [], [], False
    for i, ch in enumerate(text):
        ch = QUOTES.get(ch, ch).lower()
        if ch.isspace():
            if space:
                continue
            ch, space = " ", True
        else:
            space = False
        norm.append(ch)
        index.append(i)
    norm, target = "".join(norm), " ".join("".join(QUOTES.get(c, c) for c in find).lower().split())
    hits, at = [], norm.find(target) if target else -1
    while at >= 0:
        hits.append((index[at], index[at + len(target) - 1] + 1))
        at = norm.find(target, at + 1)
    return hits


def find_words(slide_el, find, shape=None):
    """
    CHECK ARGS for a text op: where are these words on the slide? -> (hits, repaired, note). Exact first, in the
    named shape (or anywhere on the slide); then ignoring case, curly quotes and spaces, which counts as a repair a
    person approves. A hit: {"target", "para", "start", "end"}.
    """
    targets = text_targets(slide_el)
    chosen = [t for t in targets if shape_matches(t, shape)] if shape else targets
    note = None
    for words in dict.fromkeys([find, strip_marks(find)]):
        for pool in ([chosen, targets] if shape else [targets]):
            hits = [{"target": t, "para": k, "start": m.start(), "end": m.end()}
                    for t in pool for k, p in enumerate(t["body"].findall(q("a:p")))
                    for m in re.finditer(re.escape(words), paragraph_text(p))]
            if hits:
                if pool is targets and shape and chosen is not targets:
                    note = f"the words are not in \"{shape}\": found in \"{hits[0]['target']['name']}\""
                return hits, False, note
    hits = [{"target": t, "para": k, "start": s, "end": e} for t in targets
            for k, p in enumerate(t["body"].findall(q("a:p"))) for s, e in loose_hits(paragraph_text(p),
                                                                                       strip_marks(find))]
    return hits, bool(hits), note


# ---- edit_deck: changing runs without changing their formatting ------------------------------------------------------
def _split_at(p, pos):
    """Make `pos` fall between two runs: a run that straddles it becomes two, both with its formatting."""
    for el, s, e, kind in paragraph_pieces(p):
        if kind == "r" and s < pos < e:
            t = el.find(q("a:t"))
            twin = deepcopy(el)
            twin.find(q("a:t")).text = (t.text or "")[pos - s:]
            t.text = (t.text or "")[:pos - s]
            el.addnext(twin)
            return [el, twin]
    return []


def _set_marks(run, marks, explicit):
    rpr = run.find(q("a:rPr"))
    if rpr is None:
        rpr = etree.SubElement(run, q("a:rPr"))
        run.remove(rpr)
        run.insert(0, rpr)
    for mark in ("b", "i"):
        if marks.get(mark):
            rpr.set(mark, "1")
        elif explicit and rpr.get(mark) in ("1", "true"):
            del rpr.attrib[mark]


def _merge(p, touched):
    """Join neighbouring runs this edit made (or split) that have the same formatting, so the XML stays tidy."""
    children = list(p)
    i = 0
    while i < len(children) - 1:
        a, b = children[i], children[i + 1]
        same = lambda el: etree.tostring(el.find(q("a:rPr"))) if el.find(q("a:rPr")) is not None else b""
        if local(a) == local(b) == "r" and (a in touched or b in touched) and same(a) == same(b):
            a.find(q("a:t")).text = (a.find(q("a:t")).text or "") + (b.find(q("a:t")).text or "")
            p.remove(b)
            touched.add(a)
            children.pop(i + 1)
            continue
        i += 1


def replace_range(p, start, end, parts, explicit):
    """
    Characters start..end of one paragraph -> the text in `parts` [(words, marks)]. The new words take the
    formatting of the first replaced run (or, for an insertion, of the run before); the runs around keep theirs.
    -> (the new characters with their formatting hash, whether replaced runs with different formatting were merged)
    """
    for el, s, e, kind in paragraph_pieces(p):
        if kind != "r" and s < end and e > start:
            raise OpFailed("the words cross a line break, a field or an equation, which edits leave alone")
    touched = set(_split_at(p, end)) | set(_split_at(p, start))
    pieces = paragraph_pieces(p)
    inside = [el for el, s, e, kind in pieces if kind == "r" and start <= s and e <= end and e > s]
    before = [el for el, s, e, kind in pieces if kind == "r" and e <= start]
    after = [el for el, s, e, kind in pieces if kind == "r" and s >= end]
    base = inside[0] if inside else before[-1] if before else after[0] if after else None
    if base is None:  # an empty paragraph: a run with the paragraph's end formatting
        base = etree.Element(q("a:r"))
        end_rpr = p.find(q("a:endParaRPr"))
        rpr = deepcopy(end_rpr) if end_rpr is not None else etree.Element(q("a:rPr"))
        rpr.tag = q("a:rPr")
        base.append(rpr)
        etree.SubElement(base, q("a:t"))
    made = []
    for words, marks in parts:
        if words:
            run = deepcopy(base)
            run.find(q("a:t")).text = words
            if marks or explicit:
                _set_marks(run, marks, explicit)
            made.append(run)
    if inside:
        for run in made:
            inside[0].addprevious(run)
    elif before:
        for run in reversed(made):
            before[-1].addnext(run)
    elif after:
        for run in made:
            after[0].addprevious(run)
    else:
        end_rpr = p.find(q("a:endParaRPr"))
        for run in made:
            end_rpr.addprevious(run) if end_rpr is not None else p.append(run)
    merged = len({rpr_digest(el.find(q("a:rPr"))) for el in inside}) > 1
    new_chars = [(ch, rpr_digest(run.find(q("a:rPr")))) for run in made for ch in run.find(q("a:t")).text]
    for el in inside:
        p.remove(el)
    _merge(p, touched | set(made))
    return new_chars, merged


def replace_all_text(body, text):
    """A text box's whole text -> new lines, each a paragraph with the first paragraph's and first run's formatting."""
    paragraphs = body.findall(q("a:p"))
    first = paragraphs[0]
    ppr = first.find(q("a:pPr"))
    run = first.find(q("a:r"))
    rpr = run.find(q("a:rPr")) if run is not None else None
    end = first.find(q("a:endParaRPr"))
    for p in paragraphs:
        body.remove(p)
    for line in text.split("\n"):
        p = etree.SubElement(body, q("a:p"))
        if ppr is not None:
            p.append(deepcopy(ppr))
        parts, explicit = segments(line)
        for words, marks in parts:
            r = etree.SubElement(p, q("a:r"))
            if rpr is not None:
                r.append(deepcopy(rpr))
            etree.SubElement(r, q("a:t")).text = words
            if marks or explicit:
                _set_marks(r, marks, explicit)
        if end is not None:
            p.append(deepcopy(end))


# ---- edit_deck: slides -----------------------------------------------------------------------------------------------
def all_layouts(prs):
    return [layout for master in prs.slide_masters for layout in master.slide_layouts]


def find_layout(prs, wanted, fallback=None):
    """A layout named by the LLM, matched forgivingly: exact, all its words, the closest spelling. None if none."""
    layouts = all_layouts(prs)
    norm = lambda s: " ".join(str(s).lower().split())
    names = [norm(layout.name) for layout in layouts]
    if wanted:
        words = norm(wanted).split()
        for name in ([n for n in names if n == norm(wanted)] + [n for n in names if all(w in n for w in words)]
                     + difflib.get_close_matches(norm(wanted), names, n=1, cutoff=0.6)):
            return layouts[names.index(name)]
        return None
    return fallback


def _body_placeholder(slide):
    return next((ph for ph in slide.placeholders if ph.placeholder_format.type in (2, 7) and ph.has_text_frame
                 and ph.placeholder_format.idx != 0), None)  # BODY, OBJECT


def _run_sizes(slide):
    """The text sizes a slide's body uses, by level, where its runs set them: a new slide copies its neighbour's."""
    body = _body_placeholder(slide) if slide is not None else None
    sizes = {}
    for p in (body.text_frame.paragraphs if body is not None else []):
        for r in p.runs:
            if r.font.size is not None:
                sizes.setdefault(p.level, r.font.size)
    return sizes


def copy_rels(element, src_part, dst_part, cache, same_package):
    """Every r:id in copied XML names a relationship of the SOURCE part: give the new part its own copies of the
    pictures, media and charts (never shared), the same external links, and rewrite the ids."""
    from .presentation import _clone_part
    lost = []
    for el in element.iter():
        if not isinstance(el.tag, str):
            continue
        for attr, rid in list(el.attrib.items()):
            if not attr.startswith(f"{{{R_NS}}}") or not rid:
                continue
            if rid not in src_part.rels:
                del el.attrib[attr]
                continue
            rel_ = src_part.rels[rid]
            if rel_.is_external:
                el.set(attr, dst_part.relate_to(rel_.target_ref, rel_.reltype, is_external=True))
                continue
            kind = rel_.reltype.rsplit("/", 1)[-1]
            if kind == "slide":  # a link to another slide: the same slide in this deck, none from another deck
                if same_package:
                    el.set(attr, dst_part.relate_to(rel_.target_part, rel_.reltype))
                else:
                    del el.attrib[attr]
                    lost.append("a link to another slide of the other deck")
                continue
            if kind in ("slideLayout", "slideMaster", "notesSlide", "notesMaster"):
                del el.attrib[attr]
                continue
            el.set(attr, dst_part.relate_to(_clone_part(rel_.target_part, dst_part.package, cache), rel_.reltype))
    return lost


def copy_slide(src, prs, layout, same_package):
    """A slide copied whole into `prs` on `layout`: its shapes, background, transition and animations, notes, and
    its own copies of pictures, media and charts. -> (the new slide, what could not come along)"""
    from .presentation import _Unsupported
    new = prs.slides.add_slide(layout)
    src_el, dst_el = src._element, new._element
    tree = dst_el.find("./p:cSld/p:spTree", NS)
    for child in list(tree):
        tree.remove(child)
    for child in src_el.find("./p:cSld/p:spTree", NS):
        tree.append(deepcopy(child))
    src_csld, dst_csld = src_el.find(q("p:cSld")), dst_el.find(q("p:cSld"))
    for key, value in src_csld.attrib.items():
        dst_csld.set(key, value)
    for child in list(dst_csld):
        if child is not tree:
            dst_csld.remove(child)
    for child in src_csld:
        if local(child) != "spTree":
            copy = deepcopy(child)
            tree.addprevious(copy) if local(child) == "bg" else dst_csld.append(copy)
    for key in ("show", "showMasterSp", "showMasterPhAnim"):
        if src_el.get(key) is not None:
            dst_el.set(key, src_el.get(key))
    for child in list(dst_el):
        if local(child) != "cSld":
            dst_el.remove(child)
    for child in src_el:
        if local(child) != "cSld":
            dst_el.append(deepcopy(child))
    for created in dst_el.findall(".//p14:creationId", NS):  # PowerPoint gives every slide its own
        seed = f"{new.slide_id}:{created.get('val')}".encode()
        created.set("val", str(int(hashlib.sha256(seed).hexdigest()[:8], 16)))
    cache = {}
    try:
        lost = copy_rels(dst_el, src.part, new.part, cache, same_package)
    except _Unsupported as exc:
        raise OpFailed(f"slide {src.slide_id} has files that could not be copied: {exc}") from exc
    if src.has_notes_slide and src.notes_slide.notes_placeholder is not None:
        holder = new.notes_slide.notes_placeholder
        if holder is not None:
            body = deepcopy(src.notes_slide.notes_placeholder._element.find(q("p:txBody")))
            holder._element.replace(holder._element.find(q("p:txBody")), body)
            lost += copy_rels(body, src.notes_slide.part, new.notes_slide.part, cache, same_package)
    return new, lost


def _remove_empty_placeholders(slide):
    for ph in list(slide.placeholders):  # an empty placeholder shows 'Click to add text' in PowerPoint
        if ph._element.tag.endswith("}sp") and ph.has_text_frame and not ph.text_frame.text.strip():
            ph._element.getparent().remove(ph._element)


class Batch:
    """The state of one APPLY: the slide order as ids, the slides deleted, and what each op made."""

    def __init__(self, prs):
        self.prs = prs
        self.order = [s.slide_id for s in prs.slides]
        self.original = list(self.order)
        self.deleted, self.prev, self.chain, self.moved, self.new = set(), {}, {}, set(), []

    def anchor(self, after_id):
        """Where 'after slide X' points now: X, or the slide before X if an earlier op deleted it, and after the
        slides an earlier op already put there (so ops after the same slide keep the order they were given in)."""
        anchor = self.chain.get(after_id, after_id)
        while anchor and anchor in self.deleted:
            anchor = self.prev.get(anchor, 0)
        return anchor

    def place(self, sid, after_id):
        anchor = self.anchor(after_id)
        if anchor == sid:  # moved twice after the same slide: the second move puts it right after that slide
            anchor = after_id
        if sid in self.order:
            self.order.remove(sid)
        self.order.insert(self.order.index(anchor) + 1 if anchor else 0, sid)
        self.chain[after_id] = sid

    def delete(self, sid):
        at = self.order.index(sid)
        self.prev[sid] = self.order[at - 1] if at else 0
        self.order.remove(sid)
        self.deleted.add(sid)


def apply_text(batch, plan):
    slide = batch.prs.slides.get(plan["slide_id"])
    targets = text_targets(slide._element)
    target = next(t for t in targets if t["id"] == plan["shape_id"] and t["cell"] == plan["cell"])
    if not plan.get("find"):
        before = body_text_of(target["body"])
        replace_all_text(target["body"], plan["replace"])
        plan["expect"] = {"whole": plan["replace"], "before": before}
        return
    p = target["body"].findall(q("a:p"))[plan["para"]]
    chars = char_formats(p)
    start, end = plan["start"], plan["end"]
    text = paragraph_text(p)
    if text[start:end] != plan["found"]:  # an earlier op in this batch changed the paragraph: find the words again
        spots = [m.start() for m in re.finditer(re.escape(plan["found"]), text)]
        if len(spots) != 1:
            raise OpFailed(f"after the earlier ops, \"{plan['found']}\" occurs {len(spots)} times in its paragraph")
        start, end = spots[0], spots[0] + len(plan["found"])
    old = "".join(ch for ch, _ in chars[start:end])
    parts, explicit = segments(plan["replace"])
    if not explicit:  # only the words that differ change: "12 tests" -> "14 tests" replaces "12" alone
        lead, tail = same_words(old, plan["replace"])
        new = plan["replace"]
        start, end, parts = start + lead, end - tail, [(new[lead:len(new) - tail], {})]
    new_chars, merged = replace_range(p, start, end, parts, explicit)
    plan["expect"] = {"chars": chars[:start] + new_chars + chars[end:], "cell": plan["cell"]}
    if merged:
        plan["note"] = "the words had several formats; the new words took the first one's"


def same_words(old, new):
    """How many characters at the start and at the end of old and new are the same whole words (and spaces)."""
    a, b = re.findall(r"\S+|\s+", old), re.findall(r"\S+|\s+", new)
    lead = 0
    while lead < min(len(a), len(b)) and a[lead] == b[lead]:
        lead += 1
    tail = 0
    while tail < min(len(a), len(b)) - lead and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    return sum(map(len, a[:lead])), sum(map(len, a[len(a) - tail:])) if tail else 0


def body_text_of(body):
    return "\n".join(paragraph_text(p) for p in body.findall(q("a:p")))


def apply_notes(batch, plan):
    slide = batch.prs.slides.get(plan["slide_id"])
    frame = slide.notes_slide.notes_text_frame
    if frame is None:
        raise OpFailed("this slide's notes page has no text box for notes")
    old = frame.text.strip()
    if plan["mode"] == "replace" or not old:
        frame.text = plan["text"]
        plan["expect"] = plan["text"]
    else:
        for line in plan["text"].split("\n"):
            frame.add_paragraph().text = line
        plan["expect"] = old + "\n" + plan["text"]


def apply_insert(batch, plan):
    from .presentation import _put_text
    prs = batch.prs
    neighbour = prs.slides.get(plan["after_id"]) if plan["after_id"] else None
    layout = find_layout(prs, plan.get("layout")) if plan.get("layout") else None
    if layout is None:
        layout = default_layout(prs, neighbour)
    slide = prs.slides.add_slide(layout)
    if slide.shapes.title is not None and plan.get("title"):
        slide.shapes.title.text_frame.text = strip_marks(plan["title"])
    if plan.get("bullets"):
        body = _body_placeholder(slide)
        if body is None:
            raise OpFailed(f"layout \"{layout.name}\" has no text box for bullets")
        paragraphs = []
        for line in plan["bullets"]:
            level = (len(line) - len(line.lstrip(" "))) // 2
            parts, _ = segments(re.sub(r"^\s*[-*•]\s+", "", line).strip())
            paragraphs.append({"level": min(level, 4), "bullet": True,
                               "runs": [(words, bool(m.get("b")), bool(m.get("i"))) for words, m in parts]})
        _put_text(body, paragraphs)
        sizes = _run_sizes(neighbour)  # the text sizes of the slide it follows, as a person copying it would
        for p in body.text_frame.paragraphs:
            for r in p.runs:
                if sizes:
                    r.font.size = sizes.get(p.level, sizes[min(sizes)])
    _remove_empty_placeholders(slide)
    batch.new.append(slide.slide_id)
    batch.place(slide.slide_id, plan["after_id"])
    plan["new_ids"] = [slide.slide_id]
    plan["layout_used"] = layout.name


def apply_move(batch, plan):
    batch.moved.add(plan["slide_id"])
    batch.place(plan["slide_id"], plan["after_id"])


def apply_delete(batch, plan):
    batch.delete(plan["slide_id"])


def apply_duplicate(batch, plan):
    src = batch.prs.slides.get(plan["slide_id"])
    new, lost = copy_slide(src, batch.prs, src.slide_layout, same_package=True)
    batch.new.append(new.slide_id)
    batch.place(new.slide_id, plan["after_id"])
    plan["new_ids"], plan["lost"] = [new.slide_id], lost


def apply_import(batch, plan):
    """Slides of another deck: restyled into this deck's layouts (convert_presentation's classify and fill_slide), or
    copied as drawn on the layout of the same name. Their pictures, media and charts are copied, never shared."""
    from .presentation import (SLIDE_NUMBER, classify, fill_slide, learn_from_samples, open_deck, pick_layouts,
                               read_slides, shrink_to_fit)
    prs = batch.prs
    source = open_deck(plan["from_file"])
    numbered = any(ph.placeholder_format.type == SLIDE_NUMBER for sid in batch.original
                   for ph in prs.slides.get(sid).placeholders)
    infos = read_slides(source) if plan.get("restyle", True) else None
    learned = learn_from_samples(prs) if infos else None
    neighbour = prs.slides.get(plan["after_id"]) if plan["after_id"] else None
    sizes = _run_sizes(neighbour) or next((found for sid in batch.original
                                           for found in [_run_sizes(prs.slides.get(sid))] if found), {})
    plan["new_ids"], plan["lost"], plan["how"] = [], [], []
    for n in plan["slides"]:
        if infos:
            info = infos[n - 1]
            kind, why = classify(info)
            layout = pick_layouts(prs, [kind], {})[kind][0]
            slide = prs.slides.add_slide(layout)
            said, lost = fill_slide(prs, slide, info, kind, layout, learned)
            if not numbered:  # the deck's own slides show no slide number: neither do these
                for ph in list(slide.placeholders):
                    if ph.placeholder_format.type == SLIDE_NUMBER:
                        ph._element.getparent().remove(ph._element)
            body = _body_placeholder(slide)
            if body is not None and sizes and kind in ("bullets", "picture_text", "section"):
                for p in body.text_frame.paragraphs:  # the text sizes this deck's own slides use
                    for r in p.runs:
                        if r.font.size is None:
                            r.font.size = sizes.get(p.level, sizes.get(min(sizes)))
            for shape in slide.shapes:
                number = shape.is_placeholder and shape.placeholder_format.type == SLIDE_NUMBER
                if shape.has_text_frame and not number:
                    shrink_to_fit(shape)
            plan["how"].append(f"slide {n}: {kind} on \"{layout.name}\" ({why})")
        else:
            src = source.slides[n - 1]
            layout = find_layout(prs, src.slide_layout.name) or prs.slide_layouts[0]
            slide, lost = copy_slide(src, prs, layout, same_package=False)
            plan["how"].append(f"slide {n}: kept as drawn on \"{layout.name}\"")
        plan["lost"] += [f"slide {n}: {item}" for item in lost]
        batch.new.append(slide.slide_id)
        batch.place(slide.slide_id, plan["after_id"])
        plan["new_ids"].append(slide.slide_id)


APPLY = {"text": apply_text, "notes": apply_notes, "insert": apply_insert, "move": apply_move, "delete": apply_delete,
         "duplicate": apply_duplicate, "import": apply_import}
P14 = NS["p14"]


def finish(batch):
    """The slide list in its new order; deleted slides, links to them and their places in sections and custom shows
    removed; the slide parts renamed slide1.xml, slide2.xml ... in order, as PowerPoint does."""
    prs = batch.prs
    lst = prs.slides._sldIdLst
    entries = {int(e.get("id")): e for e in lst.findall(q("p:sldId"))}
    gone_parts = {prs.part.related_part(entries[sid].get(f"{{{R_NS}}}id")) for sid in batch.deleted}
    for sid in batch.order:  # links from other slides to a deleted one
        part = prs.slides.get(sid).part
        for rid, rel_ in list(part.rels.items()):
            if not rel_.is_external and rel_.target_part in gone_parts:
                for el in part._element.iter():
                    for attr, value in list(el.attrib.items()) if isinstance(el.tag, str) else []:
                        if attr.startswith(f"{{{R_NS}}}") and value == rid:
                            del el.attrib[attr]
                part.rels.pop(rid)
    pres = prs.part._element
    for sld in pres.findall("./p:custShowLst/p:custShow/p:sldLst/p:sld", NS):
        if prs.part.related_part(sld.get(f"{{{R_NS}}}id")) in gone_parts:
            sld.getparent().remove(sld)
    for e in list(entries.values()):
        lst.remove(e)
    for sid in batch.order:
        lst.append(entries[sid])
    for sid in batch.deleted:
        prs.part.drop_rel(entries[sid].get(f"{{{R_NS}}}id"))
    sections = pres.findall(f".//{{{P14}}}sectionLst/{{{P14}}}section")
    if sections:  # a slide keeps its section; a new or moved one joins the section of the slide before it
        section_of = {int(e.get("id")): k for k, s in enumerate(sections) for e in s.iter(f"{{{P14}}}sldId")}
        final, previous = {}, None
        for sid in batch.order:
            keep = sid in section_of and sid not in batch.moved
            final[sid] = section_of[sid] if keep else (final[previous] if previous is not None else 0)
            previous = sid
        for k, section in enumerate(sections):
            ids = section.find(f"{{{P14}}}sldIdLst")
            if ids is None:
                ids = etree.SubElement(section, f"{{{P14}}}sldIdLst")
            for e in list(ids):
                ids.remove(e)
            for sid in batch.order:
                if final[sid] == k:
                    etree.SubElement(ids, f"{{{P14}}}sldId").set("id", str(sid))
    prs.part.rename_slide_parts([e.get(f"{{{R_NS}}}id") for e in lst.findall(q("p:sldId"))])


def apply_ops(source, out, plans):
    """APPLY OPS: every planned op on a fresh copy of `source`, saved as `out`. -> the final slide order (ids)"""
    from pptx import Presentation
    batch = Batch(Presentation(source))
    for plan in plans:
        if plan["status"] not in ("planned", "done"):
            continue
        try:
            APPLY[plan["op"]](batch, plan)
            plan["status"] = "done"
        except OpFailed as exc:
            plan["status"], plan["why"] = "failed", str(exc)
        except Exception as exc:  # python-pptx on an odd slide: this op fails, VERIFY catches anything half done
            plan["status"], plan["why"] = "failed", f"{type(exc).__name__}: {exc}"[:300]
    finish(batch)
    batch.prs.save(out)
    return batch.order, batch


# ---- edit_deck: CHECK OPS, before anything is applied ----------------------------------------------------------------
def plan_ops(ops, view, source, ctx):
    """Each op -> a plan: slide numbers turned into slide ids, words found once, layouts and decks found; or the
    reason it is refused. Approval questions are attached, not asked."""
    from pptx import Presentation
    prs = Presentation(source)
    ids = [s["id"] for s in view["slides"]]
    titles = {s["id"]: s["title"] for s in view["slides"]}
    count = len(ids)
    deleting = sum(op["op"] == "delete" for op in ops)
    plans = []
    for n, op in enumerate(ops, 1):
        plan = {**op, "n": n, "desc": describe(op), "status": "planned", "why": "", "ask": None}
        plans.append(plan)
        try:
            check_op(plan, prs, ids, titles, count, ctx)
        except OpFailed as exc:
            plan["status"], plan["why"] = "refused", str(exc)
    if deleting and deleting >= count:
        for plan in plans:
            if plan["op"] == "delete":
                plan["status"], plan["why"] = "refused", "a deck needs at least one slide"
    deleted = {p["slide_id"]: p["n"] for p in plans if p["op"] == "delete" and p["status"] == "planned"}
    for plan in plans:  # editing, moving or copying a slide that another op deletes
        if plan["op"] != "delete" and plan["status"] == "planned" and plan.get("slide_id") in deleted:
            plan["status"], plan["why"] = "refused", f"op {deleted[plan['slide_id']]} deletes that slide"
    return plans


def _slide_id(value, ids, what="slide"):
    n = number(value)
    if n is None:
        raise OpFailed(f"which {what}? give its number (1 to {len(ids)})")
    if not 1 <= n <= len(ids):
        raise OpFailed(f"there is no slide {n}: the deck has {len(ids)} slides")
    return ids[n - 1]


def _after_id(value, ids, default=None):
    n = default if value is None else number(value)
    if n is None:
        raise OpFailed("after which slide? give its number (0 for the start)")
    if not 0 <= n <= len(ids):
        raise OpFailed(f"there is no slide {n}: the deck has {len(ids)} slides")
    return ids[n - 1] if n else 0


def check_op(plan, prs, ids, titles, count, ctx):
    kind = plan["op"]
    if kind == "unreadable":
        raise OpFailed(plan["error"])
    if kind == "text":
        plan["slide_id"] = _slide_id(plan.get("slide"), ids)
        slide = prs.slides.get(plan["slide_id"])
        if not plan.get("find"):
            if not plan["replace"].strip():
                raise OpFailed("give find (the words to change) and replace (the new words)")
            targets = [t for t in text_targets(slide._element) if t["cell"] is None
                       and shape_matches(t, plan.get("shape") or "body")]
            if len(targets) != 1:
                raise OpFailed(f"which text box? name it with shape (\"title\", \"body\" or a name); slide "
                               f"{plan['slide']} has: {', '.join(t['name'] for t in text_targets(slide._element))}")
            target = targets[0]
            plan.update(shape_id=target["id"], cell=None, shape_name=target["name"])
            if target["ph"] not in ("title", "ctrTitle"):
                plan["ask"] = (f"Replace ALL the text of \"{target['name']}\" on slide {plan['slide']} "
                               f"(\"{titles[plan['slide_id']]}\") with \"{_clip(plan['replace'], 80)}\"? "
                               "(the result is a new copy; your deck is not changed)")
            return
        hits, repaired, note = find_words(slide._element, plan["find"], plan.get("shape"))
        if not hits:
            raise OpFailed(f"\"{plan['find']}\" is not on slide {plan['slide']} (\"{titles[plan['slide_id']]}\"): "
                           "copy the words exactly as read_deck shows them")
        if len(hits) > 1:
            where = ", ".join(dict.fromkeys(h["target"]["name"] for h in hits))
            raise OpFailed(f"\"{plan['find']}\" occurs {len(hits)} times on slide {plan['slide']} (in {where}): "
                           "quote more of the sentence so it is unique")
        hit = hits[0]
        p = hit["target"]["body"].findall(q("a:p"))[hit["para"]]
        if any(kind == "math" for *_, kind in paragraph_pieces(p)):
            raise OpFailed("that paragraph holds an equation, which edits leave alone")
        plan.update(shape_id=hit["target"]["id"], cell=hit["target"]["cell"], shape_name=hit["target"]["name"],
                    para=hit["para"], start=hit["start"], end=hit["end"],
                    found=paragraph_text(p)[hit["start"]:hit["end"]])
        if note:
            plan["note"] = note
        if repaired:
            plan["ask"] = (f"On slide {plan['slide']}, \"{plan['find']}\" is not there exactly; the harness found "
                           f"\"{plan['found']}\". Replace that with \"{plan['replace']}\"?")
    elif kind == "notes":
        plan["slide_id"] = _slide_id(plan.get("slide"), ids)
        if not plan["text"].strip():
            raise OpFailed("the notes have no text: give text")
    elif kind == "insert":
        plan["after_id"] = _after_id(plan.get("after"), ids, default=count)
        if plan.get("layout") and find_layout(prs, plan["layout"]) is None:
            names = ", ".join(dict.fromkeys(layout.name for layout in all_layouts(prs)))
            raise OpFailed(f"there is no layout called \"{plan['layout']}\". The layouts are: {names}")
        if not (plan.get("title") or plan.get("bullets")):
            raise OpFailed("a new slide needs a title or bullets")
    elif kind in ("move", "duplicate"):
        plan["slide_id"] = _slide_id(plan.get("slide"), ids)
        plan["after_id"] = _after_id(plan.get("after"), ids, default=number(plan.get("slide")) if kind == "duplicate"
                                     else None)
        if kind == "move" and plan["after_id"] == plan["slide_id"]:
            raise OpFailed("a slide can't move after itself")
    elif kind == "delete":
        plan["slide_id"] = _slide_id(plan.get("slide"), ids)
        plan["ask"] = (f"Delete slide {plan['slide']} (\"{titles[plan['slide_id']]}\")? The result is a new copy "
                       "of the deck; your deck is not changed.")
    elif kind == "import":
        if not plan.get("from"):
            raise OpFailed("import from which deck? give from, e.g. workspace/deck_demo/results.pptx")
        try:
            _, plan["from_file"] = deck_file(plan["from"], ctx)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            raise OpFailed(str(exc)) from exc
        from pptx import Presentation
        available = len(Presentation(plan["from_file"]).slides)
        plan["slides"] = plan.get("slides") or list(range(1, available + 1))
        bad = [n for n in plan["slides"] if not 1 <= n <= available]
        if bad:
            raise OpFailed(f"{Path(plan['from']).name} has {available} slides, so there is no slide "
                           f"{', '.join(map(str, bad))}")
        plan["after_id"] = _after_id(plan.get("after"), ids, default=count)


def default_layout(prs, neighbour):
    """A new slide's layout when the LLM names none: its neighbour's, if that one has a title and a body; else the
    template's bullets layout, as convert_presentation picks it."""
    from .presentation import DEFAULT_LAYOUTS
    if neighbour is not None and neighbour.shapes.title is not None and _body_placeholder(neighbour) is not None:
        return neighbour.slide_layout
    for name in DEFAULT_LAYOUTS["bullets"]:
        found = find_layout(prs, name)
        if found is not None and found.name.lower() == name.lower():
            return found
    return all_layouts(prs)[min(1, len(all_layouts(prs)) - 1)]


# ---- edit_deck: VERIFY -----------------------------------------------------------------------------------------------
SHAREABLE = {"image", "hyperlink", "slideLayout", "notesSlide", "slide", "tags", "vmlDrawing"}


def package_problems(path):
    """The package is whole: every relationship target exists, no part is orphaned, every part has a content type,
    every slide part is in the slide list. -> [problem]"""
    pkg = Package(path)
    problems = []
    rels = lambda name: name.endswith(".rels") and "/_rels/" in name
    for name in pkg.blobs:
        if name != "/[Content_Types].xml" and not rels(name) and not pkg.content_type(name):
            problems.append(f"{name} has no content type")
    reachable = pkg.reachable()
    for name in ["/"] + sorted(reachable):
        for rid, r in pkg.rels(name).items():
            if not r["external"] and r["target"] not in pkg.blobs:
                problems.append(f"{name} {rid} points to {r['target']}, which is not in the package")
    for name in pkg.blobs:
        if name != "/[Content_Types].xml" and not rels(name) and name not in reachable:
            problems.append(f"{name} is in the package but nothing points to it")
    listed = {part for _, part in slide_list(pkg)}
    for name in reachable:
        if (pkg.content_type(name) or "").endswith("presentationml.slide+xml") and name not in listed:
            problems.append(f"{name} is a slide that is not in the slide list")
    return problems


def shared_parts(path, slide_ids):
    """Charts, media and embedded files a new slide shares with another slide -> [problem]. (Pictures may be shared:
    PowerPoint does it too, and nobody edits a picture's bytes.)"""
    pkg = Package(path)
    users = {}
    for sid, part in slide_list(pkg):
        for r in pkg.rels(part).values() if part else []:
            if not r["external"] and r["type"] not in SHAREABLE:
                users.setdefault(r["target"], set()).add(sid)
    return [f"slide id {sid} shares {target} with slide id(s) {', '.join(map(str, sorted(who - {sid})))}"
            for target, who in users.items() for sid in slide_ids if sid in who and len(who) > 1]


def verify(before, out, plans, order, batch, source_problems):
    """
    VERIFY (P5): -> (the new view, {op number: why it failed}, [problems not tied to one op])
      1. untouched slides: every slide no op changed keeps its fingerprint (and with it, byte-identical media)
      2. each op shows in the new view: the words and the formatting around them, the notes, the order, new slides
      3. the package is whole, and new slides share no chart or media with another slide
    """
    after = deck_view(out)
    old = {s["id"]: s for s in before["slides"]}
    new = {s["id"]: s for s in after["slides"]}
    failed, problems = {}, []
    changed = {p["slide_id"] for p in plans if p["status"] == "done" and p["op"] in ("text", "notes")}
    for sid, slide in old.items():
        if sid in batch.deleted or sid in changed:
            continue
        if sid not in new:
            problems.append(f"slide \"{slide['title']}\" (id {sid}) is missing, but no op deleted it")
        elif new[sid]["fingerprint"] != slide["fingerprint"]:
            problems.append(f"slide \"{slide['title']}\" (id {sid}) changed, but no op named it")
    if [s["id"] for s in after["slides"]] != order:
        problems.append("the slides are not in the order the ops asked for")
    done = [p for p in plans if p["status"] == "done"]
    place = lambda p: (p["op"], p.get("slide_id"), p.get("shape_id"), p.get("cell"), p.get("para"))
    last = {place(p): p["n"] for p in done if p["op"] in ("text", "notes")}
    for plan in done:
        if plan["op"] in ("text", "notes") and last[place(plan)] != plan["n"]:
            # a later op changed the same paragraph (or notes): this one's words must still be there
            slide = new.get(plan["slide_id"])
            shape = _shape_of(slide, plan) if slide and plan["op"] == "text" else None
            there = slide["notes"] if plan["op"] == "notes" and slide else \
                body_text(shape.get("paragraphs", [])) if shape else ""
            wanted = strip_marks(plan["replace"] if plan["op"] == "text" else plan["text"])
            if " ".join(wanted.split()) not in " ".join(there.split()):
                failed[plan["n"]] = "its words are not in the new deck"
            continue
        why = check_visible(plan, old, new)
        if why:
            failed[plan["n"]] = why
    problems += [p for p in package_problems(out) if p not in source_problems]
    problems += shared_parts(out, batch.new)
    return after, failed, problems


def _shape_of(slide, plan):
    return next((s for s in slide["shapes"] if s["id"] == plan["shape_id"] and s["name"] == plan["shape_name"]), None)


def check_visible(plan, old, new):
    """Does this op show in the new view? -> None, or why not."""
    kind = plan["op"]
    if kind == "text":
        slide = new.get(plan["slide_id"])
        shape = _shape_of(slide, plan) if slide else None
        expect = plan.get("expect", {})
        if shape is None:
            return "the shape is gone from the new deck"
        if "whole" in expect:
            return None if body_text(shape.get("paragraphs", [])) == strip_marks(expect["whole"]).strip() else \
                "the text box does not read as asked"
        if plan.get("cell"):
            r, c = plan["cell"]
            cell = (shape.get("table") or [[]] * r)[r - 1][c - 1] if shape.get("table") else None
            wanted = "".join(ch for ch, _ in expect["chars"])
            return None if cell is not None and wanted in cell else "the table cell does not read as asked"
        paragraphs = shape.get("paragraphs", [])
        if plan["para"] >= len(paragraphs):
            return "the paragraph is gone"
        chars = [(ch, r["fmt"]) for r in paragraphs[plan["para"]]["runs"] for ch in
                 (r["t"] if r["fmt"] not in ("br", "math") else r["t"][:1])]
        if [ch for ch, _ in chars] != [ch for ch, _ in expect["chars"]]:
            return "the paragraph does not read as asked"
        if chars != expect["chars"]:
            return "the formatting of words outside the edit changed"
        return None
    if kind == "notes":
        slide = new.get(plan["slide_id"])
        return None if slide and " ".join(slide["notes"].split()) == " ".join(plan["expect"].split()) else \
            "the notes do not read as asked"
    if kind == "insert":
        slide = new.get(plan["new_ids"][0])
        if slide is None:
            return "the new slide is not in the deck"
        if plan.get("title") and slide["title"] != strip_marks(plan["title"]).strip():
            return "the new slide's title is not the one asked for"
        return None
    if kind == "duplicate":  # the copy looks like its slide as it was then: before this batch, or after an earlier op
        slide, source = new.get(plan["new_ids"][0]), new.get(plan["slide_id"], {})
        return None if slide and slide["fingerprint"] in (old[plan["slide_id"]]["fingerprint"],
                                                          source.get("fingerprint")) else \
            "the copy does not look like the slide it copies"
    if kind == "import":
        missing = [sid for sid in plan["new_ids"] if sid not in new]
        return "an imported slide is not in the deck" if missing else None
    return None  # move and delete: the order check covers them


# ---- check_deck ------------------------------------------------------------------------------------------------------
def edits_of(deck, ctx):
    """What the last edit_deck did to this deck: this request's (ctx.facts), else the version ledger's record."""
    found = ctx.facts.get("deck_edits", {}).get(str(Path(deck).resolve()))
    if found:
        return found
    try:
        ledger = versions.Versions.of(deck, ctx)
        entry = ledger.find(versions.sha256(deck))
    except (OSError, ValueError):
        return None
    if not entry or entry.get("tool") != "edit_deck":
        return None
    record = next((c for c in entry.get("changes", []) if isinstance(c, dict) and c.get("op") == "order"), None)
    return record


# ---- the tools -------------------------------------------------------------------------------------------------------
DECK_PATH = {"type": "string", "description": "The .pptx deck, e.g. workspace/deck_demo/briefing.pptx"}


class ReadDeckTool(Tool):
    name = "read_deck"
    description = ("Read a PowerPoint deck (.pptx) slide by slide: each slide's number, layout, title, text boxes with "
                   "their bold words, pictures, tables and speaker notes. Give slides (e.g. '3' or '2-4') to see those "
                   "slides in full. Use it before editing a deck.")
    parameters = {"type": "object",
                  "properties": {"path": DECK_PATH,
                                 "slides": {"type": "string", "description": "Optional: the slides to show in full, "
                                                                             "e.g. '3' or '2-4'"}},
                  "required": ["path"]}
    model_id = "lxml (deck_model.py, no ML)"
    check_unused, repeatable = False, True
    how_it_works = ("No model here: lxml reads the deck's XML (a .pptx is a ZIP of XML files): every slide, every "
                    "shape inside groups too, every run with its bold and italic, the notes. The LLM gets one line per "
                    "slide; deck.json and deck.md in the run folder have all of it.")

    def run(self, ctx, path, slides=None):
        original, deck = deck_file(path, ctx)
        ctx.tracer.say(self.how_it_works, kind="python")
        ledger, entry, what = versions.track(deck, ctx)
        with ctx.tracer.stage("DECK VIEW", "python", self.model_id, ".pptx file", "slides, shapes, runs, notes") as ev:
            view = deck_view(deck)
            data, md = output_path(ctx, original, "deck", ".json"), output_path(ctx, original, "deck", ".md")
            write_json(data, view)
            md.write_text(deck_markdown(view), encoding="utf-8")
            shapes = sum(len(s["shapes"]) for s in view["slides"])
            ev.summary = (f"{rel(deck)}: {view['slide_count']} slides, {shapes} shapes, notes on "
                          f"{sum(bool(s['notes']) for s in view['slides'])}")
            ev.artifacts += [str(data), str(md)]
        lines = [slide_line(s) for s in view["slides"]]
        if len(lines) > 30:
            lines = lines[:25] + [f"... and {len(lines) - 25} more slides (see {rel(md)})"]
        wanted = [n for n in numbers(slides) if 1 <= n <= view["slide_count"]][:4]
        observations = {"deck": rel(original), "slide_count": view["slide_count"], "slides": lines,
                        "version": f"v{entry['n']} ({entry['origin']}) in the version ledger", "full_view": rel(md)}
        if wanted:  # one string, so the TOOL OUTPUT GUARD can read it paragraph by paragraph like a file's text
            observations["detail"] = "\n".join(f"slide {n}:\n" + "\n".join(f"  {line}" for line in
                                                                          slide_detail(view["slides"][n - 1]))
                                               for n in wanted)
        if what == "user":
            observations["edited_since"] = ("you edited this deck since the harness's last version: diff_decks shows "
                                            "what changed")
        return {"artifacts": [{"type": "deck_view", "path": md, "note": "every slide, shape and note"}],
                "observations": observations}


class DiffDecksTool(Tool):
    name = "diff_decks"
    description = ("Compare two versions of a PowerPoint deck and list what changed: slides moved, added or deleted, "
                   "and on each slide reworded or re-formatted text, notes, pictures, tables and box sizes. old is the "
                   "earlier version, new the edited one. With one deck, the version ledger finds the other.")
    parameters = {"type": "object",
                  "properties": {"old": {"type": "string", "description": "The earlier version, e.g. "
                                                                          "workspace/deck_demo/briefing.pptx"},
                                 "new": {"type": "string", "description": "The edited version, e.g. "
                                                                          "workspace/deck_demo/briefing_edited.pptx"}},
                  "required": []}
    model_id = "lxml + difflib (no ML)"
    repeatable = True  # offered only when the user asks what changed: the completion check asks if it went unused
    how_it_works = ("No model here: both decks are read in full; slides are matched by their words and picture "
                    "hashes, keeping their order where possible (a longest common subsequence), never by part names, "
                    "which PowerPoint renumbers on every save. Then each matched pair is compared shape by shape.")

    def check_request(self, args, ctx):
        """CHECK ARGS: which deck is old and which new follows from the user's words ('what did I change in A
        compared with B': A is new). A small model swaps them easily. -> (args, summary, note or None)"""
        request = ctx.request or ""
        decks = [p for p in named_paths(request) if Path(p).suffix.lower() == ".pptx"]
        if len(decks) != 2:
            return args, "the request does not name two decks: the LLM's old and new stand", None
        a, b = decks
        between = re.search(rf"\b(?:from|between)\s+\S*{re.escape(Path(a).name)}\S*\s+(?:to|and)\b", request, re.I)
        old, new = (a, b) if between else (b, a)
        same = lambda x, y: x and y and Path(str(x).replace("\\", "/")).name.lower() == Path(str(y).replace(
            "\\", "/")).name.lower()
        if same(args.get("old"), old) and same(args.get("new"), new):
            return args, f"old {old}, new {new}: as the user's words say", None
        wanted = {**args, "old": old, "new": new}
        swapped = same(args.get("old"), new) and same(args.get("new"), old)
        note = (f"The user's words make {new} the edited version and {old} the earlier one"
                + ("; your call had them swapped, so the harness put them back." if swapped else
                   ", so the harness used those."))
        return wanted, ("old and new SWAPPED -> put back" if swapped else "old/new set from the user's words"), note

    def run(self, ctx, old=None, new=None):
        t = ctx.tracer
        t.say(self.how_it_works, kind="python")
        if not old and not new:
            raise ValueError("name the decks to compare: old (the earlier version) and new (the edited one)")
        how = "the two decks named"
        if old and new:
            _, old_deck = deck_file(old, ctx, edited=False)
            new_original, new_deck = deck_file(new, ctx, edited=False)
            if old_deck.resolve() == new_deck.resolve():
                raise ValueError("old and new are the same file: name the earlier version and the edited one")
            versions.track(old_deck, ctx)
            versions.track(new_deck, ctx)
        else:
            new_original, one = deck_file(old or new, ctx, edited=False)
            old_deck, new_deck, how = ledger_pair(one, ctx)
        with t.stage("DECK VIEW", "python", "lxml (deck_model.py, no ML)", "two .pptx files", "two views") as ev:
            before, after = deck_view(old_deck), deck_view(new_deck)
            ev.summary = f"{rel(old_deck)}: {before['slide_count']} slides; {rel(new_deck)}: {after['slide_count']}"
        with t.stage("ALIGN + COMPARE", "python", "word and picture similarity + LCS, then difflib (no ML)",
                     "two views", "moved, added, deleted, changed") as ev:
            diff = diff_views(before, after)
            for item in diff["items"]:
                t.say(item["text"], kind="python")
            ev.summary = diff["summary"]
            ev.details = {"items": diff["items"]}
        report = output_path(ctx, new_original, "deck_diff", ".md")
        report.write_text(diff_markdown(diff, rel(old_deck), rel(new_deck)), encoding="utf-8")
        ctx.notes.append(f"diff_decks {rel(old_deck)} -> {rel(new_deck)}: {diff['summary']}. Every change is in "
                         f"{rel(report)}.")
        observations = {"old": rel(old_deck), "new": rel(new_deck), "compared": how, "summary": diff["summary"],
                        "moved": diff["moved"] or "none", "deleted": diff["deleted"] or "none",
                        "added": diff["added"] or "none", "changed": diff["changed"][:MAX_LINES] or "none",
                        "report": rel(report)}
        if len(diff["changed"]) > MAX_LINES:
            observations["more"] = f"{len(diff['changed']) - MAX_LINES} more change(s) in {rel(report)}"
        return {"artifacts": [{"type": "deck_diff", "path": report, "note": "every change, slide by slide"}],
                "observations": observations}


OPS_DESCRIPTION = (
    "The operations, in order; slide numbers are as the deck is now. Each is one of: "
    "{\"op\": \"text\", \"slide\": 4, \"find\": \"12 tests\", \"replace\": \"14 tests\"} (formatting kept); "
    "{\"op\": \"notes\", \"slide\": 3, \"text\": \"...\"} (added to the speaker notes); "
    "{\"op\": \"insert\", \"after\": 2, \"title\": \"...\", \"bullets\": [\"...\"]}; "
    "{\"op\": \"move\", \"slide\": 5, \"after\": 2}; {\"op\": \"delete\", \"slide\": 7}; "
    "{\"op\": \"duplicate\", \"slide\": 4, \"after\": 4}; "
    "{\"op\": \"import\", \"from\": \"workspace/deck_demo/results.pptx\", \"slides\": \"2-3\", \"after\": 6}")


class EditDeckTool(Tool):
    name = "edit_deck"
    description = ("Edit a PowerPoint deck (.pptx), keeping everything else as it is: change words on a slide "
                   "(their formatting stays), add speaker notes, insert, move, delete or duplicate slides, or import "
                   "slides from another deck. Put every operation the user asked for in one call. The edited deck is "
                   "a new copy in the run folder; every slide not named is checked to be unchanged.")
    parameters = {"type": "object",
                  "properties": {"path": DECK_PATH,
                                 "ops": {"type": "array", "items": {"type": "object"}, "description": OPS_DESCRIPTION}},
                  "required": ["path", "ops"]}
    model_id = "python-pptx + lxml (no ML)"
    # not repeatable: the same ops again would apply to the edited copy (a second "move 5 after 2" moves another slide)
    how_it_works = ("No model here: Python reads the deck, turns each slide number into the slide's own id, applies "
                    "the operations to a copy, and then proves that every slide no operation named is unchanged: the "
                    "same fingerprint (a hash of its XML with pictures by content) before and after.")

    def validate(self, args):
        """ops is a list of objects, which Tool.validate would turn into strings. A 3B model sends a JSON string, one
        object, sentences such as 'move 5 after 2', or {"move": 5, "after": 2}: all are read here."""
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        path = args.get("path") or args.get("deck") or args.get("file")
        raw = args.get("ops", args.get("operations"))
        if not path:
            raise ValueError("missing required argument(s): path")
        if raw in (None, "", []):
            loose = {k: v for k, v in args.items() if k not in ("path", "deck", "file", "ops", "operations")}
            if not loose:
                raise ValueError("missing required argument(s): ops")
            raw = [loose]  # one op's fields sent at the top level: {"path": ..., "op": "move", "slide": 5, ...}
        ops = parse_ops(raw)
        if all(op["op"] == "unreadable" for op in ops):
            raise ValueError(ops[0]["error"])
        return {"path": str(path), "ops": ops}

    def check_request(self, args, ctx):
        """
        CHECK ARGS: the user's own words win. A quoted 'change "X" to "Y"' sets a text op's words; quoted notes set a
        notes op's text; 'move slide 5 after slide 2', 'delete slide 7', 'notes to slide 3', 'on slide 4' set the
        slide numbers; a delete the user never asked for is dropped. -> (args, summary, note or None)
        """
        request = ctx.request or ""
        ops = [dict(op) for op in args["ops"]]
        fixes = []

        def only(kind):
            found = [op for op in ops if op["op"] == kind]
            return found[0] if len(found) == 1 else None

        pairs = QUOTED_EDIT.findall(request)
        for op, (old, new) in zip([op for op in ops if op["op"] == "text"], pairs):
            if (op.get("find"), op.get("replace")) != (old, new):
                fixes.append(f"text \"{op.get('find')}\" -> \"{op.get('replace')}\" became \"{old}\" -> \"{new}\"")
                op["find"], op["replace"] = old, new
        quoted_notes = re.findall(r"notes?\b[^\"“]{0,80}?[:,]?\s*[\"“](.+?)[\"”]", request, re.I | re.S)
        op = only("notes")
        if op and len(quoted_notes) == 1 and " ".join(op["text"].split()) != " ".join(quoted_notes[0].split()):
            fixes.append("notes text set to the user's quoted words")
            op["text"] = quoted_notes[0]
        rules = [("move", r"\bmove\s+slide\s+(\d+)\s+(after|before)\s+slide\s+(\d+)"),
                 ("delete", r"\b(?:delete|remove)\s+slide\s+(\d+)\b"),
                 ("duplicate", r"\bduplicate\s+slide\s+(\d+)\b"),
                 ("notes", r"\bnotes?\s+(?:to|on|for|of)\s+slide\s+(\d+)"),
                 ("text", r"\bon\s+slide\s+(\d+)\b")]
        for kind, pattern in rules:
            found = re.findall(pattern, request, re.I)
            op = only(kind)
            if op is None or len(found) != 1 or (kind == "text" and not pairs):
                continue
            hit = found[0] if isinstance(found[0], tuple) else (found[0],)
            if int(hit[0]) != op.get("slide"):
                fixes.append(f"{kind}: slide {op.get('slide')} -> {hit[0]}")
                op["slide"] = int(hit[0])
            if kind == "move":
                after = int(hit[2]) - (hit[1].lower() == "before")
                if after != op.get("after"):
                    fixes.append(f"move: after slide {op.get('after')} -> {after}")
                    op["after"] = after
        m = re.search(r"\bimport\s+slides?\s+([\d\s,\-–]+(?:\s*(?:to|and)\s*\d+)?)\b.*?\bafter\s+slide\s+(\d+)",
                      request, re.I | re.S)
        op = only("import")
        if m and op:
            wanted, after = numbers(m.group(1)), int(m.group(2))
            if wanted and (wanted != op.get("slides") or after != op.get("after")):
                fixes.append(f"import: slides {op.get('slides')} after {op.get('after')} -> {wanted} after {after}")
                op["slides"], op["after"] = wanted, after
        decks = re.search(r"\bimport\b[^.]*?\b(?:of|from)\s+(\S+\.pptx)\b.*?\b(?:of|into|in|to)\s+(\S+\.pptx)",
                          request, re.I | re.S)
        name = lambda p: Path(str(p).replace("\\", "/")).name.lower()
        if decks and op and (name(op.get("from")) != name(decks.group(1))
                             or name(args["path"]) != name(decks.group(2))):
            fixes.append(f"import: from {op.get('from')} into {args['path']} -> from {decks.group(1)} into "
                         f"{decks.group(2)}")
            op["from"] = decks.group(1)
            args = {**args, "path": decks.group(2)}
        if request and not re.search(r"\b(?:delete|remove|drop|get rid of)\b", request, re.I):
            dropped = [op for op in ops if op["op"] == "delete"]
            if dropped:
                fixes.append(f"{len(dropped)} delete op(s) dropped: the user did not ask to delete a slide")
                ops = [op for op in ops if op["op"] != "delete"]
        if not fixes:
            return args, "the ops match the user's words", None
        if not ops:
            raise Refusal("The only operation was a delete the user did not ask for, so nothing was done.")
        note = "The harness changed your ops to match the user's words: " + "; ".join(fixes) + "."
        return {**args, "ops": ops}, "; ".join(fixes), note

    def run(self, ctx, path, ops):
        t = ctx.tracer
        original, source = deck_file(path, ctx)
        t.say(self.how_it_works, kind="python")
        ledger, entry, what = versions.track(source, ctx)
        with t.stage("DECK VIEW", "python", "lxml (deck_model.py, no ML)", ".pptx file",
                     "slide ids + fingerprints") as ev:
            before = deck_view(source)
            ev.summary = f"{rel(source)}: {before['slide_count']} slides, ids {[s['id'] for s in before['slides']]}"
        yours = self._your_edits(ledger, entry, what, source)
        with t.stage("CHECK OPS", "python", "slide numbers -> slide ids; words found once (no ML)", f"{len(ops)} op(s)",
                     "a plan per op") as ev:
            plans = plan_ops(ops, before, source, ctx)
            for plan in plans:
                t.say(f"op {plan['n']}: {plan['desc']}: " + (plan["why"] or "ok") + (f" ({plan['note']})"
                                                                                       if plan.get("note") else ""),
                      kind="python")
            ev.summary = f"{sum(p['status'] == 'planned' for p in plans)} of {len(plans)} op(s) can be applied"
        for plan in plans:
            if plan["status"] == "planned" and plan.get("ask"):
                if not ask_person(ctx, plan["ask"], "a delete" if plan["op"] == "delete" else "an edit"):
                    plan["status"], plan["why"] = "refused", "a person said no"
        if not any(p["status"] == "planned" for p in plans):
            raise Refusal("No operation could be applied: " + "; ".join(f"op {p['n']} ({p['desc']}): {p['why']}"
                                                                        for p in plans))
        out = output_path(ctx, original, "edited")
        source_problems = package_problems(source)
        with t.stage("APPLY OPS", "python", self.model_id, "the plans", ".pptx copy") as ev:
            order, batch = apply_ops(source, out, plans)
            ev.summary = (f"{sum(p['status'] == 'done' for p in plans)} op(s) applied -> {rel(out)}; "
                          f"{len(order)} slides")
        with t.stage("VERIFY", "python", "fingerprints + package checks (no ML)", "the deck before and after",
                     "each op verified or undone") as ev:
            after, failed, problems = verify(before, out, plans, order, batch, source_problems)
            if failed or problems:
                bad = set(failed)
                if problems:
                    bad |= self._culprits(source, [p for p in plans if p["n"] not in bad], before, source_problems)
                for plan in plans:
                    if plan["n"] in bad:
                        plan["status"], plan["why"] = "failed", failed.get(plan["n"]) or "; ".join(problems)[:300]
                t.say(f"undoing op(s) {sorted(bad)}: {failed or problems}", kind="python")
                for plan in plans:
                    if plan["status"] == "done":
                        plan["status"] = "planned"
                order, batch = apply_ops(source, out, plans)
                after, failed, problems = verify(before, out, plans, order, batch, source_problems)
                if failed or problems:
                    out.unlink(missing_ok=True)
                    raise RuntimeError(f"edit_deck could not make a deck that passes its checks: {failed or problems}")
            if not any(p["status"] == "done" for p in plans):
                out.unlink(missing_ok=True)
                raise RuntimeError("Every operation failed its check and was undone: " + "; ".join(
                    f"op {p['n']} ({p['desc']}): {p['why']}" for p in plans))
            untouched = sum(1 for s in before["slides"] if s["id"] in {x["id"] for x in after["slides"]}
                            and s["id"] not in {p.get("slide_id") for p in plans if p["status"] == "done"
                                                and p["op"] in ("text", "notes")})
            verified = (f"{untouched} untouched slide(s) have the same fingerprint; every op shows in the new view; "
                        "every relationship resolves and every part has a content type")
            ev.summary = verified
        done = [p for p in plans if p["status"] == "done"]
        changed_ids = [p["slide_id"] for p in done if p["op"] in ("text", "notes")] + \
            [sid for p in done for sid in p.get("new_ids", [])]
        order_record = {"op": "order", "before": [s["id"] for s in before["slides"]], "after": order,
                        "titles": {str(s["id"]): s["title"] for s in before["slides"]},
                        "changed": changed_ids, "structural": sorted({p["op"] for p in done} & STRUCTURAL)}
        with t.stage("VERSION LEDGER", "python", "versions.py (no ML)", rel(out), "a new harness version") as ev:
            changes = [{"op": p["op"], "desc": p["desc"], "slide_id": p.get("slide_id"),
                        "new_ids": p.get("new_ids", [])} for p in done] + [order_record]
            new_entry = versions.record(ledger, out, ctx, "edit_deck", changes)
            ev.summary = f"v{new_entry['n']} of {ledger.key}: {len(done)} op(s)"
        ctx.copies[Path(original)] = out  # later tools in this request (check_deck, read_deck) see the edited deck
        ctx.facts.setdefault("deck_edits", {})[str(out.resolve())] = order_record
        report = output_path(ctx, original, "deck_diff", ".md")
        diff = diff_views(before, after)
        report.write_text(diff_markdown(diff, rel(source), rel(out)), encoding="utf-8")
        position = {s["id"]: s["position"] for s in after["slides"]}
        changed = sorted({position[sid] for sid in changed_ids if sid in position})
        refused = [p for p in plans if p["status"] in ("refused", "failed")]
        ctx.notes.append(f"edit_deck on {rel(original)}: {len(done)} op(s) done, {len(refused)} refused or failed; "
                         f"the other slides were checked unchanged. New deck: {rel(out)}.")
        observations = {"deck": rel(original), "new_deck": rel(out),
                        "done": [p["desc"] + (f" ({p['note']})" if p.get("note") else "") for p in done] or "none",
                        "slides_now": len(order), "changed_slides": changed or "none (only the order changed)",
                        "verified": verified, "diff": rel(report),
                        "next": "check_deck renders the changed slides and checks that the text fits"}
        if refused:
            observations["not_done"] = [f"{p['desc']}: {p['why']}" for p in refused]
        lost = [item for p in done for item in p.get("lost", [])]
        if lost:
            observations["not_carried_over"] = lost[:6]
        if yours:
            observations["your_edits_kept"] = yours
        return {"artifacts": [{"type": "deck_edit", "path": out, "note": f"{rel(original)} with {len(done)} op(s); "
                                                                        "the original is unchanged"},
                              {"type": "deck_diff", "path": report, "note": "what the ops changed"}],
                "observations": observations}

    @staticmethod
    def _your_edits(ledger, entry, what, source):
        """'Use mine as the latest': when the deck holds edits the harness didn't make, say what they were (they are
        kept: the ops apply to your version)."""
        if entry["origin"] != "user":  # new bytes now, or recorded as yours by an earlier read or diff
            return None
        earlier = [v for v in ledger.versions if v["n"] < entry["n"] and v["origin"] in ("harness", "baseline")]
        if not earlier:
            return None
        diff = diff_views(deck_view(ledger.stored_file(earlier[-1])), deck_view(source))
        return [f"you edited this deck since the harness's v{earlier[-1]['n']}; your version is the new latest: "
                f"{diff['summary']}"] + diff["changed"][:3] + diff["moved"][:2] + diff["deleted"][:2]

    @staticmethod
    def _culprits(source, plans, before, source_problems):
        """A problem no single op owns (an untouched slide changed): try each op alone to find which one did it."""
        import tempfile
        bad = set()
        for plan in [p for p in plans if p["status"] == "done"]:
            trial = [dict(p, status="planned" if p is plan else "skipped") for p in plans]
            with tempfile.TemporaryDirectory() as folder:
                out = Path(folder) / "trial.pptx"
                order, batch = apply_ops(source, out, trial)
                _, failed, problems = verify(before, out, trial, order, batch, source_problems)
                if failed or problems:
                    bad.add(plan["n"])
        return bad  # none alone: a failed op left something half done, and applying the rest again leaves it out


class CheckDeckTool(Tool):
    name = "check_deck"
    description = ("Check a PowerPoint deck (.pptx), by default the slides just edited: PowerPoint draws them and "
                   "measures whether the text overflows its box; shapes that cover the template's logo or cross its "
                   "footer rule are listed; after a move, delete or import, words such as 'slide 7' or 'the next "
                   "section' that may now be wrong are listed. terms: words to look for in all the text and notes.")
    parameters = {"type": "object",
                  "properties": {"path": DECK_PATH,
                                 "slides": {"type": "string", "description": "Optional: the slides to check, e.g. "
                                                                             "'4' or '2-5' (default: the changed "
                                                                             "slides, or all)"},
                                 "terms": {"type": "array", "items": {"type": "string"},
                                           "description": "Optional: words to find in every slide and its notes"}},
                  "required": ["path"]}
    model_id = "PowerPoint through COM (a child process) + lxml (no ML)"
    repeatable = True
    requires = ("edit_deck",)  # "delete slide 7 and check the deck": when both are offered, the edit comes first
    how_it_works = ("No model here: PowerPoint, driven through COM in a separate process with a time limit, opens a "
                    "copy of the deck without a window, draws the slides as PNGs and measures the height of the text "
                    "in each box. Python checks every shape against the template's keep-out zones and searches the "
                    "text and notes for references that a move or delete may have broken.")

    def check_request(self, args, ctx):
        """
        CHECK ARGS: (1) "Delete slide 7 and check the deck for overflow": the user asked about the whole deck, and
        "7" was the slide to delete (Llama 3.2 3B passed slides="7" to check_deck, which is another slide by then),
        so no slide number from the request limits the check unless it says "check slide N". (2) Quoted words the
        user wants found ('check for any mention of "ten-year plan"') become terms.
        """
        request = ctx.request or ""
        whole = re.search(r"\b(?:the (?:whole |entire )?(?:deck|presentation)|all (?:the )?slides|every slide)\b",
                          request, re.I)
        if args.get("slides") and whole and not re.search(r"\bcheck\w*\s+(?:only\s+)?slides?\s+\d", request, re.I):
            args = {k: v for k, v in args.items() if k != "slides"}
            return args, f"slides dropped: the user said \"{whole.group(0)}\", so every slide is checked", \
                ("The user asked to check the whole deck, so the harness checked every slide, not only the slides "
                 "you named.")
        if not re.search(r"\b(?:mention|mentions|term|terms|leftover|occurrences?|search|find|look for|refer)", request,
                         re.I):
            return args, "no words to look for in the request", None
        quoted = [m for m in re.findall(r"[\"“]([^\"”]{2,60})[\"”]", request) if not m.lower().endswith(".pptx")]
        terms = list(args.get("terms") or [])
        missing = [m for m in quoted if m.lower() not in [x.lower() for x in terms]]
        if not missing:
            return args, "the terms match the user's words", None
        return {**args, "terms": terms + missing}, f"terms from the user's words: {missing}", \
            f"The harness also searches for the user's quoted words: {', '.join(missing)}."

    def run(self, ctx, path, slides=None, terms=None):
        t = ctx.tracer
        original, deck = deck_file(path, ctx)
        t.say(self.how_it_works, kind="python")
        view = deck_view(deck)
        count = view["slide_count"]
        edits = edits_of(deck, ctx)
        wanted = [n for n in numbers(slides) if 1 <= n <= count]
        # "check the deck for overflow" after "delete slide 7": the user means every slide, not just the edited ones
        whole = re.search(r"\b(?:the (?:whole |entire )?(?:deck|presentation)|all (?:the )?slides|every slide)\b",
                          ctx.request or "", re.I)
        if wanted:
            which = f"slide(s) {', '.join(map(str, wanted))}, as asked"
        elif whole:
            wanted, which = list(range(1, count + 1)), f"every slide: the request says \"{whole.group(0)}\""
        elif edits and edits.get("changed"):
            position = {s["id"]: s["position"] for s in view["slides"]}
            wanted = sorted({position[sid] for sid in edits["changed"] if sid in position})
            which = f"the slide(s) the last edit changed: {', '.join(map(str, wanted))}"
        if not wanted:
            wanted, which = list(range(1, count + 1)), "every slide"
        chosen = [view["slides"][n - 1] for n in wanted]
        rendered, not_rendered, pngs = None, None, []
        with t.stage("RENDER", "python", "PowerPoint via COM in a child process (no ML)", ".pptx copy",
                     "PNGs + measured text heights") as ev:
            folder = new_folder(ctx, original, "slides")
            try:
                rendered = render_slides(deck, folder, slides=wanted)
                pngs = rendered["pngs"]
                ev.summary = (f"{len(pngs)} slide(s) drawn in {rel(folder)}; PowerPoint opened the deck and counted "
                              f"{rendered['slide_count']} slides")
            except PowerPointUnavailable as exc:
                not_rendered = f"not rendered: {exc}"
                ev.summary = not_rendered
        problems = []
        with t.stage("FIT", "python", "PowerPoint's BoundHeight, else font metrics (no ML)", "text boxes",
                     "boxes whose text overflows") as ev:
            names = {(s["position"], sh["id"]): sh["name"] for s in chosen for sh in s["shapes"]}
            if rendered:
                for (n, sid), ratio in sorted(rendered["measured"].items()):
                    if ratio > 1.0 and (n, sid) in names:
                        problems.append({"kind": "overflow", "slide": n, "shape": names[(n, sid)],
                                         "text": f"slide {n}: text overflows \"{names[(n, sid)]}\" (it needs "
                                                 f"{ratio:.0%} of the box's height, measured by PowerPoint)"})
            else:
                for (n, sid), (name, ratio) in sorted(estimated_fit(deck, set(wanted)).items()):
                    if ratio > 1.0:
                        problems.append({"kind": "overflow", "slide": n, "shape": name,
                                         "text": f"slide {n}: text may overflow \"{name}\" (about {ratio:.0%} of the "
                                                 "box's height: an estimate from font sizes, not measured)"})
            ev.summary = "; ".join(p["text"] for p in problems) or "every text box fits"
        with t.stage("KEEP-OUT ZONES", "python", "a template profile + box overlap (no ML)", "shape boxes",
                     "shapes over the logo or the footer rule") as ev:
            profile, matched = profile_for(view["template"], original)
            if profile:
                boxes = rendered["boxes"] if rendered else None
                for p in keep_out_problems(chosen, profile, boxes):
                    problems.append({"kind": "keep_out", **p})
                ev.summary = (f"profile {Path(profile['file']).name} (matched by {matched}): "
                              + ("; ".join(p["text"] for p in problems if p["kind"] == "keep_out") or "nothing crosses "
                                                                                                     "a zone"))
            else:
                ev.summary = matched
        stale, hits = [], []
        if edits and edits.get("structural") and set(edits["structural"]) & {"move", "delete", "import", "insert",
                                                                              "duplicate"}:
            with t.stage("STALE REFERENCES", "python", "regular expressions (no ML)", "all text and notes",
                         "references that may now be wrong") as ev:
                stale = stale_references(view["slides"], {"order": edits["before"], "titles": edits["titles"]})
                ev.summary = "; ".join(s["text"] for s in stale) or "none found"
        if terms:
            with t.stage("TERMS", "python", "a search (no ML)", f"{len(terms)} term(s)", "where they occur") as ev:
                hits = term_hits(view["slides"], terms)
                ev.summary = f"{len(hits)} occurrence(s) of {', '.join(terms)}"
        artifacts = []
        if pngs:
            sheet = output_path(ctx, original, "checked_slides", ".png")
            contact_sheet(pngs, sheet, labels=[f"slide {n}" for n in wanted[:len(pngs)]])
            artifacts.append({"type": "preview", "path": sheet, "note": "the checked slides, as PowerPoint drew them"})
            artifacts += [{"type": "preview", "path": png, "note": f"slide {n}"} for n, png in zip(wanted, pngs)][:12]
        report = output_path(ctx, original, "check", ".md")
        report.write_text("\n".join(
            [f"# check_deck: {rel(deck)}", "", f"Checked {which}. " + (not_rendered or "Drawn and measured by "
                                                                       "PowerPoint."), "", "## Problems", ""]
            + [f"- {p['text']}" for p in problems] + (["- none"] if not problems else [])
            + (["", "## References that may now be wrong", ""] + [f"- {s['text']}" for s in stale] if stale else [])
            + (["", "## Terms", ""] + [f"- {h['term']}: {h['text']}" for h in hits] if terms else [])),
            encoding="utf-8")
        artifacts.append({"type": "deck_check", "path": report, "note": "every problem found"})
        summary = (f"{len(problems)} problem(s) on {len(chosen)} checked slide(s)"
                   + (f"; {len(stale)} reference(s) that may now be wrong" if stale else "")
                   + (f"; {not_rendered}" if not_rendered else "; drawn and measured by PowerPoint"))
        ctx.notes.append(f"check_deck on {rel(deck)}: {summary}.")
        observations = {"deck": rel(deck), "checked": which, "summary": summary,
                        "problems": [p["text"] for p in problems][:MAX_LINES] or "none",
                        "rendered": not_rendered or f"{len(pngs)} slide(s) drawn by PowerPoint",
                        "report": rel(report)}
        if rendered and rendered["slide_count"] != count:
            observations["slide_count"] = f"PowerPoint sees {rendered['slide_count']} slides, the file lists {count}"
        if stale:
            observations["stale_references"] = [s["text"] for s in stale][:MAX_LINES]
        if terms:
            observations["terms"] = [h["text"] for h in hits][:MAX_LINES] or f"none of {', '.join(terms)} found"
        return {"artifacts": artifacts, "observations": observations}
