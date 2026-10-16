import soundfile as sf
import torch

from ..config import MODELS
from .base import Tool


class MusicTool(Tool):
    name = "generate_music"
    description = ("Compose a short instrumental music clip (no singing, no words) from a description of genre, "
                   "instruments, tempo and mood. Returns a WAV file.")
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string",
                            "description": "Genre, instruments, tempo and mood, e.g. 'calm lo-fi piano, slow, rainy'."},
            "seconds": {"type": "integer", "minimum": 3, "maximum": 15, "description": "Length in seconds."},
        },
        "required": ["description"],
    }
    model_id = MODELS["music"]
    vram_gb = 3.0
    how_it_works = ("MusicGen: a T5 text encoder reads the description -> a transformer LANGUAGE MODEL writes "
                    "audio tokens instead of words (50 tokens per second of sound) -> the EnCodec decoder turns "
                    "those tokens into a 32 kHz waveform. Same next-token idea as the LLM, different vocabulary.")

    def __init__(self):
        self.processor = self.model = None

    def load(self, ctx):
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
        with ctx.tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", "model on GPU") as ev:
            self.processor = AutoProcessor.from_pretrained(self.model_id)
            self.model = MusicgenForConditionalGeneration.from_pretrained(self.model_id).to(ctx.device).eval()
            ev.summary = f"{sum(p.numel() for p in self.model.parameters()) / 1e6:.0f} million parameters"

    def gpu_modules(self):
        return {"music model": self.model} if self.model is not None else {}

    def run(self, ctx, description, seconds=8):
        if self.model is None:
            self.load(ctx)
        self.model.to(ctx.device)  # no-op unless the harness parked it in RAM
        path = ctx.new_path("music", ".wav")
        with ctx.tracer.stage("TOOL: generate_music", "generator", self.model_id, "text", "audio waveform") as ev:
            ctx.tracer.say(self.how_it_works, kind="generator")
            ctx.tracer.say(f'description: "{description}"  ->  {seconds}s = {seconds * 50} audio tokens to generate',
                           kind="generator")
            torch.manual_seed(ctx.seed)
            inputs = self.processor(text=[description], padding=True, return_tensors="pt").to(ctx.device)
            with torch.no_grad():
                audio = self.model.generate(**inputs, do_sample=True, guidance_scale=3.0,
                                            max_new_tokens=int(seconds * 50))
            sr = self.model.config.audio_encoder.sampling_rate
            wave = audio[0, 0].float().cpu().numpy()
            sf.write(path, wave, sr)
            ev.summary = f"{len(wave) / sr:.1f} seconds of music at {sr // 1000} kHz"
            ev.artifacts.append(str(path))
        return {"artifacts": [{"type": "music", "path": path}], "info": {"description_used": description}}
