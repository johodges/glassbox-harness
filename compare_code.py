"""
Reviewing and developing code: the same three requests, planned by Llama 3.2 3B and by Qwen2.5-Coder 7B (4-bit).

Every tool, check and setting is the same; only the planner (HarnessConfig.llm) changes. The requests work on
workspace/code_demo, a small project with five seeded bugs (SEEDED below; the models never see this list):

    fix      run the tests and fix the code so that the failing test passes
    review   review readings.py and report each problem with its file and line
    add      add percentile() to stats.py, a test for it, and run the tests

After each run, plain Python measures what the model left behind:

    tests afterwards  the project's own tests on the code as the model left it, plus HIDDEN checks it never saw
    findings          every place the review cites (review_code's findings, or file:line in the answer), checked
                      against SEEDED: a seeded bug's line (give or take one), another real line, or no such line
    cost              LLM calls, tokens read (all prompts) and written, seconds (model loading not counted)

    python compare_code.py                   # both models, all three requests (30-40 minutes on a busy laptop)
    python compare_code.py fix review        # some of the requests
    python compare_code.py --llm code_llm    # one model

Effort medium, AGENTS.md off, the skills on (workspace/skills/code-review is part of the code setup), and every
tool call approved. Each run is kept in outputs/code_comparison_<time>/<model>/<request>/, next to comparison.md.
"""

import gc
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import torch

from evaluate import tool_calls
from harness import Agent, HarnessConfig
from harness.config import MODELS, OUTPUT_DIR, WORKSPACE_DIR
from harness.llm import load_llm
from harness.tools.coding import read_junit

PROJECT = WORKSPACE_DIR / "code_demo"
ORIGINAL_TESTS = sum(f.read_text(encoding="utf-8").count("\ndef test_") for f in (PROJECT / "tests").glob("test_*.py"))
NAMES = {"llm": "Llama 3.2 3B", "code_llm": "Qwen2.5-Coder 7B (4-bit)"}

TASKS = {
    "fix": "Run the tests in workspace/code_demo and fix the code so that the failing test passes.",
    "review": "Review workspace/code_demo/readings.py and report each problem you find with its file and line number.",
    "add": "Add a function percentile(values, p) to workspace/code_demo/stats.py that returns the p-th percentile of "
           "the values (p from 0 to 100) by the nearest-rank method, add a test for it to "
           "workspace/code_demo/tests/test_stats.py, and run the tests.",
}

# The answer key: (file, a piece of the buggy line, what is wrong, what can catch it)
SEEDED = [
    ("stats.py", "return ordered[n // 2]", "logic error: an even count gives the upper middle value", "test_median_even"),
    ("stats.py", "spread = max(values) - min(values)", "unused variable", "ruff F841"),
    ("readings.py", "import os", "unused import", "ruff F401"),
    ("readings.py", "eval(value_text)", "unsafe: eval() runs whatever the log file says", "ruff S307"),
    ("readings.py", "range(len(values) - window)", "off by one: the last window is missing", "only a careful reading"),
]

# Checks the model never sees, run on the code it left: does the fix or the new function really work?
HIDDEN = {
    "fix": '''from stats import median


def test_hidden_median_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_hidden_median_odd():
    assert median([5, 1, 3]) == 3


def test_hidden_median_one():
    assert median([7.0]) == 7.0
''',
    "add": '''from stats import percentile


def test_hidden_p40():
    assert percentile([15, 20, 35, 40, 50], 40) == 20


def test_hidden_p50():
    assert percentile([15, 20, 35, 40, 50], 50) == 35


def test_hidden_p100():
    assert percentile([15, 20, 35, 40, 50], 100) == 50


def test_hidden_unsorted():
    assert percentile([50, 15, 40, 20, 35], 30) == 20
''',
}


def seeded_lines():
    """SEEDED -> [(file, line number in the original, what is wrong)], found by text, so the files can change."""
    found = []
    for file, text, what, _ in SEEDED:
        lines = (PROJECT / file).read_text(encoding="utf-8").splitlines()
        found.append((file, next(i for i, line in enumerate(lines, 1) if text in line), what))
    return found


# ---- tests afterwards ---------------------------------------------------------------------------------------------
def tests_afterwards(agent, task, folder):
    """The project as the model left it (its working copies over a fresh copy), with the hidden checks added ->
    {"own": (passed, total), "hidden": (passed, total), "new_tests": n}."""
    tree = folder / "measured" / "code_demo"
    shutil.copytree(PROJECT, tree, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    for source, copy in agent.last_context.copies.items():
        if Path(source).is_relative_to(PROJECT) and Path(copy).is_file():
            (tree / Path(source).relative_to(PROJECT)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy, tree / Path(source).relative_to(PROJECT))
    if task in HIDDEN:
        (tree / "tests" / "test_hidden.py").write_text(HIDDEN[task], encoding="utf-8")
    report = folder / "measured" / "pytest.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
                    f"--junitxml={report}"], cwd=tree, capture_output=True, text=True, timeout=120)
    passed_names, failed_names = junit_names(report)
    hidden = [n for n in passed_names + failed_names if "test_hidden" in n]
    own = [n for n in passed_names + failed_names if "test_hidden" not in n]
    return {"own": (sum(n in passed_names for n in own), len(own)),
            "hidden": (sum(n in passed_names for n in hidden), len(hidden)) if task in HIDDEN else None,
            "new_tests": len(own) - ORIGINAL_TESTS, "facts": read_junit(report, tree, PROJECT)}


def junit_names(report):
    from xml.etree import ElementTree
    passed, failed = [], []
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        (failed if case.find("failure") is not None or case.find("error") is not None else passed).append(name)
    return passed, failed


# ---- findings -----------------------------------------------------------------------------------------------------
CITED = re.compile(r"([\w./\\-]*\w\.py)\W{0,3}(?:line\s*)?(\d+)|\blines?\s+(\d+)", re.I)


def findings(result, reviewed="readings.py"):
    """Every place the review cites: the findings of the last review_code call, else file:line in the answer."""
    calls = [m["tool_calls"][0]["function"] for m in result.messages if m.get("tool_calls")]
    reviews = [c["arguments"] for c in calls if c["name"] == "review_code"]
    if reviews:  # one finding per item: its first file:line
        items = reviews[-1].get("findings", [])
        items = items if isinstance(items, list) else re.findall(r'"([^"]+)"', str(items)) or [str(items)]
        matches, source = [m for item in items if (m := CITED.search(str(item)))], "review_code"
    else:  # every file:line in the answer, without the harness's own note
        matches, source = list(CITED.finditer(result.answer.split("[Harness:")[0])), "the answer"
    cited = []
    for m in matches:
        place = (Path(m.group(1)).name if m.group(1) else reviewed, int(m.group(2) or m.group(3)))
        if place not in [c[:2] for c in cited]:
            cited.append((*place, m.string[max(0, m.start() - 20):m.end() + 60]))
    return cited, source


def classify(cited, reviewed="readings.py"):
    """-> per finding 'real' (a seeded bug's line, +-1), 'other' (a real line, no seeded bug) or 'invented', and the
    seeded bugs of the reviewed file that some finding hit."""
    seeded = [(f, line, what) for f, line, what in seeded_lines() if f == reviewed]
    verdicts, hit = [], set()
    for file, line, text in cited:
        path = PROJECT / file
        exists = path.is_file() and 1 <= line <= len(path.read_text(encoding="utf-8").splitlines())
        bugs = [s for s in seeded if s[0] == file and abs(s[1] - line) <= 1]
        hit |= {s[2] for s in bugs}
        verdicts.append("real" if bugs else "other" if exists else "invented")
    return verdicts, hit, seeded


# ---- cost ---------------------------------------------------------------------------------------------------------
def cost(result):
    """LLM calls, tokens read (every prompt, summed), tokens written, seconds (loading not counted)."""
    llm = [ev for ev in result.events if ev.stage == "LLM"]
    written = sum(int(m.group(1)) for ev in llm if (m := re.search(r"wrote (\d+) new tokens", ev.summary)))
    read = sum(ev.details.get("num_tokens", 0) for ev in result.events if ev.stage == "PROMPT ASSEMBLY")
    seconds = sum(ev.seconds for ev in result.events if not ev.stage.startswith("LOAD"))
    return len(llm), int(read), written, seconds


def use_llm(agent, key):
    """Swap the planner, freeing the GPU memory of the old one first. Everything else stays loaded."""
    if agent.config.llm == key:
        return
    agent.llm = None
    gc.collect()
    torch.cuda.empty_cache()
    agent.config.llm = key
    agent.llm = load_llm(agent.tracer, key, agent.device)


def main():
    args = sys.argv[1:]
    keys = [args[args.index("--llm") + 1]] if "--llm" in args else ["llm", "code_llm"]
    tasks = [t for t in TASKS if t in args] or list(TASKS)
    agent = Agent(HarnessConfig(verbosity=0, color=False, ask_before_acting=False, use_agents_md=False,
                                use_skills=True, effort="medium", llm=keys[0]))
    folder = OUTPUT_DIR / time.strftime("code_comparison_%Y%m%d_%H%M%S")
    rows = {}
    for i, task in enumerate(tasks):
        # alternate the order, so each model runs as often first as second (the laptop's load changes over time)
        for key in (keys if i % 2 == 0 else keys[::-1]):
            use_llm(agent, key)
            result = agent.run(TASKS[task])
            run = folder / key / task
            run.mkdir(parents=True)
            row = {"ran": " -> ".join(tool_calls(result)[1]) or "no tool", "cost": cost(result),
                   "answer": result.answer, "tests": tests_afterwards(agent, task, run)}
            if task == "review":
                cited, row["source"] = findings(result)
                row["cited"] = cited
                row["verdicts"], row["hit"], row["seeded"] = classify(cited)
                checked = next((ev.summary for ev in result.events if ev.stage == "VERIFY FINDINGS"), None)
                row["harness_check"] = checked
            for item in result.run_dir.iterdir():  # the run itself, next to what was measured
                shutil.move(str(item), str(run / item.name))
            result.run_dir.rmdir()
            rows[(key, task)] = row
            calls, read, written, seconds = row["cost"]
            own, hidden = row["tests"]["own"], row["tests"]["hidden"]
            print(f"{key:<9} {task:<7} ran {row['ran']}; tests {own[0]}/{own[1]}"
                  + (f", hidden {hidden[0]}/{hidden[1]}" if hidden else "")
                  + (f"; findings {row['verdicts']}" if task == "review" else "")
                  + f"; {calls} LLM calls, {read} tokens read, {written} written, {seconds:.0f}s", flush=True)
    text = report(rows, keys, tasks)
    (folder / "comparison.md").write_text(text, encoding="utf-8")
    print("\n" + text + f"\nRuns and comparison.md: {folder}")


def report(rows, keys, tasks):
    head = ["| | " + " | ".join(f"{NAMES[k]}: {MODELS[k]}" for k in keys) + " |",
            "|---|" + "---|" * len(keys)]
    line = lambda label, pick: f"| {label} | " + " | ".join(pick(rows[(k, t)]) for k in keys) + " |"
    out = []
    for t in tasks:
        out += [f"### {t}: {TASKS[t]}", ""] + head
        out.append(line("tools that ran", lambda r: r["ran"]))
        out.append(line("project's tests afterwards", lambda r: "{}/{} pass".format(*r["tests"]["own"])))
        if t in HIDDEN:
            out.append(line("hidden checks", lambda r: "{}/{} pass".format(*r["tests"]["hidden"])))
        if t == "add":
            out.append(line("tests the model added", lambda r: str(max(r["tests"]["new_tests"], 0))))
        if t == "review":
            out.append(line("findings reported", lambda r: f"{len(r['cited'])} (in {r['source']})"))
            out.append(line("on a seeded bug's line", lambda r: str(r["verdicts"].count("real"))))
            out.append(line("on another real line", lambda r: str(r["verdicts"].count("other"))))
            out.append(line("on no such line (invented)", lambda r: str(r["verdicts"].count("invented"))))
            out.append(line("seeded bugs found", lambda r: f"{len(r['hit'])} of {len(r['seeded'])}"
                                                           + (f": {'; '.join(sorted(r['hit']))}" if r["hit"] else "")))
            out.append(line("review_code's own verdict", lambda r: r["harness_check"] or "not called"))
        out.append(line("LLM calls", lambda r: str(r["cost"][0])))
        out.append(line("tokens read / written", lambda r: f"{r['cost'][1]:,} / {r['cost'][2]:,}"))
        out.append(line("seconds", lambda r: f"{r['cost'][3]:.0f}"))
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    main()
