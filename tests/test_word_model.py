"""word_model.py: the census, threads, the three text forms and lines() on a hand-written .docx whose answers are known
(OFFICE_COLLAB_DESIGN.md, W9: 'the census on a hand-written XML fixture'). No GPU, no LLM, no Word."""

import zipfile

from harness.tools import word_model as wm

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
W15 = 'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"'
A = 'w:author="A. Reviewer" w:date="2026-10-02T10:00:00Z"'
B = 'w:author="B. Checker" w:date="2026-10-03T10:00:00Z"'

BODY = f"""
<w:p><w:r><w:t xml:space="preserve">The fire </w:t></w:r>
  <w:ins w:id="1" {A}><w:r><w:t>grew quickly</w:t></w:r></w:ins>
  <w:del w:id="2" {A}><w:r><w:delText>spread</w:delText></w:r></w:del>
  <w:r><w:t xml:space="preserve"> in </w:t></w:r>
  <w:commentRangeStart w:id="10"/><w:r><w:t>the room</w:t></w:r><w:commentRangeEnd w:id="10"/>
  <w:r><w:commentReference w:id="10"/></w:r><w:r><w:commentReference w:id="11"/></w:r>
  <w:r><w:t>.</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:b/><w:rPrChange w:id="3" {B}><w:rPr/></w:rPrChange></w:rPr><w:t>Hot</w:t></w:r>
  <w:r><w:t xml:space="preserve"> layer</w:t></w:r></w:p>
<w:p><w:pPr><w:rPr><w:ins w:id="5" {A}/></w:rPr></w:pPr>
  <w:ins w:id="4" {A}><w:r><w:t>New paragraph.</w:t></w:r></w:ins></w:p>
<w:p><w:r><w:t>Before the table.</w:t></w:r></w:p>
<w:tbl><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>
  <w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:trPr><w:ins w:id="6" {B}/></w:trPr>
    <w:tc><w:p><w:pPr><w:rPr><w:ins w:id="9" {B}/></w:rPr></w:pPr>
      <w:ins w:id="7" {B}><w:r><w:t>c</w:t></w:r></w:ins></w:p></w:tc>
    <w:tc><w:p><w:pPr><w:rPr><w:ins w:id="12" {B}/></w:rPr></w:pPr>
      <w:ins w:id="8" {B}><w:r><w:t>d</w:t></w:r></w:ins></w:p></w:tc>
  </w:tr></w:tbl>
<w:p><w:r><w:t>End.</w:t></w:r></w:p>
"""
COMMENTS = f"""<w:comments {W} {W14}>
<w:comment w:id="10" w:author="A. Reviewer" w:initials="AR" w:date="2026-10-02T10:00:00Z">
  <w:p w14:paraId="00000001"><w:r><w:annotationRef/></w:r><w:r><w:t>Which room?</w:t></w:r></w:p></w:comment>
<w:comment w:id="11" w:author="B. Checker" w:initials="BC" w:date="2026-10-03T10:00:00Z">
  <w:p w14:paraId="00000002"><w:r><w:annotationRef/></w:r><w:r><w:t>Room 2.</w:t></w:r></w:p></w:comment>
</w:comments>"""
EXTENDED = f"""<w15:commentsEx {W15}><w15:commentEx w15:paraId="00000001" w15:done="1"/>
<w15:commentEx w15:paraId="00000002" w15:paraIdParent="00000001" w15:done="0"/></w15:commentsEx>"""
SETTINGS = f"""<w:settings {W}><w:trackRevisions/><w:documentProtection w:edit="readOnly" w:enforcement="1"/>
</w:settings>"""


def hand_written(path, body=BODY, settings=SETTINGS):
    """A .docx written by hand, part by part: exactly the XML above, nothing python-docx adds."""
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts = {
        "[Content_Types].xml": f"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="{ct}.document.main+xml"/>
<Override PartName="/word/comments.xml" ContentType="{ct}.comments+xml"/>
<Override PartName="/word/commentsExtended.xml" ContentType="{ct}.commentsExtended+xml"/>
<Override PartName="/word/settings.xml" ContentType="{ct}.settings+xml"/></Types>""",
        "_rels/.rels": f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/></Relationships>""",
        "word/_rels/document.xml.rels": f"""<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{rel}/comments" Target="comments.xml"/>
<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
 Target="commentsExtended.xml"/>
<Relationship Id="rId3" Type="{rel}/settings" Target="settings.xml"/></Relationships>""",
        "word/document.xml": f"<w:document {W}><w:body>{body}<w:sectPr/></w:body></w:document>",
        "word/comments.xml": COMMENTS, "word/commentsExtended.xml": EXTENDED, "word/settings.xml": settings}
    with zipfile.ZipFile(path, "w") as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return path


def test_census_counts_as_word_does(tmp_path):
    view = wm.docx_view(hand_written(tmp_path / "hand.docx"))
    census = view["census"]
    # A: an insertion and a deletion side by side (2), a whole inserted paragraph, text and mark (1); B: a formatting
    # change (1) and an inserted table row with its cells' text and marks (1)
    assert census["changes"] == 5
    assert census["by_author"] == {"A. Reviewer": 3, "B. Checker": 2}
    assert census["by_author_kind"]["A. Reviewer"] == {"insertion": 2, "deletion": 1}
    assert census["by_author_kind"]["B. Checker"] == {"formatting": 1, "row inserted": 1}
    assert census["marks"] == 10
    assert (census["comments"], census["threads"], census["open"], census["resolved"], census["replies"]) == \
        (2, 1, 0, 1, 1)
    assert view["settings"] == {"track_changes": True, "protection": "readOnly"}


def test_threads_parents_done_and_anchors(tmp_path):
    view = wm.docx_view(hand_written(tmp_path / "hand.docx"))
    comments = {c["id"]: c for c in view["comments"]}
    assert comments["10"]["anchor"] == "the room" and comments["10"]["done"]
    assert comments["11"]["parent"] == "10" and comments["10"]["replies"] == ["11"]
    assert comments["11"]["anchor"] == "the room"  # a reply sits on its parent's anchor
    line = wm.thread_line(comments, "10")
    assert line == 'c10 A. Reviewer, 2026-10-02: "Which room?" on "the room" (1 reply, resolved)'


def test_three_forms_of_a_paragraph(tmp_path):
    view = wm.docx_view(hand_written(tmp_path / "hand.docx"))
    first = view["paragraphs"][0]
    assert first["accepted"] == "The fire grew quickly in the room."
    assert first["original"] == "The fire spread in the room."
    assert first["marked"] == "The fire {+grew quickly|AR}{-spread|AR} in the room [c10] [c11]."
    assert view["paragraphs"][1]["marked"] == "{~Hot|BC} layer"
    assert view["paragraphs"][2]["marked"] == "{+New paragraph.|AR}{+¶|AR}"
    assert view["tables"][0]["rows"][1][0] == {"marked": "{+c|BC}{+¶|BC}", "accepted": "c", "original": ""}
    kinds = [r["kind"] for r in view["revisions"]]
    assert kinds.count("paragraph mark inserted") == 3 and "row inserted" in kinds and "formatting" in kinds


def test_lines_join_paragraphs_and_drop_rows(tmp_path):
    root = wm.read_parts(hand_written(tmp_path / "hand.docx"))["document"]
    assert wm.texts(root, wm.ACCEPT) == ["The fire grew quickly in the room.", "Hot layer", "New paragraph.",
                                         "Before the table.", "a", "b", "c", "d", "End."]
    # rejected: the inserted paragraph's mark goes, so its (now empty) text joins the next paragraph; the row goes
    assert wm.texts(root, wm.REJECT) == ["The fire spread in the room.", "Hot layer", "Before the table.", "a", "b",
                                         "End."]
    only_b = wm.deciding(authors=["B. Checker"], action="reject", others="keep")
    assert wm.texts(root, only_b)[4:] == ["a", "b", "End."]
    keep = wm.lines(root, wm.KEEP)
    assert keep[0][2][1][1:3] == ("+", "A. Reviewer")  # the pending insertion is marked in the signature


def test_formatting_counts_in_the_signature(tmp_path):
    root = wm.read_parts(hand_written(tmp_path / "hand.docx"))["document"]
    accepted = wm.lines(root, wm.ACCEPT)[1][2]
    rejected = wm.lines(root, wm.REJECT)[1][2]
    assert accepted != rejected  # "Hot" bold when B's change is accepted, not when it is rejected
    assert [s[0] for s in accepted[:-2]] == ["Hot", " layer"]


def test_author_keys_are_initials():
    comments = {"1": {"author": "A. Reviewer", "initials": ""}}
    assert wm.initials_of("A. Reviewer", comments) == "AR"
    assert wm.initials_of("Llama 3.2 3B Instruct, via. Glass-box harness (for J. Doe)", {}) == "GBH"
    assert wm.initials_of("x", {"1": {"author": "x", "initials": "XY"}}) == "XY"


def test_markdown_view_has_threads_and_changes(tmp_path):
    view = wm.docx_view(hand_written(tmp_path / "hand.docx"))
    md = wm.view_markdown(view, "hand.docx")
    assert "c11 B. Checker" in md and "row inserted" in md and "{+grew quickly|AR}" in md
    assert "protected (readOnly)" in md


def test_no_comments_no_settings(tmp_path):
    body = "<w:p><w:r><w:t>Plain.</w:t></w:r></w:p>"
    view = wm.docx_view(hand_written(tmp_path / "plain.docx", body=body, settings=f"<w:settings {W}/>"))
    assert wm.census_line(view["census"]).startswith("no tracked changes")
    assert view["settings"] == {"track_changes": False, "protection": None}
