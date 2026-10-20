"""
The Word review tools, measured: workspace/review_demo/ scored against review_demo_answers.json
(OFFICE_COLLAB_DESIGN.md, W9). Plain Python and Word; no LLM, no GPU. It measures:

    census      read_review's counts against the key, exactly (changes and comments by author and kind, threads open
                and resolved, replies), and against Word's own Revisions.Count and Comments.Count
    invariants  21 scripted edits, one redline_docx call each (table cells, mixed formatting, someone else's pending
                change, a loose match, paragraphs and rows): each must end as the key says (verified, needs a person,
                refused), and every verified one must pass W6: rejecting it gives the original back exactly, accepting
                it gives the intended text. Then all of them in one call, opened in Word
    compare     the planted v1/v2 differences found by Word's Compare and by the Python word diff: each one's old text
                in the redline's rejected text only, its new text in the accepted text only; both redlines verified
    Word        Word's accept-all and reject-all of each file against the reader's (when Word is installed)
    time        seconds per stage

    python compare_review.py              # with Word where it is installed
    python compare_review.py --no-word    # plain Python only (as on Linux or macOS)

The runs are kept in outputs/review_comparison_<time>/, next to comparison.md. The demo's files are never written.
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from harness.config import OFFICE, OUTPUT_DIR, PROJECT_DIR, HarnessConfig
from harness.tools import docx_markup as dm
from harness.tools import word_com
from harness.tools import word_model as wm
from harness.tools.base import Refusal, ToolContext
from harness.tools.office_common import review_author
from harness.tools.word_review import (AcceptChangesTool, CommentDocxTool, CompareDocxTool, ReadReviewTool,
                                       RedlineDocxTool)
from harness.trace import Tracer

KEY = json.loads((PROJECT_DIR / "review_demo_answers.json").read_text(encoding="utf-8"))
DEMO, V2 = KEY["document"], KEY["revised"]
USE_WORD = "--no-word" not in sys.argv and word_com.available()[0]

# (what it tests, the edit, the outcome the key expects)
EDITS = [
    ("a number", {"find": "within 5 minutes", "replace": "within 3 minutes"}, "verified"),
    ("a deleted word", {"find": "a small propane torch", "replace": "a propane torch"}, "verified"),
    ("inserted words", {"find": "The crew checks every instrument", "replace": "The crew checks and logs every "
                                                                              "instrument"}, "verified"),
    ("a table cell", {"find": "Wood crib, 20 kg", "replace": "Wood crib, 25 kg"}, "verified"),
    ("another table cell", {"find": "250 kW", "replace": "275 kW"}, "verified"),
    ("an ambiguous cell", {"find": "Door open", "replace": "Door ajar"}, "refused"),
    ("a bold caption", {"find": "Table 1. Test conditions", "replace": "Table 1. Test conditions and fuels"},
     "verified"),
    ("text with B's formatting change", {"find": "must not exceed 600 °C", "replace": "must stay below 600 °C"},
     "verified"),
    ("a first word", {"find": "Hearing protection", "replace": "Ear protection"}, "verified"),
    ("a new paragraph", {"insert_after": "safety glasses are worn in the burn hall while a test runs.",
                         "paragraphs": ["Visitors stay behind the barrier."]}, "verified"),
    ("a deleted paragraph", {"delete_paragraph": "The heat release rate from the hood must agree"}, "verified"),
    ("a new table row", {"table_row_after": "Propane burner", "cells": ["4", "Wood crib, 40 kg", "700 kW",
                                                                        "Door open"]}, "verified"),
    ("a deleted table row", {"delete_row": "Wood crib, 30 kg"}, "verified"),
    ("inside A's insertion", {"find": "three thermocouple trees", "replace": "four thermocouple trees"},
     "needs a person"),
    ("across B's deletion", {"find": "a calorimetry hood", "replace": "an extraction hood"}, "needs a person"),
    ("a loose match (two spaces)", {"find": "The tests  measure", "replace": "The tests record"}, "verified"),
    ("a shortened sentence", {"find": "before the first ignition, not during a test",
                              "replace": "before ignition"}, "verified"),
    ("the title", {"find": "Compartment Tests", "replace": "Compartment Fire Tests"}, "verified"),
    ("a text that is not there", {"find": "a flashover", "replace": "flashover"}, "refused"),
    ("a whole sentence deleted", {"find": " Longer tests are run only if the test director asks for them in writing "
                                          "beforehand.", "replace": ""}, "verified"),
    ("a reworded clause", {"find": "Nobody enters the room", "replace": "No one goes into the room"}, "verified"),
]


def context(folder, name, request=""):
    run_dir = folder / name
    run_dir.mkdir(parents=True)
    ctx = ToolContext(Tracer(verbosity=0, color=False), run_dir, "cpu", 1, {}, HarnessConfig(ask_before_acting=False),
                      request=request)
    ctx.facts["versions_dir"] = str(folder / "versions")
    return ctx


def stage_seconds(ctx, into):
    for ev in ctx.tracer.events:
        into[ev.stage.split(" > ")[-1]] += ev.seconds


def call(tool, ctx, args):
    args = tool.validate(args)
    if hasattr(tool, "check_request"):
        args, _, _ = tool.check_request(args, ctx)
    return tool.run(ctx, **args)


def docx_of(out):
    return next(Path(a["path"]) for a in out["artifacts"] if str(a["path"]).endswith(".docx"))


def word_lines(text):
    return [x for x in text.replace("\x07", "\r").split("\r") if x]


def census_section(folder, times):
    ctx = context(folder, "census", "Which comments are open, and who made the tracked changes?")
    call(ReadReviewTool(), ctx, {"path": DEMO})
    stage_seconds(ctx, times)
    view = wm.docx_view(PROJECT_DIR / DEMO)
    rows = [(k, KEY["census"][k], view["census"][k]) for k in KEY["census"]]
    comments = {c["id"]: c for c in view["comments"]}
    threads = [(t["label"], t["done"], t["replies"], comments.get(t["label"][1:], {}).get("done"),
                [f"c{r}" for r in comments.get(t["label"][1:], {}).get("replies", [])]) for t in KEY["threads"]]
    word = word_com.open_check(PROJECT_DIR / DEMO, texts=True)[0] if USE_WORD else None
    return rows, threads, word


def invariants_section(folder, times):
    original = dm.Package(PROJECT_DIR / DEMO).document
    before = wm.lines(original, wm.KEEP)
    me = None
    results = []
    for n, (what, edit, expected) in enumerate(EDITS, 1):
        ctx = context(folder, f"edit_{n:02d}")
        me = review_author(ctx)
        start = time.perf_counter()
        try:
            out = call(RedlineDocxTool(), ctx, {"path": DEMO, "edits": [edit]})
            line = out["observations"]["edits"][0]
            status = line.split(":")[0]
            root = dm.Package(docx_of(out)).document
            reject = wm.lines(root, wm.deciding(authors=[me], action="reject", others="keep")) == before
            accept = None
            if "find" in edit and status == "verified":
                want = [x.replace(edit["find"].replace("  ", " "), edit["replace"]) for x in
                        wm.texts(original, wm.ACCEPT)]
                accept = wm.texts(root, wm.ACCEPT) == want
        except Refusal as exc:  # no edit made: the reason says whether a person is needed
            status = "needs a person" if "needs a person" in str(exc) else "refused"
            reject, accept, line = None, None, str(exc)
        stage_seconds(ctx, times)
        results.append({"n": n, "what": what, "expected": expected, "got": status, "reject_ok": reject,
                        "accept_ok": accept, "seconds": time.perf_counter() - start, "line": line})
    # all of them in one call, as a planner with a list of edits sends them, and opened in Word
    ctx = context(folder, "all_edits")
    saved = OFFICE["word_check"]
    OFFICE["word_check"] = USE_WORD
    try:
        out = call(RedlineDocxTool(), ctx, {"path": DEMO, "edits": [e for _, e, _ in EDITS]})
    finally:
        OFFICE["word_check"] = saved
    stage_seconds(ctx, times)
    root = dm.Package(docx_of(out)).document
    batch = {"summary": out["observations"]["summary"], "word_check": out["observations"]["word_check"],
             "reject_ok": wm.lines(root, wm.deciding(authors=[me], action="reject", others="keep")) == before,
             "census": out["observations"]["census_after"]}
    if USE_WORD:
        word = word_com.open_check(docx_of(out), texts=True)[0]
        batch["word_texts_ok"] = word is not None and (
            word_lines(word["accepted"]) == [x for x in wm.texts(root, wm.ACCEPT) if x]
            and word_lines(word["rejected"]) == [x for x in wm.texts(root, wm.REJECT) if x])
    return results, batch


def found(root, difference):
    """Is a planted difference in the redline: its v1 text only when rejected, its v2 text only when accepted?"""
    accepted, rejected = " | ".join(wm.texts(root, wm.ACCEPT)), " | ".join(wm.texts(root, wm.REJECT))
    ok = True
    if difference["v1"]:
        ok &= difference["v1"] in rejected and difference["v1"] not in accepted
    if difference["v2"]:
        ok &= difference["v2"] in accepted and difference["v2"] not in rejected
    return ok


def compare_section(folder, times):
    out = {}
    for method in (["word"] if USE_WORD else []) + ["python"]:
        ctx = context(folder, f"compare_{method}", "Compare test_plan_v2.docx with test_plan.docx, with track changes.")
        saved = dict(OFFICE)
        OFFICE["compare"], OFFICE["word_check"] = method, USE_WORD
        start = time.perf_counter()
        try:
            result = call(CompareDocxTool(), ctx, {"original": DEMO, "revised": V2})
        finally:
            OFFICE.update(saved)
        stage_seconds(ctx, times)
        root = dm.Package(docx_of(result)).document
        out[method] = {"method": result["observations"]["method"], "seconds": time.perf_counter() - start,
                       "accepted_is_v2": wm.texts(root, wm.ACCEPT) == KEY["texts"]["v2"],
                       "rejected_is_v1": wm.texts(root, wm.REJECT) == KEY["texts"]["accepted"],
                       "found": [found(root, d) for d in KEY["v1_v2_differences"]],
                       "changes": wm.docx_view(docx_of(result))["census"]["changes"],
                       "word_check": result["observations"]["word_check"]}
    return out


def other_tools(folder, times):
    """Requests 3 and 4 of W1, scored against the key."""
    ctx = context(folder, "reply", "Reply to comment 4 in test_plan.docx that the duct sizes are now in Table 1, and "
                                   "mark it resolved.")
    out = call(CommentDocxTool(), ctx, {"path": DEMO, "comments": [{"reply_to": "c4", "text": "The duct sizes are "
                                                                                              "now in Table 1."}]})
    stage_seconds(ctx, times)
    view = wm.docx_view(docx_of(out))
    c4 = next(c for c in view["comments"] if c["id"] == "4")
    reply_ok = c4["done"] and len(c4["replies"]) == 1
    ctx = context(folder, "accept", "Make a clean copy of test_plan.docx with B. Checker's changes accepted and the "
                                    "rest rejected.")
    out = call(AcceptChangesTool(), ctx, {"path": DEMO, "which": "author:B. Checker", "action": "accept"})
    stage_seconds(ctx, times)
    clean = wm.texts(dm.Package(docx_of(out)).document, wm.KEEP) == KEY["texts"]["b_checker_accepted_rest_rejected"]
    return reply_ok, clean


def report(folder, census, invariants, compare, others, times, seconds):
    rows, threads, word = census
    results, batch = invariants
    exact = sum(key == got for _, key, got in rows)
    lines = ["# The Word review tools on workspace/review_demo", "",
             f"Answer key: review_demo_answers.json. Word: {'used' if USE_WORD else 'not used'}. "
             f"Total {seconds:.1f} s.", "", "## Census (read_review)", "",
             f"{exact}/{len(rows)} counts exact.", "", "| count | key | read_review |", "|---|---|---|"]
    lines += [f"| {k} | {key} | {got} |" for k, key, got in rows]
    lines += ["", "| thread | key: done, replies | read: done, replies |", "|---|---|---|"]
    lines += [f"| {label} | {done}, {replies} | {got_done}, {got_replies} |"
              for label, done, replies, got_done, got_replies in threads]
    if word:
        lines += ["", f"Word: {word['revisions']} revisions, {word['comments']} comments, {word['pages']} pages "
                      f"(key: {KEY['census']['changes']}, {KEY['census']['comments']}); Word's accept-all = key: "
                      f"{word_lines(word['accepted']) == [x for x in KEY['texts']['accepted'] if x]}; reject-all = "
                      f"key: {word_lines(word['rejected']) == [x for x in KEY['texts']['original'] if x]}"]
    as_expected = sum(r["expected"] == r["got"] for r in results)
    verified = [r for r in results if r["got"] == "verified"]
    lines += ["", "## Invariants on 21 scripted edits (redline_docx, one call each)", "",
              f"{as_expected}/{len(results)} ended as the key expects; of the {len(verified)} verified, rejecting "
              f"gives the original back in {sum(bool(r['reject_ok']) for r in verified)}, accepting gives the "
              f"intended text in {sum(bool(r['accept_ok']) for r in verified if r['accept_ok'] is not None)}/"
              f"{sum(r['accept_ok'] is not None for r in verified)} (find/replace edits; the others are checked "
              "inside the tool).", "", "| # | edit | key | result | reject = original | accept = intended | s |",
              "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['n']} | {r['what']} | {r['expected']} | {r['got']} | {r['reject_ok']} | {r['accept_ok']} | "
              f"{r['seconds']:.2f} |" for r in results]
    lines += ["", f"All 21 in one call: {batch['summary']}. Reject = original: {batch['reject_ok']}. "
                  f"{batch['word_check']}." + (f" Word's accept-all and reject-all = the reader's: "
                                               f"{batch['word_texts_ok']}." if "word_texts_ok" in batch else ""),
              "", "## Compare (v1 and the planted v2)", "",
              "| method | accept = v2 | reject = v1 | differences found | changes | s |", "|---|---|---|---|---|---|"]
    for c in compare.values():
        lines.append(f"| {c['method']} | {c['accepted_is_v2']} | {c['rejected_is_v1']} | "
                     f"{sum(c['found'])}/{len(c['found'])} | {c['changes']} | {c['seconds']:.1f} |")
    lines += ["", "## Requests 3 and 4", "", f"- reply to c4 and resolve it: {others[0]}",
              f"- clean copy, B. Checker's changes accepted and the rest rejected = key: {others[1]}", "",
              "## Time per stage (all runs)", ""]
    lines += [f"- {k}: {v:.2f} s" for k, v in sorted(times.items(), key=lambda kv: -kv[1])[:12]]
    path = folder / "comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    folder = OUTPUT_DIR / time.strftime("review_comparison_%Y%m%d_%H%M%S")
    folder.mkdir(parents=True)
    times = defaultdict(float)
    start = time.perf_counter()
    saved = dict(OFFICE)
    OFFICE["word_check"] = False  # each single edit without Word; the batch and compare runs switch it on
    try:
        census = census_section(folder, times)
        print(f"census: {sum(k == g for _, k, g in census[0])}/{len(census[0])} exact", flush=True)
        invariants = invariants_section(folder, times)
        print(f"invariants: {sum(r['expected'] == r['got'] for r in invariants[0])}/{len(EDITS)} as expected; "
              f"batch: {invariants[1]['summary']}", flush=True)
        compare = compare_section(folder, times)
        print("compare: " + "; ".join(f"{k} {sum(v['found'])}/{len(v['found'])}" for k, v in compare.items()),
              flush=True)
        others = other_tools(folder, times)
    finally:
        OFFICE.update(saved)
    path = report(folder, census, invariants, compare, others, times, time.perf_counter() - start)
    print(path.read_text(encoding="utf-8"))
    print(f"saved: {path}")


if __name__ == "__main__":
    main()
