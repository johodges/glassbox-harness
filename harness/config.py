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

# Planners loaded in 4 bits (bitsandbytes NF4): each weight is stored in 4 bits instead of 16 and turned back into
# bf16 on the fly, layer by layer. A quarter of the memory, a little slower, slightly less accurate.
FOUR_BIT = {"code_llm"}


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
    use_guard: bool = True        # run the prompt-injection classifier on the user's prompt
    guard_tool_outputs: bool = True  # ... and on the text tools bring back (search passages, files read)
    ask_before_acting: bool = True   # a person answers y/n before run_script runs code (False = yes to everything)
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
    ])
