"""
The tracer: records and prints every stage that data passes through.

This file has nothing to do with AI. It exists so you can SEE the harness work: every time
data moves from one component to another, the code wraps that step in

    with tracer.stage("ROUTER", kind="classifier", component=model_id,
                      data_in="text", data_out="tool names") as ev:
        ...do the work...
        ev.summary = "offered 2 tools"

and the tracer prints a coloured header, times the step, and keeps a record of it for the
end-of-run summary, trace.json and the HTML report. It also keeps everything it prints, in order
(Tracer.log), so report.html can replay the console with the same colours.
"""

import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

# What kind of component handled a stage. The colour makes the pattern visible in the console:
# cheap plain-Python glue (grey) wraps a few expensive neural networks (colours).
KINDS = {
    "python":     ("plain Python, no ML",          "\033[90m"),  # grey
    "classifier": ("classifier model",             "\033[33m"),  # yellow
    "llm":        ("large language model",         "\033[36m"),  # cyan
    "generator":  ("generative model (a tool)",    "\033[35m"),  # magenta
    "perception": ("perception model",             "\033[32m"),  # green
    "retrieval":  ("embedding model (search)",     "\033[38;5;130m"),  # brown
    "remote":     ("remote API, another computer", "\033[31m"),  # red
    "server":     ("local server, other process",  "\033[34m"),  # blue
}
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"

if os.name == "nt":
    os.system("")  # switches on ANSI colour codes in the classic Windows console


@dataclass
class Event:
    index: int
    stage: str          # what the harness is doing, e.g. "INPUT GUARD"
    kind: str           # one of KINDS
    component: str      # which model or function did it
    data_in: str        # the type of data going in, e.g. "text"
    data_out: str       # the type of data coming out, e.g. "image"
    summary: str = ""   # one line: what happened
    seconds: float = 0.0
    details: dict = field(default_factory=dict)    # anything else worth inspecting
    artifacts: list = field(default_factory=list)  # files written during this stage


class Tracer:
    def __init__(self, verbosity=1, color=True):
        self.verbosity = verbosity
        self.color = color and os.environ.get("NO_COLOR") is None
        self.events = []
        # Everything the console shows, in order, at any verbosity: banners, stage starts and ends, the lines said
        # inside them and the LLM's streamed tokens. report.html replays it, with the console's colours.
        self.log = []
        self.labels = []         # the sub-agents we are inside, e.g. ["SUBAGENT 1/3"] (see nested())
        self._line_start = True

    def reset(self):
        self.events = []
        self.log = []

    @contextmanager
    def nested(self, label):
        """Stages inside this block belong to a sub-agent: they are named 'SUBAGENT 1/3 > LLM' and indented."""
        self.labels.append(label)
        try:
            yield
        finally:
            self.labels.pop()

    # ---- printing helpers ---------------------------------------------------------------
    def emit(self, text="", end="\n"):
        """print(), but never crash on characters the console can't show (e.g. IPA phonemes)."""
        if self.labels:  # inside a sub-agent: indent every line, so the nesting shows in the console
            pad = "    |  " * len(self.labels)
            text = (pad if self._line_start else "") + str(text).replace("\n", "\n" + pad)
        self._line_start = end == "\n"
        try:
            print(text, end=end, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(text.encode(encoding, errors="replace").decode(encoding), end=end, flush=True)

    def _c(self, code, text):
        return f"{code}{text}{RESET}" if self.color else text

    def say(self, text, level=1, kind=None, indent=5):
        """Print an indented line (only if verbosity >= level). The report keeps it either way."""
        self.log.append({"type": "line", "text": str(text), "kind": kind, "level": level, "indent": indent,
                         "depth": len(self.labels)})
        if self.verbosity < level:
            return
        pad = " " * indent
        text = str(text).replace("\n", "\n" + pad)
        self.emit(pad + (self._c(KINDS[kind][1], text) if kind else text))

    def write(self, text, kind=None):
        """Print without a newline (used to stream LLM tokens)."""
        last = self.log[-1] if self.log else {}
        if last.get("type") == "stream" and last["kind"] == kind:  # one record per stretch of tokens
            last["text"] += text
        else:
            self.log.append({"type": "stream", "text": text, "kind": kind, "depth": len(self.labels)})
        if self.verbosity >= 1:
            self.emit(self._c(KINDS[kind][1], text) if kind else text, end="")

    def banner(self, text):
        self.log.append({"type": "banner", "text": text})
        if self.verbosity >= 1:
            self.emit("\n" + self._c(BOLD, "=" * 100) + "\n" + self._c(BOLD, text) + "\n"
                      + self._c(BOLD, "=" * 100))

    # ---- the main API -------------------------------------------------------------------
    @contextmanager
    def stage(self, stage, kind, component, data_in, data_out):
        stage = " > ".join(self.labels + [stage])  # e.g. "SUBAGENT 1/3 > PROMPT ASSEMBLY"
        ev = Event(len(self.events) + 1, stage, kind, component, data_in, data_out)
        self.events.append(ev)
        self.log.append({"type": "start", "event": ev.index})
        if self.verbosity >= 1:
            label, code = KINDS[kind]
            head = f"[{ev.index}] {stage}"
            self.emit("\n" + self._c(code + BOLD, f"--- {head} ") + self._c(code, "-" * max(3, 60 - len(head)))
                      + self._c(DIM, f"  {label}"))
            self.emit(self._c(DIM, f"     {component}   |   {data_in}  ->  {data_out}"))
        start = time.perf_counter()
        try:
            yield ev
        except Exception as exc:
            ev.summary = f"FAILED: {type(exc).__name__}: {exc}"
            raise
        finally:
            ev.seconds = time.perf_counter() - start
            self.log.append({"type": "end", "event": ev.index})
            if self.verbosity >= 1:
                self.emit(self._c(KINDS[kind][1], f"     => {ev.summary}") + self._c(DIM, f"   ({ev.seconds:.2f}s)"))
                for path in ev.artifacts:
                    self.emit(self._c(BOLD, f"     file: {path}"))

    # ---- after the run ------------------------------------------------------------------
    def print_summary(self):
        if self.verbosity < 1 or not self.events:
            return
        self.emit("\n" + self._c(BOLD, "DATA FLOW SUMMARY  (each row is one hop the data took)"))
        self.emit(self._c(DIM, f"  {'#':>2}  {'stage':<24} {'handled by':<28} {'data in -> data out':<36} {'time':>7}"))
        for ev in self.events:
            flow = f"{ev.data_in} -> {ev.data_out}"
            line = f"  {ev.index:>2}  {ev.stage:<24} {KINDS[ev.kind][0]:<28} {flow:<36} {ev.seconds:>6.2f}s"
            self.emit(self._c(KINDS[ev.kind][1], line))
        by_kind = {}
        for ev in self.events:
            by_kind[ev.kind] = by_kind.get(ev.kind, 0) + ev.seconds
        total = sum(by_kind.values())
        parts = ", ".join(f"{KINDS[k][0]} {v:.1f}s" for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]))
        self.emit(self._c(DIM, f"  total {total:.1f}s  =  {parts}"))

    def save_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump([asdict(ev) for ev in self.events], f, indent=2, default=str)


def shorten(text, limit=300):
    """Trim long strings for display, keeping the start and the end."""
    text = str(text)
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]} ...[{len(text) - limit} chars hidden]... {text[-half:]}"

