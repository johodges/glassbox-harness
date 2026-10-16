"""
Code tools: change a script, and run it. The same pair of moves a coding agent makes, in miniature.

    edit_code   find an exact piece of text, replace it, show the diff, check the syntax
    run_script  run the script in a separate process and return what it printed (or its error)

Both are plain Python. The LLM only writes text: the text to find, the text to put in, and a
path. A traceback from run_script goes back to the LLM as text, and that is usually all it
needs to make the fix. For a project with tests, coding.py adds the rest of a coding agent's
moves: run_tests, check_code, search_code, write_code, create_file and review_code.
"""

import ast
import difflib
import re
import subprocess
import sys
from pathlib import Path

from ..classifiers import calculates_from_a_file
from ..config import WORKSPACE_DIR
from .base import Tool
from .coding import CHANGE_TESTS, has_tests, is_test_file, project_root, syntax
from .documents import READABLE, TEXT_TYPES, read_text_keep_newlines, write_text_keep_newlines
from .files import current_version, original, rel, resolve, working_copy



def _tail(text, limit=1500):
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


class EditCodeTool(Tool):
    name = "edit_code"
    description = ("Change a script or other text file (.py, .tex, .md, .txt): replace one or more complete lines "
                   "with new lines. Copy the lines to find exactly, for example from an error message or from "
                   "read_file. The edited file is saved as a copy in the run folder, and Python files are checked "
                   "for syntax errors.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file, e.g. workspace/scripts/word_count.py"},
            "find": {"type": "string", "description": "The complete line (or lines) to replace, copied exactly."},
            "replace": {"type": "string", "description": "The new line (or lines) that take its place."},
        },
        "required": ["path", "find", "replace"],
    }
    model_id = "line matching + difflib (no ML)"
    check_unused, repeatable = False, True
    how_it_works = ("No model here: Python finds the line(s) to replace (ignoring indentation, which small models "
                    "often get wrong), swaps in the new ones, and difflib shows the change as a diff (- removed, "
                    "+ added). In a Python file the new lines' indentation is read the first way that compiles, and "
                    "an edit that would break a file that compiled is not saved.")

    def run(self, ctx, path, find, replace):
        source = resolve(path, ctx)
        if source.suffix.lower() not in TEXT_TYPES:
            raise ValueError(f"edit_code edits text files ({', '.join(sorted(TEXT_TYPES))}), not {source.name}.")
        if is_test_file(original(source, ctx)) and not CHANGE_TESTS.search(ctx.request):
            # Existing tests are the specification. Asked to make a failing test pass, a small model may "fix" the
            # test instead of the code; adding a test (write_code) is fine, changing one needs the user's own words.
            raise ValueError(f"{source.name} is a test, and existing tests say what the code must do: the user did "
                             "not ask to change a test. Change the code under test instead.")
        copy = working_copy(source, ctx)
        with ctx.tracer.stage("TOOL: edit_code", "python", self.model_id, "find + replace (text)",
                              "edited file + diff") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            before, newline = read_text_keep_newlines(copy)
            lines = before.split("\n")
            if "\\n" in find and "\n" not in find:  # small models often write a newline as the two characters \n
                find, replace = find.replace("\\n", "\n"), replace.replace("\\n", "\n")
            find = re.sub(r"(?m)^\s*\d+\| ?", "", find)  # the LLM may copy read_file's line numbers too
            target = [l.strip() for l in find.strip("\n").split("\n")]
            hits = [i for i in range(len(lines) - len(target) + 1)
                    if [l.strip() for l in lines[i:i + len(target)]] == target]
            if len(hits) != 1:
                close = difflib.get_close_matches(target[0], [l.strip() for l in lines], n=1, cutoff=0.5)
                hint = ""
                if close and not hits:
                    number = next(i for i, l in enumerate(lines, 1) if l.strip() == close[0])
                    hint = f' The closest line is line {number}: "{close[0]}". Copy the whole line exactly.'
                problem = "are not complete lines of" if not hits else f"appear {len(hits)} times in"
                raise ValueError(f"The lines to find {problem} {source.name}.{hint}"
                                 + (" Include more of the surrounding lines so they are unique." if hits else ""))
            first = hits[0]
            indent = lines[first][:len(lines[first]) - len(lines[first].lstrip())]
            readings = indentations(replace.strip("\n").split("\n"), indent)
            candidates = [(how, "\n".join(lines[:first] + new + lines[first + len(target):])) for how, new in readings]
            how, after = candidates[0]
            if copy.suffix == ".py" and syntax(before) == "ok":
                # Keep the first reading of the new lines' indentation that Python can compile, and never save an
                # edit that breaks a file that compiled: Qwen2.5-Coder 7B wrote a block whose first line had no
                # indentation and whose other lines did, and every later edit then had to match the broken copy.
                how, after = next(((h, c) for h, c in candidates if syntax(c) == "ok"), (None, None))
                if after is None:
                    raise ValueError(f"This edit was not saved: it would make {source.name} invalid Python "
                                     f"({syntax(candidates[0][1])}). Write the new lines as valid Python, indented "
                                     f"to match the code around them (line {first + 1} is indented by {len(indent)} "
                                     "spaces).")
            if how != "as written":
                ctx.tracer.say(f"indentation of the new lines: {how}", kind="python")
            write_text_keep_newlines(copy, after, newline)
            diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                f"{source.name} (before)", f"{source.name} (after)", n=1))
            ctx.tracer.say(diff.rstrip(), kind="python")
            ev.summary = f"{copy.name}: {len(find.splitlines())} line(s) replaced by {len(replace.splitlines())}"
            ev.details = {"diff": diff}
            ev.artifacts.append(str(copy))
        observations = {"diff": diff}
        if copy.suffix == ".py":
            with ctx.tracer.stage("CHECK SYNTAX", "python", "compile() (no ML)", "Python source", "ok or error") as ev:
                try:
                    compile(after, copy.name, "exec")
                    observations["syntax"] = "ok"
                except SyntaxError as exc:
                    observations["syntax"] = f"SyntaxError on line {exc.lineno}: {exc.msg}"
                ev.summary = observations["syntax"]
            # A syntax check is not a test. Say so, or a small model reports "fixed" without running anything,
            # and remember it: the completion check asks once if the LLM tries to finish before running it.
            ctx.pending.pop("edit_code", None)  # set by run_tests while a test fails and nothing is edited
            if has_tests(original(source, ctx)):  # part of a project with tests (coding.py): the tests decide
                observations["next_step"] = "The tests have not run since this edit. Call run_tests to check the fix."
                ctx.pending["run_tests"] = f"{source.name} was edited, but the tests have not run since the edit"
                ctx.facts["changed_project"] = rel(project_root(original(source, ctx)))  # for FINAL TESTS (agent.py)
            else:
                observations["next_step"] = "The edited script has not been run yet. Call run_script to check the fix."
                ctx.pending["run_script"] = f"{source.name} was edited, but it has not been run since the edit"
        note = f"edited copy of {rel(source)}; the original is unchanged"
        return {"artifacts": [{"type": "code", "path": copy, "note": note}], "observations": observations}


def indentations(new, indent):
    """
    The ways a small model may have meant the indentation of its new lines, most likely first, each with a label.
    edit_code keeps the first one Python can compile. `indent` is the indentation of the first line being replaced.
    """
    shifted = ("indented like the replaced line", [indent + l if l.strip() else l for l in new])
    as_written = ("as written", new)
    if not indent:
        return [as_written]
    if new[0][:1].isspace():  # the first line has its own indentation: trust it
        return [as_written, shifted]
    first_only = ("first line indented like the replaced line", [indent + new[0]] + new[1:])
    rest = [len(l) - len(l.lstrip()) for l in new[1:] if l.strip()]
    if not rest or min(rest) < len(indent):  # the other lines are indented relative to the first one
        return [shifted, first_only, as_written]
    return [first_only, shifted, as_written]  # the other lines already have their full indentation


PLAIN_ARG = re.compile(r"^[\w.,:=+-]{1,60}$")  # a number, a word or an option such as --unit=kW


def script_args(args, ctx):
    """
    The LLM's args -> what the script is given. Plain Python: a file must be inside the fence (resolve() turns
    whatever the LLM wrote into the real path, e.g. the CSV a tool just made in the run folder); anything else
    must be a short plain word or number. No shell is involved, so nothing in an argument can start a command.
    """
    if len(args) > 5:
        raise ValueError("Give a script at most 5 args.")
    checked = []
    for arg in args:
        if Path(arg).suffix.lower() in READABLE or "/" in arg or "\\" in arg:
            checked.append(current_version(resolve(arg, ctx), ctx))
        elif PLAIN_ARG.match(arg):
            checked.append(arg)
        else:
            raise ValueError(f'The arg "{arg[:60]}" is neither a file in the workspace nor a plain word or number.')
    return checked


class RunScriptTool(Tool):
    name = "run_script"
    description = ("Run a Python script from the workspace and return what it printed, and its error if it failed. "
                   "If you already edited the script, the edited copy is run. It runs in the workspace folder with "
                   "a 30-second time limit.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The .py file, e.g. workspace/scripts/word_count.py"},
            "args": {"type": "array", "items": {"type": "string"},  # shown only to some requests: see prepare()
                     "description": "The data file the script reads, e.g. the CSV file a tool wrote in this run."},
        },
        "required": ["path"],
    }
    model_id = "subprocess: a separate Python process (no ML)"
    check_unused, repeatable = False, True  # the script may have changed since the last run
    needs_approval = True  # a script can do anything you can: a person says yes first (APPROVAL in agent.py)
    how_it_works = ("No model here: the harness starts a separate Python process, waits at most 30 seconds, and "
                    "captures what it printed (stdout) and its error messages (stderr). Only .py files from the "
                    "workspace or this run's folder may run: a real agent would also sandbox this.")

    show_args = False  # set for each request by prepare()

    def prepare(self, ctx):
        """args is shown to the LLM only when the request calculates from a file. A small LLM fills in every
        optional argument it is shown: with args always there, Llama 3.2 3B gave word_count.py an invented
        workspace/data/ref_notes.csv and lost its way fixing the script (evaluate.py: pass -> fail -> pass)."""
        self.show_args = calculates_from_a_file(ctx.request)

    def schema(self):
        """The description ends with the scripts in workspace/scripts and the first line of each one's docstring,
        read fresh for every request (ast reads a docstring without running the script), so the LLM knows what
        it can run."""
        scripts = []
        for p in sorted((WORKSPACE_DIR / "scripts").glob("*.py")):
            try:
                doc = (ast.get_docstring(ast.parse(p.read_text(encoding="utf-8"))) or "").split("\n")[0]
            except SyntaxError:
                doc = ""
            scripts.append(f"{rel(p)}" + (f" ({doc.rstrip('.')})" if doc else ""))
        schema = super().schema()
        schema["function"]["description"] += " Scripts in the workspace: " + "; ".join(scripts) + "."
        if not self.show_args:
            shown = {k: v for k, v in self.parameters["properties"].items() if k != "args"}
            schema["function"]["parameters"] = {**self.parameters, "properties": shown}
        return schema

    def approval_question(self, args, ctx):
        script = current_version(resolve(args["path"], ctx), ctx)
        command = " ".join(["python", rel(script)] + [rel(a) if isinstance(a, Path) else a
                                                       for a in script_args(args.get("args", []), ctx)])
        return (f"The agent wants to run  {command}  on this computer (in workspace/). "
                "A script can read, change or delete any file you can. Allow it?")

    def run(self, ctx, path, args=()):
        script = current_version(resolve(path, ctx), ctx)
        if script.suffix != ".py":
            raise ValueError(f"run_script only runs .py files, not {script.name}.")
        checked = script_args(args, ctx)
        given = " ".join(rel(a) if isinstance(a, Path) else a for a in checked)
        with ctx.tracer.stage("TOOL: run_script", "python", f"python {script.name} (separate process)",
                              "script" + (" + args" if checked else ""), "printed output + exit code") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            if checked:
                ctx.tracer.say(f"args: {given}", kind="python")
            proc = subprocess.run([sys.executable, "-I", str(script), *map(str, checked)], cwd=WORKSPACE_DIR,
                                  capture_output=True, text=True, timeout=30)
            ctx.pending.pop("run_script", None)  # whatever was edited has now been run
            printed, error = _tail(proc.stdout), _tail(proc.stderr)
            if printed:
                ctx.tracer.say(printed, kind="python")
            if error:
                ctx.tracer.say(error, kind="remote")
            last = error.splitlines()[-1] if error else "no errors"
            ev.summary = f"{rel(script)}: exit code {proc.returncode} ({last})"
        return {"artifacts": [], "observations": {"script": rel(script), **({"args": given} if checked else {}),
                                                  "exit_code": proc.returncode, "printed": printed or "(nothing)",
                                                  "error": error or "none"}}
