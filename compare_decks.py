"""
Editing a deck with its author, measured: diff_decks, edit_deck and check_deck (harness/tools/deck_edit.py) on
workspace/deck_demo, scored against deck_demo_answers.json (made by workspace/make_deck_demo.py).

    diff     recall and precision on the six edits planted in briefing_edited.pptx; with PowerPoint, the same decks
             saved again by PowerPoint (which renumbers parts and rewrites the XML) must show no change, and the
             edited deck saved by PowerPoint must still show the six
    edits    16 scripted ops, each on a fresh copy of briefing.pptx, and all the compatible ones in one batch. Each
             must pass edit_deck's own VERIFY, and this script checks again on its own: every other slide's
             fingerprint unchanged, no chart or media shared by a new slide, the package whole, the slide count, and
             PowerPoint opening the deck with that count
    check    check_deck against the planted overflow and logo overlap, with no false alarm on the other slides
             (measured by PowerPoint, and by the estimate alone), and the stale "slide 7" after a delete

    python compare_decks.py                  # with PowerPoint where it is installed
    python compare_decks.py --no-powerpoint  # plain Python only

No LLM and no GPU. Every op is approved. The runs are kept in outputs/deck_comparison_<time>/, next to comparison.md.
"""

import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from harness.config import OUTPUT_DIR, PROJECT_DIR, HarnessConfig
from harness.trace import Tracer
from harness.tools import deck_edit
from harness.tools.base import ToolContext
from harness.tools.deck_check import PowerPointUnavailable, powerpoint_available, powerpoint_job
from harness.tools.deck_edit import CheckDeckTool, EditDeckTool, diff_views, package_problems, shared_parts
from harness.tools.deck_model import deck_view

ANSWERS = json.loads((PROJECT_DIR / "deck_demo_answers.json").read_text(encoding="utf-8"))
ORIGINAL, EDITED, RESULTS = (ANSWERS["decks"][k] for k in ("original", "edited", "import_from"))
USE_POWERPOINT = powerpoint_available() and "--no-powerpoint" not in sys.argv

# (name, ops): one run each, on a fresh copy of briefing.pptx
SCRIPTED = [
    ("words, bold kept", [{"op": "text", "slide": 4, "find": "12 tests", "replace": "14 tests"}]),
    ("words across runs", [{"op": "text", "slide": 2, "find": "wood cribs", "replace": "pine wood cribs"}]),
    ("a box's whole text", [{"op": "text", "slide": 8, "shape": "body", "replace": "Four more tests\nAn FDS model"}]),
    ("a table cell", [{"op": "text", "slide": 7, "find": "2.4", "replace": "2.5"}]),
    ("notes added to", [{"op": "notes", "slide": 3, "text": "Start with the hood."}]),
    ("notes replaced", [{"op": "notes", "slide": 1, "text": "Welcome, everyone.", "mode": "replace"}]),
    ("notes where none were", [{"op": "notes", "slide": 5, "text": "Read the smoke layer from the video."}]),
    ("move", [{"op": "move", "slide": 5, "after": 2}]),
    ("move to the start", [{"op": "move", "slide": 8, "after": 0}]),
    ("delete", [{"op": "delete", "slide": 7}]),
    ("duplicate", [{"op": "duplicate", "slide": 4}]),
    ("duplicate a picture", [{"op": "duplicate", "slide": 6, "after": 8}]),
    ("insert", [{"op": "insert", "after": 3, "title": "Method", "bullets": ["Calibrate the hood", "  Daily"]}]),
    ("insert a section", [{"op": "insert", "after": 8, "layout": "Section Header", "title": "Discussion"}]),
    ("import, restyled", [{"op": "import", "from": RESULTS, "slides": "2-3", "after": 6}]),
    ("import, as drawn", [{"op": "import", "from": RESULTS, "slides": "3", "after": 0, "restyle": False}]),
]
BATCH = ("all in one batch", [op for _, ops in SCRIPTED for op in ops  # not the table cell: slide 7 is deleted
                               if op["op"] != "move" and not (op["op"] == "text" and op["slide"] == 7)]
         + [{"op": "move", "slide": 5, "after": 2}])


def context(folder, name):
    run_dir = folder / name
    run_dir.mkdir(parents=True)
    ctx = ToolContext(Tracer(verbosity=0, color=False), run_dir, "cpu", 1, {}, HarnessConfig(ask_before_acting=False),
                      request="")
    ctx.facts["versions_dir"] = str(folder / "versions")  # not outputs/versions: these are measurements
    return ctx


def matches(item, planted):
    return item["kind"] == planted["kind"] and item["title"] == planted["slide"]


def score_diff(diff):
    found = {p["id"] for p in ANSWERS["planted_edits"] for item in diff["items"] if matches(item, p)}
    extra = [item["text"] for item in diff["items"] if not any(matches(item, p) for p in ANSWERS["planted_edits"])]
    return found, extra


def run_diffs(folder):
    rows = []
    diff = diff_views(deck_view(ORIGINAL), deck_view(EDITED))
    found, extra = score_diff(diff)
    rows.append(("briefing -> briefing_edited", f"{len(found)}/6", f"{len(diff['items']) - len(extra)}/"
                 f"{len(diff['items'])}", extra))
    if USE_POWERPOINT:
        for name in ("briefing", "briefing_edited", "results"):
            source = PROJECT_DIR / "workspace" / "deck_demo" / f"{name}.pptx"
            copy = folder / f"{name}_saved_by_powerpoint.pptx"
            powerpoint_job("save", source, out=str(copy.resolve()), format=24)
            same = diff_views(deck_view(source), deck_view(copy))
            rows.append((f"{name} -> saved again by PowerPoint", "-", f"{len(same['items'])} change(s) reported",
                         [it["text"] for it in same["items"]]))
        diff = diff_views(deck_view(ORIGINAL), deck_view(folder / "briefing_edited_saved_by_powerpoint.pptx"))
        found, extra = score_diff(diff)
        rows.append(("briefing -> briefing_edited saved by PowerPoint", f"{len(found)}/6",
                     f"{len(diff['items']) - len(extra)}/{len(diff['items'])}", extra))
    return rows


def run_edit(folder, name, ops):
    """One edit_deck call, then this script's own checks of the result."""
    ctx = context(folder, name.replace(" ", "_").replace(",", ""))
    before = deck_view(ORIGINAL)
    start = time.perf_counter()
    tool = EditDeckTool()
    try:
        out = tool.run(ctx, **tool.validate({"path": ORIGINAL, "ops": ops}))
    except Exception as exc:  # a refusal or a failed VERIFY: reported, not raised
        return {"name": name, "ok": False, "why": f"{type(exc).__name__}: {exc}"[:200], "seconds": 0}
    seconds = time.perf_counter() - start
    deck = Path(out["artifacts"][0]["path"])
    after = deck_view(deck)
    obs = out["observations"]
    done = obs["done"] if isinstance(obs["done"], list) else []
    changed = set(ctx.facts["deck_edits"][str(deck.resolve())]["changed"])
    old = {s["id"]: s["fingerprint"] for s in before["slides"]}
    new = {s["id"]: s["fingerprint"] for s in after["slides"]}
    deleted = {before["slides"][op["slide"] - 1]["id"] for op in ops if op["op"] == "delete"}
    untouched = [sid for sid in old if sid not in changed and sid not in deleted]
    added = sum(1 if op["op"] in ("insert", "duplicate") else len(deck_edit.numbers(op["slides"]))
                if op["op"] == "import" else 0 for op in ops)
    checks = {"VERIFY": len(done) == len(ops),
              "untouched": all(new.get(sid) == old[sid] for sid in untouched),
              "package": package_problems(deck) == [],
              "not shared": shared_parts(deck, [s["id"] for s in after["slides"] if s["id"] not in old]) == [],
              "count": len(after["slides"]) == len(before["slides"]) - len(deleted) + added}
    opened = "-"
    if USE_POWERPOINT:
        try:
            opened = powerpoint_job("count", deck)["slide_count"]
            checks["PowerPoint"] = opened == len(after["slides"])
        except PowerPointUnavailable as exc:
            opened = f"not checked: {exc}"
    return {"name": name, "ok": all(checks.values()), "checks": checks, "slides": len(after["slides"]),
            "untouched": len(untouched), "opened": opened, "seconds": seconds, "done": done,
            "not_done": obs.get("not_done", [])}


def run_checks(folder):
    rows = []
    for how in (["PowerPoint"] if USE_POWERPOINT else []) + ["estimate"]:
        ctx = context(folder, f"check_{how}")
        real = deck_edit.render_slides
        if how == "estimate":
            deck_edit.render_slides = lambda *a, **k: (_ for _ in ()).throw(PowerPointUnavailable("--estimate"))
        try:
            start = time.perf_counter()
            obs = CheckDeckTool().run(ctx, ORIGINAL)["observations"]
            seconds = time.perf_counter() - start
        finally:
            deck_edit.render_slides = real
        problems = obs["problems"] if isinstance(obs["problems"], list) else []
        hit = lambda p, text: f"slide {p['slide']}:" in text and p["shape"] in text and p.get("zone", "") in text
        found = [p["id"] for p in ANSWERS["planted_problems"] if any(hit(p, t) for t in problems)]
        false = [t for t in problems if not any(hit(p, t) for p in ANSWERS["planted_problems"])]
        rows.append((how, f"{len(found)}/{len(ANSWERS['planted_problems'])}", false, seconds))
    ctx = context(folder, "check_after_delete")
    tool = EditDeckTool()
    tool.run(ctx, **tool.validate({"path": ORIGINAL, "ops": [ANSWERS["stale_reference"]["after"]]}))
    real = deck_edit.render_slides
    deck_edit.render_slides = lambda *a, **k: (_ for _ in ()).throw(PowerPointUnavailable("not needed here"))
    try:
        obs = CheckDeckTool().run(ctx, ORIGINAL)["observations"]
    finally:
        deck_edit.render_slides = real
    stale = obs.get("stale_references", [])
    key = ANSWERS["stale_reference"]
    return rows, stale, any(key["words"] in s and key["where"] in s for s in stale)


def report(diffs, edits, checks, stale, stale_ok, folder):
    lines = ["# Editing a deck with its author: workspace/deck_demo", "",
             f"PowerPoint: {'used' if USE_POWERPOINT else 'not used'}. Answer key: deck_demo_answers.json.", "",
             "## diff_decks", "", "| decks | planted edits found | items that are planted edits |", "|---|---|---|"]
    lines += [f"| {name} | {recall} | {precision} |" for name, recall, precision, _ in diffs]
    for name, _, _, extra in diffs:
        lines += [f"- {name}: not planted: {e}" for e in extra]
    lines += ["", "## edit_deck: scripted ops (each on a fresh copy of briefing.pptx)", "",
              "| ops | result | VERIFY | untouched slides same | package whole | no shared chart/media | slide count "
              "| PowerPoint opens it | seconds |", "|---|---|---|---|---|---|---|---|---|"]
    for e in edits:
        if "checks" not in e:
            lines.append(f"| {e['name']} | FAILED: {e['why']} | | | | | | | |")
            continue
        c = e["checks"]
        mark = lambda k, c=c: "yes" if c.get(k, True) else "NO"
        lines.append(f"| {e['name']} | {'ok' if e['ok'] else 'FAILED'} | {mark('VERIFY')} | {mark('untouched')} "
                     f"({e['untouched']}) | {mark('package')} | {mark('not shared')} | {mark('count')} ({e['slides']}) "
                     f"| {e['opened'] if 'PowerPoint' not in c else mark('PowerPoint')} | {e['seconds']:.1f} |")
    lines += [f"- {e['name']}: not done: {n}" for e in edits for n in e.get("not_done", [])]
    lines += ["", "## check_deck on briefing.pptx", "",
              "| measured by | planted problems found | false alarms | seconds |",
              "|---|---|---|---|"]
    lines += [f"| {how} | {found} | {len(false)} | {seconds:.1f} |" for how, found, false, seconds in checks]
    lines += [f"- {how}: false alarm: {f}" for how, _, false, _ in checks for f in false]
    lines += ["", f"- stale reference after deleting slide 7: {'found' if stale_ok else 'MISSED'}"
              + (f": {stale[0]}" if stale else "")]
    path = folder / "comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    folder = OUTPUT_DIR / time.strftime("deck_comparison_%Y%m%d_%H%M%S")
    folder.mkdir(parents=True)
    diffs = run_diffs(folder)
    for name, recall, precision, _ in diffs:
        print(f"diff  {name}: found {recall}, planted {precision}", flush=True)
    edits = []
    for name, ops in SCRIPTED + [BATCH]:
        edits.append(run_edit(folder, name, ops))
        e = edits[-1]
        print(f"edit  {name:<24} {'ok' if e['ok'] else 'FAILED'}  {e.get('checks', e.get('why'))}", flush=True)
    checks, stale, stale_ok = run_checks(folder)
    for how, found, false, seconds in checks:
        print(f"check {how}: {found} planted problems found, {len(false)} false alarm(s), {seconds:.1f} s", flush=True)
    print(f"check stale reference after the delete: {'found' if stale_ok else 'MISSED'}")
    path = report(diffs, edits, checks, stale, stale_ok, folder)
    print(f"\nsaved: {path}")


if __name__ == "__main__":
    main()
