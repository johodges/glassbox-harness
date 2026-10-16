"""
ask_subagent: the same Llama, started again with an EMPTY context, to do one reading job.

Claude Code's Task tool works like this. The main agent picks a sub-agent and writes it a task; the harness starts a
FRESH agent loop with its own system prompt, its own list of messages, read-only tools and its own step limit. Only
the sub-agent's final answer comes back into the main conversation. Everything it read stays in its own context,
which is thrown away when it finishes.

Each sub-agent is a markdown file in workspace/agents/, like Claude Code's .claude/agents/*.md:

    ---
    name: reference-summarizer
    description: When to use it: the main LLM reads this line, in the tool's description, to choose an agent.
    tools: [read_file, search_references]
    max_steps: 4
    ---
    The body is the sub-agent's whole system prompt.

  AGENTS IN PROMPT  when the router offers the tool, the name and description of every agent file go into its description
  LOAD AGENT        the chosen file -> its system prompt, its tools (read-only ones only, whatever the file asks for)
                    and its step limit
  PLAN PARTS        plain Python cuts a file (or a folder) too long for one context into parts that fit
  SUBAGENT k/n      one fresh loop per part: PROMPT ASSEMBLY -> LLM -> PARSE -> TOOL: read_file -> ... -> answer
  SUBAGENT MERGE    with several parts, one more fresh sub-agent combines their answers (it sees only the answers)
  CONTEXT           token counts: what the sub-agents read vs. what goes back into the main context

In the console and the report, every stage of a sub-agent is indented and named "SUBAGENT k/n > ...".
"""

import difflib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import WORKSPACE_DIR
from ..instructions import read_markdown
from .base import Tool
from .documents import READABLE, as_text
from .files import ReadFileTool, current_version, rel, resolve

AGENTS_DIR = WORKSPACE_DIR / "agents"
MERGE_PROMPT = """You are a sub-agent: a helper that combines notes written by other helpers into ONE answer for another agent.
Use only what the notes say, and keep it short: one sentence per bullet point. Reply in plain text, with no JSON: the
other agent sees only your answer."""
READ_ONLY = ("read_file", "search_references")  # a sub-agent can look, never change anything
MAX_STEPS = 6          # the most an agent file may ask for (the main agent has config.max_steps)
PART_STEPS = 2         # a sub-agent given one part of a file: read it, then answer
NEW_TOKENS = 300       # per sub-agent reply: a few bullet points
READ_TOKENS = 60       # a part reader's first reply is only the read_file call (Llama otherwise writes it 4 times)
PART_CHARS = 8000      # one part = what one sub-agent reads: about 2,000 tokens, line numbers included.
                       # (The main agent's read_file shows 3,000 characters; each sub-agent has a reader of its own.)


@dataclass
class AgentFile:
    name: str
    description: str
    prompt: str          # the body of the file: the sub-agent's system prompt
    path: Path
    tools: list = field(default_factory=list)
    max_steps: int = 4


def find_agents(folder=AGENTS_DIR):
    """Every workspace/agents/*.md, read with the same frontmatter parser as the skills."""
    agents = []
    for path in sorted(Path(folder).glob("*.md")):
        meta, body = read_markdown(path)
        agents.append(AgentFile(meta.get("name", path.stem), meta.get("description", ""), body, path,
                                list(meta.get("tools") or []), int(meta.get("max_steps", 4))))
    return agents


def split(text, size=PART_CHARS):
    """Cut text into parts of at most `size` characters as read_file shows them (each line gets a 5-character
    number, '  1| '), at line ends where possible, and about equal in size: 3 parts of 7,400 characters rather
    than 3 of 8,000 and one of 300. Plain string handling."""
    lines = text.splitlines(keepends=True)
    total = sum(len(line) + 5 for line in lines)
    size = min(size, math.ceil(total / math.ceil(total / size)) + 300)  # +300: room for where the lines end
    parts, current, shown = [], "", 0
    for line in lines:
        if current and shown + len(line) + 5 > size:
            parts.append(current)
            current, shown = "", 0
        while len(line) + 5 > size:  # a single line longer than a part: cut it
            parts.append(line[:size - 5])
            line = line[size - 5:]
        current, shown = current + line, shown + len(line) + 5
    if current.strip():
        parts.append(current)
    return parts


def count_tokens(llm, text):
    return llm.tokenize(text).shape[1]


# =========================================================================================
class AskSubagentTool(Tool):
    name = "ask_subagent"
    intro = ("Hand a reading job to a sub-agent: a fresh copy of you with an empty context and read-only tools. It "
             "does the task and returns only its answer, so long files never fill your own context. Use it to "
             "summarize or check a long file, a whole folder or the reference notes. Give path for a file or folder: "
             "the harness cuts it into parts that fit, one sub-agent each. The agents (choose one by name):")
    description = intro + " (listed when the tool is offered)"
    parameters = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "The name of the agent, from the list above"},
            "task": {"type": "string", "description": "What the sub-agent should do, e.g. 'Summarize the main points "
                                                      "in five short bullet points'"},
            "path": {"type": "string", "description": "Optional: the file or folder to work through, e.g. "
                                                      "workspace/references"},
        },
        "required": ["agent", "task"],
    }
    model_id = "the same LLM, in a fresh context (no new model)"
    how_it_works = ("No new model: the SAME Llama runs a second agent loop with an empty list of messages, the agent "
                    "file's system prompt and only read-only tools. Its context fills up with what it reads; the main "
                    "agent's context gets only its final answer.")

    def prepare(self, ctx):
        """Called when the router offers the tool: the agents' names and descriptions go into its description."""
        with ctx.tracer.stage("AGENTS IN PROMPT", "python", "instructions.read_markdown() (no ML)",
                              f"{rel(AGENTS_DIR)}/*.md", "text in the tool description") as ev:
            agents = find_agents()
            index = " ".join(f"{a.name}: {a.description}" for a in agents)
            self.description = f"{self.intro} {index}"
            for agent in agents:
                ctx.tracer.say(f"{agent.name}: {agent.description}  (tools {agent.tools}, max_steps {agent.max_steps})",
                               kind="python")
            ev.summary = f"{len(agents)} agent file(s) -> their names and descriptions in the tool's description"

    def run(self, ctx, agent, task, path=""):
        t, llm = ctx.tracer, ctx.agent.llm
        main_prompt = llm._previous_prompt  # the console prints each new prompt in full: restore the main one after

        with t.stage("LOAD AGENT", "python", "instructions.read_markdown() (no ML)", "an agent's name",
                     "system prompt + tools + step limit") as ev:
            t.say(self.how_it_works, kind="python")
            agents = {a.name: a for a in find_agents()}
            name = agent if agent in agents else next(iter(difflib.get_close_matches(agent, list(agents), n=1)), None)
            if name is None:
                raise ValueError(f"There is no agent called '{agent}'. The agents are: {', '.join(agents)}")
            spec = agents[name]
            tools = {n: ctx.tools[n] for n in spec.tools if n in READ_ONLY and n in ctx.tools}
            dropped = [n for n in spec.tools if n not in tools]
            steps = min(spec.max_steps, MAX_STEPS)
            t.say(f"system prompt ({rel(spec.path)}):\n{spec.prompt}", kind="python")
            ev.summary = (f"'{agent}' -> {rel(spec.path)}: tools {list(tools)}, {steps} steps"
                          + (f"; not given (not read-only, or not enabled): {', '.join(dropped)}" if dropped else ""))

        with t.stage("PLAN PARTS", "python", "subagent.split() (no ML)", "task + file or folder",
                     "one task per sub-agent") as ev:
            document_tokens, call_tokens = None, NEW_TOKENS
            if path:
                if "read_file" not in tools:
                    raise ValueError(f"The agent {name} cannot read files. Leave out path, or choose another agent.")
                target = current_version(resolve(path, ctx, allow_dir=True), ctx)
                if target.is_dir():
                    files = [p for p in sorted(target.rglob("*")) if p.is_file() and p.suffix.lower() in READABLE]
                    text = "\n\n".join(f"=== {p.name} ===\n{as_text(p)}" for p in files)
                else:
                    text = as_text(target)
                parts = split(text)
                document_tokens = count_tokens(llm, text)
                jobs = []
                for k, part in enumerate(parts, 1):
                    part_file = ctx.run_dir / f"subagent_part_{k:02d}.txt"
                    part_file.write_text(part, encoding="utf-8")
                    ev.artifacts.append(str(part_file))
                    jobs.append(f"Read {rel(part_file)} with read_file: it is part {k} of {len(parts)} of "
                                f"{rel(target)}. Then, from this part only, write notes for this task: {task} "
                                "Write at most 4 bullet points of at most 15 words each.")
                ev.summary = (f"{rel(target)}: {len(text):,} characters = {document_tokens:,} tokens -> {len(parts)} "
                              f"part(s) of at most {PART_CHARS:,} characters, one fresh sub-agent each")
                reader = ReadFileTool()
                reader.max_chars = PART_CHARS
                tools, steps, call_tokens = {"read_file": reader}, PART_STEPS, READ_TOKENS  # one read, then answer
            else:
                jobs = [task]
                ev.summary = "no file given: one sub-agent, with the task as written"

        answers, largest = [], 0
        for k, job in enumerate(jobs, 1):
            answer, peak = self.subagent(ctx, f"SUBAGENT {k}/{len(jobs)}", job, tools, steps, spec.prompt, call_tokens)
            answers.append(answer)
            largest = max(largest, peak)
        answer = answers[0]
        if len(answers) > 1:  # the merge sees only the answers, never the document
            notes = "\n\n".join(f"Part {k}:\n{a}" for k, a in enumerate(answers, 1))
            answer, peak = self.subagent(ctx, "SUBAGENT MERGE", f"Here are notes on the {len(answers)} parts of "
                                         f"{rel(target)}, written by other helpers:\n\n{notes}\n\nUsing only these "
                                         f"notes, do this task for the whole document: {task}", {}, 1, MERGE_PROMPT)
            largest = max(largest, peak)
        llm._previous_prompt = main_prompt

        with t.stage("CONTEXT", "python", "the LLM's tokenizer (no ML)", "the sub-agents' contexts",
                     "token counts") as ev:
            tokens = {"document": document_tokens, "largest_subagent_context": largest,
                      "returned_to_main_context": count_tokens(llm, answer)}
            ev.summary = (f"{len(answers) + (len(answers) > 1)} sub-agent(s), the largest context {largest:,} tokens; "
                          f"the main context gets {tokens['returned_to_main_context']:,} tokens"
                          + (f" instead of the document's {document_tokens:,}" if document_tokens else ""))
            ev.details = tokens
        # bullet lines as a list as well: a small LLM copies a JSON list into update_powerpoint, where from one string
        # it wrote a single bullet of its own ("Summarized main points")
        bullets = [re.sub(r"^\s*(?:[•*-]|\d+[.)])\s*", "", line).strip() for line in answer.splitlines()
                   if re.match(r"^\s*(?:[•*-]|\d+[.)])\s+\S", line)]
        return {"artifacts": [], "observations": {
            "agent": name, "answer": answer, **({"bullets": bullets} if len(bullets) >= 2 else {}), "tokens": tokens,
            "note": "This is the sub-agent's final answer, from its own reading. Use it for the user's request: do not "
                    "read the same files again yourself."}}

    def subagent(self, ctx, label, task, tools, steps, system, call_tokens=NEW_TOKENS):
        """
        One sub-agent: a fresh conversation, read-only tools, its own step limit. -> (answer, largest prompt)
        Its tool calls go through the main agent's call_tool(): the same VALIDATE ARGS and TOOL OUTPUT GUARD.
        """
        from ..agent import parse_tool_call  # imported here: agent.py imports this package
        t, llm = ctx.tracer, ctx.agent.llm
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        largest, has_read, nudged = 0, False, False
        with t.nested(label):
            for step in range(1, steps + 1):
                t.say(f"{label}, step {step} of {steps}: a fresh context of {len(messages)} messages", kind="python")
                last = step == steps
                if last and tools:  # out of steps: offer no tools, so it has to answer
                    messages.append({"role": "user", "content": "[Harness] Step limit reached: answer now, from "
                                                                "what you have read."})
                schemas = [] if last else [tool.schema() for tool in tools.values()]
                largest = max(largest, count_tokens(llm, llm.render(messages, schemas)))
                reply = llm.generate(messages, schemas, call_tokens if schemas else NEW_TOKENS, 0.0)
                with t.stage("PARSE", "python", "agent.parse_tool_call() (no ML)", "LLM text",
                             "tool call or answer") as ev:
                    call = parse_tool_call(reply, list(tools)) if schemas else None
                    ev.summary = ("no tool call -> the sub-agent's answer" if call is None
                                  else f"tool call -> {call['name']}({call['arguments']})")
                if call is None and tools and not has_read and not nudged and not last:
                    # it answered without reading anything: one nudge from the harness, as in the main loop
                    nudged = True
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": "[Harness check] You have not read anything yet. "
                                                             "Call a tool first, then answer from what it returns."}]
                    continue
                if call is None:
                    return reply, largest
                broken = call.pop("broken", False)
                messages.append({"role": "assistant", "tool_calls": [{"type": "function", "function": call}]})
                if broken or call["name"] not in tools:
                    result = {"status": "error", "error": f"That was not a valid call. The tools are: {', '.join(tools)}."}
                else:
                    result = ctx.agent.call_tool(call, ctx, [], tool=tools[call["name"]])
                    has_read = has_read or result["status"] == "ok"
                messages.append({"role": "tool", "content": result})
        return "(the sub-agent used up its steps without an answer)", largest
