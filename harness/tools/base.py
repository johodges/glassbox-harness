"""
What a tool is, as far as the harness is concerned.

A tool is just a Python function plus a description of it. The description (name, what it
does, a JSON Schema of its arguments) is pasted into the LLM's prompt as text. When the LLM
writes JSON that names the tool, the harness calls run() with those arguments.

The LLM never runs anything itself, and it never sees the image/audio/video bytes. run()
returns the paths of the files it wrote; the harness then asks perception models to describe
those files in words (see perception.py).
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolContext:
    """Everything a tool may need from the harness while it runs."""
    tracer: object
    run_dir: Path        # where this run's files go
    device: str          # "cuda" or "cpu"
    seed: int
    tools: dict          # all tools, so one tool can call another (the 3D tool uses the image tool)
    config: object = None  # the HarnessConfig, for tool settings such as trellis_backend
    counter: dict = field(default_factory=dict)
    copies: dict = field(default_factory=dict)  # workspace file -> its edited working copy in run_dir
    pending: dict = field(default_factory=dict)  # tool name -> why it should still run (e.g. test an edit)
    imports: dict = field(default_factory=dict)  # a file the user named (any spelling) -> its copy in run_dir
    request: str = ""    # the user's own words, so a tool can check the LLM's arguments against them
    agent: object = None  # the Agent: a tool that runs its own loop (ask_subagent) uses its LLM and call_tool()
    facts: dict = field(default_factory=dict)  # what a tool measured, for a later tool: the last test run (review_code)
    notes: list = field(default_factory=list)  # lines the harness adds after the final answer (HARNESS NOTE in agent.py)

    def new_path(self, stem, suffix):
        """outputs/run_.../image_01.png, image_02.png, ..."""
        self.counter[stem] = self.counter.get(stem, 0) + 1
        return self.run_dir / f"{stem}_{self.counter[stem]:02d}{suffix}"


def find_file(path, run_dir):
    """Tools that take a file from an earlier tool get a path written by the LLM: it may be absolute,
    relative to the project, or just a file name. Returns the file, or None."""
    from ..config import PROJECT_DIR
    p = Path(path)
    for candidate in (p, PROJECT_DIR / path.lstrip("/\\"), run_dir / p.name):
        if candidate.is_file():
            return candidate
    return None


class Refusal(Exception):
    """
    Raised by a tool that refuses on purpose: an input outside a correlation's valid range, a DELETE on a read-only
    database, a formula that reaches outside the workbook. The LLM gets the reason as an error, as with any failure,
    but the agent loop's completion checks stay quiet afterwards (like a "no" at APPROVAL): asked to try again, a 3B
    model changes the user's numbers until something runs.
    """


class Tool:
    name = ""            # what the LLM writes to call it
    description = ""     # tells the LLM when to use it (this text goes in the prompt!)
    parameters = {}      # JSON Schema of the arguments (also goes in the prompt)
    model_id = ""        # the model behind it
    vram_gb = 0.0        # rough peak GPU memory it needs; the harness frees room first
    how_it_works = ""    # printed by the tracer, for humans only
    check_unused = True  # False for helper tools (read, search, run): the completion check ignores them
    repeatable = False   # True if calling it again with the same arguments can give a new result
    requires = ()        # tools that must run first, if the router offered them (e.g. search before writing)
    needs_approval = False  # True: a person says yes or no before each call runs (APPROVAL in agent.py)

    def approval_question(self, args, ctx):
        """What the APPROVAL stage asks a person before this tool runs (only if needs_approval)."""
        return f"The agent wants to call {self.name}({json.dumps(args)}). Allow it?"

    def prepare(self, ctx):
        """Called when the router offers this tool, before its description goes into the prompt. Most tools have a
        fixed description; query_database writes its schema into it here, ask_subagent the list of agent files."""

    def schema(self):
        """The OpenAI-style function definition that the chat template turns into prompt text."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def validate(self, args):
        """Plain-Python checks on the LLM's arguments. LLMs make mistakes; the harness must not crash."""
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        props = self.parameters.get("properties", {})
        missing = [k for k in self.parameters.get("required", []) if args.get(k) in (None, "", [])]  # 0 is fine
        if missing:
            raise ValueError(f"missing required argument(s): {', '.join(missing)}")
        clean = {}
        for key, value in args.items():
            spec = props.get(key)
            if spec is None:
                continue  # ignore arguments the tool does not have
            if spec.get("type") == "integer":
                value = int(float(value))
                value = max(spec.get("minimum", value), min(spec.get("maximum", value), value))
            elif spec.get("type") == "string":
                value = str(value)
                if "enum" in spec and value not in spec["enum"]:
                    value = spec["enum"][0]
            elif spec.get("type") == "array":  # small LLMs sometimes send a list as one string
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        if value.lstrip().startswith("["):  # a broken JSON list: keep the items that are complete
                            value = [json.loads(f'"{m}"') for m in re.findall(r'"((?:[^"\\]|\\.)*)"', value)]
                        else:
                            value = re.split(r"\n|;|\|", value)
                value = [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]
            clean[key] = value
        return clean

    def gpu_modules(self):
        """Models this tool keeps on the GPU between calls, so the harness can park them in RAM."""
        return {}

    def run(self, ctx, **args):
        """
        Do the work. Return {"artifacts": [{"type": ..., "path": ...}], "info": {...}}, plus optionally
        "observations": {...}: text for the LLM that is not about a file (what a file says, search results).
        """
        raise NotImplementedError
