"""word_com.py, Word through COM (OFFICE_COLLAB_DESIGN.md, F5), and the WORD CHECK of the tools' outputs: Word opens
each file without repairing it and counts the same revisions and comments as the census. The tests that need Word are
skipped without Windows, pywin32 or Word; the first two run anywhere. Each Word job takes 5-15 s."""

import json
import time

import pytest

from harness.config import OFFICE, PROJECT_DIR
from harness.tools import docx_markup as dm
from harness.tools import word_com
from harness.tools import word_model as wm

DEMO = PROJECT_DIR / "workspace" / "review_demo" / "test_plan.docx"
V2 = PROJECT_DIR / "workspace" / "review_demo" / "test_plan_v2.docx"
KEY = json.loads((PROJECT_DIR / "review_demo_answers.json").read_text(encoding="utf-8"))
needs_word = pytest.mark.skipif(not word_com.available()[0], reason=f"needs Word: {word_com.available()[1]}")


def test_without_word_every_job_says_why(monkeypatch, tmp_path):
    monkeypatch.setattr(word_com, "available", lambda: (False, "Word is not installed"))
    assert word_com.open_check(DEMO) == (None, "Word is not installed")
    assert word_com.compare(DEMO, V2, tmp_path / "x.docx", "me") == (None, "Word is not installed")
    assert word_com.pdf(DEMO, tmp_path / "x.pdf") == (None, "Word is not installed")


def test_available_names_the_reason(monkeypatch):
    monkeypatch.setattr(word_com.os, "name", "posix")
    ok, why = word_com.available()
    assert not ok and "Windows" in why


@needs_word
def test_word_opens_the_demo_and_counts_the_census():
    result, why = word_com.open_check(DEMO, texts=True)
    assert result is not None, why
    census = KEY["census"]
    assert (result["revisions"], result["comments"], result["pages"]) == (census["changes"], census["comments"], 3)
    assert sorted(result["authors"]) == ["A. Reviewer", "B. Checker"]
    # Word's own accept-all and reject-all give the key's texts (Word ends cells with \x07 and paragraphs with \r)
    words = lambda text: [x for x in text.replace("\x07", "\r").split("\r") if x]
    assert words(result["accepted"]) == [x for x in KEY["texts"]["accepted"] if x]
    assert words(result["rejected"]) == [x for x in KEY["texts"]["original"] if x]


@needs_word
def test_word_compare_passes_the_same_check_as_python(tmp_path):
    out = tmp_path / "compared.docx"
    result, why = word_com.compare(DEMO, V2, out, "Test author, via. Glass-box harness (for Test Person)")
    assert result is not None, why
    root = dm.Package(out).document
    assert wm.texts(root, wm.ACCEPT) == KEY["texts"]["v2"]
    assert wm.texts(root, wm.REJECT) == KEY["texts"]["accepted"]
    assert result["authors"] == ["Test author, via. Glass-box harness (for Test Person)"]
    assert result["revisions"] == wm.docx_view(out)["census"]["changes"]


@needs_word
def test_a_job_that_runs_too_long_stops_only_its_own_word(tmp_path):
    before = word_com.word_pids()
    result, why = word_com.open_check(DEMO, timeout=1)  # Word can't even start in a second
    assert result is None and "did not finish within 1 s" in why
    time.sleep(2)
    assert word_com.word_pids() <= before  # its Word is gone; any Word that was running before is untouched


@needs_word
def test_the_input_is_never_opened_by_word_itself(tmp_path):
    source = tmp_path / "plan.docx"
    source.write_bytes(DEMO.read_bytes())
    (tmp_path / "~$plan.docx").write_bytes(b"lock")  # the user has it open in Word
    result, why = word_com.open_check(source)
    assert result is not None, why
    assert result["notes"] == ["plan.docx is open in Word: a copy was read"]
    assert source.read_bytes() == DEMO.read_bytes()


@needs_word
def test_a_redline_passes_the_word_check(ctx, tmp_path, monkeypatch):
    from harness.tools.word_review import RedlineDocxTool
    ctx.facts["versions_dir"] = str(tmp_path / "versions")
    monkeypatch.setitem(OFFICE, "word_check", True)
    ctx.request = 'With track changes, change "within 5 minutes" to "within 3 minutes" in test_plan.docx'
    out = RedlineDocxTool().run(ctx, "workspace/review_demo/test_plan.docx",
                                [{"op": "replace", "find": "within 5 minutes", "replace": "within 3 minutes",
                                  "comment": "Faster."}])
    assert out["observations"]["word_check"].startswith("Word opened it without repair: 12 revision(s), 7 comment(s)")
    assert "(= the census)" in out["observations"]["word_check"]
    assert any(str(a["path"]).endswith(".pdf") for a in out["artifacts"])
