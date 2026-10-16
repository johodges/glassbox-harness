"""
Classifiers: small, fast, single-purpose models that look at the user's text BEFORE the LLM does.

Neither model can write anything. Each one reads text and outputs scores for a fixed set of
labels. That makes them cheap (they run fine on a CPU) and predictable, which is why real
systems use them as gatekeepers and routers in front of the expensive LLM.
"""

import re

import torch
from transformers import pipeline

from .config import MODELS

# "calculate", "compute", "computing", ...: numbers come from code, not from the LLM (see calculates_from_a_file)
CALCULATE = re.compile(r"\b(?:calculat|comput)\w*", re.I)


class InputGuard:
    """Prompt-injection detector (a fine-tuned DeBERTa). Text -> SAFE or INJECTION + a score."""

    def __init__(self, tracer, device="cpu", threshold=0.9):
        self.tracer, self.threshold = tracer, threshold
        self.model_id = MODELS["input_guard"]
        with tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", f"model on {device}") as ev:
            self.clf = pipeline("text-classification", model=self.model_id, device=device, dtype=_dtype(device))
            ev.summary = f"labels: {list(self.clf.model.config.id2label.values())}"

    def check(self, text):
        self.clf.model.to(self.clf.device)  # no-op unless the harness parked it in RAM
        with self.tracer.stage("INPUT GUARD", "classifier", self.model_id, "text", "label + score") as ev:
            result = self.clf(text, truncation=True, max_length=512)[0]  # e.g. {'label': 'SAFE', 'score': 0.99}
            blocked = result["label"] == "INJECTION" and result["score"] >= self.threshold
            ev.summary = f"{result['label']} (score {result['score']:.3f}) -> {'BLOCK' if blocked else 'allow'}"
            ev.details = {"text": text, **result, "threshold": self.threshold, "blocked": blocked}
        return {**result, "blocked": blocked}

    def scores(self, texts):
        """The same model on many texts at once, with no stage of its own (the TOOL OUTPUT GUARD in agent.py
        uses it on what tools bring back). -> [{'label': ..., 'score': ..., 'blocked': ...}, ...]"""
        self.clf.model.to(self.clf.device)
        results = self.clf(list(texts), truncation=True, max_length=512)
        return [{**r, "blocked": r["label"] == "INJECTION" and r["score"] >= self.threshold} for r in results]


class IntentRouter:
    """
    Zero-shot classifier that decides which tools the LLM gets to see.

    It is a Natural Language Inference model: for every label it asks "does the user's text
    imply the sentence 'This request asks for <label>.'?" and returns a 0..1 score. No training
    on our tools was needed: we just describe each tool in plain English below. A tool can have
    several descriptions; its score is the best of them.
    """

    LABELS = {
        "generate_image": ["a picture, image, drawing, painting or photo"],
        "generate_speech": ["spoken words, narration or a voice reading text aloud"],
        "generate_music": ["music, a song, a melody or a soundtrack"],
        "generate_video": ["a video, an animation or a moving clip"],
        "generate_3d_model": ["a three-dimensional model for 3D software or 3D printing"],
        "combine_video_and_audio": ["putting sound onto a video",
                                    "a video and its audio synchronized or of the same length"],
        "read_file": ["reading, checking or summarizing an existing file or document"],
        "search_references": ["information from the reference documents or notes", "facts or sources to cite"],
        "update_powerpoint": ["changing or adding slides in a PowerPoint presentation"],
        "update_word_document": ["editing a section of a Word document"],
        "update_latex": ["editing a LaTeX document"],
        "edit_code": ["changing or fixing code in a script"],
        "run_script": ["running a script or program"],
        "convert_presentation": ["moving a presentation into another template or design"],
        "export_pdf": ["saving or exporting a document as a PDF file"],
        "look_at_image": ["looking at an existing picture to answer a question about what it shows",
                          "checking that a picture shows what was asked for"],
        "digitize_plot": ["turning the curves of a plot back into numbers"],  # rule-only, see RULE_ONLY
        "fire_calculation": ["a fire engineering calculation such as a flame height"],  # rule-only
        "query_database": ["looking up values in a database"],  # rule-only
        "ask_subagent": ["handing a reading job to a helper agent"],  # rule-only
        "update_excel": ["changing cells or formulas in an Excel spreadsheet"],  # rule-only
        # code (tools/coding.py): all rule-only, see CODE_RULES
        "run_tests": ["running the tests of a program"],
        "check_code": ["checking code with a linter"],
        "search_code": ["finding where something is used in the code"],
        "write_code": ["writing a new function or test in code"],
        "create_file": ["creating a new file"],
        "review_code": ["a review of code"],
    }
    TEMPLATE = "This request asks for {}."

    # Plain-Python rules that run BEFORE the classifier, for the document tools. A regex is cheap, exact and
    # explainable when the signal is literal, like a file extension. The classifier handles the rest.
    DOCUMENT_TOOLS = {"read_file", "search_references", "update_powerpoint", "update_word_document",
                      "update_latex", "edit_code", "run_script", "convert_presentation", "export_pdf", "digitize_plot",
                      "fire_calculation", "query_database", "ask_subagent", "update_excel",
                      "run_tests", "check_code", "search_code", "write_code", "create_file", "review_code"}
    MENTIONS_FILES = re.compile(r"\.(pptx|potx|docx|xlsx|tex|py|pdf|md|txt|png|jpe?g)\b|\b(files?|documents?|slides?|deck|presentation|"
                                r"powerpoint|word document|report|references?|scripts?|code|latex|workspace|templates?|"
                                r"pdf)\b", re.I)
    FILE_RULES = [(re.compile(r"\.pptx\b|\bpowerpoint\b", re.I), ["update_powerpoint"]),
                  (re.compile(r"\.docx\b|\bword document\b", re.I), ["update_word_document"]),
                  (re.compile(r"\.tex\b|\blatex\b", re.I), ["update_latex"]),
                  (re.compile(r"\.py\b|\bscripts?\b", re.I), ["run_script", "edit_code"]),
                  (re.compile(r"\.(?:md|txt)\b", re.I), ["edit_code"]),
                  (re.compile(r"\breferences?\b|\bsources?\b", re.I), ["search_references"]),
                  (re.compile(r"\.potx\b|\btemplates?\b|\bconvert", re.I), ["convert_presentation"]),
                  # "to/as/into PDF", not just "pdf": the references are PDFs, and "summarize the PDF" exports nothing
                  (re.compile(r"\b(?:to|as|into)\s+(?:an?\s+)?pdf\b|\bexport", re.I), ["export_pdf"]),
                  # look_at_image is NOT a document tool: the classifier may also offer it to check a new picture
                  (re.compile(r"\.(?:png|jpe?g|webp)\b", re.I), ["look_at_image"]),
                  # "digitize", or the data / curves FROM a plot. Not "values ... chart": reading the numbers printed
                  # on a bar chart is look_at_image's job (and digitize_plot only follows lines)
                  (re.compile(r"\bdigiti[sz]|\b(?:data|curves?|data points)\b[^.?!]{0,40}\b(?:from|in|of|on)\b"
                              r"[^.?!]{0,30}\b(?:plot|graph|figure)s?\b|\bto (?:a )?csv\b", re.I), ["digitize_plot"]),
                  # fire-engineering words bring the calculator (such a request names no file)
                  (re.compile(r"\bflame heights?\b|\bhow (?:tall|high) is the flame|\bsprinklers?\b|"
                              r"\b(?:heat|smoke) detectors?\b|\bceiling[- ]jets?\b|\bt[- ]?(?:squared|2|²) fires?\b|"
                              r"\bRTI\b", re.I), ["fire_calculation"]),
                  (re.compile(r"\bdatabase\b|\bsql\b|\bsqlite\b", re.I), ["query_database"]),
                  (re.compile(r"\bsub-?agents?\b|\bhelper agents?\b", re.I), ["ask_subagent"]),
                  # a spreadsheet brings read_file too: update_excel requires a read first, to learn the layout
                  (re.compile(r"\.xlsx\b|\bexcel\b|\bspreadsheets?\b|\bworkbooks?\b", re.I), ["update_excel", "read_file"])]
    # Code (tools/coding.py). "the tests", "a failing test", "pytest", but not "the fire test database".
    CODE_RULES = [(re.compile(r"\bpytest\b|\b(?:the|failing|unit|a|its|new) tests?\b(?!\s+(?:database|data)\b)", re.I),
                   ["run_tests", "read_file", "edit_code", "write_code"]),
                  (re.compile(r"\blint(?:er|ing)?\b|\bruff\b|\bcheck (?:the )?code\b|\bstatic (?:check|analysis)", re.I),
                   ["check_code"]),
                  (re.compile(r"\breview\b", re.I), ["review_code", "read_file", "check_code"]),
                  (re.compile(r"\bsearch (?:the )?code\b|\bwhere (?:is|are)\b[^.?!]{0,40}\b(?:used|defined|called)\b|"
                              r"\busages? of\b|\bwho calls\b|\bgrep\b", re.I), ["search_code"]),
                  (re.compile(r"\b(?:add|write)\s+(?:a |an )?(?:new )?(?:function|method|class|test)\b", re.I),
                   ["write_code", "read_file", "run_tests"]),
                  (re.compile(r"\b(?:create|new)\s+(?:a |an )?(?:new )?(?:file|module|test file)\b", re.I),
                   ["create_file"])]
    # Tools the classifier never scores: only a rule can offer them. It offered search_references for "What are
    # the values in this chart?", and edit_code for a .tex request, which Llama then used to overwrite \section{Results}.
    RULE_ONLY = {"search_references", "edit_code", "run_script", "digitize_plot", "fire_calculation", "query_database",
                 "ask_subagent", "update_excel", "run_tests", "check_code", "search_code", "write_code", "create_file",
                 "review_code"}

    def __init__(self, tracer, device="cpu", threshold=0.5):
        self.tracer, self.threshold = tracer, threshold
        self.model_id = MODELS["router"]
        with tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", f"model on {device}") as ev:
            self.clf = pipeline("zero-shot-classification", model=self.model_id, device=device, dtype=_dtype(device))
            ev.summary = f"an NLI model: labels {list(self.clf.model.config.id2label.values())}"

    def route(self, text, available):
        """Return (tool names to offer the LLM, {tool: score})."""
        self.clf.model.to(self.clf.device)  # no-op unless the harness parked it in RAM
        with self.tracer.stage("ROUTER", "classifier", self.model_id, "text", "tool names") as ev:
            # ---- plain-Python rules first: which document tools does the text name outright? ----
            ruled = {tool: pattern.search(text).group(0) for pattern, tools in self.FILE_RULES + self.CODE_RULES
                     if pattern.search(text) for tool in tools if tool in available}
            if "run_script" in available and calculates_from_a_file(text):  # its description lists the scripts
                ruled["run_script"] = CALCULATE.search(text).group(0)
            if "update_excel" in ruled:  # "...of a fast t-squared fire" in a spreadsheet: Excel does those sums
                ruled.pop("fire_calculation", None)
            if {"run_tests", "check_code", "review_code"} & set(ruled):  # a project's code is checked by its tests and
                ruled.pop("run_script", None)                             # the linter; run_script is for workspace/scripts
            if "review_code" in ruled:  # a review reports; it changes nothing (".py" brought edit_code)
                ruled.pop("edit_code", None)
            documents = [name for name in available if name in self.DOCUMENT_TOOLS]
            if ruled:  # the request names a file: it is a document request, so media tools are not scored
                candidates = documents
                self.tracer.say("rule: " + ", ".join(f"'{word}' -> {tool}" for tool, word in ruled.items())
                                + "  (a named file type always brings its tool; media tools are not scored)",
                                kind="python")
            elif documents and not self.MENTIONS_FILES.search(text):
                candidates = [name for name in available if name not in self.DOCUMENT_TOOLS]
                self.tracer.say("rule: no file, document or script mentioned -> the document tools are not scored",
                                kind="python")
            else:
                candidates = list(available)
            candidates = [name for name in candidates if name not in self.RULE_ONLY]  # see RULE_ONLY above
            # The classifier tends to miss the second half of "draw a robot AND compose a tune", so we
            # also score each clause on its own (plain string splitting) and keep each tool's best score.
            clauses = [c.strip() for c in re.split(r",|;|\band\b|\bthen\b", text) if len(c.strip().split()) >= 2]
            pieces = list(dict.fromkeys([text] + clauses))
            labels = [label for name in candidates for label in self.LABELS[name]]
            best = {name: (0.0, self.LABELS[name][0]) for name in available}  # tool -> (score, best label)
            for piece in (pieces if labels else []):
                out = self.clf(piece, candidate_labels=labels, hypothesis_template=self.TEMPLATE, multi_label=True)
                by_label = dict(zip(out["labels"], out["scores"]))
                for name in candidates:
                    for label in self.LABELS[name]:
                        if by_label[label] > best[name][0]:
                            best[name] = (by_label[label], label)
            for tool, word in ruled.items():
                best[tool] = (1.0, f"(rule: the request says '{word}')")
            scores = {name: score for name, (score, _) in best.items()}
            if len(pieces) > 1:
                self.tracer.say(f"scored {len(pieces)} pieces: the whole request + clauses {pieces[1:]}",
                                kind="classifier")
            chosen = [name for name, s in scores.items() if s >= self.threshold]
            for name, s in sorted(scores.items(), key=lambda kv: -kv[1]):
                mark = "offer" if s >= self.threshold else "hide "
                reason = (best[name][1] if name in ruled else "(not scored)" if name not in candidates
                          else f"'{self.TEMPLATE.format(best[name][1])}'")
                self.tracer.say(f"{mark}  {s:5.3f}  {name:<23}  {reason}", kind="classifier")
            ev.summary = (f"offer {len(chosen)} tool(s) to the LLM: {', '.join(chosen)}" if chosen
                          else "no tool scored high enough -> plain chat, the LLM sees no tools")
            ev.details = {"pieces": pieces, "scores": scores, "threshold": self.threshold, "chosen": chosen,
                          "rules": ruled}
        return chosen, scores


def calculates_from_a_file(text):
    """'Calculate ...' or 'compute ...' in a request that names a file: those numbers must come from a script
    (run_script, offered by a ROUTER rule), never from the LLM. agent.py then also notes that a script must run."""
    return bool(CALCULATE.search(text) and IntentRouter.MENTIONS_FILES.search(text))


def _dtype(device):
    return torch.float16 if device == "cuda" else torch.float32
