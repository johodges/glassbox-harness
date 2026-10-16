"""
Tools for reviewing and developing code: the moves a coding agent such as Claude Code makes, each one plain Python.

    run_tests    pytest on a project in workspace/, in a separate process -> how many passed, and for each failing
                 test its name, its assertion message and the lines it went through
    check_code   compile() + ruff (a linter), and mypy if HarnessConfig.check_types -> findings: file:line, rule, message
    search_code  a text search through the workspace's code -> file:line matches
    write_code   whole functions into a .py file: one the file has is replaced (found by name), a new one added
    create_file  a new file, as a working copy in the run folder
    review_code  the LLM's review, one "path:line: problem" per finding -> plain Python checks each one against the
                 file, the linter and the last test run: confirmed, plausible, not supported, or invented

edit_code and run_script (code.py) change existing lines and run a script. Nothing here writes into workspace/:
every change lands in a working copy (files.py), and the tests and the linter run on a copy of the project with the
working copies laid over it (PROJECT COPY), so what is tested is exactly what was edited.

The LLM only ever writes text: a path, some code, a list of findings. Whether the code works is decided by pytest,
ruff and Python's own compiler, and their verdicts go back to the LLM as short structured facts, not raw logs.
"""

import ast
import difflib
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.etree import ElementTree

from ..config import WORKSPACE_DIR
from .base import Tool
from .documents import TEXT_TYPES, read_text_keep_newlines, write_text_keep_newlines
from .files import current_version, original, rel, resolve, working_copy

# "change the test", "update the tests", "edit test_stats.py": the user's own permission to change an existing test
CHANGE_TESTS = re.compile(r"\b(?:change|edit|update|rewrite|correct)\s+(?:the\s+|a\s+|that\s+|this\s+)?"
                          r"(?:\w+\s+)?(?:tests?\b|test_\w+)", re.I)

CODE_TYPES = {".py", ".toml"}  # what search_code looks through
SKIP = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
# The linter's rules, chosen here so the trace shows them: pyflakes (unused or undefined names), syntax errors, bugbear
# (likely bugs) and bandit (security: eval, shell=True, ...). Ignored: assert (it is how pytest tests) and zip(strict=).
RUFF_SELECT, RUFF_IGNORE = "F,E9,B,S", "S101,B905"
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
IMPORTS = (ast.Import, ast.ImportFrom)
TEST_TIMEOUT = 60  # seconds: an endless loop in the code under test must not hang the agent
ASKS_FOR_A_FIX = re.compile(r"\bfix|\brepair|\bmake\b[^.?!]{0,40}\bpass", re.I)  # "fix the code", "make the test pass"
# What a request for new code asks for: "add a function ...", "... add a test for it", "with a test"
ASKS_FOR_CODE = re.compile(r"\b(?:add|write|create)\s+(?:a |an )?(?:new )?(?:function|method|class)\b", re.I)
ASKS_FOR_A_TEST = re.compile(r"\b(?:add|write|create|with)\s+(?:a |an )?(?:new )?(?:unit )?tests?\b|\btests? for (?:it|this)\b",
                             re.I)


# ---- the project a file belongs to, and the copy that gets tested ---------------------------------------------------
def project_root(path):
    """The project a file belongs to: the nearest folder inside workspace/ with a pyproject.toml or a tests/ folder,
    else the file's own folder. Plain Python."""
    path = Path(path)
    folder = path if path.is_dir() else path.parent
    for candidate in [folder, *folder.parents]:
        if not candidate.is_relative_to(WORKSPACE_DIR) or candidate == WORKSPACE_DIR:
            break
        if (candidate / "pyproject.toml").is_file() or (candidate / "tests").is_dir():
            return candidate
    return folder


def project_of(path, ctx):
    """What the LLM wrote -> (the workspace file or folder it means, the project it belongs to)."""
    target = original(resolve(path, ctx, allow_dir=True), ctx)
    if target == WORKSPACE_DIR:
        raise ValueError("Give a project folder or a file, not the whole workspace. Projects: " + ", ".join(projects()))
    return target, project_root(target)


def projects():
    """Folders in workspace/ that have tests: listed in run_tests' description, so the LLM knows what it can test."""
    return [rel(p.parent) for p in sorted(WORKSPACE_DIR.glob("*/pyproject.toml"))]


def has_tests(path):
    return (project_root(path) / "tests").is_dir()


def is_test_file(path):
    path = Path(path)
    return path.name.startswith("test_") or "tests" in path.parts


def project_copy(root, ctx):
    """
    PROJECT COPY: the project folder copied into the run folder, with this request's working copies (edited and new
    files) laid over it, so pytest and ruff see the code as the LLM left it. The workspace itself is never touched.
    -> (the copy, the workspace files whose edited versions were laid over it)
    """
    tree = ctx.run_dir / "project" / root.name
    with ctx.tracer.stage("PROJECT COPY", "python", "shutil.copytree (no ML)", rel(root),
                          "the project as edited, in the run folder") as ev:
        if tree.exists():
            shutil.rmtree(tree)
        shutil.copytree(root, tree, ignore=shutil.ignore_patterns(*SKIP))
        laid = []
        for source, copy in ctx.copies.items():
            if Path(source).is_relative_to(root) and Path(copy).is_file():
                target = tree / Path(source).relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(copy, target)
                laid.append(rel(source))
        ev.summary = (f"{rel(root)} -> {rel(tree)}"
                      + (f", with the edited {', '.join(Path(p).name for p in laid)}" if laid else ", no edits yet"))
    return tree, laid


def back_to_workspace(name, tree, root):
    """'tests\\test_stats.py' in the project copy -> 'workspace/code_demo/tests/test_stats.py', the path the LLM uses."""
    return rel(root / Path(str(name).replace("\\", "/")).resolve().relative_to(tree.resolve())) \
        if Path(name).is_absolute() else rel(root / str(name).replace("\\", "/"))


def syntax(text, name="<code>"):
    """Compile, don't run: Python's own parser finds syntax errors. -> 'ok' or the error with its line."""
    try:
        compile(text, name, "exec")
        return "ok"
    except SyntaxError as exc:
        return f"SyntaxError on line {exc.lineno}: {exc.msg}"


def lint(tree, root, target):
    """ruff on a file or folder of the project copy -> [{"where", "rule", "message", "code", "file", "line"}]."""
    proc = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "--select", RUFF_SELECT,
                           "--ignore", RUFF_IGNORE, "--output-format", "json", str(target)],
                          cwd=tree, capture_output=True, text=True, timeout=60)
    found = []
    for item in json.loads(proc.stdout or "[]"):
        file, line = back_to_workspace(item["filename"], tree, root), item["location"]["row"]
        code = Path(item["filename"]).read_text(encoding="utf-8").splitlines()[line - 1].strip()
        found.append({"where": f"{file}:{line}", "rule": item.get("code") or "syntax", "message": item["message"],
                      "code": code, "file": file, "line": line})
    return found


def _diff(before, after, name):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"{name} (before)",
                                        f"{name} (after)", n=1))


def _after_change(source, copy, text, ctx, observations):
    """What every writing tool does next: a syntax check for Python, and a note that the tests have not run since."""
    ctx.pending.pop("edit_code", None)  # "a test still fails and nothing is edited": now something is (run_tests decides)
    if copy.suffix == ".py":
        with ctx.tracer.stage("CHECK SYNTAX", "python", "compile() (no ML)", "Python source", "ok or error") as ev:
            observations["syntax"] = ev.summary = syntax(text, copy.name)
    if has_tests(source):
        observations["next_step"] = "The tests have not run since this change. Call run_tests to check it."
        ctx.pending["run_tests"] = f"{Path(source).name} was changed, but the tests have not run since"
        ctx.facts["changed_project"] = rel(project_root(source))  # FINAL TESTS in agent.py tests it if the LLM doesn't


# =====================================================================================================================
class RunTestsTool(Tool):
    name = "run_tests"
    description = ("Run the tests of a Python project in the workspace with pytest. Returns how many passed and "
                   "failed, and for each failing test its name, its assertion message and the lines it ran through. "
                   "Your edited copies are tested, not the originals.")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "The project folder, e.g. workspace/code_demo"}},
        "required": ["path"],
    }
    model_id = "pytest in a separate process (no ML)"
    check_unused, repeatable = False, True  # the code may have changed since the last run
    needs_approval = True  # tests run the project's code, and code can do anything you can
    how_it_works = ("No model here: pytest runs in a separate Python process on the project copy, with a "
                    f"{TEST_TIMEOUT}-second limit, and writes a JUnit XML report. Python reads the report and keeps "
                    "only the facts: counts, the failing tests, their messages and lines. The LLM never sees raw logs.")

    def schema(self):
        schema = super().schema()
        schema["function"]["description"] += " Projects with tests: " + ", ".join(projects() or ["none"]) + "."
        return schema

    def approval_question(self, args, ctx):
        _, root = project_of(args["path"], ctx)
        return (f"The agent wants to run the tests of {rel(root)} (python -m pytest, on a copy in the run folder). "
                "Tests run the project's code, which can read, change or delete any file you can. Allow it?")

    def run(self, ctx, path):
        target, root = project_of(path, ctx)
        tree, laid = project_copy(root, ctx)
        report = ctx.new_path("pytest", ".xml")
        # one test file, or all the project's tests (also when the LLM names a module: Llama gave readings.py)
        only = [str(target.relative_to(root))] if target.is_file() and is_test_file(target) else []
        with ctx.tracer.stage("TOOL: run_tests", "python", "python -m pytest (separate process)", "project copy",
                              "passed / failed + why") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            try:
                proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
                                       "--continue-on-collection-errors", f"--junitxml={report}", *only],
                                      cwd=tree, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                                      env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            except subprocess.TimeoutExpired:
                raise ValueError(f"The tests did not finish within {TEST_TIMEOUT} seconds: look for an endless loop.")
            ctx.pending.pop("run_tests", None)  # whatever was changed has now been tested
            facts = read_junit(report, tree, root) if report.is_file() else {"passed": 0, "failed": 0, "failures": []}
            if proc.returncode == 5 or facts["passed"] + facts["failed"] == 0:
                facts["summary"] = f"no tests found in {rel(root)}"
            elif facts["failed"]:
                facts["summary"] = f"{facts['passed']} passed, {facts['failed']} failed"
            else:
                facts["summary"] = f"all {facts['passed']} tests passed"
            definitions = defined_in_project(tree, root)
            for i, failure in enumerate(facts["failures"]):
                # Where the functions the message calls are defined, and (for the first two failures) their code with
                # line numbers. "where 300.0 = median([...])" names median, not stats.py: Qwen2.5-Coder 7B then wrote
                # out a median() it had never read and decided the test was wrong.
                called = sorted(name for name in set(re.findall(r"\b(\w+)\(", failure["message"])) if name in definitions)
                if called:
                    failure["defined_at"] = {name: definitions[name]["where"] for name in called}
                    if i < 2:
                        failure["code"] = "\n".join(definitions[name]["code"] for name in called[:2])
                ctx.tracer.say(f"FAILED {failure['test']}: {failure['message']}  ({', '.join(failure['lines'])})",
                               kind="remote")
            ev.summary = f"{rel(root)}: {facts['summary']}" + (" (edited copies tested)" if laid else "")
            ev.details = {"facts": facts, "pytest_output": proc.stdout[-3000:], "junit_xml": str(report)}
        ctx.facts["tests"] = facts  # review_code checks findings against the last test run
        # A fact the harness has and the LLM may ignore: the user asked for a fix and a test still fails. Llama 3.2 3B
        # ran the tests, then wrote "change the line ... Here is the edited copy" without editing anything.
        if facts["failed"] and ASKS_FOR_A_FIX.search(ctx.request):
            ctx.pending["edit_code"] = (f"{facts['failed']} test(s) still fail ({facts['failures'][0]['test']}), and the "
                                        "user asked for the code to be fixed" + ("" if laid else ": nothing is edited yet"))
        else:
            ctx.pending.pop("edit_code", None)
        return {"artifacts": [], "observations": {"project": rel(root), **({"edited_files_tested": laid} if laid else {}),
                                                  **facts}}


def defined_in_project(tree, root, max_lines=30):
    """Every top-level function and class in the project's code (not its tests), via ast ->
    {name: {"where": "workspace/.../file.py:line", "code": its lines, numbered as read_file numbers them}}."""
    found = {}
    for f in sorted(tree.rglob("*.py")):
        if is_test_file(f.relative_to(tree)) or set(f.parts) & set(SKIP):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        try:
            nodes = [n for n in ast.parse(text).body if isinstance(n, DEFINITIONS)]
        except SyntaxError:
            continue
        for node in nodes:
            last = min(node.end_lineno, node.lineno + max_lines - 1)
            code = "\n".join(f"{n:>3}| {lines[n - 1]}" for n in range(node.lineno, last + 1))
            found.setdefault(node.name, {"where": f"{back_to_workspace(f, tree, root)}:{node.lineno}", "code": code})
    return found


def read_junit(report, tree, root):
    """pytest's JUnit XML report -> {"passed", "failed", "failures": [{"test", "message", "lines"}]}. Plain Python."""
    passed, failures = 0, []
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        bad = case.find("failure") if case.find("failure") is not None else case.find("error")
        if case.find("skipped") is not None:
            continue
        if bad is None:
            passed += 1
            continue
        module = case.get("classname", "")  # tests.test_stats -> tests/test_stats.py::test_median_even
        test = (f"{module.replace('.', '/')}.py::{case.get('name')}" if module  # a file that can't be imported has
                else f"{case.get('name', '').replace('.', '/')}.py (could not be imported)")  # no classname
        message = " ".join((bad.get("message") or "").split())
        if message in ("", "collection failure"):  # the real error is in the "E   ..." lines of the traceback
            errors = re.findall(r"^E\s+(.+)$", bad.text or "", re.M)
            message = " ".join(errors[-2:]) or message
        for folder in {str(tree), str(tree.resolve())}:  # "...\project\code_demo\stats.py" -> "workspace/code_demo/stats.py"
            message = message.replace(folder + "\\", rel(root) + "/").replace(folder, rel(root))
        message = message[:300]
        frames = re.findall(r"^([^\s:][^:\n]*\.py):(\d+):", bad.text or "", re.M)  # "tests\test_stats.py:13: in ..."
        lines = [f"{back_to_workspace(f, tree, root)}:{n}" for f, n in frames if not Path(f).is_absolute()]
        failures.append({"test": test, "message": message, "lines": list(dict.fromkeys(lines))})
    return {"passed": passed, "failed": len(failures), "failures": failures}


# =====================================================================================================================
class CheckCodeTool(Tool):
    name = "check_code"
    description = ("Check Python code without running it: syntax errors, unused imports and variables, undefined "
                   "names, likely bugs and unsafe calls such as eval. Returns each finding with its file:line, the "
                   "rule, the message and the line of code.")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string",
                                "description": "A .py file or a project folder, e.g. workspace/code_demo/stats.py"}},
        "required": ["path"],
    }
    model_id = "compile() + ruff, and optionally mypy (no ML)"
    check_unused, repeatable = False, True
    how_it_works = ("No model here: Python's compile() looks for syntax errors, and ruff, a linter, applies fixed "
                    f"rules ({RUFF_SELECT}: unused and undefined names, likely bugs, unsafe calls) to the project copy. "
                    "With HarnessConfig.check_types, mypy also checks the types. Same code in, same findings out.")

    def run(self, ctx, path):
        target, root = project_of(path, ctx)
        tree, _ = project_copy(root, ctx)
        here = tree / target.relative_to(root) if target != root else tree
        files = [here] if here.is_file() else sorted(p for p in here.rglob("*.py") if not set(p.parts) & set(SKIP))
        findings = []
        with ctx.tracer.stage("COMPILE", "python", "compile() (no ML)", f"{len(files)} .py file(s)", "syntax errors") as ev:
            for f in files:
                verdict = syntax(f.read_text(encoding="utf-8"), f.name)
                if verdict != "ok":
                    line = int(re.search(r"line (\d+)", verdict).group(1))
                    findings.append({"where": f"{back_to_workspace(f, tree, root)}:{line}", "rule": "syntax",
                                     "message": verdict, "by": "compile"})
            ev.summary = f"{len(files)} file(s): " + (f"{len(findings)} syntax error(s)" if findings else "all compile")
        with ctx.tracer.stage("LINT", "python", f"ruff --select {RUFF_SELECT} (no ML)", rel(target),
                              "findings with file:line") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            linted = lint(tree, root, here)
            for f in linted:
                ctx.tracer.say(f"{f['where']}  {f['rule']}  {f['message']}", kind="python")
            findings += [{k: f[k] for k in ("where", "rule", "message", "code")} | {"by": "ruff"} for f in linted]
            ev.summary = f"{len(linted)} finding(s)" + (f": {', '.join(sorted({f['rule'] for f in linted}))}"
                                                         if linted else "")
            ev.details = {"findings": linted}
        if ctx.config is not None and getattr(ctx.config, "check_types", False):
            findings += self.types(tree, root, here, ctx)
        ctx.facts["lint"] = linted
        shown = findings[:25]
        return {"artifacts": [], "observations": {
            "checked": rel(target), "summary": f"{len(findings)} finding(s)" if findings else "no findings",
            "findings": shown, **({"not_shown": len(findings) - len(shown)} if len(findings) > len(shown) else {})}}

    def types(self, tree, root, here, ctx):
        """mypy: a type checker, slower than ruff (seconds, not milliseconds), so it is a setting."""
        with ctx.tracer.stage("TYPES", "python", "mypy (no ML)", rel(root), "type errors with file:line") as ev:
            proc = subprocess.run([sys.executable, "-m", "mypy", "--no-incremental", "--ignore-missing-imports",
                                   "--no-error-summary", "--show-error-codes", str(here)],
                                  cwd=tree, capture_output=True, text=True, timeout=120)
            found = [{"where": f"{back_to_workspace(m.group(1), tree, root)}:{m.group(2)}",
                      "rule": m.group(4) or "type", "message": m.group(3), "by": "mypy"}
                     for m in re.finditer(r"^(.+?\.py):(\d+): error: (.*?)(?:  \[([\w-]+)\])?$", proc.stdout, re.M)]
            ev.summary = f"{len(found)} type error(s)"
        return found


# =====================================================================================================================
class SearchCodeTool(Tool):
    name = "search_code"
    description = ("Search the workspace's Python code for a name or a piece of text, like grep. Returns every "
                   "matching line as file:line: code. Use it to find where a function is defined or used.")
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The name or text to look for, e.g. median or eval("},
            "path": {"type": "string", "description": "The folder to search, e.g. workspace/code_demo"},
        },
        "required": ["text"],
    }
    model_id = "a plain text search (no ML)"
    check_unused, repeatable = False, True
    max_matches = 30
    how_it_works = ("No model here: Python reads every .py and .toml file in the folder (your edited copies where you "
                    "changed one) and keeps the lines that contain the text, ignoring upper and lower case.")

    def run(self, ctx, text, path="workspace"):
        folder = original(resolve(path, ctx, allow_dir=True), ctx)
        folder = folder if folder.is_dir() else folder.parent
        with ctx.tracer.stage("TOOL: search_code", "python", self.model_id, f'"{text}" in {rel(folder)}',
                              "file:line matches") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            files = [p for p in sorted(folder.rglob("*")) if p.suffix in CODE_TYPES and not set(p.parts) & set(SKIP)]
            files += [p for p in ctx.copies if Path(p).is_relative_to(folder) and not Path(p).exists()]  # new files
            matches = []
            for f in files:
                lines = current_version(f, ctx).read_text(encoding="utf-8", errors="replace").splitlines()
                matches += [f"{rel(f)}:{n}: {line.strip()[:120]}" for n, line in enumerate(lines, 1)
                            if text.lower() in line.lower()]
            for m in matches[:self.max_matches]:
                ctx.tracer.say(m, kind="python")
            in_files = len({m.split(":")[0] for m in matches})
            ev.summary = f"{len(matches)} match(es) in {in_files} of {len(files)} file(s)"
        return {"artifacts": [], "observations": {
            "text": text, "summary": ev.summary, "matches": matches[:self.max_matches],
            **({"not_shown": len(matches) - self.max_matches} if len(matches) > self.max_matches else {})}}


# =====================================================================================================================
class WriteCodeTool(Tool):
    name = "write_code"
    description = ("Write whole functions or classes into an existing Python file. A function or class the file "
                   "already has is replaced by your new version (found by its name); a new one is added at the end, "
                   "and a new import goes below the other imports. Other lines are not changed. The file is saved as "
                   "a copy in the run folder and checked for syntax errors.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file, e.g. workspace/code_demo/stats.py"},
            "code": {"type": "string", "description": "Complete functions or classes, each with its def or class line."},
        },
        "required": ["path", "code"],
    }
    model_id = "ast + difflib (no ML)"
    # Like edit_code, a helper: a fix may use either. Unfinished work is told by facts instead: a test that still fails
    # (run_tests), or a request for new code that nothing has written yet (prepare)
    check_unused, repeatable = False, True
    wanted, written = set(), set()  # set for each request by prepare(): "code" and/or "test"
    # No `requires = ("read_file",)`: a failing test's result already shows the function's code, and with the rule,
    # Qwen2.5-Coder 7B's correct median() was sent back twice with "call read_file first" and the run ran out of steps
    how_it_works = ("No model here: ast, Python's own parser, splits your code into its functions, classes and imports "
                    "and finds each function's lines in the file by its name. A function the file has is replaced, a "
                    "new one is added at the end, an import goes below the others. Small models write whole functions "
                    "far more reliably than they copy the exact lines edit_code needs.")

    def prepare(self, ctx):
        """What the request asks to be written, from its own words: the PENDING CHECK asks once if the LLM finishes
        before all of it is written. Llama 3.2 3B and Qwen2.5-Coder 7B both wrote the function and forgot the test."""
        self.wanted = {"code"} if ASKS_FOR_CODE.search(ctx.request) else set()
        self.wanted |= {"test"} if ASKS_FOR_A_TEST.search(ctx.request) else set()
        self.written = set()
        self._still_pending(ctx)

    def _still_pending(self, ctx):
        missing = {"code": "the new code", "test": "a test for it in a test file (tests/test_*.py, where pytest looks)"}
        left = [missing[part] for part in ("code", "test") if part in self.wanted - self.written]
        if left:
            ctx.pending["write_code"] = f"The user asked for {' and '.join(left)}, and that has not been written yet"
        else:
            ctx.pending.pop("write_code", None)

    def run(self, ctx, path, code):
        source = original(resolve(path, ctx), ctx)
        if source.suffix != ".py":
            raise ValueError(f"write_code writes into Python files, not {source.name}: use edit_code.")
        if "\\n" in code and "\n" not in code:  # small models often write a newline as the two characters \n
            code = code.replace("\\n", "\n")
        code = textwrap.dedent(re.sub(r"^\s*```\w*\n|\n```\s*$", "", code)).strip("\n")  # a markdown fence, too
        if syntax(code) != "ok":
            raise ValueError(f"Your code is not valid Python ({syntax(code)}). Send complete functions, each "
                             "starting at its def line.")
        parts, moved = pieces(code), []
        test_file = project_root(source) / "tests" / f"test_{source.stem}.py"
        if not is_test_file(source) and test_file.is_file():
            # ---- tests go where pytest looks: Llama 3.2 3B sent percentile() AND test_percentile (with "import pytest")
            # to stats.py seven times in a row, each refused. Now the harness moves them, and says so ----
            moved = [p for p in parts if (p[1] == "definition" and p[0].startswith("test_"))
                     or (p[1] == "import" and "pytest" in p[2])]
            parts = [p for p in parts if p not in moved]
        results = [self._write(source, parts, ctx)] if parts else []
        if any(p[1] == "definition" for p in moved):
            used = {n.id for _, _, piece in moved for n in ast.walk(ast.parse(piece)) if isinstance(n, ast.Name)}
            needed = sorted(used & {name for name, kind, _ in parts if kind == "definition"})
            if needed:  # the test needs the new function from the module
                moved.insert(0, (None, "import", f"from {source.stem} import {', '.join(needed)}"))
            ctx.tracer.say(f"moved {', '.join(p[0] for p in moved if p[0])} from {source.name} to {rel(test_file)}: "
                           "pytest only collects tests from the test files", kind="python")
            results.append(self._write(test_file, moved, ctx))
        observations = {"done": [f"{rel(f)}: {what}" for f, _, _, done in results for what in done],
                        "diff": "".join(diff for _, _, diff, _ in results)}
        if moved:
            observations["moved_by_harness"] = f"the test(s) went to {rel(test_file)}, where pytest finds them"
        for f, copy, _, _ in results:
            self.written.add("test" if is_test_file(f) else "code")
            _after_change(f, copy, copy.read_text(encoding="utf-8"), ctx, observations)
        self._still_pending(ctx)
        return {"artifacts": [{"type": "code", "path": copy, "note": f"changed copy of {rel(f)}; the original is "
                                                                     "unchanged"} for f, copy, _, _ in results],
                "observations": observations}

    def _write(self, source, parts, ctx):
        """The pieces into one file's working copy -> (the file, its copy, the diff, what was done)."""
        copy = working_copy(source, ctx)  # the copy this request already made (or a new file's), else a new copy
        with ctx.tracer.stage("TOOL: write_code", "python", self.model_id, "functions / classes",
                              "changed file + diff") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            before, newline = read_text_keep_newlines(copy)
            if syntax(before) != "ok":
                raise ValueError(f"{source.name} has a syntax error ({syntax(before)}): fix it with edit_code first.")
            after, done = before, []
            for name, kind, piece in parts:
                after, what = put(after, name, kind, piece, source, ctx)
                done.append(what)
            write_text_keep_newlines(copy, after, newline)
            diff = _diff(before, after, source.name)
            ctx.tracer.say(diff.rstrip(), kind="python")
            ev.summary = f"{source.name}: " + "; ".join(done)
            ev.details = {"diff": diff}
            ev.artifacts.append(str(copy))
        return source, copy, diff, done


def pieces(code):
    """New code -> [(name, kind, text)], one per top-level statement: "definition", "import" or "other". Comments and
    decorators just above a definition stay with it."""
    lines, out, start = code.split("\n"), [], 0
    for node in ast.parse(code).body:
        kind = "definition" if isinstance(node, DEFINITIONS) else "import" if isinstance(node, IMPORTS) else "other"
        out.append((getattr(node, "name", None), kind, "\n".join(lines[start:node.end_lineno]).strip("\n")))
        start = node.end_lineno
    return out


def put(text, name, kind, piece, source, ctx):
    """One piece of new code into the file's text -> (new text, what was done). Plain Python with ast."""
    tree, lines = ast.parse(text), text.split("\n")
    old = {node.name: node for node in tree.body if isinstance(node, DEFINITIONS)}
    if kind == "definition" and name in old:
        if is_test_file(source) and not CHANGE_TESTS.search(ctx.request):  # the same rule as edit_code's
            raise ValueError(f"{name} is an existing test, and existing tests say what the code must do: the user did "
                             "not ask to change a test. Give a new test a new name.")
        node = old[name]
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        lines[first - 1:node.end_lineno] = piece.split("\n")
        return "\n".join(lines), f"replaced {name} (lines {first}-{node.end_lineno})"
    if kind == "import":
        if piece.strip() in {line.strip() for line in lines}:
            return text, f"'{piece.strip()}' was already there"
        imports = [node for node in tree.body if isinstance(node, IMPORTS)]
        docstring = tree.body[0].end_lineno if tree.body and isinstance(tree.body[0], ast.Expr) else 0
        at = imports[-1].end_lineno if imports else docstring
        lines[at:at] = ([""] if at and not imports else []) + piece.split("\n")  # a blank line after a docstring
        return "\n".join(lines), f"added '{piece.strip()}' at line {at + 1}"
    added = text.rstrip("\n") + "\n\n\n" + piece + "\n"  # Python style: two blank lines before a new definition
    return added, f"added {name or piece.split(chr(10))[0][:40]} at the end"


def _defined(text):
    """Top-level function and class names in Python source -> {name: line}. Unparsable source: none."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    return {node.name: node.lineno for node in tree.body if isinstance(node, DEFINITIONS)}


# =====================================================================================================================
class CreateFileTool(Tool):
    name = "create_file"
    description = ("Create a new text file in the workspace, such as a new module or a new test file, with its "
                   "complete content. It is saved as a copy in the run folder; Python files are checked for syntax "
                   "errors. To change a file that already exists, use edit_code or write_code.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Where the new file belongs, e.g. workspace/code_demo/tests/test_new.py"},
            "content": {"type": "string", "description": "The complete text of the new file."},
        },
        "required": ["path", "content"],
    }
    model_id = "string handling (no ML)"
    repeatable = True
    how_it_works = ("No model here: Python checks that the path is inside workspace/ and that no such file exists, "
                    "then writes the file into the run folder. When the tests run, it is laid into the project copy "
                    "at the path you gave.")

    def run(self, ctx, path, content):
        raw = str(path).strip().strip("\"'").replace("\\", "/")
        relative = raw.split("workspace/", 1)[1] if "workspace/" in raw else raw.lstrip("/")
        target = (WORKSPACE_DIR / relative).resolve()
        if not target.is_relative_to(WORKSPACE_DIR) or Path(relative).is_absolute():
            raise PermissionError(f"{raw} is outside the workspace: new files go in workspace/.")
        if target.suffix not in TEXT_TYPES:
            raise ValueError(f"create_file writes text files ({', '.join(sorted(TEXT_TYPES))}), not {target.name}.")
        if target.exists() or target in ctx.copies:
            raise ValueError(f"{rel(target)} already exists. Use write_code to add a function to it, or edit_code to change lines.")
        with ctx.tracer.stage("TOOL: create_file", "python", self.model_id, "path + content", "new file") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            if "\\n" in content and "\n" not in content:
                content = content.replace("\\n", "\n")
            copy = ctx.run_dir / target.name
            if copy.exists():
                copy = ctx.run_dir / f"{target.parent.name}_{target.name}"
            copy.write_text(content.strip("\n") + "\n", encoding="utf-8")
            ctx.copies[target] = copy  # from now on, every tool sees this as the file at that path
            ev.summary = f"{rel(target)}: {len(content.splitlines())} lines, saved as {rel(copy)}"
            ev.artifacts.append(str(copy))
        observations = {"created": rel(target)}
        _after_change(target, copy, copy.read_text(encoding="utf-8"), ctx, observations)
        note = f"new file, meant for {rel(target)}; nothing in workspace/ was changed"
        return {"artifacts": [{"type": "code", "path": copy, "note": note}], "observations": observations}


# =====================================================================================================================
FINDING = re.compile(r"^\s*(?:(?P<file>[^\s:,]+\.\w+)\s*[:,]?\s*)?(?:on\s+)?(?:lines?\s*|L)?(?P<line>\d+)"
                     r"(?:\s*-\s*\d+)?(?::\d+)?\s*[:\-–)]?\s*(?P<text>.*)$", re.I | re.S)
NOT_EVIDENCE = set(keyword.kwlist) | {"self", "print", "len", "the", "and", "for", "not"}


class ReviewCodeTool(Tool):
    name = "review_code"
    description = ("Report your code review: give each problem you found as one string, path:line: what is wrong, for "
                   "example workspace/code_demo/stats.py:17: returns the wrong value for an even count. The harness "
                   "checks every finding against the file, the linter and the tests, and tells you which ones hold.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file you reviewed, e.g. workspace/code_demo/readings.py"},
            "findings": {"type": "array", "items": {"type": "string"},
                         "description": "One finding per problem, each as path:line: what is wrong"},
        },
        "required": ["path", "findings"],
    }
    model_id = "regex + ast + ruff + the last test run (no ML)"
    requires = ("read_file",)  # a review cites lines, so the file must have been read (CHECK ORDER in agent.py)
    how_it_works = ("No model here: Python reads the file and line each finding cites. A line that does not exist is "
                    "invented; a line the linter flags, or that sits in a function a failing test calls, is "
                    "confirmed; a real line that mentions what the finding is about is plausible, for a person to "
                    "check; a finding about a name that is on another line is on the wrong line; anything else is "
                    "not supported.")

    def run(self, ctx, path, findings):
        if "review" in ctx.facts:  # the rules say "call review_code once with all your findings": the harness holds the
            # LLM to it. Cut off mid-answer, Llama 3.2 3B sent a second "review" of a change it had only imagined
            raise ValueError("You already reported your review in this request, and the harness checked it (see its "
                             "result above). Do not call review_code again: tell the user what it confirmed.")
        reviewed, root = project_of(path, ctx)
        tree, _ = project_copy(root, ctx)
        with ctx.tracer.stage("VERIFY FINDINGS", "python", self.model_id, f"{len(findings)} finding(s) from the LLM",
                              "a verdict for each") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            linted = lint(tree, root, tree / reviewed.relative_to(root))
            failures = [t for t in ctx.facts.get("tests", {}).get("failures", []) if _touches(t, reviewed, ctx)]
            checked = [check_finding(_as_text(f), reviewed, linted, failures, ctx) for f in findings]
            for c in checked:
                ctx.tracer.say(f"{c['verdict'].upper():<13} {c['finding'][:90]}\n{'':>19}{c['evidence'][:110]}",
                               kind="python")
            cited = {c.get("where") for c in checked}
            missed = [f"{f['where']} {f['rule']}: {f['message']}" for f in linted if f["where"] not in cited
                      and not any(_near(f["where"], w) for w in cited if w)]
            missed += [f"{t['test']} fails: {t['message']}" for t in failures
                       if not any(c["verdict"] == "confirmed" and t["test"] in c["evidence"] for c in checked)]
            counts = {v: sum(c["verdict"] == v for c in checked)
                      for v in ("confirmed", "plausible", "wrong line", "not supported", "invented")}
            ev.summary = (f"{len(checked)} finding(s): " + ", ".join(f"{n} {v}" for v, n in counts.items() if n)
                          + (f"; the tools found {len(missed)} more the review did not cite" if missed else ""))
            ev.details = {"checked": checked, "also_found_by_tools": missed, "lint": linted,
                          "tests_run": "tests" in ctx.facts}
        invented = [c for c in checked if c["verdict"] == "invented"]
        ctx.notes.append(f"review_code checked {len(checked)} finding(s): "
                         + ", ".join(f"{n} {v}" for v, n in counts.items() if n)
                         + (" (" + "; ".join(c["evidence"] for c in invented[:3]) + ")" if invented else "")
                         + (". Also found by the linter or the tests: " + "; ".join(missed[:5]) if missed else "") + ".")
        ctx.facts["review"] = checked
        return {"artifacts": [], "observations": {
            "summary": ev.summary, "checked": [{k: c[k] for k in ("finding", "verdict", "evidence")} for c in checked],
            **({"also_found_by_tools": missed} if missed else {}),
            **({"note": "No tests were run in this request, so no finding could be confirmed by a test."}
               if "tests" not in ctx.facts else {})}}


def _as_text(item):
    """A finding the LLM wrote as a dict ({'line': 9, 'issue': ...}), which validate() turned into its repr -> text."""
    if item.startswith("{"):
        try:
            d = ast.literal_eval(item)
            file = d.get("file") or d.get("path") or ""
            line = d.get("line") or d.get("line_number") or ""
            text = d.get("issue") or d.get("problem") or d.get("message") or d.get("description") or ""
            return f"{file}:{line}: {text}".lstrip(":")
        except (ValueError, SyntaxError, AttributeError):
            pass
    return item


def _touches(failure, reviewed, ctx):
    """Is a failing test about the reviewed file or folder? Its traceback goes through it, or its message calls a
    function the file defines (an assert in the test file shows only 'where 300.0 = median([...])')."""
    if reviewed.is_dir() or any(line.startswith(rel(reviewed) + ":") for line in failure["lines"]):
        return True
    names = _defined(current_version(reviewed, ctx).read_text(encoding="utf-8", errors="replace"))
    return any(f"{name}(" in failure["message"] for name in names)


def _near(a, b):
    """'x.py:9' and 'x.py:10' are the same place give or take a line (a decorator, a wrapped statement)."""
    fa, _, la = a.rpartition(":")
    fb, _, lb = b.rpartition(":")
    return fa == fb and la.isdigit() and lb.isdigit() and abs(int(la) - int(lb)) <= 1


def enclosing_function(text, line):
    """The name of the innermost function whose lines include `line`, via ast. None outside any function."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= node.end_lineno:
            if best is None or node.lineno >= best.lineno:
                best = node
    return best.name if best else None


def check_finding(finding, reviewed, linted, failures, ctx):
    """One finding -> {"finding", "where", "verdict", "evidence"}. The five verdicts, in the order they are tried:
    invented (no such file or line), confirmed (the linter or a failing test), plausible, wrong line, not supported."""
    m = FINDING.match(finding)
    if not m:
        return {"finding": finding, "verdict": "invented", "evidence": "names no line: a finding must be path:line: problem"}
    file, number, claim = m.group("file"), int(m.group("line")), m.group("text").strip() or finding
    try:
        source = current_version(resolve(file, ctx) if file else reviewed, ctx)
    except (FileNotFoundError, PermissionError):
        return {"finding": finding, "verdict": "invented", "evidence": f"there is no file {file} in the workspace"}
    if source.is_dir():
        return {"finding": finding, "verdict": "invented", "evidence": "names no file: give it as path:line: problem"}
    name = rel(original(source, ctx))
    text = source.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not 1 <= number <= len(lines):
        return {"finding": finding, "where": None, "verdict": "invented",
                "evidence": f"{Path(name).name} has {len(lines)} lines: there is no line {number}"}
    where, code = f"{name}:{number}", lines[number - 1].strip()
    flagged = [f for f in linted if _near(f["where"], where)]
    if flagged:
        f = flagged[0]
        return {"finding": finding, "where": where, "verdict": "confirmed",
                "evidence": f"ruff {f['rule']} at {f['where']}: {f['message']}"}
    function = enclosing_function(text, number) if source.suffix == ".py" else None
    for t in failures:
        if any(_near(line, where) for line in t["lines"]) or (function and f"{function}(" in t["message"]):
            return {"finding": finding, "where": where, "verdict": "confirmed",
                    "evidence": f"the failing test {t['test']} goes through {function or where}: {t['message'][:120]}"}
    words = {w for w in re.findall(r"[A-Za-z_]\w{2,}", code) if w not in NOT_EVIDENCE}
    mentioned = sorted(w for w in words if re.search(rf"\b{re.escape(w)}\b", claim, re.I))
    if code and not code.startswith("#") and mentioned:
        return {"finding": finding, "where": where, "verdict": "plausible",
                "evidence": f"line {number} is `{code[:80]}` and has {', '.join(mentioned)}; no linter rule or failing "
                            "test confirms it, so a person should check"}
    elsewhere = names_elsewhere(text, claim, number) if source.suffix == ".py" else []
    if elsewhere:  # the right problem on the wrong line: Llama 3.2 3B wrote "readings.py:33: undefined name 'eval'"
        return {"finding": finding, "where": where, "verdict": "wrong line",
                "evidence": f"line {number} is `{code[:80] or '(blank)'}`; "
                            + "; ".join(f"{name} is on line {line}" for name, line in elsewhere[:2])}
    return {"finding": finding, "where": where, "verdict": "not supported",
            "evidence": f"line {number} is `{code[:80] or '(blank)'}`: nothing on it matches the finding"}


def names_elsewhere(text, claim, number):
    """Names from the code (via ast: functions, variables, imports, calls) that the finding mentions, on lines other
    than the one it cites -> [(name, first line)]. Plain Python."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    first, inside = {}, set()  # a function's own name is not "elsewhere" when the cited line is inside it
    for node in ast.walk(tree):
        if isinstance(node, DEFINITIONS) and node.lineno <= number <= node.end_lineno:
            inside.add(node.name)
        names = ([node.id] if isinstance(node, ast.Name) else [node.name] if isinstance(node, DEFINITIONS)
                 else [a.asname or a.name for a in node.names] if isinstance(node, IMPORTS) else [])
        for name in names:
            if len(name) > 2 and name not in NOT_EVIDENCE and node.lineno != number:
                first[name] = min(first.get(name, node.lineno), node.lineno)
    return sorted(((n, line) for n, line in first.items() if n not in inside and re.search(rf"\b{re.escape(n)}\b", claim)),
                  key=lambda pair: pair[1])
