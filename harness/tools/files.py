"""
The file tools, and the guardrail that every document tool goes through.

The LLM writes a file path as text. Before any tool touches a file, plain Python:
  1. resolves whatever the LLM wrote ("workspace/report.docx", "report.docx", a full path, ...),
  2. refuses anything outside workspace/ or this run's output folder,
  3. never edits an original: the first edit makes a WORKING COPY in outputs/run_.../, and every
     later edit, read or run of that file in the same request uses the copy.
The harness, not the LLM, keeps track of which copy belongs to which file (ctx.copies).

One way through the fence: a file the USER names in the request ("convert ..\\deck.pptx ..."). Typing its
path is the permission, so before the LLM runs, import_named_files() copies it into the run folder
(outputs/run_.../inputs/) and the tools accept either name for it. The LLM still can't reach any other file.
"""

import re
import shutil
from pathlib import Path

import torch

from ..config import MODELS, PROJECT_DIR, WORKSPACE_DIR
from .base import Tool
from .documents import READABLE, as_text, split_passages

REFERENCES_DIR = WORKSPACE_DIR / "references"
MAX_CHARS = 3000  # how much of a file read_file puts into the prompt (the context window is not free)


def rel(path):
    """A short path for the LLM and the trace: relative to the project folder when possible."""
    try:
        return Path(path).resolve().relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return str(path)


def workspace_files():
    return [rel(p) for p in sorted(WORKSPACE_DIR.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]


IMPORTABLE = READABLE | {".potx", ".png", ".jpg", ".jpeg", ".webp"}
WORDS = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')  # a quoted path (may contain spaces), or one word


def _key(text):
    """How a path is looked up: the LLM may write ..\\deck.pptx, ../deck.pptx or DECK.pptx for the same file."""
    return str(text).strip().strip("\"'").replace("\\", "/").lower()


def named_paths(prompt):
    """Words in the request that look like a file the tools could use (by extension), a .zip, or a folder (a path
    with a slash and no extension, e.g. ..\\papers\\fire_note). Plain string handling."""
    words = (next(g for g in m.groups() if g).rstrip(".,;:!?)") for m in WORDS.finditer(prompt))
    return [w for w in words if Path(w).suffix.lower() in IMPORTABLE | {".zip"}
            or (("/" in w or "\\" in w) and not Path(w).suffix)]


# A folder or .zip comes in without these: build files (a stale .aux misleads the audit), macOS and git litter
SKIP_ON_IMPORT = ("__MACOSX", ".git", ".DS_Store", "*.aux", "*.log", "*.out", "*.toc", "*.fls", "*.fdb_latexmk",
                  "*.synctex.gz", "*.bbl", "*.blg", "*.bcf", "*.run.xml", "*.xdv", "__pycache__")


def import_folder(hit, dest):
    """A folder the user named -> a copy in the run folder (size capped), or a reason it wasn't copied."""
    from ..config import LATEX
    size = sum(f.stat().st_size for f in hit.rglob("*") if f.is_file()) / 1e6
    if size > LATEX["max_project_mb"]:
        return f"not copied: {size:.0f} MB, over the {LATEX['max_project_mb']} MB limit (config LATEX)"
    shutil.copytree(hit, dest, ignore=shutil.ignore_patterns(*SKIP_ON_IMPORT), dirs_exist_ok=True)
    return None


def import_zip(hit, dest):
    """A .zip the user named -> unpacked in the run folder (size capped). A member whose name points outside the
    folder ('../../x') is skipped, so an archive can never write anywhere else."""
    import fnmatch
    import zipfile
    from ..config import LATEX
    with zipfile.ZipFile(hit) as z:
        members = [m for m in z.infolist() if not m.is_dir()
                   and not any(fnmatch.fnmatch(part, pattern) for part in Path(m.filename).parts
                               for pattern in SKIP_ON_IMPORT)]
        size = sum(m.file_size for m in members) / 1e6
        if size > LATEX["max_project_mb"]:
            return f"not unpacked: {size:.0f} MB, over the {LATEX['max_project_mb']} MB limit (config LATEX)"
        for m in members:
            target = (dest / m.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return None


def import_named_files(prompt, ctx, cwd=None):
    """
    Copy each file the user named into this run's folder, so it is inside the fence. A relative path is read
    from the folder the harness was started in (as a shell would); a path that doesn't exist (a typo such as
    "...\\template.pptx") is looked up by its file name in that folder and its parent. Files already inside
    workspace/ need nothing. A folder is copied, and a .zip unpacked, into outputs/run_.../inputs/<name>/.
    -> [(the name as written, the copy or None, how it was found)]
    """
    cwd = Path(cwd or Path.cwd()).resolve()
    imported = []
    for raw in named_paths(prompt):
        p = Path(raw)
        folder = not p.suffix
        exists = Path.is_dir if folder else Path.is_file
        hit = next((c.resolve() for c in ([p] if p.is_absolute() else [cwd / p, PROJECT_DIR / p]) if exists(c)), None)
        how = "found where the request said"
        if hit is None and not folder:
            hit = next(((d / p.name).resolve() for d in (cwd, cwd.parent, PROJECT_DIR.parent) if (d / p.name).is_file()),
                       None)
            how = "found by its file name"
        if hit is None:
            if not folder:  # a word with a slash that is no folder ("kW/s") is not worth a line
                imported.append((raw, None, "not found"))
            continue
        if hit.is_relative_to(WORKSPACE_DIR) or hit.is_relative_to(ctx.run_dir.resolve()):
            continue  # already inside the fence
        if folder or hit.suffix.lower() == ".zip":
            copy = ctx.run_dir / "inputs" / (hit.name if folder else hit.stem)
            problem = import_folder(hit, copy) if folder else import_zip(hit, copy)
            if problem:
                imported.append((raw, None, problem))
                continue
            how += ", copied" if folder else ", unpacked"
        else:
            copy = ctx.run_dir / "inputs" / hit.name
            copy.parent.mkdir(exist_ok=True)
            shutil.copy2(hit, copy)
        for name in (raw, str(hit), hit.name):
            ctx.imports[_key(name)] = copy.resolve()
        ctx.facts.setdefault("imported_from", {})[str(copy.resolve())] = str(hit)  # versions.py names it by this
        imported.append((raw, copy, f"{how}: {hit}"))
    return imported


CONTROL_TO_ESCAPE = {ord("\n"): "\\n", ord("\t"): "\\t", ord("\f"): "\\f", ord("\b"): "\\b", ord("\r"): "\\r"}


def resolve(path, ctx, allow_dir=False):
    """Text written by the LLM -> a real path inside workspace/ or this run's folder. Plain Python."""
    # JSON reads the \n in "C:\new\deck.pptx" as a line break: turn such characters back into the path's text
    raw = str(path).translate(CONTROL_TO_ESCAPE).strip().strip("\"'")
    p = Path(raw)
    imported = ctx.imports.get(_key(raw)) or ctx.imports.get(_key(p.name))
    if imported is not None:  # a file the user named in the request: use the copy made in this run's folder
        return imported
    candidates = [p] if p.is_absolute() else [PROJECT_DIR / raw, WORKSPACE_DIR / raw, ctx.run_dir / p.name]
    candidates += sorted(WORKSPACE_DIR.rglob(p.name))  # just a file name: look for it anywhere in workspace/
    for candidate in candidates:
        candidate = candidate.resolve()
        if not (candidate.is_file() or (allow_dir and candidate.is_dir())):
            continue
        if not (candidate.is_relative_to(WORKSPACE_DIR) or candidate.is_relative_to(ctx.run_dir.resolve())):
            raise PermissionError(f"{raw} is outside the workspace. The document tools may only use files in "
                                  "workspace/ (and the copies they make in this run's folder).")
        return candidate
    raise FileNotFoundError(f"There is no file called {raw}. Files you can use: {', '.join(workspace_files())}")


def current_version(path, ctx):
    """The edited working copy if this request already changed the file, else the file itself."""
    return ctx.copies.get(Path(path), Path(path))


def original(path, ctx):
    """The workspace file behind a working copy (the reverse of current_version); any other path is returned as is."""
    path = Path(path)
    return next((k for k, v in ctx.copies.items() if Path(v) == path), path)


def working_copy(path, ctx, name=None):
    """
    Where an edit goes. The first edit of a workspace file copies it into the run folder; the
    original is never changed. A human can compare the two and copy the result back.
    name: a tool that writes a NEW file from this one (convert_presentation) gets that path instead,
    with nothing copied; from then on it is this file's working copy, so later edits, reads and
    exports in the same request use it.
    """
    path = Path(path)
    if name:
        original = next((k for k, v in ctx.copies.items() if v == path), path)  # the workspace file behind a copy
        ctx.copies[original] = ctx.run_dir / name
        ctx.tracer.say(f"working copy: {rel(original)} -> {rel(ctx.copies[original])} (a new file; the original "
                       "is not changed)", kind="python")
        return ctx.copies[original]
    if path.is_relative_to(ctx.run_dir.resolve()):
        return path  # already a copy made in this run
    if path not in ctx.copies:
        target = ctx.run_dir / path.name
        if target.exists():  # two workspace files with the same name
            target = ctx.run_dir / f"{path.parent.name}_{path.name}"
        shutil.copy2(path, target)
        ctx.copies[path] = target
        ctx.tracer.say(f"working copy: {rel(path)} -> {rel(target)} (the original is not changed)", kind="python")
    return ctx.copies[path]


def check_written_text(texts, ctx):
    """
    Writing tools need the real words. A 3B model sometimes sends a pointer instead, such as
    'passages from search_references(question=...)' or 'To be written'. Catch that in plain Python.
    """
    for text in texts:
        named = [name for name in ctx.tools if name in text]
        if named or len(text.split()) < 3 or text.strip().lower().rstrip(".") in ("to be written", "tbd", "todo"):
            raise ValueError(f"The text \"{text[:80]}\" is not the real content. Write the actual sentences, using the "
                             "facts from the passages you found, without tool names.")


# =========================================================================================
class ReadFileTool(Tool):
    name = "read_file"
    description = ("Read a file in the workspace and return its text: the slides of a .pptx, the headings and "
                   "paragraphs of a .docx, the text of a .pdf, or the numbered lines of a .tex, .py, .md or .txt "
                   "file. Give a folder to list the files in it. Use it when you need to know what a file says.")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "The file or folder, e.g. workspace/report.docx"}},
        "required": ["path"],
    }
    model_id = "python-pptx, python-docx, pypdf (no ML)"
    check_unused, repeatable = False, True  # a file can change between two reads
    max_chars = MAX_CHARS  # a sub-agent's own reader may show more (see subagent.py)
    how_it_works = ("No model here: a parser for each file type turns the file into plain text, and that text goes "
                    "into the LLM's prompt. Documents are already words, so 'perception' is cheap and exact.")

    def run(self, ctx, path):
        with ctx.tracer.stage("TOOL: read_file", "python", self.model_id, "file", "text") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            target = current_version(resolve(path, ctx, allow_dir=True), ctx)
            if target.is_dir():
                files = [rel(p) for p in sorted(target.rglob("*")) if p.is_file() and p.suffix.lower() in READABLE]
                ev.summary = f"{rel(target)}: {len(files)} readable files"
                return {"artifacts": [], "observations": {"folder": rel(target), "files": files}}
            text = as_text(target, numbered=True)
            limit = self.max_chars
            shown = text if len(text) <= limit else (text[:limit] + f"\n... [{len(text) - limit} more characters not shown]")
            ev.summary = f"{rel(target)} -> {len(text):,} characters of text ({len(shown):,} put into the prompt)"
            ev.details = {"text_for_the_llm": shown}
        return {"artifacts": [], "observations": {"file": rel(target), "content": shown}}


# =========================================================================================
class SearchReferencesTool(Tool):
    name = "search_references"
    description = ("Search the reference documents in workspace/references (notes, text files and PDFs) for the "
                   "passages that best answer a question. Returns the passages with their file names, so you can "
                   "use and cite them. Call it before writing anything that should be based on the references.")
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What you need to know, as a question or a few keywords."},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5,
                      "description": "How many passages to return (default 3)."},
        },
        "required": ["question"],
    }
    model_id = MODELS["embedder"]
    vram_gb = 0.3
    check_unused = False
    how_it_works = ("An EMBEDDING model (MiniLM, 23 million parameters) turns each passage, and the question, into "
                    "384 numbers that capture its meaning. Passages whose numbers point the same way as the "
                    "question's (cosine similarity, plain numpy) are returned. The model never writes text: it only "
                    "measures how close two pieces of text are in meaning.")

    def __init__(self):
        self.tokenizer = self.model = None
        self.cache = {}  # passage text -> vector, so the references are embedded once

    def load(self, ctx):
        from transformers import AutoModel, AutoTokenizer
        with ctx.tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", "model on GPU") as ev:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            self.model = AutoModel.from_pretrained(self.model_id).to(ctx.device).eval()
            ev.summary = f"{sum(p.numel() for p in self.model.parameters()) / 1e6:.0f} million parameters"

    def gpu_modules(self):
        return {"embedding model": self.model} if self.model is not None else {}

    def embed(self, texts, device):
        """texts -> unit vectors. Tokens -> transformer -> average over the tokens -> length 1."""
        batch = self.tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors="pt").to(device)
        with torch.no_grad():
            hidden = self.model(**batch).last_hidden_state  # (texts, tokens, 384)
        mask = batch["attention_mask"].unsqueeze(-1).float()
        vectors = (hidden * mask).sum(1) / mask.sum(1)        # mean pooling over the real tokens
        return torch.nn.functional.normalize(vectors, dim=1).cpu()

    def run(self, ctx, question, top_k=3):
        if self.model is None:
            self.load(ctx)
        self.model.to(ctx.device)  # no-op unless the harness parked it in RAM
        with ctx.tracer.stage("CHUNK REFERENCES", "python", "pypdf + string splitting (no ML)", "files",
                              "passages") as ev:
            files = [p for p in sorted(REFERENCES_DIR.iterdir()) if p.suffix.lower() in READABLE]
            passages = [(rel(f), chunk) for f in files for chunk in split_passages(as_text(f))]
            # Llama writes lists as JSON inside a JSON string, so a " copied from a passage needs escaping twice,
            # and the 3B model stops mid-call. Prose doesn't need double quotes: give it single ones.
            passages = [(source, text.replace('"', "'")) for source, text in passages]
            ev.summary = f"{len(files)} files in {rel(REFERENCES_DIR)} -> {len(passages)} passages"
        with ctx.tracer.stage("EMBED", "retrieval", self.model_id, f"{len(passages) + 1} pieces of text",
                              "vectors of 384 numbers") as ev:
            ctx.tracer.say(self.how_it_works, kind="retrieval")
            new = [text for _, text in passages if text not in self.cache]
            if new:
                self.cache.update(zip(new, self.embed(new, ctx.device)))
            matrix = torch.stack([self.cache[text] for _, text in passages])
            query = self.embed([question], ctx.device)[0]
            ev.summary = (f"question + {len(passages)} passages -> {len(passages) + 1} x 384 numbers "
                          f"({len(new)} passages embedded now, {len(passages) - len(new)} from the cache)")
        with ctx.tracer.stage("RANK", "python", "cosine similarity (numpy, no ML)", "vectors", "best passages") as ev:
            scores = (matrix @ query).numpy()
            best = scores.argsort()[::-1][:top_k]
            found = [{"source": passages[i][0], "score": round(float(scores[i]), 2), "text": passages[i][1]}
                     for i in best]
            for f in found:
                ctx.tracer.say(f"{f['score']:.2f}  {f['source']}: {f['text'][:90]}...", kind="python")
            ev.summary = ", ".join(f"{f['source'].split('/')[-1]} {f['score']:.2f}" for f in found)
            ev.details = {"question": question, "passages": found}
        return {"artifacts": [], "observations": {"question": question, "passages": found}}
