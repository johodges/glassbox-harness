"""agent.py's COMPLETION CHECK and ANSWER CHECK, in the real loop with a scripted LLM and three trivial tools (no GPU,
no model). The case: "audit, fix what you can and make a latexdiff", where Llama 3.2 3B stopped after each step."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import agent as agent_module
from harness.agent import Agent, fix_paths, invented_paths
from harness.config import HarnessConfig
from harness.tools.base import Tool
from harness.trace import Tracer

STEPS = ("audit_step", "fix_step", "diff_step")


class Step(Tool):
    """A tool that writes one small file, as each LaTeX step writes its report or PDF."""
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name):
        self.name, self.description = name, f"The {name} of the request."

    def run(self, ctx):
        path = Path(ctx.run_dir) / f"{self.name}.txt"
        path.write_text(self.name, encoding="utf-8")
        return {"artifacts": [{"type": "data", "path": path}], "observations": {"done": self.name}}


def scripted_agent(tmp_path, replies):
    """An Agent without models: the LLM replies with the next scripted text; perception describes nothing."""
    agent = Agent.__new__(Agent)
    agent.config = HarnessConfig(verbosity=0, color=False, use_guard=False, use_router=False, use_agents_md=False,
                                 use_skills=False, route_planner=False, import_named_files=False,
                                 enabled_tools=list(STEPS), output_dir=tmp_path)
    agent.tracer = Tracer(verbosity=0, color=False)
    agent.device = agent.small_device = "cpu"
    agent.guard = agent.router = None
    script = iter(replies)
    agent.llm = SimpleNamespace(key=agent.config.llm, hit_limit=False, date=None,
                                generate=lambda *args: next(script))
    agent.parked_llms, agent.ask_user, agent.last_context = {}, input, None
    agent.perception = SimpleNamespace(describe=lambda artifact: {})
    agent.all_tools = {name: Step(name) for name in STEPS}
    return agent


def call(name):
    return f'{{"name": "{name}", "parameters": {{}}}}'


def stages(result, name):
    return [ev for ev in result.events if ev.stage == name]


def ran(tmp_path):
    return sorted(p.stem for p in tmp_path.glob("run_*/*_step.txt"))


def test_the_completion_check_asks_again_after_the_tool_it_prompted(tmp_path):
    """The LLM stops after each of three steps: the check asks, the LLM does the next step, the check asks again."""
    result = scripted_agent(tmp_path, [call("audit_step"), "The audit is done.", call("fix_step"),
                                       "The audit and the fixes are done.", call("diff_step"),
                                       "All three are done."]).run("Audit, fix what you can and make a diff")
    assert ran(tmp_path) == sorted(STEPS)
    checks = stages(result, "COMPLETION CHECK")
    assert [ev.summary for ev in checks] == [
        "the LLM wants to finish, but offered tools are unused: fix_step, diff_step",
        "the LLM wants to finish, but offered tools are unused: diff_step (question 2 of at most 3)"]
    assert result.answer.startswith("All three are done.")


def test_a_no_ends_the_questions(tmp_path):
    """After a check, a final answer instead of a tool call is a no: nothing asks again, and the first, full answer
    is kept."""
    result = scripted_agent(tmp_path, [call("audit_step"), "The audit is done, with all the details.",
                                       "Only the audit was needed."]).run("Audit it")
    assert ran(tmp_path) == ["audit_step"] and len(stages(result, "COMPLETION CHECK")) == 1
    assert result.answer.startswith("The audit is done, with all the details.")


def test_the_questions_are_bounded(tmp_path, monkeypatch):
    """At most COMPLETION_CHECKS questions in a request: with one, the third step is left undone (phase 1's case)."""
    monkeypatch.setattr(agent_module, "COMPLETION_CHECKS", 1)
    result = scripted_agent(tmp_path, [call("audit_step"), "The audit is done.", call("fix_step"),
                                       "Done: see the latexdiff."]).run("Audit, fix what you can and make a diff")
    assert ran(tmp_path) == ["audit_step", "fix_step"] and len(stages(result, "COMPLETION CHECK")) == 1


def test_the_answer_check_names_a_path_to_no_file(tmp_path):
    """The final answer names a latexdiff that no tool made: the harness says so under the answer, even though no
    file the run wrote has that name."""
    result = scripted_agent(tmp_path, [call("audit_step"), call("fix_step"), call("diff_step"),
                                       "Done. The latexdiff is at outputs/run_x/latexdiff.pdf."]).run("Audit it all")
    assert result.answer.endswith("[Harness: outputs/run_x/latexdiff.pdf does not exist: no tool in this request "
                                  "made it.]")
    check, = stages(result, "ANSWER CHECK")
    assert check.summary.startswith("outputs/run_x/latexdiff.pdf does not exist, and no tool wrote a file")


@pytest.mark.parametrize("text, missing", [
    ("The latexdiff is at outputs/run_x/latexdiff.pdf.", ["outputs/run_x/latexdiff.pdf"]),
    ("Found at sections/results.tex:16 and in workspace/latex_demo/refs.bib.", []),  # the demo paper's own files
    ("See https://doi.org/10.1000/xyz.pdf, doi.org/10.6028/NIST.SP.1018, 1/2.5 of it, W/m2.K.", []),  # not files
    (r"..\no_such_folder\paper.tex and ../no_such_folder/paper.tex", [r"..\no_such_folder\paper.tex",
                                                                     "../no_such_folder/paper.tex"]),
])
def test_invented_paths(tmp_path, text, missing):
    assert invented_paths(text, tmp_path) == missing


def test_a_garbled_path_is_still_corrected_first(tmp_path):
    """fix_paths runs first: a wrong path whose file name a tool wrote becomes the real path, and is not flagged."""
    made = tmp_path / "report.docx"
    made.write_text("x", encoding="utf-8")
    answer, fixed = fix_paths("Saved as workspace/run_1/report.docx.", [str(made)])
    assert fixed and str(made.name) in answer and invented_paths(answer, tmp_path) == []
