"""
Perception: turning the tools' outputs BACK into text.

The LLM only reads text. After a tool writes an image, a sound or a video, the LLM has no idea
what is actually in that file. So the harness runs other models over the file and pastes their
text output into the conversation:

    image -> BLIP captioner        -> "a red fox sitting in the snow"
    image -> ViT safety classifier -> "normal 99.8%"
    speech-> Whisper (speech-to-text) -> "in a snowy forest a clever fox..."
    sound -> AST audio tagger      -> "Music 62%, Jazz 31%, Saxophone 12%"
    video -> BLIP on 3 frames      -> start / middle / end captions
    3D    -> trimesh (plain code)  -> vertex count, size, watertight?
    .pptx / .docx / PDF -> read the saved file back (plain code) -> what the slide or section now says
    whole deck -> read back + font metrics, then PowerPoint draws PNGs -> layouts, text, what overflows

Each model is loaded the first time it is needed and then kept in memory.
"""

import difflib
import re
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from PIL import Image
from transformers import BlipForConditionalGeneration, BlipProcessor, pipeline

from .config import MODELS


class Perception:
    def __init__(self, tracer, device="cuda"):
        self.tracer, self.device = tracer, device
        self.dtype = torch.float16 if device == "cuda" else torch.float32
        self._models = {}

    def _get(self, key):
        """Load a model the first time it is needed, then keep it in memory."""
        if key not in self._models:
            with self.tracer.stage("LOAD MODEL", "python", MODELS[key], "weights on disk",
                                   f"model on {self.device}") as ev:
                if key == "captioner":
                    self._models[key] = (BlipProcessor.from_pretrained(MODELS[key]),
                                         BlipForConditionalGeneration.from_pretrained(
                                             MODELS[key], dtype=self.dtype).to(self.device))
                else:
                    task = {"image_safety": "image-classification", "asr": "automatic-speech-recognition",
                            "audio_tagger": "audio-classification"}[key]
                    self._models[key] = pipeline(task, model=MODELS[key], device=self.device, dtype=self.dtype)
                ev.summary = f"loaded the {key} model (kept in memory for next time)"
        self._module(key).to(self.device)  # no-op unless the harness parked it in RAM
        return self._models[key]

    def _module(self, key):
        loaded = self._models[key]
        return loaded[1] if key == "captioner" else loaded.model

    def gpu_modules(self):
        return {key: self._module(key) for key in self._models}

    # ---- the models ---------------------------------------------------------------------
    def caption(self, image):
        """Image -> one-sentence caption (BLIP: a vision encoder + a small text decoder)."""
        processor, model = self._get("captioner")
        inputs = processor(images=image.convert("RGB"), return_tensors="pt").to(self.device, self.dtype)
        ids = model.generate(**inputs, max_new_tokens=30, num_beams=3, no_repeat_ngram_size=3)
        return processor.decode(ids[0], skip_special_tokens=True).strip()

    def image_safety(self, image):
        """Image -> {'normal': p, 'nsfw': p} (a Vision Transformer fine-tuned as a classifier)."""
        clf = self._get("image_safety")
        return {r["label"]: round(r["score"], 4) for r in clf(image.convert("RGB"))}

    def transcribe(self, audio, sr):
        """Speech -> text (Whisper: an audio encoder + a text decoder)."""
        asr = self._get("asr")
        # Whisper listens in 30-second windows; return_timestamps=True lets it stitch longer audio together.
        clip = {"raw": resample(audio, sr, 16000), "sampling_rate": 16000}
        return asr(clip, return_timestamps=True)["text"].strip()

    def tag_audio(self, audio, sr, top_k=4):
        """Sound -> labels from the 527 AudioSet classes (Audio Spectrogram Transformer)."""
        tagger = self._get("audio_tagger")
        clip = {"raw": resample(audio, sr, 16000), "sampling_rate": 16000}
        return {r["label"]: round(r["score"], 3) for r in tagger(clip, top_k=top_k)}

    # ---- one entry point per kind of file -------------------------------------------------
    def describe(self, artifact):
        """Look at one file a tool produced and return a dict of plain-text observations."""
        kind, path = artifact["type"], artifact["path"]
        if kind == "image":
            return self._describe_image(path)
        if kind == "speech":
            return self._describe_speech(path, artifact.get("intended_text", ""))
        if kind == "music":
            return self._describe_music(path)
        if kind == "video":
            return self._describe_video(path)
        if kind == "mesh":
            return self._describe_mesh(path)
        if kind == "slides":
            return self._read_back_slides(path, artifact["slide"])
        if kind == "document":
            return self._read_back_docx(path, artifact["heading"])
        if kind == "pdf":
            return self._read_back_pdf(path, artifact.get("section", ""))
        if kind == "deck":
            return self._read_back_deck(path)
        return {}

    # ---- documents: already text, so "perception" is reading the saved file back (no ML) ----
    def _read_back_slides(self, path, number):
        from .tools.documents import pptx_slides
        with self.tracer.stage("READ BACK", "python", "python-pptx (no ML)", ".pptx file", "slide text") as ev:
            slides = pptx_slides(path)
            slide = slides[number - 1]
            ev.summary = f"the saved deck has {len(slides)} slides; slide {number}: \"{slide['title']}\", " \
                         f"{len(slide['bullets'])} bullet(s)"
        return {"slides_in_deck": len(slides), "slide_now_reads": {"title": slide["title"], "bullets": slide["bullets"]}}

    def _read_back_docx(self, path, heading):
        from .tools.documents import docx_sections
        with self.tracer.stage("READ BACK", "python", "python-docx (no ML)", ".docx file", "outline + section") as ev:
            sections = docx_sections(path)
            outline = [f"{s['heading']} ({sum(len(p.split()) for p in s['paragraphs'])} words)"
                       for s in sections if s["heading"]]
            section = next((s for s in sections if s["heading"] == heading), {"paragraphs": []})
            ev.summary = "outline of the saved file: " + ", ".join(outline)
        return {"outline": outline, "section_now_reads": section["paragraphs"]}

    def _read_back_pdf(self, path, section):
        from .tools.documents import pdf_text
        with self.tracer.stage("READ BACK", "python", "pypdf (no ML)", "PDF", "text") as ev:
            from pypdf import PdfReader
            pages = len(PdfReader(path).pages)
            text = " ".join(pdf_text(path).split())
            start = text.find(section)
            excerpt = text[start:start + 400] if start >= 0 else text[:400]
            ev.summary = (f"{pages} page(s); " + (f"the PDF's text around \"{section}\": " if section else "it begins: ")
                          + f"{excerpt[:120]}...")
        return {"pdf_pages": pages, "pdf_text_excerpt": excerpt}

    def _read_back_deck(self, path):
        """
        A whole converted deck. The LLM can't see slides, so plain code checks them: python-pptx reads each
        slide's layout and text back, font metrics estimate whether each text box is too full, and then
        PowerPoint (if installed) draws every slide as a PNG and measures the text exactly.
        """
        from .tools.deck_check import check_deck, contact_sheet, powerpoint_available, render_deck
        from .tools.presentation import shorten_list
        from .tools.files import rel
        path = Path(path)
        with self.tracer.stage("READ BACK", "python", "python-pptx + font metrics (no ML)", ".pptx file",
                               "layout + text per slide") as ev:
            slides = check_deck(path)
            estimated = {}
            for s in slides:
                estimated.update({(s["slide"], sid): (name, ratio) for sid, (name, ratio) in s["fit"].items()})
                fullest = max((ratio for _, ratio in s["fit"].values()), default=0)
                self.tracer.say(f"slide {s['slide']}: {s['layout']:<20} \"{s['title'][:40]}\"  {len(s['text'])} "
                                f"paragraph(s), {s['pictures']} picture(s), {s['tables']} table(s); fullest text box "
                                f"~{fullest:.0%}", kind="python")
            over = [f"slide {n} {name} ~{ratio:.0%}" for (n, _), (name, ratio) in estimated.items() if ratio > 1.0]
            ev.summary = (f"{len(slides)} slides read back; estimate: "
                          + (f"text may overflow on {', '.join(over)}" if over else "every text box fits"))
            ev.details = {"slides": slides}
        measured = {}
        with self.tracer.stage("RENDER", "python", "PowerPoint via COM (a separate program, no ML)", ".pptx file",
                               "a PNG per slide + measured text heights") as ev:
            if not powerpoint_available():
                ev.summary = "PowerPoint is not installed here: no slide pictures, only the estimate above"
            else:
                pngs, measured = render_deck(path, path.parent / "slides")
                for key, (name, guess) in estimated.items():
                    if key in measured:
                        self.tracer.say(f"slide {key[0]} {name}: estimate {guess:.0%}, PowerPoint measured "
                                        f"{measured[key]:.0%}", kind="python")
                over = [f"slide {n}" for (n, _), ratio in measured.items() if ratio > 1.0]
                ev.summary = (f"{len(pngs)} slides drawn as PNGs in {rel(path.parent / 'slides')}; measured: "
                              + (f"text overflows on {', '.join(over)}" if over else "every text box fits"))
                ev.artifacts.append(str(contact_sheet(pngs, path.parent / "slides_overview.png")))
        # what the LLM reads: short, because a 3B model loses track in long results
        names = {key: name for key, (name, _) in estimated.items()}
        ratios = measured or {key: ratio for key, (_, ratio) in estimated.items()}
        too_long = [f"slide {n}: {names.get((n, sid), 'a text box')} needs {r:.0%} of its height"
                    for (n, sid), r in sorted(ratios.items()) if r > 1.0]
        lines = [f"slide {s['slide']} ({s['layout']}): {s['title'] or '(no title)'}" for s in slides]
        if len(lines) > 12:  # a long deck: count the layouts, show the first and last slides
            from collections import Counter
            counts = Counter(s["layout"] for s in slides)
            lines = ([f"{len(slides)} slides: " + ", ".join(f"{n} {layout}" for layout, n in counts.most_common())]
                     + lines[:3] + ["..."] + lines[-2:])
        return {"deck_now": lines,
                "text_too_long": shorten_list(too_long) if too_long else "none, every text box fits",
                "checked_by": "PowerPoint (slide pictures in slides/)" if measured else "an estimate from font sizes"}

    def _describe_image(self, path):
        image = Image.open(path)
        self._get("captioner")
        self._get("image_safety")
        with self.tracer.stage("PERCEIVE IMAGE", "perception", MODELS["captioner"], "image", "caption text") as ev:
            caption = self.caption(image)
            ev.summary = f'"{caption}"'
        with self.tracer.stage("OUTPUT GUARD", "classifier", MODELS["image_safety"], "image", "label + score") as ev:
            scores = self.image_safety(image)
            ev.summary = ", ".join(f"{k} {v:.1%}" for k, v in scores.items())
        return {"caption": caption, "safety_scores": scores}

    def _describe_speech(self, path, intended_text):
        audio, sr = sf.read(path, dtype="float32")
        self._get("asr")
        with self.tracer.stage("PERCEIVE SPEECH", "perception", MODELS["asr"], "audio", "transcript text") as ev:
            heard = self.transcribe(audio, sr)
            # Compare what we asked for with what Whisper heard: a cheap automatic check of the TTS.
            norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower()).split()
            match = difflib.SequenceMatcher(None, norm(intended_text), norm(heard)).ratio()
            ev.summary = f'heard "{heard}"  (word match with the intended text: {match:.0%})'
        return {"transcript": heard, "word_match": f"{match:.0%}", "duration_s": round(len(audio) / sr, 1)}

    def _describe_music(self, path):
        audio, sr = sf.read(path, dtype="float32")
        self._get("audio_tagger")
        with self.tracer.stage("PERCEIVE SOUND", "perception", MODELS["audio_tagger"], "audio", "labels") as ev:
            tags = self.tag_audio(audio, sr)
            ev.summary = ", ".join(f"{k} {v:.0%}" for k, v in tags.items())
        return {"sound_labels": tags, "duration_s": round(len(audio) / sr, 1)}

    def _describe_video(self, path):
        from .tools.media import read_video
        frames, fps = read_video(path)  # (num_frames, height, width, 3) uint8
        duration = len(frames) / fps
        self._get("captioner")
        count = min(8, max(3, round(duration / 5)))  # about one caption every 5 seconds
        picks = np.linspace(0, len(frames) - 1, count).round().astype(int)
        with self.tracer.stage("PERCEIVE VIDEO", "perception", MODELS["captioner"], f"{count} video frames",
                               f"a timeline of {count} captions") as ev:
            timeline = {f"{i / fps:.0f}s": self.caption(Image.fromarray(frames[i])) for i in picks}
            ev.summary = " | ".join(f"{k}: {v}" for k, v in timeline.items())
        with self.tracer.stage("CHECK REPEATS", "python", "pixel differences (numpy, no ML)", "video frames",
                               "repeated or not") as ev:
            repeat = find_repeat(frames, fps)
            ev.summary = (f"frames at {repeat[0]:.1f}s and {repeat[1]:.1f}s are identical: the footage repeats"
                          if repeat else "no repeated footage")
        return {"timeline": timeline, "duration_s": round(duration, 1),
                "repeats": ev.summary if repeat else "no"}

    def _describe_mesh(self, path):
        import trimesh
        with self.tracer.stage("INSPECT MESH", "python", "trimesh (geometry code, no ML)", "GLB file",
                               "numbers") as ev:
            mesh = trimesh.load(path, force="mesh")
            info = {"vertices": len(mesh.vertices), "triangles": len(mesh.faces),
                    "size_xyz": [round(float(x), 3) for x in mesh.extents], "watertight": bool(mesh.is_watertight)}
            ev.summary = f"{info['vertices']:,} vertices, {info['triangles']:,} triangles, size {info['size_xyz']}"
        return info


def resample(audio, sr, target_sr):
    """
    Whisper and AST were trained on 16 kHz mono audio, but Kokoro writes 24 kHz and MusicGen
    32 kHz. Every model has its own expected input format and the harness converts between
    them (here with simple linear interpolation).
    """
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr == target_sr:
        return audio
    n_out = int(len(audio) * target_sr / sr)
    return np.interp(np.linspace(0, len(audio) - 1, n_out), np.arange(len(audio)), audio).astype(np.float32)


def find_repeat(frames, fps, min_gap_s=2.0, every=4, threshold=1.0):
    """
    Captions can't tell "the fire spreads for 40 s" from "the same 5 s clip plays back and forth":
    both give similar sentences. So compare the pixels: shrink every 4th frame to 64x36 grey and look
    for two frames at least 2 s apart that are practically identical (mean difference under 1 of 255).
    Measured: a ping-pong loop scores 0.12; real motion scores about 6 even 0.25 s apart.
    """
    small = np.stack([np.asarray(Image.fromarray(f).convert("L").resize((64, 36)), dtype=np.float32)
                      for f in frames[::every]])
    gap = max(1, round(min_gap_s * fps / every))
    for i in range(len(small)):
        for j in range(i + gap, len(small)):
            if np.abs(small[i] - small[j]).mean() < threshold:
                return i * every / fps, j * every / fps
    return None
