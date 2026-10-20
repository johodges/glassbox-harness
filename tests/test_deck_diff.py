"""deck_edit.py's diff_decks: slides matched by content, so a deleted slide shows as deleted and a moved one as moved,
whatever the part names; the demo's six planted edits found and nothing else; old and new taken from the user's words;
and (with PowerPoint) a deck PowerPoint saved again shows no change. No GPU, no LLM."""

import json
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.oxml.ns import qn
from test_deck_model import DEMO, small_deck

from harness.tools.deck_check import powerpoint_available, powerpoint_job
from harness.tools.deck_edit import DiffDecksTool, align, diff_views
from harness.tools.deck_model import deck_view

ANSWERS = json.loads((Path(__file__).resolve().parent.parent / "deck_demo_answers.json").read_text(encoding="utf-8"))
needs_powerpoint = pytest.mark.skipif(not powerpoint_available(), reason="needs PowerPoint (Windows, COM)")


def matches(item, planted):
    return item["kind"] == planted["kind"] and item["title"] == planted["slide"]


def test_a_deleted_slide_is_deleted_and_a_moved_one_moved(tmp_path):
    deck = small_deck(tmp_path / "six.pptx", slides=6)
    prs = Presentation(deck)
    ids = prs.slides._sldIdLst
    entries = list(ids)
    ids.remove(entries[1])               # delete slide 2
    prs.part.drop_rel(entries[1].get(qn("r:id")))
    ids.remove(entries[4])               # move slide 5 to the front
    ids.insert(0, entries[4])
    prs.part.rename_slide_parts([e.get(qn("r:id")) for e in ids])
    prs.save(tmp_path / "new.pptx")
    old, new = deck_view(deck), deck_view(tmp_path / "new.pptx")
    pairs, moved, deleted, added = align(old["slides"], new["slides"])
    assert moved == [(4, 0)] and deleted == [1] and added == []
    assert pairs == [(0, 1), (2, 2), (3, 3), (5, 4)]
    diff = diff_views(old, new)
    assert diff["summary"].startswith("1 slide(s) moved, 1 deleted, 0 added, 0 changed")


def test_the_demo_diff_finds_the_six_planted_edits_and_nothing_else(ctx, tmp_path):
    ctx.facts["versions_dir"] = str(tmp_path / "versions")
    out = DiffDecksTool().run(ctx, old=ANSWERS["decks"]["original"], new=ANSWERS["decks"]["edited"])
    diff = diff_views(deck_view(DEMO / "briefing.pptx"), deck_view(DEMO / "briefing_edited.pptx"))
    found = {p["id"] for p in ANSWERS["planted_edits"] for item in diff["items"] if matches(item, p)}
    extra = [item for item in diff["items"] if not any(matches(item, p) for p in ANSWERS["planted_edits"])]
    assert found == {1, 2, 3, 4, 5, 6} and extra == []
    text = next(item["text"] for item in diff["items"] if item["kind"] == "text")
    assert "**only**" in text and "bold added: only" in text
    assert "Instrumentation" in out["observations"]["moved"][0]
    assert Path(out["observations"]["report"]).is_file()


def test_old_and_new_come_from_the_users_words(ctx):
    tool = DiffDecksTool()
    ctx.request = "What did I change in workspace/deck_demo/briefing_edited.pptx compared with briefing.pptx?"
    args, summary, note = tool.check_request({"old": "workspace/deck_demo/briefing_edited.pptx",
                                              "new": "workspace/deck_demo/briefing.pptx"}, ctx)
    assert (args["old"], args["new"]) == ("briefing.pptx", "workspace/deck_demo/briefing_edited.pptx")
    assert "SWAPPED" in summary and note
    ctx.request = "Show the changes from workspace/deck_demo/briefing.pptx to workspace/deck_demo/briefing_edited.pptx"
    args, _, note = tool.check_request({"old": "workspace/deck_demo/briefing.pptx",
                                        "new": "workspace/deck_demo/briefing_edited.pptx"}, ctx)
    assert note is None


@needs_powerpoint
def test_a_deck_saved_again_by_powerpoint_shows_no_change(tmp_path):
    """PowerPoint renumbers parts and rewrites the XML (and the charts) on save: the diff, by content, sees nothing."""
    for name in ("briefing", "results"):
        copy = tmp_path / f"{name}_resaved.pptx"
        powerpoint_job("save", DEMO / f"{name}.pptx", out=str(copy), format=24)
        diff = diff_views(deck_view(DEMO / f"{name}.pptx"), deck_view(copy))
        assert diff["items"] == [], diff["items"]
