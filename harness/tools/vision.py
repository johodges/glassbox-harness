"""
A vision-language model (VLM): eyes for an agent whose planner can only read text.

Llama 3.2 3B never sees pixels. Qwen3-VL reads an image AND text in one prompt:

    image -> resized -> cut into 16x16-pixel patches -> neighbours merged 2x2 -> one "image token" each
    question -> ordinary word tokens
    <|vision_start|> [image tokens] <|vision_end|> + question -> language model -> answer (text)

So an image is just more tokens in the prompt, the same way the tool descriptions were just more
text in the LLM's prompt. To the harness this is a tool like any other: a path and a question go
in, a sentence comes out, and that sentence is all the planner ever learns about the image.

Compare it with the BLIP captioner in perception.py: BLIP writes one generic caption for every image;
a VLM answers the question you ask ("is there text on this slide that is cut off?").
"""

import time

import torch
from PIL import Image

from ..config import MODELS
from .base import Tool
from .files import rel, resolve

MAX_SIDE = 1280  # bigger images are shrunk first: every 32x32 pixels costs one token in the prompt


class LookAtImageTool(Tool):
    name = "look_at_image"
    description = ("Ask a vision-language model a question about an image file: a picture a tool made, a rendered "
                   "slide, a chart or a scanned page. It looks at the pixels and answers in words. Use it when you "
                   "need something specific from an image, such as the text or numbers in it, what is in it, or "
                   "whether it matches what the user asked for.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The image file (.png or .jpg), e.g. workspace/images/chart.png "
                                                      "or the file a tool just made."},
            "question": {"type": "string", "description": "What you want to know about the image, as a question."},
        },
        "required": ["path", "question"],
    }
    model_id = MODELS["vlm"]
    vram_gb = 5.0
    check_unused, repeatable = False, True  # a helper; and the same image can get a new question
    how_it_works = ("Qwen3-VL (2 billion parameters): a vision encoder cuts the image into 16x16-pixel patches and "
                    "merges each 2x2 group into one image token; those tokens sit in the prompt between "
                    "<|vision_start|> and <|vision_end|>, followed by the question's word tokens, and a language "
                    "model writes the answer. One model reads both pictures and words.")

    def __init__(self):
        self.processor = self.model = None

    def load(self, ctx):
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
        with ctx.tracer.stage("LOAD MODEL", "python", self.model_id, "weights on disk", f"model on {ctx.device}") as ev:
            self.processor = AutoProcessor.from_pretrained(self.model_id)
            dtype = torch.bfloat16 if ctx.device == "cuda" else torch.float32
            self.model = Qwen3VLForConditionalGeneration.from_pretrained(self.model_id, dtype=dtype).to(ctx.device).eval()
            ev.summary = f"{sum(p.numel() for p in self.model.parameters()) / 1e9:.1f} billion parameters"

    def gpu_modules(self):
        return {"vision-language model": self.model} if self.model is not None else {}

    def on_gpu(self):
        return self.model is not None and next(self.model.parameters()).is_cuda

    def ask(self, ctx, image, question, max_new_tokens=60):
        """One short question about one PIL image, for other tools: digitize_plot reads axis labels with it.
        The same model, loaded once; the calling tool traces what it asked and why."""
        if self.model is None:
            self.load(ctx)
        self.model.to(ctx.device)
        messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": question}]}]
        inputs = self.processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                    return_dict=True, return_tensors="pt").to(ctx.device)
        with torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return self.processor.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0].strip()

    def run(self, ctx, path, question):
        source = resolve(path, ctx)
        if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            raise ValueError(f"look_at_image needs an image file (.png or .jpg), not {source.name}.")
        if self.model is None:
            self.load(ctx)
        self.model.to(ctx.device)  # no-op unless the harness parked it in RAM
        say = lambda msg: ctx.tracer.say(msg, kind="perception")

        with ctx.tracer.stage("PREPARE IMAGE", "python", "PIL + the model's processor (no ML)", "image file",
                              "pixel patches + token ids") as ev:
            image = Image.open(source).convert("RGB")
            original = image.size
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            messages = [{"role": "user", "content": [{"type": "image", "image": image},
                                                     {"type": "text", "text": question}]}]
            inputs = self.processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                        return_dict=True, return_tensors="pt")
            # image_grid_thw = (frames, rows, columns) of 16-pixel patches; 2x2 patches are merged into one token
            frames, rows, cols = (int(x) for x in inputs["image_grid_thw"][0])
            merge = self.processor.image_processor.merge_size
            patch = self.processor.image_processor.patch_size
            image_tokens = frames * rows * cols // (merge * merge)
            total = inputs["input_ids"].shape[1]
            prompt = self.processor.decode(inputs["input_ids"][0])
            pad = "<|image_pad|>"
            shown = prompt.replace(pad * image_tokens, f"{pad} x {image_tokens}")
            say(self.how_it_works)
            say(f"{source.name}: {original[0]}x{original[1]} pixels -> {cols * patch}x{rows * patch} -> "
                f"{cols}x{rows} patches of {patch} px -> merged {merge}x{merge} -> {image_tokens} image tokens")
            say(f"the prompt the model reads ({total} tokens, {image_tokens} of them the image):\n{shown.strip()}")
            ev.summary = (f"{original[0]}x{original[1]} image -> {image_tokens} image tokens + "
                          f"{total - image_tokens} text tokens")
            ev.details = {"prompt_with_image_tokens_collapsed": shown, "image_tokens": image_tokens,
                          "text_tokens": total - image_tokens}

        with ctx.tracer.stage("TOOL: look_at_image", "perception", self.model_id, "image tokens + question",
                              "answer text") as ev:
            start = time.time()
            inputs = inputs.to(ctx.device)
            with torch.no_grad():
                out = self.model.generate(**inputs, max_new_tokens=200, do_sample=False)
            answer = self.processor.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
            answer = answer.strip()
            new_tokens = out.shape[1] - inputs["input_ids"].shape[1]
            say(f'question: "{question}"')
            say(f'answer:   "{answer}"')
            ev.summary = f'"{answer[:160]}" ({new_tokens} tokens in {time.time() - start:.1f}s)'
        return {"artifacts": [], "observations": {"image": rel(source), "question": question, "answer": answer}}
