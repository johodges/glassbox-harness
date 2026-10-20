"""
The paper audit, measured: audit_latex on workspace/latex_demo, scored against latex_demo_answers.json.

The demo paper has 12 planted errors. Their answer key sits outside workspace/, so no tool can show it to the LLM.
Plain Python runs the same audit without and with JUDGE (the LLM's one question per "likely" finding), then lets
fix_latex apply the certain fixes, and measures:

    recall      planted errors found: a finding in the same file whose evidence holds the error's words
    confidence  how sure the audit was of each, against the answer key's own
    precision   findings that match no planted error, each listed for a person to look at
    JUDGE       the likely findings the LLM confirmed or rejected, and how long it took
    fixes       the fixes applied and verified (a rebuild and the same audit again), and whether each is the key's
    time        seconds per stage

    python compare_audit.py               # without and with JUDGE (Llama 3.2 3B), then the fixes
    python compare_audit.py --no-judge    # plain Python only: no LLM, no GPU

Every tool call is approved. The runs are kept in outputs/audit_comparison_<time>/, next to comparison.md.
"""

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from harness.config import OUTPUT_DIR, PROJECT_DIR, HarnessConfig
from harness.trace import Tracer
from harness.tools.base import ToolContext
from harness.tools.latex_audit import audit, one_line, squash
from harness.tools.latex_fix import FixLatexTool
from harness.tools.latex_project import latex_project

ANSWERS = json.loads((PROJECT_DIR / "latex_demo_answers.json").read_text(encoding="utf-8"))
PROJECT = ANSWERS["project"]


def matches(finding, planted):
    return finding["file"] == planted["file"] and squash(planted["anchor"]) in squash(finding["evidence"])


def score(ledger):
    """-> ({planted id: the finding that caught it}, [findings that match nothing])"""
    caught, extra = {}, []
    for f in ledger["findings"]:
        hits = [p for p in ANSWERS["planted"] if matches(f, p)]
        for p in hits:
            caught.setdefault(p["id"], f)
        if not hits:
            extra.append(f)
    return caught, extra


def stage_seconds(tracer):
    seconds = defaultdict(float)
    for ev in tracer.events:
        seconds[ev.stage.split(" > ")[-1]] += ev.seconds
    return dict(seconds)


def run(name, folder, agent=None):
    """One audit (and, for the last one, the fixes) in its own run folder."""
    tracer = Tracer(verbosity=0, color=False)
    if agent:
        agent.llm.tracer = tracer  # its PROMPT ASSEMBLY and LLM stages are timed with the rest
    run_dir = folder / name
    run_dir.mkdir(parents=True)
    ctx = ToolContext(tracer, run_dir, "cuda" if agent else "cpu", 1, {}, HarnessConfig(ask_before_acting=False),
                      request=f"Audit {PROJECT}", agent=agent)
    root, main = latex_project(PROJECT, ctx)
    start = time.perf_counter()
    ledger, _, _, _ = audit(root, main, ctx, use_judge=agent is not None)
    seconds = time.perf_counter() - start
    ctx.facts.setdefault("latex_ledgers", {})[str(root)] = ledger
    return SimpleNamespace(name=name, ledger=ledger, seconds=seconds, ctx=ctx, stages=stage_seconds(tracer))


def judge_llm():
    """Only what JUDGE needs: the planner LLM (no guard, router or perception models)."""
    import torch
    from harness.llm import load_llm
    tracer = Tracer(verbosity=0, color=False)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    return SimpleNamespace(llm=load_llm(tracer, "llm", device), config=HarnessConfig(ask_before_acting=False),
                           ask_user=lambda question: "y")


def report(runs, fixes, folder):
    lines = [f"# The paper audit on {PROJECT}", "",
             f"{len(ANSWERS['planted'])} planted errors (latex_demo_answers.json). A finding catches one when it is "
             "in the same file and its evidence holds the error's words.", "",
             "| # | planted error | key | " + " | ".join(r.name for r in runs) + " | fix verified |",
             "|---|---|---|" + "---|" * len(runs) + "---|"]
    fixed = {f["id"]: f for f in fixes["ledger"]["findings"]} if fixes else {}
    last = score(runs[-1].ledger)[0]
    for p in ANSWERS["planted"]:
        cells = []
        for r in runs:
            f = score(r.ledger)[0].get(p["id"])
            cells.append("missed" if f is None else f"{f['id']} {f['confidence']}"
                         + (" (rejected)" if f["status"] == "rejected" else "")
                         + (f", JUDGE: {f['judge']['answer']}" if f.get("judge") else ""))
        f = last.get(p["id"])
        g = fixed.get(f["id"]) if f else None
        if g and g["status"] == "verified":
            same = p["fix"] is None or squash(p["fix"]) in squash(g["fix"]["new"])
            verdict = "yes" + ("" if same else f" (not the key's fix: {g['fix']['new']})")
        else:
            verdict = g["status"] if g and g["status"] != "found" else "-"
        lines.append(f"| {p['id']} | {p['what']} ({p['file']}) | {p['confidence']} | " + " | ".join(cells)
                     + f" | {verdict} |")
    lines += [""]
    for r in runs:
        caught, extra = score(r.ledger)
        by = Counter(f["confidence"] for f in r.ledger["findings"] if f["status"] != "rejected")
        agree = sum(caught[p["id"]]["confidence"] == p["confidence"] for p in ANSWERS["planted"] if p["id"] in caught)
        lines += [f"## {r.name}", "",
                  f"- **recall:** {len(caught)}/{len(ANSWERS['planted'])} planted errors found",
                  f"- **precision:** {len(r.ledger['findings']) - len(extra)}/{len(r.ledger['findings'])} findings "
                  f"match a planted error; {len(extra)} other(s) below",
                  f"- **confidence:** {agree} of {len(caught)} found errors at the key's confidence; findings: "
                  f"{by['certain']} certain, {by['likely']} likely, {by['needs a person']} need a person",
                  f"- **time:** {r.seconds:.1f} s; by stage: "
                  + ", ".join(f"{k} {v:.1f} s" for k, v in sorted(r.stages.items(), key=lambda kv: -kv[1])[:8])]
        if r.ledger.get("judge", {}).get("asked"):
            j = r.ledger["judge"]
            lines.append(f"- **JUDGE:** asked about {j['asked']} likely finding(s): "
                         + ", ".join(f"{k} {v}" for k, v in j.items() if k != "asked"))
        lines += [f"  - not planted: {one_line(f)}" for f in extra] + [""]
    if fixes:
        o = fixes["observations"]
        lines += ["## fix_latex (which=\"certain\")", "", f"- {o['summary']}", f"- build: {o['build']}"]
        lines += [f"- new finding after the fixes: {n}" for n in o.get("new_findings", [])]
        lines += [f"- failed: {n}" for n in o.get("failed", [])]
    path = folder / "comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    folder = OUTPUT_DIR / time.strftime("audit_comparison_%Y%m%d_%H%M%S")
    runs = [run("plain Python", folder)]
    print(f"plain Python: {len(score(runs[0].ledger)[0])}/{len(ANSWERS['planted'])} found in {runs[0].seconds:.1f} s",
          flush=True)
    if "--no-judge" not in sys.argv:
        runs.append(run("with JUDGE", folder, judge_llm()))
        print(f"with JUDGE: {len(score(runs[1].ledger)[0])}/{len(ANSWERS['planted'])} found in "
              f"{runs[1].seconds:.1f} s", flush=True)
    last = runs[-1]
    out = FixLatexTool().run(last.ctx, PROJECT, which="certain")
    fixes = {"observations": out["observations"], "ledger": last.ctx.facts["latex_ledgers"][str(
        latex_project(PROJECT, last.ctx)[0])]}
    print(f"fix_latex: {out['observations']['summary']}", flush=True)
    path = report(runs, fixes, folder)
    print(path.read_text(encoding="utf-8"))
    print(f"saved: {path}")


if __name__ == "__main__":
    main()
