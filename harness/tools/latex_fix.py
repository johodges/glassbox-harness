"""
fix_latex and edit_tex: change a copy of a LaTeX project, and check every change by building and auditing it again.

  LEDGER          the project's last audit (this request's, or outputs/ledgers/<project>.json; latex_audit.py)
  CORRECTED COPY  the project copied to outputs/run_.../latex/corrected/, with the fixes already verified in earlier
                  requests applied again. The original never changes.
  APPROVAL        a "likely" fix asks a person y/n first; a "needs a person" finding has no fix to apply
  APPLY FIXES     each fix is an exact-match edit: its old text must occur exactly once in the paragraph of its line
                  (or, if the paragraph moved, once in the file). No guessing.
  VERIFY          build + the same audit again: a fix is verified if its finding is gone and no new finding appeared
                  on its line; otherwise it failed, and its edit is undone

Every change goes into the ledger, which make_latexdiff (latexdiff.py) reads to mark the changes up for review.
Nothing here is AI: the LLM only chooses which tool to call.
"""

import difflib
import re
from pathlib import Path

from .base import Refusal, Tool, approve_in_tool
from .documents import read_text_keep_newlines, write_text_keep_newlines
from .files import rel
from .latex_audit import (CHECKS, audit, counts_of, last_ledger, one_line, paragraph, project_name, snippet, squash,
                          write_ledger)
from .latex_project import PROJECT_PARAMETERS, build, build_summary, copy_project, latex_project, publish


# ---- the corrected copy ------------------------------------------------------------------------------------------------
def corrected_copy(root, main, ctx, ledger):
    """This request's corrected copy of the project, made on first use: the original plus every change verified in an
    earlier request (so the corrections add up from one request to the next)."""
    made = ctx.facts.setdefault("latex_corrected", {})
    if str(root) in made:
        return Path(made[str(root)])
    tree = copy_project(root, ctx, "corrected")
    again = [f for f in ledger["findings"] if f["status"] == "verified" and f["fix"]]
    if again:
        with ctx.tracer.stage("APPLY FIXES", "python", "exact-match edits (no ML)",
                              f"{len(again)} change(s) verified earlier", "the corrected copy") as ev:
            applied, failed = apply_fixes(tree, again)
            for f, why in failed:
                f["status"], f["note"] = "failed", f"could not apply it again: {why}"
            ev.summary = f"{len(applied)} earlier change(s) applied again" + (f", {len(failed)} no longer apply"
                                                                             if failed else "")
    made[str(root)] = str(tree)
    return tree


def apply_fixes(tree, fixes):
    """
    Exact-match edits, file by file, from the bottom of each file up, so an edit never moves the text of the ones
    still to come. -> (applied [(finding, file, its line after all the edits)], failed [(finding, why)])
    """
    applied, failed = [], []
    for name in sorted({f["file"] for f in fixes}):
        path = Path(tree) / name
        if not path.is_file():
            failed += [(f, f"{name} is not in the project") for f in fixes if f["file"] == name]
            continue
        text, newline = read_text_keep_newlines(path)
        done = []
        for f in sorted((f for f in fixes if f["file"] == name), key=lambda f: -(f["line"] or 0)):
            old, new = f["fix"]["old"], f["fix"]["new"]
            low, high = paragraph(text, f["line"] or 1)
            if text[low:high].count(old) == 1:
                at = low + text[low:high].index(old)
            elif text.count(old) == 1:  # the paragraph moved (an earlier edit): the one place in the file will do
                at = text.index(old)
            else:
                failed.append((f, f"the text to change occurs {text.count(old)} times in {name}, not once"))
                continue
            text = text[:at] + new + text[at + len(old):]
            done.append((f, at))
        write_text_keep_newlines(path, text, newline)
        for f, at in done:  # where each change is now
            applied.append((f, name, text.count("\n", 0, at) + 1))
    return applied, failed


def same_finding(a, b):
    """The same problem in two audits (line numbers move after edits): the same check in the same file, about the
    same text."""
    if a["check"] != b["check"] or a["file"] != b["file"]:
        return False
    if a["fix"] and b["fix"]:
        return squash(a["fix"]["old"]) == squash(b["fix"]["old"])
    return squash(a["evidence"]) == squash(b["evidence"])


def verify(root, main, tree, ctx, before, applied, checks):
    """
    VERIFY: build and audit the corrected copy (a fresh copy of it, so corrected/ stays free of build files), then
    judge each applied fix -> ({id: why it failed}, the new audit's ledger and build facts).
    """
    after, _, facts, _ = audit(tree, tree / main.relative_to(root), ctx, checks, use_judge=False, name="verify",
                               save=False)
    with ctx.tracer.stage("VERIFY", "python", "the two audits compared (no ML)", "the audit before and after",
                          "verified or failed, per fix") as ev:
        old = before["findings"]
        new_problems = [g for g in after["findings"] if not any(same_finding(g, f) for f in old)]
        failed = {}
        for f, name, line in applied:
            if not facts["pdf"]:
                failed[f["id"]] = "the corrected copy no longer builds a PDF"
            elif any(same_finding(g, f) for g in after["findings"]) and f["category"] != "requested":
                failed[f["id"]] = "the audit still finds it"
            else:
                near = [g for g in new_problems if g["file"] == name and abs((g["line"] or 0) - line) <= 1
                        and g["check"] != "overfull box"]  # a longer word can push a line out: report, don't undo
                if near:
                    failed[f["id"]] = f"a new problem appeared on its line: {near[0]['check']}: {near[0]['why']}"
        ev.summary = (f"{len(applied) - len(failed)} of {len(applied)} change(s) verified; "
                      f"{len(new_problems)} new finding(s) anywhere; build: {build_summary(facts)}")
        ev.details = {"failed": failed, "new findings": [one_line(g) for g in new_problems]}
    return failed, new_problems, facts


def undo(tree, snapshot, keep, ctx):
    """Put the files back as they were before APPLY FIXES and apply only the changes that verified."""
    with ctx.tracer.stage("UNDO", "python", "the files as they were + the verified edits (no ML)",
                          "the failed change(s)", "the corrected copy without them") as ev:
        for name, (text, newline) in snapshot.items():
            write_text_keep_newlines(Path(tree) / name, text, newline)
        applied, _ = apply_fixes(tree, keep)
        ev.summary = f"{len(snapshot)} file(s) restored; {len(applied)} verified change(s) applied again"


def snapshot_of(tree, names):
    return {name: read_text_keep_newlines(Path(tree) / name) for name in names if (Path(tree) / name).is_file()}


def remember(ledger, root, main, tree, facts, ctx):
    """The corrected copy goes into the ledger, so make_latexdiff finds it in this request or a later one."""
    ledger["corrected"] = {"tree": str(tree), "main": main.relative_to(root).as_posix(), "pdf": facts["pdf"],
                           "build": build_summary(facts), "run_dir": str(ctx.run_dir)}
    ctx.facts.setdefault("latex_ledgers", {})[str(root)] = ledger
    return write_ledger(ledger, ctx.run_dir)


def approve(ctx, question):
    """APPROVAL for one likely fix (base.approve_in_tool: config.ask_before_acting off = yes without asking)."""
    return approve_in_tool(ctx, question, "a likely fix")


# ---- the tools -----------------------------------------------------------------------------------------------------------
class FixLatexTool(Tool):
    name = "fix_latex"
    description = ("Fix the errors the last audit of a LaTeX project found, on a corrected copy, then rebuild and "
                   "audit it again to verify each fix. which: 'certain' (default), 'all' (likely fixes ask a person "
                   "first), or finding ids such as 'A003, A007'.")
    parameters = {"type": "object",
                  "properties": {**PROJECT_PARAMETERS["properties"],
                                 "which": {"type": "string",
                                           "description": "certain (default), all, or finding ids, e.g. A003, A007"}},
                  "required": ["path"]}
    model_id = "exact-match edits + latexmk + the audit again (no ML)"
    requires = ("audit_latex",)  # if the router offered the audit too, it runs first (CHECK ORDER in agent.py)
    how_it_works = ("No model here: each fix in the ledger is an exact-match edit on a copy of the project. Then "
                    "latexmk builds the copy and the same audit runs again: a fix whose finding is gone, with "
                    "nothing new on its line, is verified; any other is undone.")

    def run(self, ctx, path, which="certain"):
        t = ctx.tracer
        root, main = latex_project(path, ctx)
        t.say(self.how_it_works, kind="python")
        ledger = last_ledger(root, ctx)
        if ledger is None:  # no audit yet: make one
            t.say(f"no audit of {rel(root)} yet: auditing it first", kind="python")
            ledger, _, _, _ = audit(root, main, ctx)
        findings = ledger["findings"]
        ids = {i.upper() for i in re.findall(r"\b[AaEe]\d{3}\b", str(which))}
        mode = "ids" if ids else "all" if "all" in str(which).lower() else "certain"
        with t.stage("SELECT FIXES", "python", "the ledger (no ML)", f"which={which!r}", "the fixes to apply") as ev:
            if mode == "ids":
                chosen = [f for f in findings if f["id"] in ids]
            else:
                chosen = [f for f in findings if f["status"] in ("found", "failed") and f["fix"]
                          and (f["confidence"] == "certain" or (mode == "all" and f["confidence"] == "likely"))]
            no_fix = [f for f in chosen if not f["fix"]]
            chosen = [f for f in chosen if f["fix"] and f["status"] != "verified"]
            unknown = sorted(ids - {f["id"] for f in findings})
            ev.summary = (f"{len(chosen)} fix(es) chosen ({mode})" + (f"; {len(no_fix)} without a fix" if no_fix
                                                                       else "")
                          + (f"; not in the ledger: {', '.join(unknown)}" if unknown else ""))
        for f in no_fix:
            f["status"] = "skipped"
            f["note"] = "needs a person: no fix follows from the facts"
        approved = []
        for f in chosen:  # a person says yes to anything a checker only suspects
            if f["confidence"] == "certain" or approve(ctx, f"Apply {f['id']} in {f['file']}:{f['line']}: "
                                                            f"\"{f['fix']['old']}\" -> \"{f['fix']['new']}\"?"):
                approved.append(f)
            else:
                f["status"], f["note"] = "skipped", "a person said no"
        if not approved:
            write_ledger(ledger, ctx.run_dir)
            raise Refusal(f"Nothing to fix in {rel(root)} with which={which!r}: " + counts_of(findings)
                          + ". Findings that need a person: " + "; ".join(one_line(f) for f in findings
                                                                         if f["confidence"] == "needs a person")[:600])
        tree = corrected_copy(root, main, ctx, ledger)
        snapshot = snapshot_of(tree, {f["file"] for f in approved})
        with t.stage("APPLY FIXES", "python", "exact-match edits (no ML)", f"{len(approved)} fix(es)",
                     "the corrected copy") as ev:
            applied, not_applied = apply_fixes(tree, approved)
            for f, why in not_applied:
                f["status"], f["note"] = "failed", f"could not apply: {why}"
            diff = changes_diff(snapshot, tree)
            t.say(diff.rstrip() or "(no change)", kind="python")
            ev.summary = f"{len(applied)} applied, {len(not_applied)} could not be applied, in {rel(tree)}"
            ev.details = {"diff": diff}
        failed, new_problems, facts = verify(root, main, tree, ctx, ledger, applied, ledger.get("checks", CHECKS))
        for f, _, _ in applied:
            f["status"] = "failed" if f["id"] in failed else "verified"
            if f["id"] in failed:
                f["note"] = failed[f["id"]]
        if failed:
            undo(tree, snapshot, [f for f, _, _ in applied if f["id"] not in failed], ctx)
            check = copy_project(tree, ctx, "verify")  # built on a copy: corrected/ stays free of build files
            facts = build(check, check / main.relative_to(root), ctx)
        paths = remember(ledger, root, main, tree, facts, ctx)
        verified = [f for f, _, _ in applied if f["id"] not in failed]
        broken = [f for f in approved if f["status"] == "failed"]
        people = [f for f in findings if f["confidence"] == "needs a person" and f["status"] != "verified"]
        ctx.notes.append(f"fix_latex on {rel(root)}: {len(verified)} fix(es) verified by a rebuild and a second "
                         f"audit, {len(broken)} failed and undone; {len(people)} finding(s) need a person "
                         f"({', '.join(f['id'] for f in people)}).")
        artifacts = [{"type": "ledger", "path": paths["report"], "note": "the audit, with what was fixed"}]
        if facts.get("pdf"):
            artifacts.append({"type": "latex_pdf", "path": publish(facts["pdf"], ctx, root, "corrected"),
                              "note": "the corrected paper"})
        return {"artifacts": artifacts,
                "observations": {
                    "project": rel(root), "corrected_copy": rel(tree),
                    "summary": (f"{len(approved)} fix(es) tried: {len(verified)} verified, {len(broken)} failed and "
                                f"undone; {sum(f['status'] == 'skipped' for f in findings)} skipped; "
                                f"{len(people)} finding(s) need a person"),
                    "verified": [one_line(f) for f in verified][:12],
                    "failed": [f"{one_line(f)} -- {f.get('note', '')}" for f in broken][:6],
                    "needs_a_person": [one_line(f) for f in people][:8],
                    "new_findings": [f"{g['file']}:{g['line']} {g['check']}: {g['why']}" for g in new_problems][:6],
                    "build": build_summary(facts),
                    "next": "make_latexdiff marks the changes up for review"}}


def changes_diff(snapshot, tree):
    """A unified diff of each changed file, for the trace."""
    out = []
    for name, (before, _) in snapshot.items():
        after, _ = read_text_keep_newlines(Path(tree) / name)
        out += difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"{name} (before)",
                                    f"{name} (after)", n=0)
    return "".join(out)


QUOTED_EDIT = re.compile(r"\b(?:change|replace|correct)\s+[\"“'](.+?)[\"”']\s+(?:to|with|by|into)\s+"
                         r"[\"“'](.+?)[\"”']", re.I | re.S)


def words_pattern(text):
    """'a fast fire' -> a regex that also finds it across a line break ('a fast\\nfire')."""
    return re.compile(r"\s+".join(re.escape(w) for w in text.split()))


class EditTexTool(Tool):
    name = "edit_tex"
    description = ("Change words in a LaTeX project (its folder or main .tex) on a corrected copy: replace the exact "
                   "text `old` with `new`, then rebuild. Use it for an edit the user asks for in words. The change "
                   "goes into the ledger, so make_latexdiff shows it.")
    parameters = {"type": "object",
                  "properties": {**PROJECT_PARAMETERS["properties"],
                                 "old": {"type": "string", "description": "The exact text to replace"},
                                 "new": {"type": "string", "description": "The text that takes its place"}},
                  "required": ["path", "old", "new"]}
    model_id = "an exact-match edit + latexmk (no ML)"
    check_unused, repeatable = False, True
    how_it_works = ("No model here: Python finds the old text (once, in one .tex file of the project, line breaks "
                    "allowed), replaces it on the corrected copy, and latexmk rebuilds the copy. An edit that "
                    "breaks the build is undone.")

    def check_request(self, args, ctx):
        """
        CHECK ARGS: when the user wrote 'change "X" to "Y"', X and Y are the user's own words: the LLM's old and
        new must be them (a small model shortens or 'improves' quoted text). -> (args, summary, note or None)
        """
        quoted = QUOTED_EDIT.findall(ctx.request or "")
        if not quoted:
            return args, "the request quotes no 'change \"X\" to \"Y\"': the LLM's old and new stand", None
        old, new = next(((o, n) for o, n in quoted if squash(o) == squash(args["old"])), quoted[0])
        if squash(old) == squash(args["old"]) and squash(new) == squash(args["new"]):
            return args, f"old and new match the user's words (change \"{old}\" to \"{new}\")", None
        wanted = {**args, "old": old, "new": new}
        note = (f"The user asked to change \"{old}\" to \"{new}\"; your call had old=\"{args['old']}\", "
                f"new=\"{args['new']}\", so the harness used the user's words.")
        return wanted, f"changed to the user's words: \"{old}\" -> \"{new}\"", note

    def run(self, ctx, path, old, new):
        t = ctx.tracer
        root, main = latex_project(path, ctx)
        ledger = last_ledger(root, ctx) or {"project": project_name(root, ctx), "root": str(root),
                                            "main": main.relative_to(root).as_posix(), "audited": None,
                                            "checks": list(CHECKS), "findings": [], "build": "not audited"}
        tree = corrected_copy(root, main, ctx, ledger)
        pattern = words_pattern(old)
        hits = [(p, m) for p in sorted(tree.rglob("*.tex"))
                for m in pattern.finditer(read_text_keep_newlines(p)[0])]
        if len(hits) != 1:
            where = ", ".join(f"{p.relative_to(tree).as_posix()}:{read_text_keep_newlines(p)[0].count(chr(10), 0, m.start()) + 1}"
                              for p, m in hits[:5])
            raise ValueError(f"\"{old}\" occurs {len(hits)} times in the .tex files of {rel(root)}"
                             + (f" ({where}): quote more of the sentence so it is unique." if hits else
                                ": copy the text exactly as it is in the paper (scan_latex or read_file shows it)."))
        source, m = hits[0]
        name = source.relative_to(tree).as_posix()
        text, newline = read_text_keep_newlines(source)
        with t.stage("TOOL: edit_tex", "python", self.model_id, "old + new (text)", "the corrected copy") as ev:
            t.say(self.how_it_works, kind="python")
            after = text[:m.start()] + new + text[m.end():]
            write_text_keep_newlines(source, after, newline)
            diff = "".join(difflib.unified_diff(text.splitlines(True), after.splitlines(True), f"{name} (before)",
                                                f"{name} (after)", n=0))
            t.say(diff.rstrip(), kind="python")
            line = text.count("\n", 0, m.start()) + 1
            ev.summary = f"{name}:{line}: \"{m.group(0)}\" -> \"{new}\""
            ev.details = {"diff": diff}
        check = copy_project(tree, ctx, "verify")
        facts = build(check, check / main.relative_to(root), ctx)
        mine = [e for e in facts["errors"] if e["file"] == name and e["line"] and abs(e["line"] - line) <= 2]
        if not facts["pdf"] or mine:
            write_text_keep_newlines(source, text, newline)
            raise ValueError(f"The edit was undone: it broke the build ({(mine or facts['errors'] or [{}])[0].get('message', 'no PDF')}). "
                             "Check that the new text is valid LaTeX (escape %, & and _ with a backslash).")
        number = 1 + sum(f["id"].startswith("E") for f in ledger["findings"])
        ledger["findings"].append({
            "id": f"E{number:03d}", "category": "requested", "check": "edit asked for", "file": name, "line": line,
            "evidence": snippet(after, m.start(), m.start() + len(new)), "why": f"asked for: \"{old}\" -> \"{new}\"",
            "fix": {"old": m.group(0), "new": new}, "confidence": "certain", "source": "edit_tex", "judge": None,
            "status": "verified"})
        remember(ledger, root, main, tree, facts, ctx)
        artifacts = [{"type": "latex_pdf", "path": publish(facts["pdf"], ctx, root, "corrected"),
                      "note": "the corrected paper, rebuilt"}]
        return {"artifacts": artifacts,
                "observations": {"project": rel(root), "changed": f"{name}:{line}", "diff": diff,
                                 "build": build_summary(facts),
                                 "next": "make_latexdiff marks this change (and any fixes) up for review"}}
