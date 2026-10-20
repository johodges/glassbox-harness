"""check_deck and deck_check.py: the template profile and its keep-out zones, the fit estimate, references a delete
made stale, terms; the PowerPoint jobs in a child process (F5); and, with PowerPoint, the demo's two planted problems
found and no false alarm. No GPU, no LLM; tests marked needs_powerpoint are skipped without PowerPoint."""

from pathlib import Path

import pytest
from test_deck_model import DEMO

from harness.tools import deck_check, deck_edit
from harness.tools.deck_check import (PowerPointUnavailable, estimated_fit, keep_out_problems, powerpoint_available,
                                      powerpoint_job, powerpoint_pids, profile_for, render_deck, render_slides,
                                      save_as_pdf, stale_references, term_hits)
from harness.tools.deck_edit import CheckDeckTool, EditDeckTool
from harness.tools.deck_model import deck_view

needs_powerpoint = pytest.mark.skipif(not powerpoint_available(), reason="needs PowerPoint (Windows, COM)")
BRIEFING = "workspace/deck_demo/briefing.pptx"


@pytest.fixture
def deck_ctx(ctx, tmp_path):
    ctx.facts["versions_dir"] = str(tmp_path / "versions")
    return ctx


def test_the_profile_is_matched_by_the_templates_layouts():
    profile, how = profile_for(deck_view(DEMO / "briefing.pptx")["template"])
    assert profile["template"] == "workshop_template" and "Section Header Light" in how
    assert profile_for(deck_view(DEMO / "results.pptx")["template"])[0] is None  # the plain Office look: no profile


def test_keep_out_zones_find_the_logo_overlap_and_nothing_else():
    view = deck_view(DEMO / "briefing.pptx")
    profile, _ = profile_for(view["template"])
    problems = keep_out_problems(view["slides"], profile)
    assert [(p["slide"], p["shape"], p["zones"]) for p in problems] == [(6, "Picture 2", ["logo", "footer rule"])]


def test_the_estimate_flags_only_the_planted_overflow():
    fits = estimated_fit(DEMO / "briefing.pptx", set(range(1, 9)))
    assert [(n, name) for (n, _), (name, ratio) in fits.items() if ratio > 1.0] == [(5, "Content Placeholder 2")]


def test_stale_references_and_terms():
    view = deck_view(DEMO / "briefing.pptx")
    before = {"order": [s["id"] for s in view["slides"]], "titles": {s["id"]: s["title"] for s in view["slides"]}}
    after = [s for s in view["slides"] if s["title"] != "Peak values by test"]  # slide 7 deleted
    for position, s in enumerate(after, 1):
        s["position"] = position
    stale = stale_references(after, before)
    assert len(stale) == 1 and stale[0]["slide"] == 3 and "now deleted" in stale[0]["text"]
    assert stale_references(view["slides"], before) == []  # nothing moved: "slide 7" is still right
    hits = term_hits(view["slides"], ["flashover", "ten-year plan"])
    assert {h["term"] for h in hits} == {"flashover"} and len(hits) >= 4


def test_check_deck_without_powerpoint_says_so_and_estimates(deck_ctx, monkeypatch):
    def absent(*args, **kwargs):
        raise PowerPointUnavailable("PowerPoint is in use")

    monkeypatch.setattr(deck_edit, "render_slides", absent)
    out = CheckDeckTool().run(deck_ctx, BRIEFING)["observations"]
    assert out["rendered"] == "not rendered: PowerPoint is in use" and out["checked"] == "every slide"
    assert any("an estimate" in p and "slide 5" in p for p in out["problems"])
    assert any("slide 6" in p and "logo" in p for p in out["problems"]) and len(out["problems"]) == 2


def test_check_after_a_delete_lists_the_stale_reference(deck_ctx, monkeypatch):
    monkeypatch.setattr(deck_edit, "render_slides", lambda *a, **k: (_ for _ in ()).throw(PowerPointUnavailable("x")))
    tool = EditDeckTool()
    tool.run(deck_ctx, **tool.validate({"path": BRIEFING, "ops": [{"op": "delete", "slide": 7}]}))
    out = CheckDeckTool().run(deck_ctx, BRIEFING, terms=["peak values"])["observations"]
    assert "edited" in out["deck"] and "now deleted" in out["stale_references"][0]
    assert any("peak values" in t.lower() for t in out["terms"])


def test_a_powerpoint_job_without_powerpoint_raises(monkeypatch):
    monkeypatch.setattr(deck_check, "powerpoint_available", lambda: False)
    with pytest.raises(PowerPointUnavailable, match="not installed"):
        powerpoint_job("count", DEMO / "briefing.pptx")


def test_a_hung_job_is_stopped_after_the_timeout(monkeypatch):
    """F5: every COM job runs in a child process with a time limit: a child that hangs is stopped, and the harness
    goes on."""
    monkeypatch.setattr(deck_check, "powerpoint_available", lambda: True)
    monkeypatch.setattr(deck_check, "CHILD", "import time; time.sleep(60)")
    with pytest.raises(PowerPointUnavailable, match="did not finish within 2 s"):
        powerpoint_job("count", DEMO / "briefing.pptx", timeout=2)


@needs_powerpoint
def test_check_deck_finds_the_planted_problems_with_powerpoint(deck_ctx):
    running = powerpoint_pids()
    out = CheckDeckTool().run(deck_ctx, BRIEFING)["observations"]
    assert out["rendered"] == "8 slide(s) drawn by PowerPoint" and "slide_count" not in out
    assert len(out["problems"]) == 2
    assert "slide 5" in out["problems"][0] and "measured by PowerPoint" in out["problems"][0]
    assert "slide 6" in out["problems"][1] and "logo" in out["problems"][1]
    assert powerpoint_pids() == running  # F5: PowerPoint is left as it was found (not started, not quit)


@needs_powerpoint
def test_render_deck_and_save_as_pdf_keep_their_interface(tmp_path):
    """perception.py's RENDER and export_pdf / convert_presentation use these two, now through the child process."""
    pngs, measured = render_deck(DEMO / "results.pptx", tmp_path / "slides")
    assert [p.name for p in pngs] == ["slide_01.png", "slide_02.png", "slide_03.png"]
    assert measured and all(isinstance(k, tuple) and len(k) == 2 for k in measured)
    assert save_as_pdf((DEMO / "results.pptx").resolve(), (tmp_path / "results.pdf").resolve()) == "PowerPoint (COM)"
    assert (tmp_path / "results.pdf").read_bytes()[:4] == b"%PDF"


@needs_powerpoint
def test_render_measures_a_table_as_powerpoint_lays_it_out(tmp_path):
    result = render_slides(DEMO / "briefing.pptx", tmp_path / "slides", slides=[7])
    assert result["slide_count"] == 8 and [p.name for p in result["pngs"]] == ["slide_07.png"]
    table = next(box for (n, _), box in result["boxes"].items() if result["names"][(n, _)].startswith("Table"))
    assert table[3] > 6.3  # 7 rows of 0.9 cm in the file; PowerPoint grows them to fit the text
    assert Path(result["pngs"][0]).stat().st_size > 1000
