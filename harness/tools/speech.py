import numpy as np
import soundfile as sf

from ..config import MODELS
from .base import Tool

SAMPLE_RATE = 24000
WORDS_PER_SECOND = 2.5          # Kokoro at normal speed (measured: 2.5 to 2.7)
MIN_SPEED, MAX_SPEED = 0.8, 1.25  # outside this range the voice starts to sound unnatural
MAX_SILENCE = 0.2  # pad at most 20% of the target with silence; shorter texts are sent back


class SpeechTool(Tool):
    name = "generate_speech"
    description = ("Text-to-speech: a voice reads the given text aloud. If the user wants a certain length, set "
                   "target_seconds and write about 2.5 words per second of it (40 seconds = about 100 words). "
                   "Returns a WAV file and a transcript of what a speech recogniser heard in it.")
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The exact words to speak."},
            "voice": {"type": "string", "enum": ["af_heart", "am_michael", "bf_emma", "bm_george"],
                      "description": "af_heart = American woman, am_michael = American man, "
                                     "bf_emma = British woman, bm_george = British man."},
            "target_seconds": {"type": "integer", "minimum": 2, "maximum": 120,
                               "description": "How long the speech should last, if the user asked for a length."},
        },
        "required": ["text"],
    }
    model_id = MODELS["speech"]
    vram_gb = 1.0
    how_it_works = ("Kokoro-82M: a rule+dictionary system (misaki) converts letters to phonemes (sounds) -> "
                    "a small neural network predicts how each sound should be pronounced and how long it lasts -> "
                    "a vocoder turns that into a 24 kHz waveform.")

    def __init__(self):
        self.model, self.pipelines = None, {}

    def _pipeline(self, ctx, lang):
        from kokoro import KModel, KPipeline
        if self.model is None:
            with ctx.tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", "model on GPU") as ev:
                self.model = KModel(repo_id=self.model_id).to(ctx.device).eval()
                ev.summary = f"{sum(p.numel() for p in self.model.parameters()) / 1e6:.0f} million parameters"
        if lang not in self.pipelines:  # 'a' = American English, 'b' = British English
            self.pipelines[lang] = KPipeline(lang_code=lang, repo_id=self.model_id, model=self.model)
        return self.pipelines[lang]

    def gpu_modules(self):
        return {"speech model": self.model} if self.model is not None else {}

    @staticmethod
    def _speak(pipe, text, voice, speed):
        chunks, phonemes = [], []
        for result in pipe(text, voice=voice, speed=speed):
            phonemes.append(result.phonemes)
            chunks.append(result.audio.cpu().numpy())
        return np.concatenate(chunks), " ".join(phonemes)

    def run(self, ctx, text, voice="af_heart", target_seconds=None):
        pipe = self._pipeline(ctx, voice[0])
        self.model.to(ctx.device)  # no-op unless the harness parked it in RAM
        path = ctx.new_path("speech", ".wav")
        def say(msg):
            ctx.tracer.say(msg, kind="generator")

        with ctx.tracer.stage("TOOL: generate_speech", "generator", self.model_id, "text", "audio waveform") as ev:
            say(self.how_it_works)
            audio, phonemes = self._speak(pipe, text, voice, 1.0)
            say(f'text    : "{text}"')
            say(f'phonemes: "{phonemes}"')
            note = None
            if target_seconds:
                # Plain-Python timing control around the model: measure, re-speak faster or slower, pad.
                say(f"{len(text.split())} words took {len(audio) / SAMPLE_RATE:.1f}s at normal speed; target "
                    f"{target_seconds}s (about {round(target_seconds * WORDS_PER_SECOND)} words)")
                speed = 1.0
                for _ in range(3):  # duration is not exactly proportional to speed, so measure and correct
                    duration = len(audio) / SAMPLE_RATE
                    if abs(duration - target_seconds) <= 0.02 * target_seconds:
                        break
                    new_speed = min(max(speed * duration / target_seconds, MIN_SPEED), MAX_SPEED)
                    if abs(new_speed - speed) < 0.01:
                        break  # already at the slowest or fastest natural speed
                    speed = new_speed
                    audio, _ = self._speak(pipe, text, voice, speed)
                    say(f"re-spoken at speed {speed:.2f} -> {len(audio) / SAMPLE_RATE:.1f}s")
                missing = target_seconds * SAMPLE_RATE - len(audio)
                words_needed = round(target_seconds * WORDS_PER_SECOND)
                # Far too short or too long: send the LLM back to rewrite (the error reaches it as text, like
                # every tool error) instead of a long silent ending or an overrun.
                if missing > MAX_SILENCE * target_seconds * SAMPLE_RATE:
                    raise ValueError(
                        f"The text is too short for {target_seconds} seconds: {len(text.split())} words last only "
                        f"{len(audio) / SAMPLE_RATE:.0f}s even when spoken slowly. Write about {words_needed} words "
                        "and call generate_speech again, or leave out target_seconds if the user did not ask for a "
                        "length.")  # without that last option, Llama padded a one-sentence story it gave a length
                if -missing > 0.05 * target_seconds * SAMPLE_RATE:
                    raise ValueError(
                        f"The text is too long for {target_seconds} seconds: {len(text.split())} words take "
                        f"{len(audio) / SAMPLE_RATE:.0f}s even when spoken fast. Write about {words_needed} words "
                        "and call generate_speech again.")
                if missing > 0:
                    audio = np.concatenate([audio, np.zeros(missing, dtype=audio.dtype)])
                    say(f"padded with {missing / SAMPLE_RATE:.1f}s of silence to reach {target_seconds}s")
                note = f"{len(audio) / SAMPLE_RATE:.1f}s of audio for a {target_seconds}s target, spoken at speed {speed:.2f}"
            sf.write(path, audio, SAMPLE_RATE)
            ev.summary = note or f"{len(audio) / SAMPLE_RATE:.1f} seconds of speech, voice {voice}"
            ev.artifacts.append(str(path))
        artifact = {"type": "speech", "path": path, "intended_text": text}
        if note:
            artifact["note"] = note
        return {"artifacts": [artifact], "info": {"voice": voice, "phonemes": phonemes}}
