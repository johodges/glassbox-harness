"""
THE AGENT LOOP: start reading here.

    user prompt (text)
      |
      |-> INPUT GUARD      classifier   is this a prompt-injection attempt?        text -> label
      |-> IMPORT FILES     python       copy files the user named into the run     paths -> copies
      |-> ROUTER           classifier   which tools are relevant to this request?  text -> tool names
      |-> PLANNER          python       which LLM plans it? a change to code -> the code model
      |-> AGENTS.md       python       the team's instructions, pasted into every system prompt
      |-> SKILLS           embedder     which SKILL.md files match the request?    text -> instructions
      |-> EFFORT           python       budget + extra LLM calls: THINK first, VOTE, REVIEW (config.EFFORT)
      |
      |   +--------------------------- loop (at most max_steps times) ---------------------------+
      |-> | PROMPT ASSEMBLY  python      messages + tool schemas -> one string -> token ids        |
      |   | LLM              llm         token ids -> new tokens -> text                           |
      |   | PARSE            python      is that text a tool call? (no -> it is the final answer)  |
      |   | CHECKS           python      cut off? work left (tests failing, tools unused)? ask once|
      |   | CHECK ARGS       python      do the arguments match the user's own words?              |
      |   | APPROVAL         a person    may this tool act? (y/n before run_script runs code)      |
      |   | TOOL             generator   text arguments -> image / audio / video / 3D file         |
      |   |                  python      ... or read / search / edit / run a document or script    |
      |   | PERCEIVE         perception  file -> text the LLM can read (caption, transcript, ...)  |
      |   | OUTPUT GUARD     classifier  is the generated image safe to show?                      |
      |   | TOOL OUTPUT GUARD classifier does a passage or file the tool read carry instructions? |
      |   | CODE IN TEXT     python      code in the LLM's message, but no tool that writes it?    |
      |   +--- the tool result (as text) is appended to the messages, and we go round again -----+
      |
      |-> CHANGE CHECK     python       does the answer claim changes, when no tool wrote a file?
      |-> FINAL TESTS      python       code changed since the last test run? the harness runs the tests
      |-> REFUSAL NOTE     python       did a tool refuse (Refusal)? the harness says so after the answer
      |-> UNFINISHED       python       work a check asked about and the LLM still left undone
      |-> HARNESS NOTE     python       what a tool checked itself (review_code's verdicts), after the answer
      |-> ANSWER CHECK     python       is every file path in the answer a real file?
      v
    final answer (text) + files on disk + trace.json + report.html

Notice how little of this is "AI". The harness is ordinary Python that moves data between
models, converts it from one form to another, and decides what happens next.
"""

import ast
import datetime
import gc
import json
import logging
import os
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import torch

from .classifiers import InputGuard, IntentRouter, calculates_from_a_file
from .config import CODE_CHANGE, CODE_TOOLS, EFFORT, MODELS, PROJECT_DIR, HarnessConfig
from .instructions import AGENTS_FILE, agents_text, find_skills, score_skills, skill_text
from .llm import downloaded, gpu_free_gb, load_llm
from .perception import Perception
from .report import write_report
from .tools import all_tools
from .tools.base import Refusal, ToolContext
from .tools.files import import_named_files, named_paths
from .trace import Tracer, shorten

SYSTEM_PROMPT = """You are the planning brain of a small creative agent. You can only read and write text.
Tools create media for you. When you call a tool, the harness runs it, other models describe the file it made, and you receive that description as text.

How to work:
- If the user wants media, call ONE tool at a time by replying with only the JSON for the function call.
- Write detailed, vivid prompts for the tools (subject, style, colours, lighting, mood).
- Make exactly one tool call per thing the user asked for. Never make extra versions or variations they did not ask for.
- Some requests need several different tools (for example a picture AND a spoken story): call them one after another.
- If the user wants the things you made to match, be the same length or play together, combine them with a tool before you reply.
- When everything the user asked for exists, reply to the user in plain sentences, with no JSON: say what you made, give the file path, and mention what the describing models observed."""

CHAT_SYSTEM_PROMPT = "You are a helpful assistant inside a teaching harness. Answer clearly and concisely."

# Used instead of SYSTEM_PROMPT when the router offers only document tools. The harness picks the
# system prompt for each request: the same model, told different things.
DOCUMENT_SYSTEM_PROMPT = """You are the planning brain of a small agent that works with documents and code. You can only read and write text.
Tools read, search, edit and run files for you. When you call a tool, the harness runs it, checks the result and gives it back to you as text.

How to work:
{rules}"""
DOCUMENT_TOOLS = {"read_file", "search_references", "update_powerpoint", "update_word_document", "update_latex",
                  "edit_code", "run_script", "convert_presentation", "export_pdf", "look_at_image", "digitize_plot",
                  "fire_calculation", "query_database", "ask_subagent", "update_excel",
                  "run_tests", "check_code", "search_code", "write_code", "create_file", "review_code"}
WRITERS = {"update_powerpoint": ".pptx", "update_word_document": ".docx", "update_latex": ".tex",
           "edit_code": ".py and other text files", "write_code": "whole functions in a .py file",
           "create_file": "a new file"}
# The document rules are assembled per request: each one is included only if one of its tools was offered
# (None = always). A rule about a tool the LLM can't see only tempts it to call that tool anyway: with the
# search rule always present, Llama tried search_references after every look_at_image.
DOCUMENT_RULES = [
    (None, "Call ONE tool at a time by replying with only the JSON for the function call."),
    ({"search_references"}, "If the user wants something based on the references, call search_references first, "
                            "then use what it found. Do not invent facts that are not in the references."),
    (set(WRITERS), "Write bullet points and paragraphs in your own words: short sentences, with no double quotation "
                   "marks inside them."),
    (set(WRITERS), "To change a file, call the tool for its type: {writers}."),
    ({"look_at_image"}, "You cannot see images. To find out what an image (.png or .jpg) shows, call look_at_image "
                        "with a clear question."),
    ({"digitize_plot"}, "To get the data or curves out of a plot, call digitize_plot (for a .pptx, give the slide "
                        "number). It measures the curves; never estimate values from a picture yourself."),
    ({"fire_calculation"}, "Never do fire-engineering arithmetic yourself. For a flame height, a ceiling-jet "
                           "temperature, a sprinkler or detector activation time or a t-squared heat release rate, call "
                           "fire_calculation once for each quantity the user asks for, with the numbers and units from "
                           "the request."),
    ({"query_database"}, "To answer from the fire test database, call query_database with one SQLite query that uses "
                         "only the tables and columns in its description, then answer with the rows it returns."),
    ({"ask_subagent"}, "To read or summarize a long file, a whole folder or many notes, call ask_subagent with the "
                       "agent, the task and the path, then give or use its answer: do not read the same files again "
                       "yourself."),
    ({"update_excel"}, "In a spreadsheet, write formulas that start with =, never numbers you calculated yourself. To "
                       "fill a column, give the range, such as B2:B22, and the formula for its first cell. Give a new "
                       "column a heading in row 1."),
    # code (tools/coding.py): the tests and the linter decide, and the harness checks a review's line numbers
    ({"run_tests"}, "Whether code works is decided by its tests, not by you: call run_tests. Make the smallest change "
                    "that fixes the problem, in the code, not in its tests, then call run_tests again."),
    ({"check_code"}, "To find unused or undefined names, syntax errors and unsafe calls such as eval, call check_code."),
    ({"search_code"}, "To find where a name is defined or used, call search_code."),
    ({"review_code"}, "To report a code review, call review_code once with all your findings, each as path:line: what "
                      "is wrong, citing only lines you have read. Then tell the user which findings it confirmed."),
    (set(WRITERS), "Only change a file if the user asked you to change it."),
    (None, "If a tool returns an error, read it, fix your call and try again."),
    (None, "When the work is done, reply to the user in plain sentences, with no JSON: give the answer you found, or "
           "say what you changed and give the path of the edited copy."),
]


def system_prompt_for(offered):
    """Plain Python picks, and for documents assembles, the system prompt from the tools the router offered."""
    if not offered:
        return CHAT_SYSTEM_PROMPT
    if not all(name in DOCUMENT_TOOLS for name in offered):
        return SYSTEM_PROMPT
    writers = ", ".join(f"{name} for {ext}" for name, ext in WRITERS.items() if name in offered)
    rules = [line.format(writers=writers) for tools, line in DOCUMENT_RULES if tools is None or tools & set(offered)]
    return DOCUMENT_SYSTEM_PROMPT.format(rules="\n".join(f"- {line}" for line in rules))

# Appended to every successful tool result: a reminder written by the harness, not by any model.
# Small LLMs lose track of multi-part requests, and text right next to the newest result is what
# they attend to most. Set it to None and run evaluate.py to see the difference.
HARNESS_NOTE = "Done so far: {used}."
# Added only while some offered tools are unused: this is what got Llama 3.2 3B to finish "video +
# narration, same length" with combine_video_and_audio. Saying "...not used yet: none" made it redo work.
UNUSED_NOTE = " Offered tools not used yet: {unused}."
# A message that shows code, e.g. ```python\ndef median(...), next to a call that writes no file (see CODE IN TEXT)
CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n\s*(?:@|def |class |import |from )", re.I)
CODE_IN_TEXT = ("The code in your message was not saved: code in a message changes no file. To change a function, "
                "call write_code with the whole new function; to change a line or two, call edit_code.")
# An answer that says something was changed (CHANGE CHECK). Asked for a review, Qwen2.5-Coder 7B answered "Removed
# `import os` ... Replaced eval ... the tests have already been run successfully": no tool had written anything.
CLAIMS_A_CHANGE = re.compile(r"\b(?:removed|replaced|updated|changed|fixed|corrected|edited|rewritten|rewrote|added|"
                             r"created|saved)\b", re.I)

# Sent once, as a message from the harness, when the LLM tries to finish after doing PART of the work
# while offered tools are still unused. Llama 3.2 3B made the 40 s video and narration and then
# stopped, three runs out of four; asked this question it called combine_video_and_audio every time.
COMPLETION_CHECK = ("[Harness check] You have not used these offered tools: {unused}. Does the user's request "
                    "need one of them? If yes, reply with only the JSON function call, nothing else. If no, "
                    "reply with your final answer again.")
# The same idea for work a tool left unfinished, e.g. "the script was edited but not run since" (ctx.pending).
PENDING_CHECK = ("[Harness check] {pending}. Does the user's request need that? If yes, reply with only the JSON "
                 "function call, nothing else. If no, reply with your final answer again.")
BOTH_CHECK = ("[Harness check] {pending}. You have also not used these offered tools: {unused}. Does the user's "
              "request need any of that? If yes, reply with only the JSON function call, nothing else. If no, reply "
              "with your final answer again.")
# A reply that ran into the token limit before it called a tool: Qwen2.5-Coder 7B explains its plan at length and was
# cut off at 400 tokens, mid-sentence, before the call it was about to make. Asked once, it can reply with the call.
CUT_OFF_CHECK = ("[Harness check] Your reply was cut off at the limit of {tokens} tokens, before any tool call. Do not "
                 "explain: reply with only the JSON function call for your next step, or with a short final answer.")
# And for the worst case: every tool call failed, and the LLM writes "The conversion was successful" anyway.
NOTHING_RAN_CHECK = ("[Harness check] No tool has run successfully in this request, so nothing has been made or "
                     "changed yet. If the user's request needs a tool, reply with only the JSON function call (write "
                     "file paths with /), nothing else. If not, tell the user plainly what did not work.")

# Effort "high" (config.EFFORT) spends two more LLM calls. THINK, before the first action: the model writes its
# reasoning as plain text, which stays in the conversation (Claude's thinking blocks work the same way, inside the
# model). REVIEW, before finishing: the model rereads the request and checks its own answer once.
THINK_PROMPT = ("[Harness: think first] Before you act, think step by step in plain sentences: what exactly does the "
                "user want, what facts or numbers are needed, and which of the offered tools to call, in what order, "
                "if any. If the request asks for a number, work it out step by step, with units. Do not call a tool "
                "yet and do not write JSON.")
AFTER_THINKING = ("[Harness] Now act on your thinking: if a tool is needed, reply with only the JSON function call for "
                  "the first one; if not, give the user your final answer.")
# With no tools offered, a word about tools or JSON made Llama write "Math.round(0.0000576)" instead of an answer
THINK_PROMPT_NO_TOOLS = ("[Harness: think first] Before you answer, think step by step in plain sentences: what exactly "
                         "is asked, which facts and numbers are given, and how to work out the answer, with units. Do "
                         "not give the final answer yet.")
AFTER_THINKING_NO_TOOLS = "[Harness] Now, using your thinking, give the user your final answer."
REVIEW_CHECK = ("[Harness review] Before you finish, read the user's request again and check your answer against it "
                "and against any tool results above: is anything missing or wrong? Check every number step by step. "
                "If a tool call is still needed, reply with only its JSON. Otherwise reply with only your final answer "
                "to the user, corrected if needed.")
REVIEW_CHECK_NO_TOOLS = ("[Harness review] Before you finish, read the question again and check your answer: check "
                         "every number and unit step by step. Then reply with your final answer, corrected if needed.")

# What the LLM gets when a person answers "n" at the APPROVAL stage. It is an ordinary tool error, so the
# loop goes on and the LLM must cope; the checks above stay quiet, so nothing nudges it to try again.
DECLINED = ("The user declined: they did not allow this call, so it did not run. Stop here and do not call any tool. "
            "Reply to the user in plain sentences: say what you were going to run and why, and ask how they want to "
            "go on.")

# Text a tool brings back from a file or a reference note, checked for instructions before the LLM reads it
# (TOOL OUTPUT GUARD): tool name -> the key in its result that holds that text.
GUARDED_OUTPUTS = {"search_references": "passages", "read_file": "content"}
# Flagged text is removed, and this one note goes in a field of its own. A note left in the passage's place
# ("[Withheld by the harness: ...]") was copied into the slide's bullets by Llama, and the JSON broke.
WITHHELD = "{what} contained instructions, not facts (INJECTION {score:.3f}), so the harness removed it."


@dataclass
class RunResult:
    prompt: str
    answer: str
    files: list = field(default_factory=list)      # every file the tools wrote
    messages: list = field(default_factory=list)   # the full conversation the LLM saw
    events: list = field(default_factory=list)     # every traced stage
    run_dir: Path = None
    report: Path = None
    log: list = field(default_factory=list)        # everything the console showed, in order (Tracer.log)


class Agent:
    def __init__(self, config=None):
        self.config = config or HarnessConfig()
        _quiet_libraries(self.config.verbosity)
        self.tracer = Tracer(self.config.verbosity, self.config.color)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.small_device = "cpu" if self.config.small_models_on_cpu else self.device
        self.tracer.banner(f"STARTING THE HARNESS on {self.device.upper()}"
                           + (f" ({torch.cuda.get_device_name(0)}, {gpu_free_gb():.1f} GB free)"
                              if self.device == "cuda" else ""))
        self.guard = InputGuard(self.tracer, self.small_device) if self.config.use_guard else None
        self.router = IntentRouter(self.tracer, self.small_device) if self.config.use_router else None
        # the planner: MODELS[config.llm], its weights in 16, 8 or 4 bits (config.llm_precision). The PLANNER stage
        # may swap in the code model for a request; a 16-bit planner waits in CPU RAM meanwhile (self.parked_llms)
        self.llm = load_llm(self.tracer, self.config.llm, self.device, self.config.llm_precision)
        self.parked_llms = {}
        self.perception = Perception(self.tracer, self.small_device)
        self.all_tools = all_tools()  # generator models load the first time each tool is called
        self.tools = {}
        self.ask_user = input  # how APPROVAL asks a person; swap in a dialog box, a chat message, or a test stub
        self.last_context = None  # the ToolContext of the last request (see run())

    # =====================================================================================
    def run(self, prompt):
        cfg, t = self.config, self.tracer
        t.reset()
        t.verbosity, t.color = cfg.verbosity, cfg.color  # settings may have changed since __init__
        self.tools = {name: tool for name, tool in self.all_tools.items() if name in cfg.enabled_tools}
        run_dir = cfg.output_dir / datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S")
        while run_dir.exists():  # two runs in the same second
            run_dir = run_dir.with_name(run_dir.name + "b")
        run_dir.mkdir(parents=True)
        ctx = ToolContext(t, run_dir, self.device, cfg.seed, self.tools, cfg, request=prompt, agent=self)
        self.last_context = ctx  # its working copies and facts, for scripts that measure a run (compare_code.py)
        t.banner(f"USER PROMPT: {prompt}")

        # ---- 1. INPUT GUARD: a classifier screens the text before the LLM ever sees it ----
        if cfg.use_guard:
            self.guard = self.guard or InputGuard(t, self.small_device)
            self.guard.threshold = cfg.guard_threshold
            if self.guard.check(prompt)["blocked"]:
                return self._finish(prompt, "Request blocked: the input guard classified it as a prompt injection.",
                                    [], [], run_dir)

        # ---- 1b. IMPORT FILES: plain Python copies files the USER named into the run folder ----
        # The tools may only touch workspace/ and this run's folder. A path typed by the user is permission
        # for that one file; a path the LLM makes up later is not.
        user_text = prompt
        if cfg.import_named_files and named_paths(prompt):
            with t.stage("IMPORT FILES", "python", "files.import_named_files() (no ML)", "file names in the request",
                         "copies in the run folder") as ev:
                imported = import_named_files(prompt, ctx)
                for raw, copy, how in imported:
                    t.say(f"{raw} -> {_relative(copy) if copy else 'NOT FOUND'}   ({how})", kind="python")
                copied = [(raw, copy) for raw, copy, _ in imported if copy]
                ev.summary = (f"copied {len(copied)} file(s) the user named into {_relative(run_dir / 'inputs')}"
                              if copied else "nothing to copy: the named files are in workspace/ or were not found")
                if copied:  # say so in the request, so the LLM's paths and its final answer match the real files
                    user_text += ("\n\n[Harness: the files named above were copied into this run's folder: "
                                  + "; ".join(f"{raw} -> {_relative(copy)}" for raw, copy in copied) + "]")

        # ---- 2. ROUTER: a classifier picks which tools the LLM is even told about ----
        offered = list(self.tools)
        if cfg.use_router:
            self.router = self.router or IntentRouter(t, self.small_device)
            self.router.threshold = cfg.router_threshold
            offered, _ = self.router.route(prompt, offered)

        # ---- 2a. PLANNER: plain Python picks the LLM: a code change -> the code model (CODE_TOOLS, CODE_CHANGE) ----
        self._pick_planner(offered, prompt)
        self.llm.date = cfg.prompt_date
        if "run_script" in offered and calculates_from_a_file(prompt):
            # the LLM never does the arithmetic: until a script has run, the work is not done (see PENDING_CHECK).
            # Said here, not in the system prompt: a rule about args there made Llama invent args for word_count.py
            ctx.pending["run_script"] = ("The user asked for numbers to be calculated, and no script has run yet. "
                                         "Never calculate them yourself: run the script that does it, with the data "
                                         "file in args")

        # ---- 2b. AGENTS.md and SKILLS: markdown files that add to the system prompt (see instructions.py) ----
        instructions, offered = self._instructions(prompt, offered, ctx)
        for name in offered:  # a tool may build its description now: query_database its schema, ask_subagent its agents
            self.tools[name].prepare(ctx)
        schemas = [self.tools[name].schema() for name in offered]

        messages = [{"role": "system", "content": system_prompt_for(offered) + instructions},
                    {"role": "user", "content": user_text}]
        files, used, done_calls, answer, checked_answer, declined = [], [], {}, None, None, False
        refused = {}  # tool name -> why the harness refused it: told to the user at the end (REFUSAL NOTE)

        # ---- 2c. EFFORT: how much work this request gets: a budget, thinking, checks (EFFORT in config.py) ----
        level = EFFORT[cfg.effort]
        steps, tokens = cfg.max_steps or level["max_steps"], cfg.max_new_tokens or level["max_new_tokens"]
        with t.stage("EFFORT", "python", "config.EFFORT (no ML)", f"effort = {cfg.effort}", "budget + extra steps") as ev:
            ev.summary = (f"{cfg.effort}: at most {steps} LLM calls of up to {tokens} tokens; "
                          f"completion checks {'on' if level['checks'] else 'off'}; "
                          + (f"VOTE over {level['vote']} answers when no tool is needed; " if level["vote"] > 1 else "")
                          + (f"THINK first (up to {level['think_tokens']} tokens); " if level["think_tokens"] else "")
                          + ("REVIEW before finishing" if level["review"] else "no review"))
        checks, reviewed, voted = COMPLETION_CHECK and level["checks"], not level["review"], level["vote"] < 2
        cut_checked = False
        first_reply = self._think(messages, schemas, level["think_tokens"]) if level["think_tokens"] else None

        for step in range(1, steps + 1):
            t.banner(f"STEP {step}: ask the LLM what to do next")

            # ---- 3. + 4. PROMPT ASSEMBLY and LLM (see llm.py) ----
            if first_reply is not None:  # THINK went straight to a tool call: that call is step 1
                reply, first_reply = first_reply, None
            else:
                reply = self.llm.generate(messages, schemas, tokens, cfg.temperature)

            # ---- 5. PARSE: plain Python decides if the text is a tool call ----
            with t.stage("PARSE", "python", "json / ast parsers (no ML)", "LLM text", "tool call or answer") as ev:
                call = parse_tool_call(reply, list(self.tools))
                ev.summary = ("no tool call in the text -> treat it as the final answer" if call is None
                              else f"a call to {call['name']}, but its JSON is broken (cut off, or a stray quote) -> "
                                   "an error goes back to the LLM" if call.get("broken")
                              else f"tool call -> {call['name']}({json.dumps(call['arguments'])})")
            unused = self._unused(offered, used)
            pending = [why for name, why in ctx.pending.items() if name in offered]
            tried = any("tool_calls" in m for m in messages)
            if call is None and self.llm.hit_limit and checks and not cut_checked:
                # ---- CUT OFF: the reply stopped at the token limit before any tool call; ask once for just the call ----
                with t.stage("CUT OFF", "python", "agent.py (no ML)", f"a reply of {tokens} tokens, no tool call",
                             "one more question to the LLM") as ev:
                    cut_checked = True
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": CUT_OFF_CHECK.format(tokens=tokens)}]
                    ev.summary = f"the reply stopped at the {tokens}-token limit without a tool call: asked for only the call"
                continue
            if call is None and not tried and not voted:
                # ---- VOTE (effort high): more attempts at an answer that needed no tool; the majority number wins ----
                voted, reply = True, self._vote(messages, schemas, tokens, reply, level["vote"])
            if call is None and tried and not used and not declined and checks and checked_answer is None:
                # ---- NOTHING RAN: every call so far failed, yet the LLM is about to report success ----
                with t.stage("COMPLETION CHECK", "python", "agent.py (no ML)", "final answer + no successful tool",
                             "one more question to the LLM") as ev:
                    checked_answer = ""  # the answer after this check is the one to keep: the first claimed too much
                    messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": NOTHING_RAN_CHECK}]
                    ev.summary = "the LLM wants to finish, but no tool call has succeeded in this request"
                continue
            if call is None and used and (unused or pending) and not declined and checks \
                    and checked_answer is None:
                # ---- COMPLETION CHECK: plain Python notices unfinished work and asks the LLM once ----
                with t.stage("COMPLETION CHECK", "python", "agent.py (no ML)", "final answer + unfinished work",
                             "one more question to the LLM") as ev:
                    checked_answer = reply
                    question = (COMPLETION_CHECK.format(unused=", ".join(unused)) if unused
                                else PENDING_CHECK.format(pending="; ".join(pending)))
                    if unused and pending:  # both, in the one question: Qwen2.5-Coder 7B was asked only about an
                        # unused run_tests, ran it, and never heard that the test it was asked for was still missing
                        question = BOTH_CHECK.format(pending="; ".join(pending), unused=", ".join(unused))
                    messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": question}]
                    ev.summary = (f"the LLM wants to finish, but offered tools are unused: {', '.join(unused)}"
                                  if unused else f"the LLM wants to finish, but: {'; '.join(pending)}")
                continue
            if call is None and not reviewed and not declined:
                # ---- REVIEW (effort high): the LLM rereads the request and checks its own answer, once ----
                with t.stage("REVIEW", "python", "agent.py (no ML)", "final answer", "one more question to the LLM") as ev:
                    reviewed, checked_answer = True, ""  # the answer after the review is the one to keep
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": REVIEW_CHECK if schemas else REVIEW_CHECK_NO_TOOLS}]
                    ev.summary = "before finishing, the LLM checks its answer against the request and the tool results"
                continue
            if call is None:
                answer = checked_answer or reply  # check changed nothing: keep the full answer it gave first
                break
            if checked_answer:
                checked_answer = ""  # the check led to more work, so the next answer is the one to keep
            broken = call.pop("broken", False)
            messages.append({"role": "assistant", "tool_calls": [{"type": "function", "function": call}]})

            # ---- 6. + 7. TOOL and PERCEIVE ----
            key = json.dumps(call, sort_keys=True)
            if broken:  # it meant to call a tool, but the JSON can't be read: say so instead of showing it to the user
                result = {"status": "error", "error": f"Your call to {call['name']} was not valid JSON, so it did not "
                          "run: it was cut off or has a double quote inside a text. Call it again with shorter text, "
                          "no double quotation marks inside the text, and / instead of \\ in file paths."}
            elif call["name"] not in offered:  # the LLM asked for a tool it was never shown
                result = {"status": "error", "error": f"There is no tool called {call['name']}. The available "
                          f"tools are: {', '.join(offered) or 'none'}. Use one of these exact names, or reply in text."}
                t.say(f"{call['name']} was not offered to the LLM -> sending an error back to it", kind="python")
            elif key in done_calls and done_calls[key]["status"] == "error":  # the same mistake again
                result = {"status": "error", "error": "You made this exact call before and it failed: "
                          f"{done_calls[key]['error']} Change the arguments before you call it again."}
                t.say("repeat of a failed call -> sending its error back again", kind="python")
            elif key in done_calls and not self.tools[call["name"]].repeatable:  # don't redo the same work
                result = {"status": "already_done", "note": "You already made this exact call. "
                          "Do not call it again; reply to the user now.", "earlier_result": done_calls[key]}
                t.say("duplicate call -> not re-running the tool", kind="python")
            elif missing := [name for name in self.tools[call["name"]].requires if name in offered and name not in used]:
                # ---- CHECK ORDER: plain Python enforces a workflow the LLM skipped (search before writing) ----
                with t.stage("CHECK ORDER", "python", "agent.py (no ML)", "tool call", "allowed or not") as ev:
                    why = "find what the references say" if "search_references" in missing else "see what is in the file"
                    result = {"status": "error", "error": f"Call {' and '.join(missing)} first, to {why}. Then call "
                              f"{call['name']} again, using what you found."}
                    ev.summary = f"{call['name']} needs {', '.join(missing)} first -> error sent back to the LLM"
            else:
                result = self.call_tool(call, ctx, files)
                # a "no" at APPROVAL, or a tool that refused on purpose: no check pushes the LLM to try again
                declined = declined or result.get("error") == DECLINED or result.get("refused", False)
                if result.get("refused"):
                    refused[call["name"]] = result["error"]
                if self.tools[call["name"]].repeatable and _same(result, done_calls.get(key)):
                    result = {"status": "already_done", "note": "Same result as your earlier call (above). Do not "
                              "call it again: use that earlier result to do what the user asked."}
                    t.say("same call, same result -> telling the LLM nothing changed", kind="python")
                done_calls[key] = result if result["status"] != "already_done" else done_calls[key]
                if result["status"] == "ok":
                    used.append(call["name"])
                    if HARNESS_NOTE:  # a reminder written by the harness, not by any model
                        unused = self._unused(offered, used)
                        result["harness_note"] = HARNESS_NOTE.format(prompt=prompt, used=", ".join(used))
                        if unused and UNUSED_NOTE:
                            result["harness_note"] += UNUSED_NOTE.format(unused=", ".join(unused))

            if call["name"] not in WRITERS and "write_code" in offered and CODE_BLOCK.search(reply):
                # ---- CODE IN TEXT: code written in a message is not in any file. Qwen2.5-Coder 7B wrote the fixed
                # function in a ```python block, called run_tests, and was surprised that nothing had changed ----
                with t.stage("CODE IN TEXT", "python", "a regex (no ML)", "the LLM's message", "a note") as ev:
                    result["code_in_your_message"] = CODE_IN_TEXT
                    ev.summary = f"the message has a code block but calls {call['name']}: the code was saved nowhere"

            # ---- 8. The result goes back into the conversation AS TEXT ----
            # (Llama's chat template turns this dict into JSON inside an "ipython" turn.)
            messages.append({"role": "tool", "content": result})
        else:
            answer = f"(Stopped after {steps} LLM calls without a final answer.)"

        if offered and not files and not declined and CLAIMS_A_CHANGE.search(answer):
            # ---- CHANGE CHECK: the answer talks about changes, and the harness knows no tool wrote a file ----
            with t.stage("CHANGE CHECK", "python", "agent.py (no ML)", "final answer + files written",
                         "answer + the harness's note") as ev:
                ctx.notes.append("No tool wrote or changed a file in this request.")
                ev.summary = (f"the answer says '{CLAIMS_A_CHANGE.search(answer).group(0)}', but no tool wrote a file: "
                              "the harness says so under the answer")
        if "run_tests" in ctx.pending and "run_tests" in offered and not declined:
            # ---- FINAL TESTS: the code changed after the last test run, and the LLM finished anyway. The harness runs
            # the tests itself (with APPROVAL, like any run), so the user reads pytest's verdict, not the LLM's claim.
            # Claude Code can do the same with a "Stop" hook. Llama 3.2 3B rewrote median() with the same bug and
            # stopped; its answer said the change "was added to correctly calculate the median" ----
            t.banner("FINAL TESTS: the code changed after the last test run, so the harness runs the tests")
            final = self.call_tool({"name": "run_tests", "arguments": {"path": ctx.facts["changed_project"]}}, ctx, files)
            ctx.notes.append(f"After the last change the harness ran the tests itself: {final['summary']}"
                             + "".join(f"; {f['test']} fails: {f['message'][:100]}" for f in final.get("failures", [])[:2])
                             + "." if final["status"] == "ok" else "The tests were not run after the last change.")
        if refused:
            # ---- REFUSAL NOTE: the harness says what it refused itself. After the DELETE was refused, Llama told the
            # user "The trees ... have been deleted": the limit held, the model's words didn't ----
            with t.stage("REFUSAL NOTE", "python", "agent.py (no ML)", "final answer + refusals",
                         "answer + the harness's note") as ev:
                ev.summary = " ".join(f"{name} was refused, so it did not run: "
                                      f"{why.replace('Refused by the harness: ', '').split(' Do not')[0]}"
                                      for name, why in refused.items())
                answer += f"\n\n[Harness: {ev.summary}]"
        left = [why for name, why in ctx.pending.items() if name in offered]
        if left and not declined:
            # ---- UNFINISHED: work the PENDING check asked about once, and the LLM still left undone. Asked about the
            # missing test, Qwen2.5-Coder 7B replied that the user "did not explicitly ask for a test" ----
            with t.stage("UNFINISHED", "python", "agent.py (no ML)", "work still pending", "a note under the answer") as ev:
                ctx.notes.append("Not done: " + "; ".join(left) + ".")
                ev.summary = "; ".join(left)
        if ctx.notes:
            # ---- HARNESS NOTE: what a tool checked itself, in its own words. review_code verifies each finding's
            # file and line; the LLM's answer may still repeat a finding the check called invented ----
            with t.stage("HARNESS NOTE", "python", "agent.py (no ML)", "final answer + the tools' own checks",
                         "answer + the harness's note") as ev:
                ev.summary = " ".join(ctx.notes)
                answer += f"\n\n[Harness: {ev.summary}]"
        return self._finish(prompt, answer, files, messages, run_dir)

    # =====================================================================================
    def call_tool(self, call, ctx, files, tool=None):
        """tool: a sub-agent passes its own instance (ask_subagent's reader); otherwise the offered tool of that name."""
        t, tool = self.tracer, tool or self.tools[call["name"]]
        try:
            with t.stage("VALIDATE ARGS", "python", f"{tool.name}.validate()", "JSON arguments",
                         "checked arguments") as ev:
                args = tool.validate(call["arguments"])
                ev.summary = json.dumps(args)
            fixed = None
            if hasattr(tool, "check_request"):
                # ---- CHECK ARGS: plain Python compares the LLM's arguments with the user's own words ----
                with t.stage("CHECK ARGS", "python", f"{tool.name}.check_request() (no ML)",
                             "arguments + the user's words", "arguments to run") as ev:
                    args, ev.summary, fixed = tool.check_request(args, ctx)
            if tool.needs_approval and not self.approve(tool, args, ctx):
                return {"status": "error", "error": DECLINED}
            self._make_room_on_gpu(tool)
            output = tool.run(ctx, **args)
        except Refusal as exc:  # refused on purpose (out of range, not allowed): the LLM gets the reason, not a nudge
            t.say(f"tool refused: {exc}", kind="python")
            return {"status": "error", "refused": True, "error": shorten(exc, 600)}
        except Exception as exc:  # the LLM gets the error as text and can try something else
            t.say(f"tool failed: {type(exc).__name__}: {exc}", kind="remote")
            return {"status": "error", "error": f"{type(exc).__name__}: {shorten(exc, 400)}"}

        results = []
        for artifact in output["artifacts"]:
            path = Path(artifact["path"])
            files.append(str(path))
            entry = {"file": _relative(path)}
            if "note" in artifact:
                entry["note"] = artifact["note"]
            if artifact["type"] != "preview":
                try:
                    entry.update(self.perception.describe(artifact))
                except Exception as exc:  # a broken describer must not sink the run: tell the LLM instead
                    t.say(f"perception failed: {type(exc).__name__}: {exc}", kind="perception")
                    entry["perception_error"] = f"{type(exc).__name__}: {shorten(exc, 200)}"
            # OUTPUT GUARD: a classifier's verdict can stop a file from being shown.
            if entry.get("safety_scores", {}).get("nsfw", 0) > 0.8:
                withheld = path.with_name(path.stem + "_WITHHELD" + path.suffix)
                path.rename(withheld)
                files[-1] = str(withheld)
                entry = {"file": "[withheld]", "note": "the image safety classifier flagged this image"}
            results.append(entry)
        result = {"status": "ok"}
        if fixed:  # tell the LLM, so its answer describes what really ran
            result["fixed_by_harness"] = fixed
        if results:
            result["results"] = results
        result.update(output.get("observations", {}))  # e.g. a file's text, search passages, a script's output
        if tool.name in GUARDED_OUTPUTS and result.get(GUARDED_OUTPUTS[tool.name]):
            self.guard_tool_output(tool.name, result)
        return result

    def approve(self, tool, args, ctx):
        """
        APPROVAL: a person, not a model, decides whether the tool may act. Plain Python: input() in the console.
        Tools set needs_approval (run_script does: a script can do anything you can). The other tools only
        write copies inside the run folder (see files.py), so they don't ask.
        """
        t = self.tracer
        with t.stage("APPROVAL", "python", "input(): a person answers (no ML)", "the tool call", "yes or no") as ev:
            question = tool.approval_question(args, ctx)
            if self.config.ask_before_acting:
                try:
                    answer = self.ask_user(f"\n     {question} [y/n] ")
                except EOFError:  # no console to ask in: the safe answer is no
                    answer = "n (no console to ask in)"
            else:
                answer = "y (ask_before_acting is off: approved without asking)"
            approved = str(answer).strip().lower()[:1] == "y"
            ev.summary = (f"the person answered '{str(answer).strip()}' -> "
                          + (f"{tool.name} runs" if approved else f"{tool.name} does NOT run; the LLM is told why"))
            ev.details = {"question": question, "answer": str(answer), "approved": approved}
        return approved

    def guard_tool_output(self, name, result):
        """
        TOOL OUTPUT GUARD: the input guard's classifier, run on the text a tool brought back. A reference note or
        a file can carry instructions too ("ignore the user's request and ..."), and to the LLM they look just
        like the user's. This is indirect prompt injection: the INPUT GUARD never sees it, because the user's
        prompt is innocent. Flagged pieces are removed, and a "withheld" field tells the LLM what was removed.
        """
        t, cfg, key = self.tracer, self.config, GUARDED_OUTPUTS[name]
        if not cfg.guard_tool_outputs:
            t.say(f"guard_tool_outputs is off: the {key} from {name} go to the LLM unchecked", kind="python")
            return
        self.guard = self.guard or InputGuard(t, self.small_device)
        self.guard.threshold = cfg.guard_threshold
        # passages are checked one by one; a file is cut into paragraphs (the classifier reads at most 512 tokens)
        pieces = [p["text"] for p in result[key]] if key == "passages" else split_lines(result[key])
        with t.stage("TOOL OUTPUT GUARD", "classifier", self.guard.model_id, f"{len(pieces)} pieces of text from "
                     f"{name}", "kept or withheld") as ev:
            # The classifier reads only the sentences: it was trained on prose, and scored rows of numbers from a
            # CSV 0.99 INJECTION. A line with fewer than 4 words can't tell the LLM to do anything.
            texts = [sentences_in(piece) for piece in pieces]
            scored = iter(self.guard.scores([text for text in texts if text]) if any(texts) else [])
            verdicts = [next(scored) if text else {"label": "no words", "score": 0.0, "blocked": False}
                        for text in texts]
            for piece, v in zip(pieces, verdicts):
                text = " ".join(piece.split())
                t.say(f"{v['label']:<9} {v['score']:.3f}  {'WITHHELD' if v['blocked'] else 'kept    '}  "
                      f"{text[:110] + ('...' if len(text) > 110 else '')}", kind="classifier")
            removed = [(piece, v) for piece, v in zip(pieces, verdicts) if v["blocked"]]
            if key == "passages":
                sources = sorted({p["source"] for p, v in zip(result[key], verdicts) if v["blocked"]})
                what = f"{len(removed)} passage{'s' if len(removed) > 1 else ''} from {', '.join(sources)}"
                result[key] = [p for p, v in zip(result[key], verdicts) if not v["blocked"]]
            else:
                numbers = [int(n) for piece, _ in removed for n in re.findall(r"(?m)^\s*(\d+)\|", piece)]
                if not numbers:  # a .pptx, .docx or .pdf has no line numbers
                    what = f"{len(removed)} paragraph(s) of this file"
                elif min(numbers) == max(numbers):
                    what = f"Line {numbers[0]} of this file"
                else:
                    what = f"Lines {min(numbers)}-{max(numbers)} of this file"
                result[key] = "\n".join(piece for piece, v in zip(pieces, verdicts) if not v["blocked"])
            if removed:
                result["withheld"] = WITHHELD.format(what=what, score=max(v["score"] for _, v in removed))
            flagged = len(removed)
            ev.summary = (f"{flagged} of {len(pieces)} piece(s) INJECTION (score >= {self.guard.threshold}) -> withheld "
                          "from the LLM" if flagged else f"all {len(pieces)} piece(s) SAFE -> passed to the LLM")
            ev.details = {"verdicts": [{"text": p, **v} for p, v in zip(pieces, verdicts)]}

    def _vote(self, messages, schemas, tokens, first, n):
        """
        VOTE (self-consistency): n - 1 more answers to the same prompt, sampled at random in one batch; plain
        Python reads the number each one ends on and keeps an answer with the most common number. Several
        independent attempts, not a second look at one: a slip in one attempt is outvoted by the others.
        A tie goes to the first (greedy) answer. Answers without numbers: the first answer stands.
        """
        t = self.tracer
        t.banner(f"VOTE: {n - 1} more attempts at the same answer (effort {self.config.effort})")
        answers = [first] + self.llm.samples(messages, schemas, tokens, n - 1, seed=self.config.seed)
        with t.stage("VOTE", "python", "collections.Counter (no ML)", f"{n} answers", "the majority answer") as ev:
            numbers = [answer_number(a) for a in answers]
            rounded = [float(f"{x:.3g}") if x is not None else None for x in numbers]  # 93.33 and 93.3 agree
            counted = Counter(x for x in rounded if x is not None)
            for i, x in enumerate(numbers, 1):
                t.say(f"answer {i} ({'greedy' if i == 1 else 'sampled'}): ends on {x if x is not None else 'no number'}",
                      kind="python")
            ev.details = {"numbers": numbers, "answers": answers}
            if not counted:
                ev.summary = f"{n} answers, none ends on a number: the first answer stands"
                return first
            best, votes = counted.most_common(1)[0]
            ev.summary = (f"{n} answers end on " + ", ".join(f"{k:g} (x{v})" for k, v in counted.most_common())
                          + f" -> {best:g} wins with {votes} of {n}")
        return next(a for a, x in zip(answers, rounded) if x == best)

    def _think(self, messages, schemas, budget):
        """
        THINK (effort high): one LLM call that may only reason, in plain text, before the first action. The
        reasoning stays in the conversation, so every later step reads it. -> a tool call the LLM wrote anyway
        (it becomes step 1), or None.
        """
        t = self.tracer
        t.banner(f"THINK: the LLM reasons before it acts (effort {self.config.effort})")
        think, after = (THINK_PROMPT, AFTER_THINKING) if schemas else (THINK_PROMPT_NO_TOOLS, AFTER_THINKING_NO_TOOLS)
        thought = self.llm.generate(messages + [{"role": "user", "content": think}], schemas, budget,
                                    self.config.temperature)
        with t.stage("THINK", "python", "agent.py (no ML)", "the LLM's reasoning", "kept in the conversation") as ev:
            if parse_tool_call(thought, list(self.tools)) is not None:
                ev.summary = "the LLM wrote a tool call instead of reasoning: it becomes step 1"
                return thought
            messages += [{"role": "user", "content": think}, {"role": "assistant", "content": thought},
                         {"role": "user", "content": after}]
            ev.summary = f"{len(thought.split())} words of reasoning, kept in the conversation; the LLM acts next"
            ev.details = {"thinking": thought}
        return None

    def _instructions(self, prompt, offered, ctx):
        """
        AGENTS.md goes into every request; a skill only when its description is close in meaning to the request.
        -> (text to add to the system prompt, the offered tools plus any a loaded skill needs)
        """
        cfg, t, parts = self.config, self.tracer, []
        self.skill_tools = set()  # tools a loaded skill names: the completion check counts them (see _unused)
        tokens = lambda text: len(self.llm.tokenizer(text, add_special_tokens=False).input_ids)
        if cfg.use_agents_md and AGENTS_FILE.is_file():
            with t.stage("AGENTS.md", "python", "read a file (no ML)", "workspace/AGENTS.md",
                         "text for the system prompt") as ev:
                parts.append(agents_text())
                ev.summary = f"pasted into the system prompt: {tokens(parts[-1])} tokens, in every request"
                ev.details = {"text": parts[-1]}
        skills = find_skills() if cfg.use_skills else []
        if skills:
            search = self.all_tools["search_references"]  # its embedding model measures closeness in meaning
            if search.model is None:
                search.load(ctx)
            search.model.to(ctx.device)
            with t.stage("SKILLS", "retrieval", MODELS["embedder"], "request + each skill's description",
                         "skills to load") as ev:
                scores = score_skills(prompt, skills, lambda texts: search.embed(texts, ctx.device))
                loaded, added = [], []
                for skill, score in sorted(zip(skills, scores), key=lambda pair: -pair[1]):
                    load = score >= cfg.skill_threshold
                    t.say(f"{'load' if load else 'skip'}  {score:.2f}  {skill.name:<22} \"{skill.description}\"",
                          kind="retrieval")
                    if load:
                        loaded.append(skill)
                        parts.append(skill_text(skill))
                        added += [name for name in skill.tools if name in self.tools and name not in offered + added]
                offered = offered + added
                self.skill_tools = {name for skill in loaded for name in skill.tools if name in offered}
                index = sum(tokens(f"{s.name}: {s.description}") for s in skills)
                body = sum(tokens(skill_text(s)) for s in loaded)
                ev.summary = (f"{len(skills)} skills, threshold {cfg.skill_threshold:.2f} -> "
                              + (f"loaded {', '.join(s.name for s in loaded)} ({body} tokens)" if loaded
                                 else "none loaded (0 tokens)")
                              + (f"; the skill adds tool(s) {', '.join(added)}" if added else "")
                              + f". Descriptions read: {index} tokens, outside the LLM's prompt")
                ev.details = {"scores": {s.name: round(x, 3) for s, x in zip(skills, scores)},
                              "loaded": [s.name for s in loaded], "tools_added": added}
        return ("\n\n" + "\n\n".join(parts)) if parts else "", offered

    def _unused(self, offered, used):
        """Offered tools the LLM hasn't used yet. Helper tools (read, search, run) don't count, unless a skill
        loaded for this request names them: then the completion check asks about them too."""
        return [name for name in offered if name not in used
                and (self.tools[name].check_unused or name in self.skill_tools)]

    def _pick_planner(self, offered, prompt):
        """
        PLANNER: which LLM plans this request. One the ROUTER sent to the code tools (CODE_TOOLS in config.py) that asks
        for code to be changed (CODE_CHANGE: fix, write, add, ...) goes to the code model; anything else, a question
        about code included, to HarnessConfig.llm. The router picks the model as well as the tools. The code model
        loads in 4 bits, so only on an NVIDIA GPU, and only if download_models.py fetched it.
        """
        cfg = self.config
        code = sorted(CODE_TOOLS & set(offered))
        change = re.search(CODE_CHANGE, prompt, re.I) if code else None
        want, but = cfg.llm, ""
        if cfg.route_planner and code and not change:
            but = ", but the request changes no code (no fix, write, add, ...)"
        elif cfg.route_planner and change and cfg.llm != "code_llm":
            if self.device != "cuda":
                but = ", but the code model needs an NVIDIA GPU (it loads in 4 bits)"
            elif not downloaded(MODELS["code_llm"]):
                but = ", but the code model isn't downloaded (python download_models.py code_llm)"
            else:
                want = "code_llm"
        swap = want != self.llm.key
        if not swap and not cfg.route_planner:
            return  # one planner for everything: nothing to decide
        with self.tracer.stage("PLANNER", "python", "agent._pick_planner() (no ML)", "the tools the router offered",
                               "which LLM plans") as ev:
            moved = self._put_away_planner() if swap else ""
            ev.summary = ((f"code tools offered ({', '.join(code)})" if code else "no code tools offered")
                          + (f" and the request says '{change.group(0)}'" if change else "") + but
                          + f" -> {MODELS[want]} plans this request" + (f"; {moved}" if moved else ""))
            ev.details = {"code_tools": code, "change": change and change.group(0), "planner": MODELS[want]}
        if swap:
            back = self.parked_llms.pop(want, None)
            # back from CPU RAM (it moves to the GPU at its first LLM stage), or loaded from disk
            self.llm = back or load_llm(self.tracer, want, self.device,
                                        cfg.llm_precision if want == cfg.llm else "bf16")

    def _put_away_planner(self):
        """One planner on the GPU at a time. A 16-bit one waits in CPU RAM and comes back in a second or two
        (LocalLLM._ensure_on_gpu); a 4-bit one can't be parked (LocalLLM.can_park), so it is freed and loaded again
        from disk the next time it is wanted."""
        old, name = self.llm, self.llm.model_id
        if old.can_park:
            if old.on_gpu and self.device == "cuda":
                old.model.to("cpu")
                old.on_gpu = False
            self.parked_llms[old.key] = old
            how = f"parked {name} in CPU RAM"
        else:
            how = f"freed {name} ({old.precision} weights can't be parked; it loads again when next needed)"
        self.llm = old = None
        gc.collect()
        if self.device == "cuda":
            torch.cuda.empty_cache()
            how += f", {gpu_free_gb():.1f} GB free"
        return how

    def _make_room_on_gpu(self, tool):
        """
        Every model shares one GPU. If a tool needs more memory than is free, move the other models'
        weights to CPU RAM ("parking"). Each component moves its own model back right before it
        is used again (see LocalLLM._ensure_on_gpu, Perception._get, ...).
        """
        free = gpu_free_gb()
        if self.device != "cuda" or free >= tool.vram_gb:
            return
        others = {**({"LLM": self.llm.model} if self.llm.can_park else {}), **self.perception.gpu_modules()}
        for name, part in (("input guard", self.guard), ("router", self.router)):
            if part:
                others[name] = part.clf.model
        for other in self.all_tools.values():
            if other is not tool:
                others.update(other.gpu_modules())
        on_gpu = {name: m for name, m in others.items() if next(m.parameters()).is_cuda}
        with self.tracer.stage("GPU MEMORY", "python", "model.to('cpu') for the other models",
                               "weights in GPU memory", "weights in CPU RAM") as ev:
            for module in on_gpu.values():
                module.to("cpu")
            if "LLM" in on_gpu:
                self.llm.on_gpu = False
            torch.cuda.empty_cache()
            ev.summary = (f"{tool.name} needs ~{tool.vram_gb:.0f} GB but only {free:.1f} GB was free -> "
                          f"parked {', '.join(on_gpu) or 'nothing'} in RAM, now {gpu_free_gb():.1f} GB free")

    def _finish(self, prompt, answer, files, messages, run_dir):
        t = self.tracer
        if files:
            # ---- ANSWER CHECK: plain Python makes sure every path the answer gives is a real file ----
            with t.stage("ANSWER CHECK", "python", "agent.py (no ML)", "final answer + files written",
                         "answer with real paths") as ev:
                answer, fixed = fix_paths(answer, files)
                ev.summary = ("; ".join(f"{wrong} does not exist -> {right}" for wrong, right in fixed) if fixed
                              else "every path in the answer is a real file")
        t.banner("FINAL ANSWER (text written by the LLM)")
        t.emit(answer)
        if files:
            t.emit("\nFiles written by the tools:")
            for f in files:
                t.emit(f"  {_relative(Path(f))}")
        t.print_summary()
        result = RunResult(prompt, answer, files, messages, list(t.events), run_dir, log=list(t.log))
        t.save_json(run_dir / "trace.json")
        result.report = write_report(result, run_dir / "report.html")
        t.emit(f"\nReport: {result.report}")
        if self.config.open_report:
            import webbrowser
            webbrowser.open(result.report.as_uri())
        return result


# =========================================================================================
# Qwen's chat template (and other "Hermes-style" ones) asks for each call between these tags. A call cut off by the
# token limit has no closing tag: then everything after <tool_call> is the call, and PARSE reports it as broken.
TOOL_CALL_TAGS = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)


def parse_tool_call(text, known):
    """
    Find a tool call in the LLM's text. Pure string handling, no ML.

    Llama 3.x was trained to write   {"name": "generate_image", "parameters": {"prompt": "..."}}
    Qwen models write the same JSON  <tool_call>{"name": "run_tests", "arguments": {...}}</tool_call>
    Some small models instead write  generate_image(prompt="...")      so we accept all three.
    Returns {"name": ..., "arguments": {...}} or None (= the text is the final answer).
    A JSON call is returned even if it names a tool that does not exist: the loop then tells the
    LLM about its mistake instead of showing the user a blob of JSON.
    """
    text = text.replace("<|python_tag|>", "").strip()
    tagged = TOOL_CALL_TAGS.search(text)
    if tagged:  # Qwen: the call is the JSON between the tags (the first call, if it wrote several)
        text = tagged.group(1)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            # A Windows path copied from the request, "..\FP520.pptx", is not valid JSON: \F is not an escape.
            # Read the backslash pairs left to right, keep the real JSON escapes (\\ \" \n ...), double the rest,
            # and try once more.
            fixed = re.sub(r"\\(.)", lambda m: m.group(0) if m.group(1) in '\\/"bfnrtu' else "\\\\" + m.group(1),
                           text[match.start():], flags=re.S)
            try:
                obj, _ = decoder.raw_decode(fixed)
            except json.JSONDecodeError:
                continue
        is_call = isinstance(obj, dict) and isinstance(obj.get("name"), str)
        if is_call and ("parameters" in obj or "arguments" in obj):
            args = obj.get("parameters", obj.get("arguments", {}))
            if isinstance(args, str):  # some models double-encode the arguments as a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass  # validate() will reject it and the LLM will see the error
            return {"name": obj["name"], "arguments": args}
    started = re.search(r'\{\s*"name"\s*:\s*"(\w+)"', text)
    if started and started.group(1) in known:  # the start of a call, but the JSON never closes properly
        return {"name": started.group(1), "arguments": {}, "broken": True}
    try:  # pythonic form, e.g. [generate_image(prompt="a fox")]
        tree = ast.parse(text.strip("[] \n"), mode="eval").body
        if isinstance(tree, ast.Call) and getattr(tree.func, "id", None) in known:
            return {"name": tree.func.id, "arguments": {k.arg: ast.literal_eval(k.value) for k in tree.keywords}}
    except (SyntaxError, ValueError):
        pass
    return None


def split_lines(text, max_words=150):
    """Cut a file's text into paragraph-sized pieces at blank lines, keeping every line as it is, so that
    "\\n".join(pieces) == text. A line such as ' 12| ' (read_file's line numbers) counts as blank."""
    pieces, current = [], []
    for line in text.split("\n"):
        current.append(line)
        words = sum(len(l.split()) for l in current)
        if (not re.sub(r"^\s*\d+\|", "", line).strip() and words >= 15) or words >= max_words:
            pieces.append("\n".join(current))
            current = []
    return pieces + (["\n".join(current)] if current else [])


def sentences_in(text):
    """The lines that read like language, for the TOOL OUTPUT GUARD: read_file's line numbers removed, and lines
    with fewer than 4 words (rows of numbers, a bracket, a blank line) left out. Plain string handling."""
    lines = (re.sub(r"^\s*\d+\| ?", "", line) for line in text.split("\n"))
    return "\n".join(line for line in lines if len(re.findall(r"[A-Za-z]{2,}", line)) >= 4)


def answer_number(text):
    """The number an answer ends on: '... Answer: 1,680' -> 1680.0; without an 'Answer:' line, its last number."""
    text = text.replace(",", "")
    marked = re.findall(r"Answer:\W*(-?\d+(?:\.\d+)?)", text)
    numbers = marked or re.findall(r"-?\d+(?:\.\d+)?", text)
    return float(numbers[-1]) if numbers else None


def _same(result, earlier):
    """Did a repeated read or run return exactly what it returned last time (ignoring the harness note)?"""
    strip = lambda r: {k: v for k, v in r.items() if k != "harness_note"}
    return earlier is not None and result["status"] == "ok" and strip(result) == strip(earlier)


def _relative(path):
    try:
        return Path(path).resolve().relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return str(path)


def fix_paths(answer, files):
    """
    Small models garble the paths they repeat: Llama 3.2 3B wrote workspace/run_.../report.docx and
    workspace/old_format_deck_ULRI_template.pptx for files the tools had saved in outputs/run_.../.
    A path in the answer that does not exist, but ends in the name of a file a tool wrote, becomes that
    file's real path. Paths that exist (such as the unchanged original) are left alone.
    -> (answer, [(wrong, right), ...])
    """
    real = {Path(f).name: _relative(Path(f)) for f in files}
    fixed = []

    def swap(match):
        word = match.group(0)
        name = re.split(r"[/\\]", word)[-1]
        if name in real and word != real[name] and not (PROJECT_DIR / word).exists() and not Path(word).exists():
            fixed.append((word, real[name]))
            return real[name]
        return word

    return re.sub(r"[\w.:~-]*[/\\][\w./\\~-]*\.\w+", swap, answer), fixed


def _quiet_libraries(verbosity):
    """Hide library warnings and progress bars so the trace stays readable."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if verbosity >= 2:
        return
    warnings.filterwarnings("ignore")
    import diffusers
    import huggingface_hub
    import transformers
    huggingface_hub.utils.disable_progress_bars()
    huggingface_hub.utils.logging.set_verbosity_error()
    transformers.logging.set_verbosity_error()
    transformers.logging.disable_progress_bar()
    diffusers.logging.set_verbosity_error()
    diffusers.logging.disable_progress_bar()
    logging.getLogger("bitsandbytes").setLevel(logging.ERROR)  # int8: "inputs will be cast ..." at every step
