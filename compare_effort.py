"""
Effort: the same requests at low, medium and high effort (EFFORT in harness/config.py).

Effort is how much work the harness lets the LLM do on one request. Each level adds cost: medium adds
checks before the LLM may finish; high adds VOTE, five attempts at an answer that needs no tool, the
majority number winning; max adds THINK, plain-text reasoning before the first action, and REVIEW, a
second look at the answer. This script runs nine word problems (no tools: is the number right?) and three
multi-step tool requests (did every tool the request needs run?) at each level, and counts what the effort
cost: LLM calls, tokens written, seconds.

    python compare_effort.py                # everything, at all four levels (about 8 minutes)
    python compare_effort.py questions      # only the word problems
    python compare_effort.py tools          # only the tool requests

AGENTS.md and the skills are off, so effort is the only thing that changes. Every run is kept in
outputs/effort_comparison_<time>/<level>/, next to comparison.md (the tables printed at the end).
"""

import re
import shutil
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evaluate import tool_calls
from harness import Agent, HarnessConfig
from harness.agent import answer_number
from harness.config import EFFORT, OUTPUT_DIR

LEVELS = list(EFFORT)  # low, medium, high, max

QUESTIONS = [  # (short name, question, the right answer). Fire-flavoured arithmetic none of the tools does.
    ("energy", "A fire burns at a steady 2 MW for 10 minutes. How much energy does it release, in MJ?", 1200),
    ("layer", "A smoke layer starts at a 6 m ceiling and descends at 0.05 m/s. How many seconds until it is 1.8 m "
              "above the floor?", 84),
    ("sofa", "A 30 kg sofa has a heat of combustion of 20 MJ/kg and burns with 70 percent efficiency. How much heat "
             "does it release, in MJ?", 420),
    ("office", "An office is 12 m by 8 m with a fuel load of 600 MJ per square metre. What is the total fuel load, in "
               "GJ?", 57.6),
    ("plume", "A 2 MW fire puts 70 percent of its heat into the plume. The plume carries 15 kg/s of gas with a "
              "specific heat of 1 kJ per kg per kelvin. How much does the plume gas warm up, in kelvin?", 93.3),
    ("pump", "A pump delivers 1500 litres per minute. How many cubic metres does it deliver in 2 hours?", 180),
    ("doubling", "A fire doubles its heat release rate every 40 seconds. It is 50 kW now. What will it be in 160 "
                 "seconds, in kW?", 800),
    ("ventilation", "A fan extracts 4 cubic metres of smoke per second. A shop is 20 m by 15 m by 4 m. How many "
                    "minutes does one full air change take?", 5),
    ("detectors", "Smoke detectors must be at most 7.5 m apart in a grid. How many detectors does a 30 m by 15 m "
                  "hall need at least?", 15),
]

TOOL_TASKS = [  # (short name, request, the tools that must run, in order)
    ("robot", "Draw a cartoon robot and compose a short upbeat tune for it", ["generate_image", "generate_music"]),
    ("script", "Run workspace/scripts/word_count.py, fix the error it reports, and run it again.",
     ["run_script", "edit_code", "run_script"]),
    ("deck_pdf", "Convert workspace/old_format_deck.pptx to the workshop template and export it as a PDF.",
     ["convert_presentation", "export_pdf"]),
]


# Asked of every word problem at every level, so the number to check is easy to find ("... in 2 hours." at the end
# of an answer made its last number 2). VOTE reads the same line (answer_number in agent.py).
ANSWER_LINE = " End with a line that says Answer: and the number."


def cost(result):
    """What the run spent: LLM calls, tokens the LLM wrote, seconds (model loading, done once, not counted)."""
    llm = [ev for ev in result.events if ev.stage == "LLM"]
    written = sum(int(m.group(1)) for ev in llm if (m := re.search(r"wrote (\d+) new tokens", ev.summary)))
    seconds = sum(ev.seconds for ev in result.events if not ev.stage.startswith("LOAD"))
    return len(llm), written, seconds


def main():
    which = sys.argv[1:] or ["questions", "tools"]
    config = HarnessConfig(verbosity=0, color=False, ask_before_acting=False, use_agents_md=False, use_skills=False)
    agent = Agent(config)
    folder = OUTPUT_DIR / time.strftime("effort_comparison_%Y%m%d_%H%M%S")
    rows = []  # (kind, name, level, ok, shown, calls, tokens, seconds)
    jobs = ([("question", n, q, a) for n, q, a in QUESTIONS] if "questions" in which else []) + \
           ([("tools", n, r, e) for n, r, e in TOOL_TASKS] if "tools" in which else [])
    for level in LEVELS:
        agent.config.effort = level
        (folder / level).mkdir(parents=True, exist_ok=True)
        for kind, name, request, expected in jobs:
            result = agent.run(request + (ANSWER_LINE if kind == "question" else ""))
            if kind == "question":
                got = answer_number(result.answer)
                ok = got is not None and abs(got - expected) <= 0.01 * abs(expected)
                shown = f"{got:g}" if got is not None else "no number"
            else:
                _, executed = tool_calls(result)
                ok = executed == expected
                shown = " -> ".join(executed) or "no tool"
            calls, tokens, seconds = cost(result)
            rows.append((kind, name, level, ok, shown, calls, tokens, seconds))
            shutil.move(str(result.run_dir), str(folder / level / name))
            print(f"{level:<7} {name:<9} {'RIGHT' if ok else 'WRONG'}  {shown:<45} {calls} LLM calls, "
                  f"{tokens:4d} tokens, {seconds:5.1f}s", flush=True)

    lines = []
    for kind, title in (("question", "Word problems (no tools): is the final number right?"),
                        ("tools", "Tool requests: did every tool the request needs run?")):
        mine = [r for r in rows if r[0] == kind]
        if not mine:
            continue
        lines += [f"### {title}", "", "| | " + " | ".join(LEVELS) + " |", "|---|" + "---|" * len(LEVELS)]
        names = list(dict.fromkeys(r[1] for r in mine))
        for name in names:
            cells = [next(f"{'right' if r[3] else 'WRONG'}: {r[4]}" for r in mine if r[1] == name and r[2] == lv)
                     for lv in LEVELS]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        for label, pick in (("right", lambda rs: f"{sum(r[3] for r in rs)} of {len(rs)}"),
                            ("LLM calls", lambda rs: f"{sum(r[5] for r in rs) / len(rs):.1f} each"),
                            ("tokens written", lambda rs: f"{sum(r[6] for r in rs) / len(rs):.0f} each"),
                            ("seconds", lambda rs: f"{sum(r[7] for r in rs) / len(rs):.1f} each")):
            lines.append(f"| **{label}** | " + " | ".join(pick([r for r in mine if r[2] == lv]) for lv in LEVELS)
                         + " |")
        lines.append("")
    (folder / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines) + f"\nRuns and comparison.md: {folder}")


if __name__ == "__main__":
    main()
