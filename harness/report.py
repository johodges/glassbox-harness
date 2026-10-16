"""
Writes report.html for a run: the same stages the console showed, as a timeline you can click
through, with the generated files embedded and the raw prompts the LLM saw.
"""

import base64
import html
import json
from pathlib import Path

from .trace import KINDS

COLORS = {"python": "#7a7f87", "classifier": "#c99a06", "llm": "#0b8fb3", "generator": "#b03bb0",
          "perception": "#2f9e44", "retrieval": "#a0522d", "remote": "#d9480f", "server": "#1c7ed6"}

CSS = """
:root { --bg:#f7f7f8; --card:#fff; --text:#1d1f23; --muted:#6b7078; --border:#e2e4e8; --code:#f1f2f4; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181c; --card:#1f2228; --text:#e6e7ea; --muted:#9aa0a8; --border:#30343c; --code:#121418; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 10px; }
.muted { color: var(--muted); }
.answer { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px;
          white-space: pre-wrap; }
.legend span { display:inline-block; margin:0 14px 6px 0; font-size:13px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
.ev { background:var(--card); border:1px solid var(--border); border-left:5px solid; border-radius:8px;
      padding:10px 14px; margin:8px 0; }
.ev .head { display:flex; flex-wrap:wrap; gap:4px 14px; align-items:baseline; }
.ev .stage { font-weight:650; } .ev .time { margin-left:auto; font-variant-numeric: tabular-nums; }
.ev .flow { font-size:13px; } .ev .summary { margin-top:4px; overflow-wrap:anywhere; }
pre { background:var(--code); padding:10px; border-radius:6px; overflow-x:auto; font-size:12.5px;
      white-space:pre-wrap; overflow-wrap:anywhere; max-height:480px; }
details summary { cursor:pointer; color:var(--muted); font-size:13px; margin-top:6px; }
img, video { max-width:100%; border-radius:6px; margin-top:8px; display:block; }
audio { width:100%; margin-top:8px; } model-viewer { width:100%; height:380px; margin-top:8px;
      background:var(--code); border-radius:6px; }
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
        return (f'<details><summary>{name}</summary><pre>'
                f'{html.escape(p.read_text(encoding="utf-8", errors="replace"))}</pre></details>')
    return f'<p><a href="{src}">{name}</a></p>'


def write_report(result, path):
    run_dir = Path(path).parent
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>Harness run report</title><style>{CSS}</style>"
           f"<script type='module' src='https://cdn.jsdelivr.net/npm/@google/model-viewer@4.0.0/dist/model-viewer.min.js'>"
           f"</script></head><body><main>",
           f"<h1>{html.escape(result.prompt)}</h1>",
           f"<div class='muted'>{len(result.events)} stages, {sum(e.seconds for e in result.events):.1f} s total"
           f" &middot; {html.escape(str(run_dir))}</div>",
           "<h2>Final answer (written by the LLM)</h2>",
           f"<div class='answer'>{html.escape(result.answer)}</div>"]

    if result.files:
        out.append("<h2>Files the tools made</h2>")
        out += [_media(f, run_dir) for f in result.files]

    out.append("<h2>Data flow, stage by stage</h2><div class='legend'>")
    out += [f"<span><i style='background:{COLORS[k]}'></i>{html.escape(v[0])}</span>" for k, v in KINDS.items()]
    out.append("</div>")

    for ev in result.events:
        color = COLORS[ev.kind]
        indent = 28 * ev.stage.count(" > ")  # a sub-agent's stages ("SUBAGENT 1/3 > LLM") are indented
        out.append(f"<div class='ev' style='border-left-color:{color}; margin-left:{indent}px'><div class='head'>"
                   f"<span class='stage' style='color:{color}'>[{ev.index}] {html.escape(ev.stage)}</span>"
                   f"<span class='muted'>{html.escape(KINDS[ev.kind][0])}</span>"
                   f"<span class='time muted'>{ev.seconds:.2f} s</span></div>"
                   f"<div class='flow muted'>{html.escape(ev.component)} &nbsp;|&nbsp; "
                   f"{html.escape(ev.data_in)} &rarr; {html.escape(ev.data_out)}</div>"
                   f"<div class='summary'>{html.escape(ev.summary)}</div>")
        if ev.stage != "TOOL: generate_3d_model":  # its files are shown above; GLBs are large
            out += [_media(a, run_dir) for a in ev.artifacts]
        long_text = {k: v for k, v in ev.details.items() if isinstance(v, str) and len(v) > 200}
        small = {k: v for k, v in ev.details.items() if k not in long_text}
        for key, value in long_text.items():
            label = "the exact text the LLM received" if key == "raw_prompt" else key.replace("_", " ")
            out.append(f"<details><summary>{html.escape(label)}</summary><pre>{html.escape(value)}</pre></details>")
        if small:
            out.append("<details><summary>details</summary><pre>"
                       + html.escape(json.dumps(small, indent=2, default=str)) + "</pre></details>")
        out.append("</div>")

    out.append("<h2>The conversation as a list of messages</h2><details><summary>show JSON</summary><pre>"
               + html.escape(json.dumps(result.messages, indent=2, default=str)) + "</pre></details>")
    out.append("</main></body></html>")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return Path(path)
