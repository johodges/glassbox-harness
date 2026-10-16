import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import torch

from ..config import MODELS, PROJECT_DIR
from .base import Tool, find_file

OBJECT_STYLE = ", a single object, centered, full view, plain white background, soft studio lighting"
LOCAL_DIR = PROJECT_DIR / "trellis_local"

# Where TRELLIS runs (HarnessConfig.trellis_backend). Both servers offer the same Gradio API, so the
# code below is identical for both: only the address changes, and the colour the trace uses.
BACKENDS = {
    "remote": {"address": MODELS["trellis_space"], "kind": "remote",
               "label": f"https://huggingface.co/spaces/{MODELS['trellis_space']}",
               "where": "SOMEONE ELSE'S GPU (a Hugging Face Space), reached over HTTPS"},
    "local": {"address": MODELS["trellis_local"], "kind": "server",
              "label": f"{MODELS['trellis_local']}  (trellis_local/server.py in WSL)",
              "where": "THIS PC's GPU but in ANOTHER PROCESS (Linux Python in WSL, its own PyTorch), reached over HTTP"},
}


class Model3DTool(Tool):
    name = "generate_3d_model"
    description = ("Create a 3D model (GLB mesh file) of ONE object. Give either `prompt` (a text description; a "
                   "reference picture is generated first) or `image_path` (a PNG made earlier by generate_image). "
                   "Runs on a separate TRELLIS server and takes about a minute.")
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Description of the single object to model."},
            "image_path": {"type": "string", "description": "Path of an existing image of the object (optional)."},
        },
        "required": [],
    }
    model_id = MODELS["trellis_space"]
    # The reference picture needs ~8 GB (SDXL). With the "local" backend the TRELLIS server then borrows
    # ~11 GB, but from another process that the harness cannot see or park: on Windows, the free memory
    # the harness measures does not even include it. So this asks the harness to park its OWN models
    # until it holds no more than a few GB itself.
    vram_gb = 13.0
    how_it_works = ("TRELLIS runs on {where}: the image is uploaded -> a background-removal model cuts out the "
                    "object -> DINOv2 image features steer two flow transformers that build a sparse 3D voxel "
                    "structure and its latent features -> decoders turn those into a textured mesh, which is "
                    "downloaded back as a .glb file.")

    def validate(self, args):
        args = super().validate(args)
        if not args.get("prompt") and not args.get("image_path"):
            raise ValueError("give either `prompt` or `image_path`")
        return args

    def run(self, ctx, prompt=None, image_path=None):
        # ---- step 1: get a picture of the object (a pipe: one tool's output feeds the next) ----
        image = find_file(image_path, ctx.run_dir) if image_path else None
        artifacts = []
        if image is None:
            if not prompt:
                raise FileNotFoundError(f"image_path not found: {image_path}")
            ctx.tracer.say("no usable image given -> calling generate_image first (tool -> tool pipe)", kind="python")
            made = ctx.tools["generate_image"].run(ctx, prompt=prompt + OBJECT_STYLE)
            image = Path(made["artifacts"][0]["path"])
            artifacts += [{**a, "note": "reference picture made automatically as TRELLIS's input"}
                          for a in made["artifacts"]]

        # ---- step 2: send it to TRELLIS, a separate server, over the network ----
        from gradio_client import Client, handle_file
        backend = BACKENDS[ctx.config.trellis_backend if ctx.config else "remote"]
        local = backend["kind"] == "server"
        if local and torch.cuda.is_available():
            torch.cuda.empty_cache()  # hand this process's cached GPU memory back, so the TRELLIS process can use it
        if local:
            _ensure_local_server(ctx, backend["address"])
        glb_path, preview_path = ctx.new_path("model3d", ".glb"), ctx.new_path("model3d_preview", ".mp4")
        with ctx.tracer.stage("TOOL: generate_3d_model", backend["kind"], backend["label"],
                              "image (uploaded)", "3D mesh (downloaded)") as ev:
            ctx.tracer.say(self.how_it_works.format(where=backend["where"]), kind=backend["kind"])
            ctx.tracer.say(f"uploading {image}", kind=backend["kind"])
            try:
                client = Client(backend["address"], token=None if local else os.environ.get("HF_TOKEN"),
                                verbose=False)
            except Exception as exc:
                if local:
                    raise ConnectionError(f"no TRELLIS server answers at {backend['address']} "
                                          "(start it with trellis_local\\start.ps1)") from exc
                raise
            client.predict(api_name="/start_session")
            cut_out = client.predict(image=handle_file(str(image)), api_name="/preprocess_image")
            preview, glb, _ = client.predict(
                image=handle_file(cut_out), multiimages=[], seed=ctx.seed,
                ss_guidance_strength=7.5, ss_sampling_steps=12, slat_guidance_strength=3.0, slat_sampling_steps=12,
                multiimage_algo="stochastic", mesh_simplify=0.95, texture_size=1024,
                api_name="/generate_and_extract_glb")
            shutil.copy(glb, glb_path)
            # The Space's older Gradio sends the video as {"video": path, ...}; Gradio 6 sends just the path.
            shutil.copy(preview["video"] if isinstance(preview, dict) else preview, preview_path)
            ev.summary = f"received a {glb_path.stat().st_size / 1e6:.1f} MB GLB mesh and a turntable preview video"
            ev.artifacts += [str(glb_path), str(preview_path)]
        artifacts = [{"type": "mesh", "path": glb_path, "note": "the 3D model (GLB)"},
                     {"type": "preview", "path": preview_path, "note": "turntable preview video of the 3D model"},
                     *artifacts]
        return {"artifacts": artifacts, "info": {"source_image": str(image)}}


def _server_up(address):
    try:
        with urllib.request.urlopen(address, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def _ensure_local_server(ctx, address, timeout_s=180):
    """
    The local server is the default backend, so if nothing answers yet, the harness starts it: the
    same command as trellis_local\\start.ps1, in the background. It keeps running after this request,
    so later 3D requests skip the wait. `wsl --terminate trellis` stops it and frees its RAM.
    """
    if _server_up(address):
        return
    with ctx.tracer.stage("START TRELLIS SERVER", "python", "wsl -d trellis ... start.sh", "a shell command",
                          f"a server at {address}") as ev:
        listed = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, env={**os.environ, "WSL_UTF8": "1"})
        if "trellis" not in listed.stdout.decode("utf-8", errors="ignore").split():
            raise ConnectionError("the local TRELLIS server is not set up on this PC: run "
                                  "trellis_local\\setup.ps1 once, or use the Hugging Face Space (--trellis remote)")
        log_path = LOCAL_DIR / "server.log"
        ctx.tracer.say(f"no server at {address} -> starting it in WSL (about 25 s); its output goes to {log_path}",
                       kind="python")
        with open(log_path, "w") as log:
            server = subprocess.Popen(
                ["wsl", "-d", "trellis", "-u", "root", "--cd", str(LOCAL_DIR), "--", "bash", "./start.sh"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        start = time.time()
        while not _server_up(address):
            if server.poll() is not None:
                raise RuntimeError(f"the TRELLIS server stopped while starting; see {log_path}")
            if time.time() - start > timeout_s:
                raise TimeoutError(f"the TRELLIS server did not answer within {timeout_s}s; see {log_path}")
            time.sleep(2)
        ev.summary = (f"server ready after {time.time() - start:.0f}s; it stays up for later requests "
                      "(stop it with: wsl --terminate trellis)")
