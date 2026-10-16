"""
Download every model the harness uses, ahead of time (about 65 GB in total; the code model,
MODELS["code_llm"], is 15 GB of it: skip it with a list of keys if you won't use it).

    python download_models.py                 # everything
    python download_models.py llm image       # just some of them (keys from harness/config.py)

Weights go to the normal Hugging Face cache (~/.cache/huggingface/hub), so other projects
can reuse them. If you skip this step, each model downloads the first time it is needed.
"""

import sys
import time

from huggingface_hub import snapshot_download

from harness.config import MODELS

# Only fetch the files we actually load (many repos also ship ONNX / TensorFlow / fp32 copies).
WEIGHTS = ["*.json", "*.txt", "*.model", "*.jinja", "*.safetensors"]
PATTERNS = {
    "llm": ["*.json", "*.jinja", "model-*.safetensors"],
    "image": ["model_index.json", "*/config.json", "scheduler/*", "tokenizer*/*",
              "*/*.fp16.safetensors"],  # fp16 weights only: ~7 GB instead of ~55 GB
    "speech": ["config.json", "kokoro-v1_0.pth",
               "voices/af_heart.pt", "voices/am_michael.pt", "voices/bf_emma.pt", "voices/bm_george.pt"],
    "video": ["model_index.json", "text_encoder/*", "tokenizer/*", "vae/*", "scheduler/*"],
    "video_vace": ["model_index.json", "transformer/*", "scheduler/*"],  # the video tool's transformer
}
SKIP = ["trellis_space", "trellis_local"]  # TRELLIS runs in a separate server (see trellis_local/)


def main(keys):
    for key in keys or [k for k in MODELS if k not in SKIP]:
        repo = MODELS[key]
        print(f"\n=== {key}: {repo}")
        start = time.time()
        path = snapshot_download(repo, allow_patterns=PATTERNS.get(key, WEIGHTS))
        print(f"    done in {time.time() - start:.0f}s -> {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
