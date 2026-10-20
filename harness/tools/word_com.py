"""
Word through COM, done so that it can't hurt the user's work (OFFICE_COLLAB_DESIGN.md, F5). Three jobs:

  compare(original, revised, out, author)   Word's own Compare: a new document with the differences as tracked changes
  open_check(path)                          does Word open the file without repairing it? its revisions, comments, pages
  pdf(path, out)                            a PDF with the tracked changes and comments shown

How each job runs:
  - in a CHILD Python process with a time limit (config.OFFICE["timeout"]): hung PDF exports took minutes in 3 past
    jobs. The child is this file, run as a script; the harness waits for its one line of JSON;
  - in its OWN Word: DispatchEx starts a new, invisible WINWORD.EXE with DisplayAlerts off. Its process id is written
    to a file at once, so on a timeout the harness stops that process and no other. The user's Word is never attached
    to, and never quit;
  - on COPIES: the inputs are copied into a private temporary folder first, so Word never opens the user's own file
    (a "~$" lock file next to it means Word has it open: noted, and the copy read);
  - with the user's settings untouched: Application.UserName is never set. Compared changes get their author from
    CompareDocuments(RevisedAuthor=...); the harness's own changes from the XML it writes.

Word does none of the editing: that is plain Python (docx_markup.py), testable on any computer. Without Word (Linux,
macOS, no pywin32, Word busy or broken) every function here returns (None, the reason) and the tools say what was
skipped, e.g. "WORD CHECK skipped: Word is not installed".
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WD_ALERTS_NONE = 0
WD_COMPARE_DESTINATION_NEW = 2
WD_GRANULARITY_WORD = 1
WD_FORMAT_DOCUMENT_DEFAULT = 16
WD_EXPORT_PDF = 17
WD_EXPORT_WITH_MARKUP = 7
WD_STATISTIC_PAGES = 2
BUSY = (-2147418111, -2147417846)  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER: Word is busy, try again


# ---- in the harness's process ---------------------------------------------------------------------------------------
def available():
    """-> (True, '') or (False, why): Windows, pywin32, and Word registered as a COM server."""
    if os.name != "nt":
        return False, "Word automation needs Windows"
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError:
        return False, "pywin32 is not installed"
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
    except OSError:
        return False, "Word is not installed"
    return True, ""


def word_pids():
    """The process ids of every WINWORD.EXE running now (tasklist, no extra package)."""
    if os.name != "nt":
        return set()
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"], capture_output=True,
                         text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("winword") and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def stop_word(pid):
    """Stop the Word the harness started, and only it: the pid must still be a WINWORD.EXE."""
    if pid and pid in word_pids():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    return False


def started_by_com():
    """WINWORD.EXE processes that COM started ('/Automation -Embedding' on their command line): a Word a program
    asked for, never one the user opened."""
    if os.name != "nt":
        return set()
    query = ("Get-CimInstance Win32_Process -Filter \"Name='WINWORD.EXE'\" | "
             "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True, text=True,
                             timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(line.split()[0]) for line in out.splitlines()
            if line.strip() and line.split()[0].isdigit() and "embedding" in line.lower()}


def stop_orphan(before, wait=10):
    """A job stopped before it could write down its Word's pid: the one Word that COM started since `before` (the
    pids running when the job began) is the job's. Waits a little, since Word may still be starting. If two appeared
    (another program automating Word at the same moment), neither is stopped: the harness can't tell which
    is its own."""
    for _ in range(wait):
        new = started_by_com() - before
        if len(new) == 1:
            pid = next(iter(new))
            return [pid] if stop_word(pid) else []
        if len(new) > 1:
            return []
        time.sleep(1)
    return []


def run_job(job, args, inputs=(), outputs=(), timeout=None):
    """
    One job in a child process and its own Word. inputs: argument names holding files to copy in first; outputs:
    argument names of files to bring back. -> (result dict, '') or (None, why)
    """
    ok, why = available()
    if not ok:
        return None, why
    if timeout is None:
        from ..config import OFFICE
        timeout = OFFICE["timeout"]
    from .office_common import lock_file
    work = Path(tempfile.mkdtemp(prefix="glassbox_word_"))
    notes = []
    try:
        local = dict(args)
        for key in inputs:
            source = Path(args[key])
            if lock_file(source):
                notes.append(f"{source.name} is open in Word: a copy was read")
            copy = work / f"in_{key}{source.suffix}"
            shutil.copy2(source, copy)
            local[key] = str(copy)
        for key in outputs:
            local[key] = str(work / f"out_{key}{Path(args[key]).suffix}")
        pid_file = work / "word.pid"
        command = [sys.executable, "-I", str(Path(__file__).resolve()), job, json.dumps(local), str(pid_file)]
        before = word_pids()
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pid = _read_pid(pid_file)
            stopped = [pid] if stop_word(pid) else ([] if pid else stop_orphan(before))
            return None, (f"Word did not finish within {timeout} s (a dialog it could not show, or a hang), so the "
                          + (f"Word the harness started (pid {', '.join(map(str, stopped))}) was stopped" if stopped
                             else "job was stopped"))
        stop_word(_read_pid(pid_file))  # the child quits its Word; if it could not, stop it
        line = next((x for x in reversed(out.splitlines()) if x.startswith("{")), None)
        if line is None:
            return None, f"the Word job gave no answer: {(err or out).strip()[-300:]}"
        result = json.loads(line)
        if not result.pop("ok", False):
            return None, result.get("why", "Word failed")
        for key in outputs:
            made = Path(local[key])
            if made.is_file():
                Path(args[key]).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(made), args[key])
                result[key] = str(args[key])
        if notes:
            result["notes"] = notes
        return result, ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _read_pid(pid_file):
    try:
        return int(Path(pid_file).read_text().strip())
    except (OSError, ValueError):
        return None


def compare(original, revised, out, author, timeout=None):
    """Word's Compare, word by word, the changes under `author` -> ({'revisions', 'comments', 'pages', 'file'}, '')
    or (None, why). Formatting changes are compared too; moves are written as a deletion and an insertion."""
    return run_job("compare", {"original": str(original), "revised": str(revised), "out": str(out),
                               "author": author}, inputs=("original", "revised"), outputs=("out",), timeout=timeout)


def open_check(path, pdf=None, texts=False, timeout=None):
    """
    Word opens a copy read-only, with OpenAndRepair off: a file it would have to repair fails here, instead of showing
    the user a repair prompt later. -> ({'revisions', 'comments', 'pages', 'types', ['accepted', 'rejected'],
    ['pdf']}, '') or (None, why). texts: also the text with every change accepted and rejected, as Word sees it.
    pdf: also export the file (with its markup) to this path, in the same Word. A why that starts with OPEN_FAILED
    means the file is broken; any other means Word could not run the check (missing, busy, too slow).
    """
    args = {"path": str(path), "texts": bool(texts)}
    outputs = ()
    if pdf:
        args["pdf"] = str(pdf)
        outputs = ("pdf",)
    return run_job("open_check", args, inputs=("path",), outputs=outputs, timeout=timeout)


def pdf(path, out, timeout=None):
    """A PDF of the document as Word shows it, tracked changes and comments included."""
    return run_job("pdf", {"path": str(path), "out": str(out)}, inputs=("path",), outputs=("out",), timeout=timeout)


# ---- in the child process: Word itself ------------------------------------------------------------------------------
def _retry(call, tries=10):
    """Word starting up or busy rejects calls for a moment: try again, a few times."""
    import pywintypes
    for attempt in range(tries):
        try:
            return call()
        except pywintypes.com_error as exc:
            if exc.args[0] not in BUSY or attempt == tries - 1:
                raise
            time.sleep(0.5)


def _open(app, path):
    return _retry(lambda: app.Documents.Open(FileName=str(path), ConfirmConversions=False, ReadOnly=True,
                                             AddToRecentFiles=False, Visible=False, OpenAndRepair=False,
                                             NoEncodingDialog=True))


def _counts(doc):
    types = [int(r.Type) for r in doc.Revisions] if doc.Revisions.Count <= 2000 else []
    return {"revisions": int(doc.Revisions.Count), "comments": int(doc.Comments.Count),
            "pages": int(doc.ComputeStatistics(WD_STATISTIC_PAGES)), "types": types,
            "authors": sorted({str(r.Author) for r in doc.Revisions}) if doc.Revisions.Count <= 2000 else []}


def _texts(app, path):
    """Word's own accept-all and reject-all of a read-only copy (never saved): the reference for the Python ones."""
    out = {}
    for key, how in (("accepted", "AcceptAll"), ("rejected", "RejectAll")):
        doc = _open(app, path)
        try:
            getattr(doc.Revisions, how)()
            out[key] = str(doc.Content.Text)
        finally:
            doc.Close(SaveChanges=0)
    return out


OPEN_FAILED = "Word could not open the file without repairing it"


def job_open_check(app, path, texts=False, pdf=None):
    import pywintypes
    try:
        doc = _open(app, path)
    except pywintypes.com_error as exc:
        if exc.args[0] in BUSY:
            raise
        details = exc.args[2] if len(exc.args) > 2 and exc.args[2] else None
        raise RuntimeError(f"{OPEN_FAILED}: {(details[2] if details and details[2] else exc.args[1])}") from exc
    try:
        result = _counts(doc)
        if pdf:
            doc.ExportAsFixedFormat(OutputFileName=pdf, ExportFormat=WD_EXPORT_PDF, OpenAfterExport=False,
                                    Item=WD_EXPORT_WITH_MARKUP)
    finally:
        doc.Close(SaveChanges=0)
    if texts:
        result.update(_texts(app, path))
    return result


def job_compare(app, original, revised, out, author):
    old, new = _open(app, original), _open(app, revised)
    try:
        made = _retry(lambda: app.CompareDocuments(
            OriginalDocument=old, RevisedDocument=new, Destination=WD_COMPARE_DESTINATION_NEW,
            Granularity=WD_GRANULARITY_WORD, CompareFormatting=True, CompareCaseChanges=True, CompareWhitespace=True,
            CompareTables=True, CompareHeaders=True, CompareFootnotes=True, CompareTextboxes=True, CompareFields=True,
            CompareComments=True, CompareMoves=False, RevisedAuthor=author, IgnoreAllComparisonWarnings=True))
        try:
            made.SaveAs2(FileName=out, FileFormat=WD_FORMAT_DOCUMENT_DEFAULT, AddToRecentFiles=False)
            result = _counts(made)
        finally:
            made.Close(SaveChanges=0)
    finally:
        old.Close(SaveChanges=0)
        new.Close(SaveChanges=0)
    return result


def job_pdf(app, path, out):
    doc = _open(app, path)
    try:
        doc.ExportAsFixedFormat(OutputFileName=out, ExportFormat=WD_EXPORT_PDF, OpenAfterExport=False,
                                Item=WD_EXPORT_WITH_MARKUP)
        return {"pages": int(doc.ComputeStatistics(WD_STATISTIC_PAGES))}
    finally:
        doc.Close(SaveChanges=0)


def _own_pid(app, before):
    """The WINWORD.EXE this child started: its window (found by a caption only it has), else the one new process."""
    try:
        import win32gui
        import win32process
        caption = f"glassbox-{os.getpid()}-{time.time_ns()}"
        app.Caption = caption
        hwnd = win32gui.FindWindow("OpusApp", caption)
        if hwnd:
            return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        pass
    new = word_pids() - before
    return next(iter(new)) if len(new) == 1 else None


def child_main(job, args, pid_file):
    import pythoncom
    import pywintypes
    import win32com.client
    pythoncom.CoInitialize()
    app = None
    try:
        before = word_pids()
        app = win32com.client.DispatchEx("Word.Application")  # a NEW Word, never the user's
        pid = _own_pid(app, before)
        if pid:
            Path(pid_file).write_text(str(pid))
        app.Visible = False
        app.DisplayAlerts = WD_ALERTS_NONE
        try:
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable: no macros in files opened from code
        except pywintypes.com_error:
            pass
        result = {"open_check": job_open_check, "compare": job_compare, "pdf": job_pdf}[job](app, **args)
        print(json.dumps({"ok": True, **result}))
    except Exception as exc:
        why = str(exc)
        if isinstance(exc, pywintypes.com_error):
            details = exc.args[2] if len(exc.args) > 2 and exc.args[2] else None
            why = (details[2] if details and details[2] else exc.args[1]) or why
            if exc.args[0] in BUSY:
                why = f"Word is busy: {why}"
        print(json.dumps({"ok": False, "why": " ".join(str(why).split())[:400]}))
    finally:
        if app is not None:
            try:
                for doc in list(app.Documents):
                    doc.Close(SaveChanges=0)
                app.Quit(SaveChanges=0)
            except Exception:
                pass
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    child_main(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])
