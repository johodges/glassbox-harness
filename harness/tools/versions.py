"""
The version ledger: every version of a Word document or deck the harness has seen or written, so that "with track
changes from v5", "what did I change since your last version" and "use mine as the latest" mean something exact.

  outputs/versions/<document>.json      the record: the baseline, then each version in order
  outputs/versions/<document>/v3.docx   a copy of each version, so the baseline survives the user saving over it

A version is told apart by its SHA-256, not its name. Each one has an origin:
  baseline  the file the first request started from
  harness   a file a tool wrote (redline, comments, accepted copy, edited deck)
  user      a file named in a request whose hash matches no known version: the user edited something
The harness's second state that lasts from one request to the next, after the LaTeX ledger (latex_audit.py).
"""

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..config import OFFICE, OUTPUT_DIR
from .files import rel

VERSIONS_DIR = OUTPUT_DIR / "versions"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def original_of(path, ctx):
    """The file the user means: for a copy IMPORT FILES made, the file it was copied from; else the path itself."""
    path = Path(path).resolve()
    return Path(ctx.facts.get("imported_from", {}).get(str(path), path))


def document_key(path, ctx):
    """One ledger per document, named after the file the user knows ('workspace/review_demo/test_plan.docx').
    Versions the harness writes (test_plan_redline.docx) belong to their source's ledger, through record(). A long
    path keeps its last 60 characters plus a hash of the whole, so the ledger's own path stays under Windows' 260."""
    key = re.sub(r"[^A-Za-z0-9]+", "_", rel(original_of(path, ctx))).strip("_")
    if len(key) > 80:
        key = key[-60:].strip("_") + "_" + hashlib.sha256(key.encode()).hexdigest()[:10]
    return key


class Versions:
    def __init__(self, key, folder=None):
        self.folder = Path(folder or VERSIONS_DIR)
        self.key, self.path = key, self.folder / f"{key}.json"
        self.data = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file()
                     else {"document": key, "versions": []})

    @classmethod
    def of(cls, path, ctx):
        """The ledger of the document a tool is reading (a source, or a version the harness wrote earlier)."""
        digest, here = sha256(path), str(Path(path).resolve())
        folder = Path(ctx.facts.get("versions_dir") or VERSIONS_DIR)
        ledgers = [json.loads(f.read_text(encoding="utf-8")) for f in folder.glob("*.json")] if folder.is_dir() else []
        for data in ledgers:  # the same bytes as a version: a file the harness wrote, or one it has seen
            if any(v["sha256"] == digest for v in data.get("versions", [])):
                return cls(data["document"], folder)
        for data in ledgers:  # a file the harness wrote, which the user then saved over in place: its ledger still
            if any(v.get("origin") == "harness" and str(Path(v["file"]).resolve()) == here
                   for v in data.get("versions", [])):
                return cls(data["document"], folder)
        return cls(document_key(path, ctx), folder)

    @property
    def versions(self):
        return self.data["versions"]

    def find(self, digest):
        return next((v for v in self.versions if v["sha256"] == digest), None)

    def latest(self, origin=None):
        return next((v for v in reversed(self.versions) if origin is None or v["origin"] == origin), None)

    def baseline(self):
        return self.versions[0] if self.versions else None

    def _add(self, path, origin, ctx, tool="", changes=None, note=""):
        path = Path(path)
        number = len(self.versions)
        stored = self.folder / self.key / f"v{number}{path.suffix}"
        if path.stat().st_size / 1e6 <= OFFICE["max_version_mb"]:
            stored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, stored)
        else:
            stored = None
        entry = {"n": number, "sha256": sha256(path), "origin": origin, "file": str(path),
                 "stored": str(stored) if stored else None, "tool": tool, "request": getattr(ctx, "request", ""),
                 "run_dir": str(ctx.run_dir), "time": datetime.now().isoformat(timespec="seconds"),
                 "changes": changes or [], "note": note}
        self.versions.append(entry)
        self.save()
        return entry

    def save(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def stored_file(self, entry):
        """A version's own copy (the baseline even after the user saved over the original), or its path."""
        if entry.get("stored") and Path(entry["stored"]).is_file():
            return Path(entry["stored"])
        return Path(entry["file"])


def track(path, ctx, tracer_stage=True):
    """
    VERSION CHECK: where does this file stand? -> (the ledger, its version entry, what happened):
      'baseline'   first seen: recorded as v0
      'known'      the same bytes as a version already recorded
      'user'       new bytes: the user edited it since; recorded, and 'since' is the harness version before it
    """
    ledger = Versions.of(path, ctx)
    digest = sha256(path)

    def check():
        known = ledger.find(digest)
        if known:
            return known, "known"
        if not ledger.versions:
            return ledger._add(path, "baseline", ctx, note="the first version the harness saw"), "baseline"
        since = ledger.latest("harness") or ledger.latest()
        return ledger._add(path, "user", ctx, note=f"edited since v{since['n']}"), "user"

    if not tracer_stage:
        entry, what = check()
        return ledger, entry, what
    with ctx.tracer.stage("VERSION CHECK", "python", "SHA-256 + outputs/versions/ (no ML)", rel(path),
                          "baseline, known, or edited by you") as ev:
        entry, what = check()
        ev.summary = {"baseline": f"first time seen: recorded as v0 of {ledger.key}",
                      "known": f"v{entry['n']} ({entry['origin']}) of {ledger.key}",
                      "user": f"new bytes: recorded as v{entry['n']}, edited by you since the harness's last "
                              f"version"}[what]
    return ledger, entry, what


def record(ledger, path, ctx, tool, changes=None):
    """A file a tool wrote becomes the ledger's newest harness version."""
    return ledger._add(path, "harness", ctx, tool=tool, changes=changes)


def unchanged_since_read(path, digest):
    """Before writing a result from a file: is the file still the bytes the tool read? (OneDrive sync, the user
    saving in Word.) The result goes to a new file either way; this only stops a result built on stale input."""
    return sha256(path) == digest
