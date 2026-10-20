"""
Writes report.html for a run: the console, replayed. The same banners, stages, colours and lines that run_agent.py
prints (Tracer.log, recorded at any verbosity), with every STEP and every stage folding open and shut, the files the
tools made shown where they were made, and the data-flow summary at the end. A LaTeX audit run also gets its
audit_report.md as a page section, with links to the original, corrected and latexdiff PDFs.
"""

import base64
import html
import json
import os
import re
from pathlib import Path

from .trace import KINDS

# The console's colours (the ANSI codes in trace.KINDS), as a dark terminal shows them
COLORS = {"python": "#8b8b8b", "classifier": "#d7b500", "llm": "#3ab7e0", "generator": "#c769d6",
          "perception": "#3cc35a", "retrieval": "#c77d34", "remote": "#f05555", "server": "#4c8dff"}

CSS = """
:root { --bg:#0c0c0c; --panel:#161616; --text:#cccccc; --dim:#8a8a8a; --border:#2c2c2c; --code:#1c1c1c; }
* { box-sizing: border-box; }
body { margin:0; padding:20px 16px; background:var(--bg); color:var(--text);
       font:13.5px/1.45 "Cascadia Mono", Consolas, "SF Mono", Menlo, "DejaVu Sans Mono", monospace; }
main { max-width: 1120px; margin: 0 auto; }
h1 { font: 600 19px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color:#fff; margin:0 0 4px;
     overflow-wrap:anywhere; }
h2 { font: 600 15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color:#fff; margin:26px 0 8px; }
.dim { color:var(--dim); overflow-wrap:anywhere; }
.answer { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:12px 14px;
          white-space:pre-wrap; overflow-wrap:anywhere; color:#fff; }
.legend span { display:inline-block; margin:0 16px 6px 0; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; }
.toolbar { position:sticky; top:0; z-index:2; background:var(--bg); padding:8px 0; border-bottom:1px solid var(--border); }
.toolbar button { font:inherit; color:var(--text); background:var(--panel); border:1px solid var(--border);
                  border-radius:4px; padding:3px 10px; margin:0 6px 4px 0; cursor:pointer; }
.toolbar button:hover { border-color:#555; }
.toolbar label { white-space:nowrap; cursor:pointer; }
summary { list-style:none; cursor:pointer; }
summary::-webkit-details-marker { display:none; }
summary::marker { content:""; }
details.step { margin:16px 0 4px; }
details.step > summary { color:#fff; font-weight:700; border-top:2px solid #4a4a4a; border-bottom:2px solid #4a4a4a;
                         padding:3px 0; overflow-wrap:anywhere; }
details.step > summary::before { content:"\\25B8  "; color:var(--dim); }
details.step[open] > summary::before { content:"\\25BE  "; }
details.stage { margin:10px 0 2px; }
details.stage > summary .head { color:var(--k); font-weight:700; white-space:pre-wrap; overflow-wrap:anywhere; }
details.stage > summary .head::before { content:"\\25B8 "; color:var(--dim); font-weight:400; }
details.stage[open] > summary .head::before { content:"\\25BE "; }
.flow, .data > summary, .file, .media { padding-left:5ch; }
.flow { color:var(--dim); white-space:pre-wrap; overflow-wrap:anywhere; }
.result { color:var(--k); padding-left:5ch; white-space:pre-wrap; overflow-wrap:anywhere; }
.result .t { color:var(--dim); }
details.stage[open] > summary .result { display:none; }  /* open: the result comes at the end, as in the console */
.line { white-space:pre-wrap; overflow-wrap:anywhere; }
.v2 { display:none; }
body.verbose .v2 { display:block; }
.stream { white-space:pre-wrap; overflow-wrap:anywhere; color:#3ab7e0; padding-left:7ch; margin:2px 0; }
.file { font-weight:700; color:#fff; overflow-wrap:anywhere; }
.sub { border-left:1px solid #3a3a3a; }
details.stage.sub { padding-left:10px; }
.data > summary { color:var(--dim); font-size:12px; margin-top:2px; }
pre { background:var(--code); color:var(--text); padding:8px 10px; border-radius:4px; white-space:pre-wrap;
      overflow-wrap:anywhere; max-height:480px; overflow:auto; font-family:inherit; font-size:12.5px;
      line-height:1.4; margin:4px 0 4px 5ch; }
table.flowsum { border-collapse:collapse; display:block; overflow-x:auto; }
table.flowsum th, table.flowsum td { padding:1px 14px 1px 0; text-align:left; white-space:nowrap; }
table.flowsum th { color:var(--dim); font-weight:400; }
table.flowsum td.n { text-align:right; }
img, video { max-width:100%; border-radius:4px; margin-top:6px; display:block; }
audio { width:100%; max-width:520px; margin-top:6px; display:block; }
model-viewer { width:100%; height:380px; margin-top:6px; background:var(--code); border-radius:4px; }
a { color:#6cb6ff; }
.md { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color:var(--text); background:var(--panel);
      border:1px solid var(--border); border-radius:6px; padding:4px 16px 10px; overflow-wrap:anywhere; }
.md h3, .md h4 { font: 600 15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color:#fff; margin:14px 0 6px; }
.md h4 { font-size:14px; }
.md ul { padding-left:20px; margin:6px 0; }
.md li { margin:3px 0; }
.md code { font-family:"Cascadia Mono", Consolas, "SF Mono", Menlo, monospace; font-size:12.5px; background:var(--code);
           border-radius:3px; padding:0 3px; }
.md table { border-collapse:collapse; display:block; overflow-x:auto; margin:6px 0; }
.md th, .md td { border:1px solid var(--border); padding:2px 10px; text-align:left; }
.md th { color:#fff; }
.pdfs { margin:0 0 10px; padding-left:20px; }
"""

SCRIPT = """
function setAll(selector, open) { document.querySelectorAll(selector).forEach(d => d.open = open); }
function stepsOnly() { setAll('details.stage', false); setAll('details.step', true); }
"""


def _media(path, run_dir):
    p = Path(path)
    name = html.escape(p.name)
    src = html.escape(p.relative_to(run_dir).as_posix() if p.is_relative_to(run_dir) else p.as_uri())
    ext = p.suffix.lower()
    if ext == ".png":  # a sheet of rendered slides gets the full width
        return f'<img src="{src}" alt="{name}" style="max-width:{"100%" if "overview" in p.stem else "384px"}">'
    if ext == ".wav":
        return f'<audio controls src="{src}"></audio>'
    if ext == ".mp4" and p.name.startswith("narrated_video"):  # has sound: let the viewer press play
        return f'<video controls src="{src}" style="max-width:640px"></video>'
    if ext == ".mp4":
        return f'<video controls loop autoplay muted src="{src}" style="max-width:640px"></video>'
    if ext == ".glb" and p.exists():  # embedded, because browsers block fetch() of local files
        data = base64.b64encode(p.read_bytes()).decode()
        return (f'<model-viewer src="data:model/gltf-binary;base64,{data}" camera-controls auto-rotate '
                f'shadow-intensity="1" alt="{name}"></model-viewer>')
    if ext in (".py", ".tex", ".md", ".txt", ".csv") and p.exists():  # working copies, tables: show the text
        return (f'<details class="data"><summary>{name}</summary><pre>'
                f'{html.escape(p.read_text(encoding="utf-8", errors="replace"))}</pre></details>')
    return f'<p><a href="{src}">{name}</a></p>'


def _rel(path, run_dir):
    p = Path(path)
    return p.relative_to(run_dir.parent.parent).as_posix() if p.is_relative_to(run_dir.parent.parent) else str(p)


def _nest(depth):
    """Inside a sub-agent (Tracer.nested): the console indents with '    |  '; here, a margin and a rule."""
    return (" sub", f"; margin-left:{2 * depth}ch") if depth else ("", "")


def _stage_start(ev):
    """The console's stage header (--- [n] STAGE ---  kind) and its component line, as a <summary> that also holds
    the result, so a folded stage still says what happened."""
    label = KINDS[ev.kind][0]
    head = f"[{ev.index}] {ev.stage}"
    depth = ev.stage.count(" > ")  # a sub-agent's stages ("SUBAGENT 1/3 > LLM") are indented, as in the console
    return (f"<details class='stage{' sub' if depth else ''}' open style='--k:{COLORS[ev.kind]}"
            f"{f'; margin-left:{2 * depth}ch' if depth else ''}'><summary>"
            f"<div><span class='head'>--- {html.escape(head)} {'-' * max(3, 60 - len(head))}</span>"
            f"<span class='dim'>  {html.escape(label)}</span></div>"
            f"<div class='flow'>{html.escape(ev.component)}   |   {html.escape(ev.data_in)}  -&gt;  "
            f"{html.escape(ev.data_out)}</div>{_result(ev)}</summary><div class='body'>")


def _result(ev):
    return (f"<div class='result'>=&gt; {html.escape(ev.summary)}<span class='t'>   ({ev.seconds:.2f}s)</span></div>")


def _stage_end(ev, run_dir, streamed):
    out = []
    if ev.kind == "llm" and not streamed and isinstance(ev.details.get("output_text"), str):
        # no tokens were streamed (verbosity 0, as in evaluate.py): show the text the LLM wrote, from the trace
        out.append(f"<div class='stream'>{html.escape(ev.details['output_text'])}</div>")
    out.append(_result(ev))
    for path in ev.artifacts:
        out.append(f"<div class='file'>file: {html.escape(_rel(path, run_dir))}</div>")
        if ev.stage != "TOOL: generate_3d_model":  # its files are shown at the top; GLBs are large
            out.append(f"<div class='media'>{_media(path, run_dir)}</div>")
    long_text = {k: v for k, v in ev.details.items() if isinstance(v, str) and len(v) > 200}
    small = {k: v for k, v in ev.details.items() if k not in long_text}
    for key, value in long_text.items():
        label = "the exact text the LLM received" if key == "raw_prompt" else key.replace("_", " ")
        out.append(f"<details class='data'><summary>{html.escape(label)}</summary><pre>{html.escape(value)}</pre>"
                   "</details>")
    if small:
        out.append("<details class='data'><summary>recorded details (trace.json)</summary><pre>"
                   + html.escape(json.dumps(small, indent=2, default=str)) + "</pre></details>")
    out.append("</div></details>")
    return out


def _console(result, run_dir):
    """Replay Tracer.log: banners open a STEP section, stages fold, lines keep their colour and indent."""
    events = {ev.index: ev for ev in result.events}
    log = result.log or [{"type": t, "event": ev.index} for ev in result.events for t in ("start", "end")]
    out, open_stages, streamed, in_section = [], [], set(), False
    for item in log:
        kind = item.get("kind")
        if item["type"] == "banner" and not open_stages:
            out.append("</div></details>" if in_section else "")
            out.append(f"<details class='step' open><summary>{html.escape(item['text'])}</summary><div>")
            in_section = True
            if item["text"].startswith("FINAL ANSWER"):  # the console prints the answer and the files after it
                out.append(f"<div class='line' style='color:#fff'>{html.escape(result.answer)}</div>")
                if result.files:
                    out.append("<div class='line'>\nFiles written by the tools:</div>")
                    out += [f"<div class='line'>  {html.escape(_rel(f, run_dir))}</div>" for f in result.files]
        elif item["type"] == "banner":  # a banner inside a stage: a bold line, not a new section
            out.append(f"<div class='line' style='color:#fff; font-weight:700'>{html.escape(item['text'])}</div>")
        elif item["type"] == "start":
            open_stages.append(item["event"])
            out.append(_stage_start(events[item["event"]]))
        elif item["type"] == "end":
            if item["event"] in open_stages:
                open_stages.remove(item["event"])
            out += _stage_end(events[item["event"]], run_dir, item["event"] in streamed)
        elif item["type"] == "line":
            sub, margin = _nest(item["depth"])
            style = f"padding-left:{item['indent']}ch" + (f"; color:{COLORS[kind]}" if kind else "") + margin
            out.append(f"<div class='line{' v2' if item['level'] > 1 else ''}{sub}' "
                       f"style='{style}'>{html.escape(item['text'])}</div>")
        elif item["type"] == "stream" and item["text"].strip():
            if open_stages:
                streamed.add(open_stages[-1])
            sub, margin = _nest(item["depth"])
            out.append(f"<div class='stream{sub}' style='{margin.lstrip('; ')}'>"
                       f"{html.escape(item['text'].strip())}</div>")
    out += ["</div></details>"] * len(open_stages)  # a run that stopped inside a stage
    if in_section:
        out.append("</div></details>")
    return out


def _flow_summary(result):
    """The console's DATA FLOW SUMMARY: one row per stage, in its kind's colour, and the time by kind."""
    rows = ["<table class='flowsum'><tr><th>#</th><th>stage</th><th>handled by</th><th>data in -&gt; data out</th>"
            "<th>time</th></tr>"]
    for ev in result.events:
        rows.append(f"<tr style='color:{COLORS[ev.kind]}'><td class='n'>{ev.index}</td><td>{html.escape(ev.stage)}</td>"
                    f"<td>{html.escape(KINDS[ev.kind][0])}</td>"
                    f"<td>{html.escape(ev.data_in)} -&gt; {html.escape(ev.data_out)}</td>"
                    f"<td class='n'>{ev.seconds:.2f}s</td></tr>")
    rows.append("</table>")
    by_kind = {}
    for ev in result.events:
        by_kind[ev.kind] = by_kind.get(ev.kind, 0) + ev.seconds
    parts = ", ".join(f"<span style='color:{COLORS[k]}'>{html.escape(KINDS[k][0])} {v:.1f}s</span>"
                      for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]))
    rows.append(f"<div class='dim' style='margin-top:4px'>total {sum(by_kind.values()):.1f}s  =  {parts}</div>")
    return rows


# ---- a LaTeX audit: audit_report.md as HTML, and the three PDFs -------------------------------------------------------
# As CommonMark: `x`, or ``x`` when x has a ` in it; \` outside a span is a backtick that opens none
CODE_SPAN = re.compile(r"(?<![`\\])(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
PDF_KINDS = {"original": "the original, as built", "corrected": "the corrected copy (fix_latex, edit_tex)",
             "latexdiff": "the latexdiff for review: deletions struck through, additions underlined"}


def _bold(text):
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text)).replace("\\`", "`")


def _inline(text):
    """One line of Markdown -> HTML: `code` spans and **bold**; everything else escaped."""
    out, i = [], 0
    for m in CODE_SPAN.finditer(text):
        body = m.group(2)
        if body.startswith(" ") and body.endswith(" ") and body.strip():
            body = body[1:-1]  # `` ``x'' `` -> ``x''
        out += [_bold(text[i:m.start()]), f"<code>{html.escape(body)}</code>"]
        i = m.end()
    return "".join(out) + _bold(text[i:])


def markdown_html(text):
    """
    The Markdown that audit_report.md uses -> HTML, in plain Python: # headings (two levels below the page's own),
    paragraphs, - lists (an indented line continues an item), | tables | with a |---| line under the header, `code`
    and **bold**.
    """
    out, para, items, rows = [], [], [], []

    def flush():
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
        if rows:
            head = len(rows) > 1 and all(re.fullmatch(r":?-+:?", c) for c in rows[1])
            body = [r for k, r in enumerate(rows) if not (head and k == 1)]
            out.append("<table>" + "".join(
                "<tr>" + "".join(f"<{'th' if head and k == 0 else 'td'}>{_inline(c)}</{'th' if head and k == 0 else 'td'}>"
                                 for c in r) + "</tr>" for k, r in enumerate(body)) + "</table>")
        para.clear(), items.clear(), rows.clear()

    for line in text.splitlines():
        s = line.strip()
        heading = re.match(r"(#{1,6})\s+(.*)", s)
        if s.startswith("|"):
            if para or items:
                flush()
            rows.append([c.strip() for c in s.strip("|").split("|")])
        elif heading:
            flush()
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif s.startswith(("- ", "* ")):
            if para or rows:
                flush()
            items.append(s[2:])
        elif not s:
            flush()
        elif items and line[:1].isspace():
            items[-1] += " " + s
        else:
            if items or rows:
                flush()
            para.append(s)
    flush()
    return "\n".join(out)


def _link(path, run_dir, note):
    href = os.path.relpath(path, run_dir).replace("\\", "/")
    where = "" if Path(path).parent == run_dir else f" (from {html.escape(Path(path).parent.name)})"
    return f"<li><a href='{html.escape(href)}'>{html.escape(Path(path).name)}</a>{where}: {html.escape(note)}</li>"


def latex_audit_section(run_dir):
    """
    A run of the LaTeX tools: the run folder's audit_report.md as HTML, and links to <project>_original.pdf,
    _corrected.pdf and _latexdiff.pdf (latex_project.publish names them so). A PDF this run didn't make, but an earlier
    run of the same audit did (the ledger says which), is linked there. -> HTML lines, or [] for any other run.
    """
    run_dir = Path(run_dir)
    report, ledger_path = run_dir / "audit_report.md", run_dir / "ledger.json"
    ledger = {}
    if ledger_path.is_file():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = {}
    names = [Path(ledger["root"]).name] if ledger.get("root") else []
    names += [p.name[:-len("_latexdiff.pdf")] for p in sorted(run_dir.glob("*_latexdiff.pdf"))]
    earlier = {"original": ledger.get("run_dir"), "corrected": (ledger.get("corrected") or {}).get("run_dir")}
    links = []
    for name in dict.fromkeys(names):
        for kind, note in PDF_KINDS.items():
            here = run_dir / f"{name}_{kind}.pdf"
            there = Path(earlier[kind]) / here.name if earlier.get(kind) else None
            pdf = here if here.is_file() else there if there and there.is_file() else None
            if pdf:
                links.append(_link(pdf, run_dir, note))
    if not report.is_file() and not links:
        return []
    out = ["<h2 id='audit'>The audit: audit_report.md and the PDFs</h2>"]
    if links:
        out.append("<ul class='pdfs'>" + "".join(links) + "</ul>")
    if report.is_file():
        out.append(f"<div class='md'>{markdown_html(report.read_text(encoding='utf-8', errors='replace'))}</div>")
    return out


def write_report(result, path):
    run_dir = Path(path).parent
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>Harness run report</title><style>{CSS}</style><script>{SCRIPT}</script>"
           f"<script type='module' src='https://cdn.jsdelivr.net/npm/@google/model-viewer@4.0.0/dist/model-viewer.min.js'>"
           f"</script></head><body><main>",
           f"<h1>{html.escape(result.prompt)}</h1>",
           f"<div class='dim'>{len(result.events)} stages, {sum(e.seconds for e in result.events):.1f} s total"
           f" &middot; {html.escape(str(run_dir))}</div>",
           "<h2>Final answer (written by the LLM)</h2>",
           f"<div class='answer'>{html.escape(result.answer)}</div>"]
    out += latex_audit_section(run_dir)

    if result.files:
        out.append("<h2>Files the tools made</h2>")
        out += [_media(f, run_dir) for f in result.files]

    out.append("<h2 id='console'>The run, as the console showed it</h2><div class='legend'>")
    out += [f"<span><i style='background:{COLORS[k]}'></i>{html.escape(v[0])}</span>" for k, v in KINDS.items()]
    out.append("</div><div class='toolbar'>"
               "<button onclick=\"setAll('details.step, details.stage', true)\">Expand all</button>"
               "<button onclick='stepsOnly()'>Stages folded</button>"
               "<button onclick=\"setAll('details.step', false)\">Steps folded</button>"
               "<label><input type='checkbox' onchange=\"document.body.classList.toggle('verbose', this.checked)\">"
               " level-2 lines (--verbose 2)</label></div>")
    out += _console(result, run_dir)

    out.append("<h2>Data flow summary (each row is one hop the data took)</h2>")
    out += _flow_summary(result)

    out.append("<h2>The conversation as a list of messages</h2><details class='data'><summary>show JSON</summary><pre>"
               + html.escape(json.dumps(result.messages, indent=2, default=str)) + "</pre></details>")
    out.append("</main></body></html>")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return Path(path)
