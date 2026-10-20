"""
Every knob in the harness lives here, so students can see the whole system at a glance.

Each entry in MODELS is a Hugging Face repo id. Swap any of them for another model of the
same *kind* (e.g. a different chat LLM) and the rest of the harness keeps working.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "outputs"
WORKSPACE_DIR = PROJECT_DIR / "workspace"  # the only folder the document tools may read; edits go to copies

MODELS = {
    # ---- The "brain": a chat LLM. Text in, text out. It never touches pixels or sound. ----
    "llm": "unsloth/Llama-3.2-3B-Instruct",  # ungated copy of meta-llama/Llama-3.2-3B-Instruct
    # ... or a code-tuned one, for reviewing and developing code (HarnessConfig.llm = "code_llm", or --llm code_llm).
    # 7.6 billion parameters: 15 GB in bf16, ~5.5 GB in 4 bits (see FOUR_BIT), so it fits beside the other models.
    "code_llm": "Qwen/Qwen2.5-Coder-7B-Instruct",

    # ---- Classifiers: small single-purpose models that run BEFORE the LLM ----
    "input_guard": "protectai/deberta-v3-base-prompt-injection-v2",  # text -> SAFE / INJECTION
    "router": "MoritzLaurer/deberta-v3-base-zeroshot-v2.0",          # text -> which tools are relevant

    # ---- Generators: the tools. Each turns text into a different kind of media ----
    "image": "stabilityai/sdxl-turbo",                 # text -> image   (diffusion U-Net)
    "speech": "hexgrad/Kokoro-82M",                    # text -> speech  (phonemes -> waveform)
    "music": "facebook/musicgen-small",                # text -> music   (a language model over audio tokens)
    "video": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",       # its text encoder, tokenizer and VAE are reused by:
    "video_vace": "Wan-AI/Wan2.1-VACE-1.3B-diffusers", # text (+ earlier frames) -> video (diffusion transformer)
    "trellis_space": "trellis-community/TRELLIS",      # image -> 3D     (remote API on Hugging Face)
    "trellis_local": "http://127.0.0.1:7960",         # image -> 3D     (the same API, served from WSL on this PC)

    # ---- Perception: models that turn the tools' outputs BACK into text for the LLM ----
    "captioner": "Salesforce/blip-image-captioning-large",  # image -> caption
    "image_safety": "Falconsai/nsfw_image_detection",       # image -> normal / nsfw
    "asr": "openai/whisper-base.en",                        # speech -> transcript
    "audio_tagger": "MIT/ast-finetuned-audioset-10-10-0.4593",  # sound -> labels ("Music", "Piano", ...)
    "vlm": "Qwen/Qwen3-VL-2B-Instruct",  # image + question -> answer (look_at_image, and the labels for
                                         # digitize_plot); try Qwen3-VL-4B-Instruct

    # ---- Retrieval: a model that turns text into numbers, so passages can be found by meaning ----
    "embedder": "sentence-transformers/all-MiniLM-L6-v2",   # text -> 384 numbers (search_references)
}

# Precision: how many bits each of the planner's weights is stored in (HarnessConfig.llm_precision, --precision).
# bitsandbytes quantizes while the model loads; compare_quantization.py measures what each one costs.
PRECISIONS = {
    "bf16": "16-bit floats, as trained",
    "int8": "8-bit integers, LLM.int8()",
    "nf4": "4-bit NormalFloat",
}

# Planners loaded in 4 bits (bitsandbytes NF4): each weight is stored in 4 bits instead of 16 and turned back into
# bf16 on the fly, layer by layer. A quarter of the memory, a little slower, slightly less accurate.
FOUR_BIT = {"code_llm"}

# PLANNER (agent.py): when the ROUTER offers one of these tools (the code project's tools, tools/coding.py) AND the
# request asks for code to be changed (CODE_CHANGE), MODELS["code_llm"] plans it instead of MODELS[HarnessConfig.llm].
# The router picks the model as well as the tools, as products that route between a small and a large model do.
# Only changes: on "run the tests and tell me which fails" or "lint this file" the 7B coder edited files nobody asked
# it to touch, while Llama 3B answered right in a seventh of the time. HarnessConfig.route_planner turns it off.
CODE_TOOLS = {"run_tests", "check_code", "search_code", "write_code", "create_file", "review_code"}

# LaTeX projects (tools/latex_project.py, latex_audit.py, latex_fix.py, latexdiff.py): latexmk, BibTeX and latexdiff
# come with TeX Live. timeout: seconds per build; overfull_pt: smaller overfull boxes are not reported; language: the
# spell checker's dictionary; judge: the LLM looks at each "likely" finding once (JUDGE in latex_audit.py)
LATEX = {"timeout": 300, "overfull_pt": 1.0, "max_project_mb": 500, "language": "en", "judge": True}
# Word and PowerPoint review (tools/word_*.py, tools/deck_*.py, OFFICE_COLLAB_DESIGN.md). Every tracked change and
# comment carries REVIEW_AUTHOR: the model that planned the request and the person it worked for
# (HarnessConfig.review_for), never the person's name alone, so their own revisions and the harness's stay apart.
REVIEW_AUTHOR = "{model}, via. Glass-box harness (for {user})"
REVIEW_INITIALS = "GBH"
# timeout: seconds for one job in Word or PowerPoint (a PDF export hung for minutes in 3 past jobs); track_changes_on:
# Word outputs open with track changes on, so the user's next edits are tracked too; compare: "word" uses Word's own
# Compare and falls back to Python where Word is missing or busy, "python" never uses Word; word_check: open every
# Word output in Word and count its revisions and comments
OFFICE = {"timeout": 120, "track_changes_on": True, "compare": "word", "word_check": True, "max_version_mb": 200}
CODE_CHANGE = (r"\b(?:fix(?:es|ing)?|repair|correct|write|add|implement|refactor|rewrite|change|rename|create|update)\b"
               r"|\bmake\b[^.?!]{0,40}\bpass(?:es)?\b")


# Effort: how much work the harness lets the LLM do on one request. More effort means more tokens and more
# LLM calls: checks before finishing, several attempts at an answer, thinking before acting, a second look at
# the answer, and a bigger budget. Claude's effort setting and OpenAI's reasoning effort trade the same things:
# time and tokens for care. HarnessConfig.effort picks a row; max_steps and max_new_tokens, if set, override it.
# The levels only ever add cost; compare_effort.py measures which of the extras pay off (for Llama 3.2 3B:
# the checks and the vote do; thinking and the self-review don't).
EFFORT = {
    #  checks: completion checks before the LLM may finish
    #  vote:   answers to a question without tools: n attempts, the majority number wins (VOTE)
    #  think_tokens: plain-text reasoning before the first action (THINK); review: a second look (REVIEW)
    "low":    {"checks": False, "vote": 1, "think_tokens": 0,   "review": False, "max_steps": 4,  "max_new_tokens": 250},
    "medium": {"checks": True,  "vote": 1, "think_tokens": 0,   "review": False, "max_steps": 8,  "max_new_tokens": 400},
    "high":   {"checks": True,  "vote": 5, "think_tokens": 0,   "review": False, "max_steps": 10, "max_new_tokens": 500},
    "max":    {"checks": True,  "vote": 5, "think_tokens": 400, "review": True,  "max_steps": 12, "max_new_tokens": 600},
}


@dataclass
class HarnessConfig:
    # How much the tracer prints: 0 = only the answer, 1 = one panel per stage, 2 = everything
    # (full raw prompts, token ids, ...).
    verbosity: int = 1
    llm: str = "llm"              # which MODELS entry plans: "llm" (Llama 3.2 3B) or "code_llm" (Qwen2.5-Coder 7B)
    llm_precision: str = "bf16"   # the planner's weights: "bf16", "int8" or "nf4" (PRECISIONS); FOUR_BIT models: nf4
    # PLANNER: code requests (the ROUTER offered a CODE_TOOLS tool) are planned by MODELS["code_llm"], in 4 bits,
    # when there is an NVIDIA GPU and the model was downloaded; False = MODELS[llm] plans everything
    route_planner: bool = True
    use_guard: bool = True        # run the prompt-injection classifier on the user's prompt
    guard_tool_outputs: bool = True  # ... and on the text tools bring back (search passages, files read)
    ask_before_acting: bool = True   # a person answers y/n before run_script runs code (False = yes to everything)
    review_for: str = None        # the person in REVIEW_AUTHOR; None = GLASSBOX_USER, else git's user.name, else the login
    use_router: bool = True       # let a classifier choose which tools the LLM is shown
    use_agents_md: bool = True    # paste workspace/AGENTS.md into every system prompt (see instructions.py)
    use_skills: bool = True       # paste in a workspace/skills/*/SKILL.md when the request matches its description
    skill_threshold: float = 0.45  # how close in meaning (cosine, 0..1) a request and a description must be
    effort: str = "medium"        # "low", "medium", "high" or "max": a row of EFFORT above
    max_steps: int = None         # max LLM calls per request (stops runaway tool loops); None = the effort's
    max_new_tokens: int = None    # max tokens the LLM may write per call; None = the effort's
    temperature: float = 0.0      # 0 = greedy decoding: same prompt -> same answer (good for teaching)
    seed: int = 1234              # seed for the generators, so demos are repeatable
    guard_threshold: float = 0.90  # INJECTION score above this blocks the request
    router_threshold: float = 0.50  # a tool is offered to the LLM if its intent score is above this
    small_models_on_cpu: bool = False  # True = classifiers/perception on CPU (slower, saves ~2 GB of GPU)
    import_named_files: bool = True  # copy files the user names outside workspace/ into the run folder (see files.py)
    # 3D tool: "local" = trellis_local/ server in WSL (Windows only, started automatically), "remote" = the Hugging
    # Face Space (needs an HF_TOKEN); the default is local on Windows, remote on Linux and Mac
    trellis_backend: str = "local" if os.name == "nt" else "remote"
    check_types: bool = False     # check_code also runs mypy, a type checker (a few seconds more per check)
    # The date the chat template writes into the prompt, e.g. "15 Oct 2026"; None = today. Llama 3's template puts
    # "Today Date: ..." in every prompt, so the same request can take another path tomorrow: evaluate.py pins it.
    prompt_date: str = None
    output_dir: Path = OUTPUT_DIR
    open_report: bool = False     # open the HTML report in a browser after each run
    color: bool = True            # ANSI colours in the console (Spyder and Windows Terminal show them)
    enabled_tools: list = field(default_factory=lambda: [
        "generate_image", "generate_speech", "generate_music", "generate_video", "generate_3d_model",
        "combine_video_and_audio",
        # document tools: they work on the files in workspace/ (see harness/tools/files.py)
        "read_file", "search_references", "update_powerpoint", "update_word_document", "update_latex",
        "edit_code", "run_script", "convert_presentation", "export_pdf",
        # eyes: a vision-language model the LLM can ask about any image (see harness/tools/vision.py),
        # and a plot digitizer: Python measures the curves, the same VLM reads the labels (harness/tools/digitize.py)
        "look_at_image", "digitize_plot",
        # tools that hold knowledge or limits of their own: textbook fire correlations, a read-only SQL database, a
        # sub-agent with its own context, and a spreadsheet Excel calculates (harness/tools/fire_calcs.py,
        # database.py, subagent.py, excel.py)
        "fire_calculation", "query_database", "ask_subagent", "update_excel",
        # reviewing and developing code: pytest, a linter, a code search, new code, a checked review (tools/coding.py)
        "run_tests", "check_code", "search_code", "write_code", "create_file", "review_code",
        # LaTeX projects: build, map, audit, fix and mark up the changes for review (tools/latex_project.py, ...)
        "build_latex", "scan_latex", "audit_latex", "fix_latex", "edit_tex", "make_latexdiff",
        # a Word document under review: tracked changes, comments, compare, accept or reject (tools/word_review.py)
        "read_review", "redline_docx", "comment_docx", "compare_docx", "accept_changes",
        # a deck edited with its author: what changed, edits that touch nothing else, a render check (deck_edit.py)
        "read_deck", "diff_decks", "edit_deck", "check_deck",
    ])
