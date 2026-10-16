import math
import time

import numpy as np
import torch
from PIL import Image

from ..config import MODELS
from .base import Tool
from .media import fit_frames, join_with_crossfade, write_video

NEGATIVE_PROMPT = "blurry, low quality, static, still image, distorted, deformed, extra limbs, watermark, text, subtitles"
SEGMENT_SECONDS = 5  # Wan 2.1 was trained on clips of up to 81 frames: about 5 seconds at 16 fps


class VideoTool(Tool):
    name = "generate_video"
    description = ("Create a silent video from a description of a scene and its motion. The model makes 5 seconds at a "
                   "time. For longer videos, set `seconds` and write a `storyboard`: one short sentence per 5 seconds "
                   "saying how the scene moves on. mode 'continue' (default) makes one continuous shot, each part "
                   "continuing from the end of the last; it is slow, about 12 minutes per 5 seconds. mode 'loop' is a "
                   "fast preview that plays one 5 second clip forward and backward. Returns an MP4 and a timeline of "
                   "captions.")
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "What is SEEN throughout: the scene, camera, style, lighting. No sound or "
                                      "narration: the video is silent."},
            "seconds": {"type": "integer", "minimum": 1, "maximum": 60,
                        "description": "Length of the video in seconds (default 2)."},
            "storyboard": {"type": "array", "items": {"type": "string"},
                           "description": "For videos longer than 5 seconds: what the camera SEES next, in order, "
                                          "one short sentence per 5 seconds. Visual events only: no advice or narration."},
            "mode": {"type": "string", "enum": ["continue", "loop"],
                     "description": "continue = one continuous shot (slow), loop = quick preview."},
        },
        "required": ["prompt"],
    }
    model_id = MODELS["video_vace"]
    vram_gb = 13.0  # the umT5 text encoder alone is ~11 GB, so the harness parks the LLM first
    how_it_works = ("Wan 2.1 VACE: a umT5 text encoder (5.7B params, bigger than our LLM!) embeds the prompt -> a "
                    "diffusion transformer denoises a 3D block of video latents (time x height x width) over 20 steps, "
                    "optionally guided by frames it must keep -> a 3D VAE decodes up to 81 frames (5 s) of 832x480. "
                    "Longer videos are made 5 s at a time: each part is given the last frames of the previous one "
                    "and continues from there.")
    height, width, steps, fps = 480, 832, 20, 16
    segment_frames, overlap = 81, 9  # each continuation is given the previous segment's last 9 frames (0.6 s)

    def __init__(self):
        self.pipe = None

    def load(self, ctx):
        from diffusers import AutoencoderKLWan, UniPCMultistepScheduler, WanVACEPipeline, WanVACETransformer3DModel
        from transformers import AutoTokenizer, UMT5EncoderModel
        base, vace = MODELS["video"], MODELS["video_vace"]
        with ctx.tracer.stage("LOAD MODEL", "python", f"{vace} (+ text encoder and VAE from {base})",
                              "weights on disk", "pipeline in RAM") as ev:
            self.pipe = WanVACEPipeline(
                tokenizer=AutoTokenizer.from_pretrained(base, subfolder="tokenizer"),
                text_encoder=UMT5EncoderModel.from_pretrained(base, subfolder="text_encoder", dtype=torch.bfloat16),
                vae=AutoencoderKLWan.from_pretrained(base, subfolder="vae", torch_dtype=torch.float32),
                scheduler=UniPCMultistepScheduler.from_pretrained(vace, subfolder="scheduler", flow_shift=3.0),
                transformer=WanVACETransformer3DModel.from_pretrained(vace, subfolder="transformer",
                                                                      torch_dtype=torch.bfloat16))
            # The VAE encodes and decodes whole clips at 832x480 in float32. Doing it in tiles keeps its memory
            # small; on a 16 GB laptop GPU, Windows' desktop compositor alone holds about 3 GB.
            self.pipe.vae.enable_tiling()
            params = sum(p.numel() for p in self.pipe.transformer.parameters())
            ev.summary = f"VACE transformer {params / 1e9:.1f}B parameters; text encoder and VAE shared with Wan 2.1 T2V"

    # GPU scheduling. Automatic offloading (move each model to the GPU when it runs) assumes the order text
    # encoder -> transformer -> VAE, but VACE also runs the VAE BEFORE the transformer, so the 11 GB text encoder
    # was still on the GPU, memory spilled into system RAM and generation slowed to a crawl. So the tool
    # schedules it by hand: the text encoder visits the GPU once for ALL prompts, then the video model moves in.
    def _encode(self, ctx, prompts, say):
        start = time.time()
        self.pipe.text_encoder.to(ctx.device)
        with torch.no_grad():
            embeds = {p: self.pipe.encode_prompt(p, NEGATIVE_PROMPT, device=ctx.device) for p in dict.fromkeys(prompts)}
        self.pipe.text_encoder.to("cpu")
        if ctx.device == "cuda":
            torch.cuda.empty_cache()
        say(f"text encoder: moved to the GPU, encoded {len(embeds)} prompt(s), moved back to RAM "
            f"({time.time() - start:.0f}s)")
        return embeds

    def _models_to(self, device):
        self.pipe.transformer.to(device)
        self.pipe.vae.to(device)
        if device == "cpu" and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _segment(self, embeds, num_frames, seed, keep=None):
        """One call to the model. `keep`: frames the new segment must start with (to continue from them)."""
        video = mask = None
        if keep is not None:
            gray = Image.new("RGB", (self.width, self.height), (128, 128, 128))
            video = [Image.fromarray(f) for f in keep] + [gray] * (num_frames - len(keep))
            black, white = Image.new("L", (self.width, self.height), 0), Image.new("L", (self.width, self.height), 255)
            mask = [black] * len(keep) + [white] * (num_frames - len(keep))  # black = keep, white = generate
        frames = self.pipe(
            prompt_embeds=embeds[0], negative_prompt_embeds=embeds[1], video=video, mask=mask, height=self.height,
            width=self.width, num_frames=num_frames, num_inference_steps=self.steps, guidance_scale=5.0,
            generator=torch.Generator("cpu").manual_seed(seed)).frames[0]
        return (np.asarray(frames) * 255).round().astype(np.uint8)  # floats 0..1 -> 8-bit pixels

    def run(self, ctx, prompt, seconds=2, storyboard=None, mode="continue"):
        if self.pipe is None:
            self.load(ctx)
        total = round(seconds * self.fps)
        chain = seconds > SEGMENT_SECONDS and mode == "continue"
        n = 1 + math.ceil(max(total - self.segment_frames, 0) / (self.segment_frames - self.overlap)) if chain else 1
        storyboard = storyboard or []
        beats = [storyboard[i * len(storyboard) // n] if storyboard else "" for i in range(n)]
        prompts = [f"{prompt} {beat}".strip() for beat in beats]
        path = ctx.new_path("video", ".mp4")

        def say(msg):
            ctx.tracer.say(msg, kind="generator")

        with ctx.tracer.stage("TOOL: generate_video", "generator", self.model_id, "text prompt (+ storyboard)",
                              "video") as ev:
            say(self.how_it_works)
            say(f'prompt: "{prompt}"')
            try:
                embeds = self._encode(ctx, prompts, say)
                self._models_to(ctx.device)
                if seconds <= SEGMENT_SECONDS:
                    num_frames = 4 * round(total / 4) + 1  # Wan needs 4k+1 frames: 2 s -> 33, 5 s -> 81
                    video = self._segment(embeds[prompts[0]], num_frames, ctx.seed)
                    note = f"{len(video) / self.fps:.1f}s clip made by the model"
                elif not chain:
                    clip = self._segment(embeds[prompts[0]], self.segment_frames, ctx.seed)
                    video = fit_frames(clip, total)
                    note = (f"{len(clip) / self.fps:.1f}s clip looped forward and backward to "
                            f"{len(video) / self.fps:.1f}s (a quick preview: the action repeats)")
                else:
                    video, note = self._continue(ctx, prompts, beats, embeds, total, say)
            finally:
                self._models_to("cpu")  # give the GPU back to the LLM and the other tools
            write_video(video, path, self.fps)
            ev.summary = note
            ev.artifacts.append(str(path))
        return {"artifacts": [{"type": "video", "path": path, "note": note}], "info": {"prompt_used": prompt}}

    def _continue(self, ctx, prompts, beats, embeds, total, say):
        """A continuation chain: segment 1 from text alone, every later segment from text + the end of the video so far."""
        n = len(prompts)
        say(f"{total / self.fps:.0f}s = {n} segments of {self.segment_frames} frames, each continuing from the last "
            f"{self.overlap} frames of the one before; storyboard: {len(set(b for b in beats if b))} beat(s)")
        segments_dir = ctx.run_dir / "segments"
        segments_dir.mkdir(exist_ok=True)
        video, start = None, time.time()
        for i, (segment_prompt, beat) in enumerate(zip(prompts, beats)):
            say(f"segment {i + 1}/{n}: {beat or '(no storyboard beat: same prompt)'}")
            keep = None if video is None else video[-self.overlap:]
            segment = self._segment(embeds[segment_prompt], self.segment_frames, ctx.seed + i, keep)
            write_video(segment, segments_dir / f"segment_{i + 1:02d}.mp4", self.fps)  # saved as soon as it exists
            video = segment if video is None else join_with_crossfade(video, segment, self.overlap)
            done = time.time() - start
            say(f"segment {i + 1}/{n} done: {len(video) / self.fps:.1f}s so far, {done / 60:.1f} min elapsed, "
                f"about {done / (i + 1) * (n - i - 1) / 60:.0f} min to go")
        video = video[:total]
        note = (f"{len(video) / self.fps:.1f}s continuous video from {n} segments, each continuing from the end of the "
                f"last (storyboard beats used: {len(set(b for b in beats if b))}; segments saved in "
                f"{segments_dir.name}/)")
        return video, note
