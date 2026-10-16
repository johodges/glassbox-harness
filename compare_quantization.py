"""
Quantization: the planner's weights in 16, 8 and 4 bits (HarnessConfig.llm_precision, run_agent.py --precision).

Every weight of a model is one number. Llama 3.2 3B was trained with 16-bit numbers (bf16): 3.2 billion of them,
6.4 GB. Quantization stores each one in fewer bits, 8 (int8) or 4 (nf4, plus a scale for every block of 64), and
bitsandbytes does it while the model loads. Less memory, but what does it cost? For each precision this script
measures:

  - the GPU memory the weights take (torch.cuda.memory_allocated before and after LOAD LLM)
  - how long LOAD LLM takes
  - how fast the LLM writes: tokens per second in the LLM stages of the word problems
  - the nine word problems from compare_effort.py at effort medium: is the final number right?
  - the evaluate.py suite: did exactly the right tools run?
  - every tool call the LLM wrote in the suite: was it valid JSON? Exact formats are where fewer bits show first.

    python compare_quantization.py                   # bf16, int8 and nf4, everything (1.5 hours; int8 is the slow one)
    python compare_quantization.py questions         # memory, load time, speed and the word problems only (~20 min)
    python compare_quantization.py nf4 int8 eval     # some precisions, the suite only

AGENTS.md and the skills are off and the date in the prompt is pinned, as in evaluate.py, so the precision is the only
thing that changes. Every run is kept in outputs/quantization_comparison_<time>/<precision>/<questions|eval>/, next to
comparison.md.
"""

import gc
import json
import platform
import re
import shutil
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psutil
import torch

from compare_effort import ANSWER_LINE, QUESTIONS
from evaluate import TESTS, evaluate
from harness import Agent, HarnessConfig, parse_tool_call
from harness.agent import answer_number
from harness.config import MODELS, OUTPUT_DIR, PRECISIONS


def warm_disk_cache():
    """Read the planner's weight files once, so the first precision isn't the only one loaded from a cold disk."""
    from huggingface_hub import snapshot_download
    folder = snapshot_download(MODELS["llm"], allow_patterns=["*.safetensors"], local_files_only=True)
    for path in sorted(Path(folder).glob("*.safetensors")):
        with open(path, "rb") as f:
            while f.read(64 << 20):
                pass


def speed(results):
    """Tokens the LLM wrote per second of LLM time, from the LLM stages' own summaries ("wrote 132 new tokens")."""
    tokens = seconds = 0
    for result in results:
        for ev in result.events:
            if m := re.search(r"wrote (\d+) new tokens", ev.summary):
                tokens, seconds = tokens + int(m.group(1)), seconds + ev.seconds
    return tokens / seconds if seconds else 0.0


def is_json_call(text):
    """True if the whole reply is one JSON object with a tool name: valid as the model wrote it, no repairs."""
    try:
        obj = json.loads(text.replace("<|python_tag|>", "").strip())
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and isinstance(obj.get("name"), str)


def tool_call_json(results, known):
    """
    Every reply in which the LLM meant to call a tool, sorted by its JSON:
      valid:    the whole reply is one JSON object, exactly as written
      repaired: parse_tool_call (agent.py) could read it only after fixing it: text around the JSON, a Windows
                path's backslashes, two calls in one reply, or the name(arg=...) form
      broken:   the JSON never closes (cut off, a stray quote): the call cannot run, an error goes back to the LLM
    and, separately, calls to a tool that doesn't exist and calls whose arguments validate() rejected.
    """
    n = dict.fromkeys(["calls", "valid", "repaired", "broken", "unknown tool", "arguments rejected"], 0)
    for result in results:
        for ev in result.events:
            if ev.stage.split(" > ")[-1] == "VALIDATE ARGS" and ev.summary.startswith("FAILED"):
                n["arguments rejected"] += 1
            if ev.stage.split(" > ")[-1] != "LLM":
                continue
            text = ev.details.get("output_text", "")
            call = parse_tool_call(text, known)
            if call is None:
                continue  # an answer in words
            n["calls"] += 1
            n["broken" if call.get("broken") else "valid" if is_json_call(text) else "repaired"] += 1
        n["unknown tool"] += sum(1 for m in result.messages if m.get("role") == "tool" and isinstance(m["content"], dict)
                                 and str(m["content"].get("error", "")).startswith("There is no tool called"))
    return n


def keep(result, folder):
    folder.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(result.run_dir), str(folder))


def measure(precision, which, folder):
    """One precision: load the harness with the planner at that precision, then run the word problems and the suite."""
    gc.collect()
    torch.cuda.empty_cache()
    cpu = psutil.cpu_percent(interval=2)  # other programs keep the CPU busy too: batch-1 generation waits on the CPU
    # as evaluate.py: no AGENTS.md or skills, and the date in the prompt pinned, so only the precision changes
    agent = Agent(HarnessConfig(verbosity=0, color=False, ask_before_acting=False, use_agents_md=False,
                                use_skills=False, prompt_date="15 Oct 2026", llm_precision=precision))
    row = {"precision": precision, "weights_gb": agent.llm.weights_gb, "load_seconds": agent.llm.load_seconds,
           "cpu_busy": cpu}
    print(f"\n{precision}: {row['weights_gb']:.2f} GB of weights, loaded in {row['load_seconds']:.1f}s "
          f"(CPU {cpu:.0f}% busy)", flush=True)

    if "questions" in which:
        agent.config.effort = "medium"
        results, row["questions"] = [], []
        for name, question, expected in QUESTIONS:
            result = agent.run(question + ANSWER_LINE)
            got = answer_number(result.answer)
            ok = got is not None and abs(got - expected) <= 0.01 * abs(expected)
            row["questions"].append((name, ok, f"{got:g}" if got is not None else "no number"))
            results.append(result)
            print(f"{precision:<5} {name:<12} {'RIGHT' if ok else 'WRONG'}  {row['questions'][-1][2]}", flush=True)
            keep(result, folder / precision / "questions" / name)
        row["tokens_per_second"] = speed(results)

    if "eval" in which:
        kept, start = [], time.perf_counter()
        row["passed"] = evaluate(agent, TESTS, keep=kept)
        row["eval_minutes"] = (time.perf_counter() - start) / 60
        row["tests"] = [(prompt, ok) for prompt, ok, _ in kept]
        row["json"] = tool_call_json([result for _, _, result in kept], list(agent.all_tools))
        for i, (prompt, _, result) in enumerate(kept, 1):
            keep(result, folder / precision / "eval" / f"{i:02d}_{'_'.join(re.findall(r'[a-z0-9]+', prompt.lower())[:5])}")

    del agent
    gc.collect()
    torch.cuda.empty_cache()
    return row


def table(rows):
    """comparison.md: one column per precision."""
    head = ["| | " + " | ".join(r["precision"] for r in rows) + " |", "|---|" + "---|" * len(rows)]
    line = lambda label, cell: f"| {label} | " + " | ".join(cell(r) for r in rows) + " |"
    lines = ["### The planner, Llama 3.2 3B, at each precision", ""] + head + [
        line("bits per weight", lambda r: PRECISIONS[r["precision"]]),
        line("GPU memory for the weights", lambda r: f"{r['weights_gb']:.2f} GB"),
        line("load time", lambda r: f"{r['load_seconds']:.1f} s"),
    ]
    if all("questions" in r for r in rows):
        lines += [line("tokens per second (word problems)", lambda r: f"{r['tokens_per_second']:.1f}"),
                  line("word problems right (effort medium)",
                       lambda r: f"{sum(ok for _, ok, _ in r['questions'])} of {len(r['questions'])}")]
    if all("tests" in r for r in rows):
        lines += [line("evaluate.py suite passed", lambda r: f"{r['passed']} of {len(r['tests'])}"),
                  line("suite time", lambda r: f"{r['eval_minutes']:.1f} min"),
                  line("tool calls the LLM wrote in the suite", lambda r: str(r["json"]["calls"])),
                  line("... valid JSON as written", lambda r: str(r["json"]["valid"])),
                  line("... readable only after the parser's repairs", lambda r: str(r["json"]["repaired"])),
                  line("... broken JSON (could not run)", lambda r: str(r["json"]["broken"])),
                  line("calls to a tool that doesn't exist", lambda r: str(r["json"]["unknown tool"])),
                  line("arguments rejected by validate()", lambda r: str(r["json"]["arguments rejected"]))]
    lines += ["", "CPU busy with other programs when each precision started: "
              + ", ".join(f"{r['precision']} {r['cpu_busy']:.0f}%" for r in rows)
              + ". Generating one token at a time waits on the CPU as much as on the GPU.", ""]
    if all("questions" in r for r in rows):
        lines += ["### Word problems (effort medium): is the final number right?", ""] + head
        for i, (name, _, _) in enumerate(rows[0]["questions"]):
            lines.append(line(name, lambda r: f"{'right' if r['questions'][i][1] else 'WRONG'}: {r['questions'][i][2]}"))
        lines.append("")
    if all("tests" in r for r in rows):
        lines += ["### evaluate.py, test by test", ""] + head
        for i, (prompt, _) in enumerate(rows[0]["tests"]):
            lines.append(line(prompt[:70].replace("|", "/"), lambda r: "pass" if r["tests"][i][1] else "FAIL"))
        lines.append("")
    return "\n".join(lines)


def main():
    precisions = [a for a in sys.argv[1:] if a in PRECISIONS] or list(PRECISIONS)
    which = [a for a in sys.argv[1:] if a in ("questions", "eval")] or ["questions", "eval"]
    folder = OUTPUT_DIR / time.strftime("quantization_comparison_%Y%m%d_%H%M%S")
    import bitsandbytes
    print(f"{torch.cuda.get_device_name(0)}, torch {torch.__version__}, bitsandbytes {bitsandbytes.__version__}, "
          f"Python {platform.python_version()}. Reading the weight files once ...", flush=True)
    warm_disk_cache()
    rows = [measure(precision, which, folder) for precision in precisions]
    text = (f"{torch.cuda.get_device_name(0)}, torch {torch.__version__}, bitsandbytes {bitsandbytes.__version__}. "
            f"Planner: {MODELS['llm']}. AGENTS.md and skills off.\n\n" + table(rows))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "comparison.md").write_text(text, encoding="utf-8")
    print("\n" + text + f"\nRuns and comparison.md: {folder}")


if __name__ == "__main__":
    main()
