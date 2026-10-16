import torch

from ..config import MODELS
from .base import Tool


class ImageTool(Tool):
    name = "generate_image"
    description = ("Create a picture from a text description. Returns the PNG file path and a caption "
                   "of what is actually in the picture.")
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "Detailed visual description: subject, setting, style, colours, lighting."},
        },
        "required": ["prompt"],
    }
    model_id = MODELS["image"]
    vram_gb = 8.0
    how_it_works = ("SDXL-Turbo: two CLIP text encoders turn the prompt into embedding vectors -> a U-Net removes "
                    "noise from a 64x64x4 latent in 4 steps, steered by those vectors -> a VAE decoder "
                    "turns the latent into 512x512 RGB pixels.")

    def __init__(self):
        self.pipe = None

    def load(self, ctx):
        from diffusers import AutoPipelineForText2Image
        with ctx.tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", "pipeline in RAM") as ev:
            self.pipe = AutoPipelineForText2Image.from_pretrained(
                self.model_id, torch_dtype=torch.float16, variant="fp16")
            if ctx.device == "cuda":
                # Each sub-model hops onto the GPU only while it is working, then back to RAM.
                self.pipe.enable_model_cpu_offload()
            self.pipe.set_progress_bar_config(disable=True)
            ev.summary = "components: " + ", ".join(
                f"{k} ({type(v).__name__})" for k, v in self.pipe.components.items() if v is not None)

    def run(self, ctx, prompt):
        if self.pipe is None:
            self.load(ctx)
        path = ctx.new_path("image", ".png")
        with ctx.tracer.stage("TOOL: generate_image", "generator", self.model_id, "text prompt", "image") as ev:
            ctx.tracer.say(self.how_it_works, kind="generator")
            ctx.tracer.say(f'prompt: "{prompt}"', kind="generator")
            image = self.pipe(prompt=prompt, num_inference_steps=4, guidance_scale=0.0, width=512, height=512,
                              generator=torch.Generator("cpu").manual_seed(ctx.seed)).images[0]
            image.save(path)
            ev.summary = f"{image.width}x{image.height} image"
            ev.artifacts.append(str(path))
        return {"artifacts": [{"type": "image", "path": path}], "info": {"prompt_used": prompt}}
