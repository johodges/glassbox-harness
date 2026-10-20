"""word_review.py, the five tools, on the demo plan (workspace/review_demo/, made by workspace/make_review_demo.py) and
scored against review_demo_answers.json: requests 1-5 of OFFICE_COLLAB_DESIGN.md W1 with the arguments a planner
sends, the shapes small models write, CHECK ARGS, refusals, approval, the version ledger, and that no input is ever
written. No GPU, no LLM; Word is switched off here (test_word_com.py has the Word tests)."""

import json
import re
import shutil

import pytest

from harness.config import OFFICE, PROJECT_DIR
from harness.tools import docx_markup as dm
from harness.tools import versions
from harness.tools import word_model as wm
from harness.tools.base import Refusal
from harness.tools.office_common import person, review_author
from harness.tools.versions import Versions, sha256
from harness.tools.word_review import (AcceptChangesTool, CommentDocxTool, CompareDocxTool, ReadReviewTool,
                                       RedlineDocxTool, version_number)

DEMO = "workspace/review_demo/test_plan.docx"
V2 = "workspace/review_demo/test_plan_v2.docx"
KEY = json.loads((PROJECT_DIR / "review_demo_answers.json").read_text(encoding="utf-8"))


@pytest.fixture
def wctx(ctx, tmp_path, monkeypatch):
    """The conftest ctx, with its version ledger in the temporary folder, Word off and compare in Python. A ledger is
    named after the file's path from the project folder; pytest's folder is outside it, and its whole path as a name
    would pass Windows' 260-character limit, so here a ledger is named after the file alone."""
    ctx.facts["versions_dir"] = str(tmp_path / "versions")
    monkeypatch.setitem(OFFICE, "word_check", False)
    monkeypatch.setitem(OFFICE, "compare", "python")
    monkeypatch.setattr(versions, "document_key",
                        lambda path, ctx: re.sub(r"[^A-Za-z0-9]+", "_", versions.original_of(path, ctx).name))
    return ctx


def call(tool, ctx, request, args):
    """What agent.call_tool does: VALIDATE ARGS, CHECK ARGS, run."""
    ctx.request = request
    args = tool.validate(args)
    if hasattr(tool, "check_request"):
        args, _, _ = tool.check_request(args, ctx)
    return tool.run(ctx, **args)


def written(out, kind="word_review"):
    return next(a["path"] for a in out["artifacts"] if a["type"] == kind and str(a["path"]).endswith(".docx"))


def test_read_review_gives_the_census_of_the_key(wctx):
    before = sha256(PROJECT_DIR / DEMO)
    out = call(ReadReviewTool(), wctx, "Which comments in workspace/review_demo/test_plan.docx are still open, and "
                                      "who made the tracked changes?", {"path": DEMO})
    o = out["observations"]
    assert "10 tracked change(s)" in o["census"] and "A. Reviewer 7" in o["census"] and "B. Checker 3" in o["census"]
    assert [t.split()[0] for t in o["open_threads"]] == ["c1", "c2", "c4"]
    assert o["resolved_threads"][0].startswith("c3")
    view = wm.docx_view(PROJECT_DIR / DEMO)
    for key in ("changes", "by_author", "comments", "threads", "open", "resolved", "replies", "comments_by_author"):
        assert view["census"][key] == KEY["census"][key], key
    assert view["census"]["by_author_kind"] == KEY["census"]["by_author_kind"]
    assert sha256(PROJECT_DIR / DEMO) == before and wctx.notes  # read only; the census goes under the answer


def test_read_review_shows_a_section_the_request_names(wctx):
    out = call(ReadReviewTool(), wctx, "Show me the review state of the Procedure section of test_plan.docx",
               {"path": DEMO})
    section = out["observations"]["section"]
    assert section["heading"] == "6 Procedure"
    assert any("{-approximately|AR}{+about|AR}" in line for line in section["marked"])


def test_redline_request_two_is_verified(wctx):
    request = ('In workspace/review_demo/test_plan.docx, with track changes, change "within 5 minutes" to "within 3 '
               'minutes" and add a comment that the sprinkler data supports it.')
    out = call(RedlineDocxTool(), wctx, request, {"path": DEMO, "edits": [
        {"find": "within 5 minutes", "replace": "within 3 minutes", "comment": "The sprinkler data supports it."}]})
    o = out["observations"]
    assert o["summary"].startswith("1 of 1 edit(s) verified")
    path = written(out)
    assert path.parent == wctx.run_dir and path.name == "test_plan_redline.docx"
    root = dm.Package(path).document
    me = review_author(wctx)
    assert me != person(wctx) and "via. Glass-box harness" in me
    accepted = [line.replace("within 5 minutes", "within 3 minutes") for line in KEY["texts"]["accepted"]]
    assert wm.texts(root, wm.ACCEPT) == accepted
    mine = wm.deciding(authors=[me], action="reject", others="keep")
    assert wm.lines(root, mine) == wm.lines(dm.Package(PROJECT_DIR / DEMO).document, wm.KEEP)
    view = wm.docx_view(path)
    assert view["census"]["by_author"][me] == 2 and view["census"]["changes"] == 12
    comment = next(c for c in view["comments"] if c["author"] == me)
    assert comment["text"] == "The sprinkler data supports it." and "3" in comment["anchor"]
    assert view["settings"]["track_changes"]


def test_redline_takes_the_users_quoted_words(wctx):
    tool = RedlineDocxTool()
    wctx.request = 'With track changes, change "within 5 minutes" to "within 3 minutes" in test_plan.docx.'
    args = tool.validate({"path": DEMO, "edits": [{"find": "5 minutes", "replace": "3 min"}]})
    args, _, note = tool.check_request(args, wctx)
    assert (args["edits"][0]["find"], args["edits"][0]["replace"]) == ("within 5 minutes", "within 3 minutes")
    assert note


@pytest.mark.parametrize("edits, expected", [
    ('[{"find": "a", "replace": "b"}]', [("a", "b")]),
    (["within 5 minutes -> within 3 minutes"], [("within 5 minutes", "within 3 minutes")]),
    ('{"old": "x", "new": "y", "comment": "why"}', [("x", "y")]),
    (['{"find": "p", "replace": "q"}', "r => s"], [("p", "q"), ("r", "s")]),
])
def test_redline_accepts_the_shapes_small_models_write(edits, expected):
    args = RedlineDocxTool().validate({"path": DEMO, "edits": edits})
    assert [(e["find"], e["replace"]) for e in args["edits"]] == expected


def test_redline_accepts_a_single_top_level_pair():
    args = RedlineDocxTool().validate({"path": DEMO, "find": "a", "replace": "b", "comment": "c"})
    assert args["edits"] == [{"op": "replace", "find": "a", "replace": "b", "comment": "c"}]
    args = RedlineDocxTool().validate({"path": DEMO, "edits": [{"insert_after": "x.", "text": "New one."}]})
    assert args["edits"][0] == {"op": "insert", "after": "x.", "paragraphs": ["New one."], "comment": ""}


def test_an_edit_in_someone_elses_change_needs_a_person(wctx):
    request = "Fix the wording in test_plan.docx with track changes."
    out = call(RedlineDocxTool(), wctx, request, {"path": DEMO, "edits": [
        {"find": "three thermocouple trees", "replace": "four thermocouple trees"},
        {"find": "Data are logged", "replace": "Data are stored"}]})
    o = out["observations"]
    assert "1 of 2 edit(s) verified" in o["summary"] and "1 need a person" in o["summary"]
    assert any("A. Reviewer" in line and line.startswith("needs a person") for line in o["edits"])


def test_an_ambiguous_or_missing_text_is_refused_with_its_places(wctx):
    with pytest.raises(Refusal) as refused:
        call(RedlineDocxTool(), wctx, "x", {"path": DEMO, "edits": [{"find": "the doorway", "replace": "the door"},
                                                                     {"find": "no such words", "replace": "x"}]})
    assert "occurs 5 times" in str(refused.value) and "¶" in str(refused.value)
    assert "not in the document" in str(refused.value)
    with pytest.raises(Refusal) as refused:  # the closest words are named, so the LLM can copy them exactly
        call(RedlineDocxTool(), wctx, "x", {"path": DEMO, "edits": [{"find": "within five minutes", "replace": "x"}]})
    assert 'closest text is ¶38: "within 5 minutes' in str(refused.value)


def test_paragraph_and_row_edits(wctx):
    out = call(RedlineDocxTool(), wctx, "With track changes, in test_plan.docx ...", {"path": DEMO, "edits": [
        {"insert_after": "safety glasses are worn in the burn hall while a test runs.",
         "paragraphs": ["Visitors stay behind the barrier."]},
        {"delete_paragraph": "The heat release rate from the hood must agree"},
        {"table_row_after": "Propane burner", "cells": ["4", "Wood crib, 40 kg", "700 kW", "Door open"]},
        {"delete_row": "Wood crib, 30 kg"}]})
    assert out["observations"]["summary"].startswith("4 of 4 edit(s) verified")
    accepted = wm.texts(dm.Package(written(out)).document, wm.ACCEPT)
    assert "Visitors stay behind the barrier." in accepted and not any("must agree with" in x for x in accepted)
    assert "Wood crib, 30 kg" not in accepted and accepted[accepted.index("700 kW") - 2] == "4"


def test_reply_to_comment_four_and_resolve_it(wctx):
    request = ("Reply to comment 4 in workspace/review_demo/test_plan.docx that the duct sizes are now in Table 1, and "
               "mark it resolved.")
    out = call(CommentDocxTool(), wctx, request, {"path": DEMO, "comments": [
        {"reply_to": "4", "text": "The duct sizes are now in Table 1."}]})  # CHECK ARGS adds the resolve
    o = out["observations"]
    assert "c4 resolved" in o["summary"] and "to c4" in o["summary"]
    view = wm.docx_view(written(out))
    c4 = next(c for c in view["comments"] if c["id"] == "4")
    reply = next(c for c in view["comments"] if c["parent"] == "4")
    assert c4["done"] and reply["author"] == review_author(wctx)
    assert reply["text"] == "The duct sizes are now in Table 1."
    assert (view["census"]["open"], view["census"]["resolved"], view["census"]["replies"]) == (2, 2, 3)
    assert wm.texts(dm.Package(written(out)).document, wm.KEEP) == \
        wm.texts(dm.Package(PROJECT_DIR / DEMO).document, wm.KEEP)  # comments never change the text


def test_comment_shapes_and_a_new_comment(wctx):
    tool = CommentDocxTool()
    args = tool.validate({"path": DEMO, "comments": '[{"reply_to": "c2", "text": "0.5 mm", "resolve": true}]'})
    assert args["comments"] == [{"op": "reply", "to": "c2", "text": "0.5 mm", "resolve": True}]
    assert tool.validate({"path": DEMO, "comments": ["resolve c3"]})["comments"] == [{"op": "resolve", "to": "c3"}]
    out = call(tool, wctx, "Add a comment on eight heights in test_plan.docx", {"path": DEMO, "comments": [
        {"on": "eight heights", "text": "Is eight enough?"}]})
    view = wm.docx_view(written(out))
    new = next(c for c in view["comments"] if c["text"] == "Is eight enough?")
    assert new["anchor"] == "eight heights" and view["census"]["threads"] == 5


def test_clean_copy_with_b_checkers_changes(wctx):
    request = ("Make a clean copy of workspace/review_demo/test_plan.docx with B. Checker's changes accepted and the "
               "rest rejected.")
    out = call(AcceptChangesTool(), wctx, request, {"path": DEMO, "which": "all", "action": "accept"})
    o = out["observations"]
    assert "B. Checker" in o["summary"] and "the rest rejected" in o["summary"]
    root = dm.Package(written(out)).document
    assert wm.texts(root, wm.KEEP) == KEY["texts"]["b_checker_accepted_rest_rejected"]
    assert not list(root.iter(*dm.REVISION_TAGS))
    assert wm.docx_view(written(out))["census"]["comments"] == 6  # comments kept unless asked


def test_accept_names_the_authors_when_one_is_unknown(wctx):
    with pytest.raises(Refusal, match="A. Reviewer, B. Checker"):
        call(AcceptChangesTool(), wctx, "Accept C. Nobody's changes", {"path": DEMO, "which": "author:C. Nobody"})


def test_accepting_others_changes_asks_a_person(wctx, monkeypatch):
    wctx.config.ask_before_acting = True
    monkeypatch.setattr("builtins.input", lambda question: "n")
    with pytest.raises(Refusal, match="said no"):
        call(AcceptChangesTool(), wctx, "Accept all changes in test_plan.docx", {"path": DEMO, "which": "all"})
    assert not list(wctx.run_dir.glob("*.docx"))


def test_mine_means_the_harness_authors_only(wctx):
    redline = call(RedlineDocxTool(), wctx, "x", {"path": DEMO, "edits": [{"find": "Data are logged",
                                                                           "replace": "Data are stored"}]})
    path = written(redline)
    out = call(AcceptChangesTool(), wctx, "Reject your own changes", {"path": str(path), "which": "mine",
                                                                      "action": "reject"})
    root = dm.Package(written(out)).document
    assert wm.docx_view(written(out))["census"]["by_author"] == KEY["census"]["by_author"]
    assert wm.texts(root, wm.KEEP) == wm.texts(dm.Package(PROJECT_DIR / DEMO).document, wm.KEEP)


def test_compare_in_python_matches_the_key(wctx):
    out = call(CompareDocxTool(), wctx, "Compare workspace/review_demo/test_plan_v2.docx with test_plan.docx, with "
                                        "track changes.", {"original": V2, "revised": DEMO})  # swapped by CHECK ARGS
    o = out["observations"]
    assert o["revised"].endswith("test_plan_v2.docx") and o["method"].startswith("a Python word diff")
    root = dm.Package(written(out)).document
    assert wm.texts(root, wm.ACCEPT) == KEY["texts"]["v2"]
    assert wm.texts(root, wm.REJECT) == KEY["texts"]["accepted"]  # v1 with its own changes accepted
    assert "verified" in o["summary"] and o["changes"]


def test_version_numbers_and_compare_from_the_ledger(wctx):
    assert (version_number("test_plan_v2.docx"), version_number("test_plan.docx"), version_number("x version 5.docx")) \
        == (2, 1, 5)
    first = call(RedlineDocxTool(), wctx, "x", {"path": DEMO, "edits": [{"find": "Data are logged",
                                                                         "replace": "Data are stored"}]})
    ours = written(first)
    ledger = Versions.of(ours, wctx)
    assert [v["origin"] for v in ledger.versions] == ["baseline", "harness"]
    pkg = dm.Package(ours)  # the user opens the harness's redline, edits it, and saves it in place
    p = next(p for p in wm.paragraphs_of(pkg.document) if "Hearing protection" in wm.accepted_text(p))
    t = p.find(wm.w("r")).find(wm.w("t"))
    t.text = t.text.replace("Hearing", "Ear")
    pkg.touch(pkg.names["document"])
    pkg.save(ours)
    out = call(CompareDocxTool(), wctx, "What did I change since your last version?", {"revised": str(ours)})
    o = out["observations"]
    assert len(o["changes"]) == 1 and "{-Hearing|GBH}{+Ear|GBH}" in o["changes"][0]


def test_a_file_open_in_word_is_read_from_a_copy_and_never_written(wctx):
    source = wctx.run_dir / "plan.docx"
    shutil.copy2(PROJECT_DIR / DEMO, source)
    (wctx.run_dir / "~$plan.docx").write_bytes(b"lock")
    before = sha256(source)
    out = call(RedlineDocxTool(), wctx, "x", {"path": str(source), "edits": [{"find": "Data are logged",
                                                                              "replace": "Data are stored"}]})
    assert sha256(source) == before and written(out) != source
    assert (wctx.run_dir / "inputs" / "open_in_word_plan.docx").is_file()


def test_redline_asks_for_read_review_first_when_the_request_is_about_comments(wctx):
    tool = RedlineDocxTool()
    wctx.request = "Address the open comments in test_plan.docx with track changes."
    tool.prepare(wctx)
    assert tool.requires == ("read_review",)
    wctx.request = 'With track changes change "a" to "b" in test_plan.docx'
    tool.prepare(wctx)
    assert tool.requires == ()
