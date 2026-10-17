"""
The LLM: the only component that "thinks", and it can only read and write text.

Two stages happen on every call, and both are printed by the tracer:

  PROMPT ASSEMBLY  (plain Python)
      The list of chat messages AND the tool definitions are flattened by the model's
      chat template into ONE long string with special marker tokens like
      <|start_header_id|>user<|end_header_id|>. The tokenizer then splits that string into
      integer token ids. This is literally all the model receives: tools are not an API
      feature of the network, they are just more text in the prompt.

  LLM  (neural network)
      The model predicts one token at a time until it emits an end-of-turn token. The new
      tokens are decoded back into text. Whether that text is "a tool call" or "an answer"
      is decided afterwards, by ordinary Python code (see agent.py: parse_tool_call).

Each model family has its own template. Llama 3 puts the tools in the first user turn and writes
a call as bare JSON; Qwen (the code model, MODELS["code_llm"]) lists them inside <tools></tools>
in the system turn and writes a call between <tool_call> and </tool_call>. Same idea, other markers.
"""

import datetime
import json
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TextStreamer

from .config import FOUR_BIT, MODELS, PRECISIONS

# How bitsandbytes stores the weights at each precision below 16 bits (PRECISIONS in config.py). Only the big weight
# matrices (nn.Linear) are quantized; the embedding table, which Llama 3.2 shares with its output layer, stays bf16.
QUANTIZE = {
    # LLM.int8(): 8-bit integers with a scale per row; the few input features with large values ("outliers") are
    # multiplied in 16 bits instead
    "int8": dict(load_in_8bit=True),
    # NF4: 4-bit numbers, 16 levels spaced for bell-shaped weights, and one scale per block of 64 weights (the scales
    # themselves stored in 8 bits: "double quant")
    "nf4": dict(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16),
}


def load_llm(tracer, key="llm", device="cuda", precision="bf16"):
    """HarnessConfig.llm and .llm_precision -> the planner: MODELS[key] at that precision. A model in FOUR_BIT is too
    big for 16 bits beside the other models, so it loads in 4 bits unless int8 is asked for."""
    if key in FOUR_BIT and precision == "bf16":
        precision = "nf4"
    llm = LocalLLM(tracer, MODELS[key], device, precision=precision)
    llm.key = key  # which MODELS entry this is (the PLANNER stage in agent.py compares it with the one it wants)
    return llm


def downloaded(model_id):
    """Is the model in the Hugging Face cache, where download_models.py puts it? Loading one that isn't would start a
    download in the middle of a request (15 GB for the code model)."""
    from huggingface_hub import try_to_load_from_cache
    return isinstance(try_to_load_from_cache(model_id, "config.json"), str)


class LocalLLM:
    def __init__(self, tracer, model_id=None, device="cuda", precision="bf16", four_bit=False):
        self.tracer, self.device = tracer, device
        self.model_id = model_id or MODELS["llm"]
        # bitsandbytes needs an NVIDIA GPU; on the CPU the model loads in 32 bits
        self.precision = ("nf4" if four_bit else precision) if device == "cuda" else "float32"
        how = PRECISIONS.get(self.precision, "32 bits")
        with tracer.stage("LOAD LLM", "python", self.model_id, "weights on disk",
                          f"model on {device}, {self.precision}") as ev:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            before = torch.cuda.memory_allocated() if device == "cuda" else 0
            if self.precision in QUANTIZE:
                # Quantization: every big weight matrix is stored in 8 or 4 bits plus a scale per row or block.
                # bitsandbytes does it while loading, and turns the weights back into bf16 for each matrix multiply.
                from transformers import BitsAndBytesConfig
                quant = BitsAndBytesConfig(**QUANTIZE[self.precision])
                self.model = AutoModelForCausalLM.from_pretrained(self.model_id, quantization_config=quant,
                                                                  dtype=torch.bfloat16, device_map={"": 0}).eval()
            else:
                dtype = torch.bfloat16 if device == "cuda" else torch.float32
                self.model = AutoModelForCausalLM.from_pretrained(self.model_id, dtype=dtype).to(device).eval()
            params = self.model.num_parameters()
            # the GPU memory the weights took: what torch has allocated now, minus what the models before it took
            self.weights_gb = ((torch.cuda.memory_allocated() - before) if device == "cuda"
                               else self.model.get_memory_footprint()) / 1e9
            ev.summary = (f"{params / 1e9:.1f} billion parameters at {self.precision} ({how}), vocabulary of "
                          f"{len(self.tokenizer):,} tokens, {self.weights_gb:.2f} GB of weights"
                          + (f" (in bf16: {params * 2 / 1e9:.1f} GB)" if self.precision in QUANTIZE else ""))
            ev.details = {"precision": self.precision, "weights_gb": round(self.weights_gb, 3), "parameters": params}
        self.load_seconds = ev.seconds
        self.on_gpu = device == "cuda"
        # Quantized weights stay on the GPU (agent._make_room_on_gpu). nf4 could move to CPU RAM and back (tested with
        # bitsandbytes 0.50), but int8 can't: 2.8 of its 3.6 GB stayed on the GPU after .to("cpu").
        self.can_park = self.precision not in QUANTIZE
        self._previous_prompt = ""
        self.hit_limit = False  # did the last reply stop at max_new_tokens?
        self.date = None  # the date Llama 3's template writes into every prompt; None = today (HarnessConfig.prompt_date)

    # ---- PROMPT ASSEMBLY ----------------------------------------------------------------
    def render(self, messages, tools=None):
        """messages + tool schemas -> the single string the model actually reads."""
        if "ipython" not in self.tokenizer.chat_template:
            # Llama 3's template turns tool-result dicts into JSON itself (in an "ipython" turn);
            # other model families (e.g. Qwen) expect the harness to pass a JSON string.
            messages = [{**m, "content": json.dumps(m["content"])}
                        if m["role"] == "tool" and not isinstance(m["content"], str) else m for m in messages]
        return self.tokenizer.apply_chat_template(
            messages, tools=tools or None, add_generation_prompt=True, tokenize=False,
            date_string=self.date or datetime.date.today().strftime("%d %b %Y"))

    def tokenize(self, text):
        return self.tokenizer(text, return_tensors="pt", add_special_tokens=False).input_ids

    def show_tokens(self, text, limit=40):
        """'Paint a fox' -> [Paint][ a][ fox] with ids: how the model sees your words."""
        ids = self.tokenize(text)[0].tolist()
        pieces = "".join(f"[{self.tokenizer.decode([i])}]" for i in ids[:limit])
        more = f" ... (+{len(ids) - limit} more)" if len(ids) > limit else ""
        return f"{len(ids)} tokens: {pieces}{more}\n     ids: {ids[:limit]}{' ...' if more else ''}"

    # ---- LLM ----------------------------------------------------------------------------
    def generate(self, messages, tools=None, max_new_tokens=400, temperature=0.0):
        t = self.tracer
        with t.stage("PROMPT ASSEMBLY", "python", "tokenizer.apply_chat_template()", "messages + tool schemas",
                     "token ids") as ev:
            prompt = self.render(messages, tools)
            ids = self.tokenize(prompt)
            ev.summary = (f"{len(messages)} messages + {len(tools or [])} tool schemas -> "
                          f"{len(prompt):,} characters -> {ids.shape[1]:,} tokens")
            ev.details = {"raw_prompt": prompt, "num_tokens": ids.shape[1]}
            new_conversation = not self._previous_prompt or not prompt.startswith(self._previous_prompt)
            if new_conversation or t.verbosity >= 2:
                user_text = next(m["content"] for m in messages if m["role"] == "user")
                t.say("Your request, as the model sees it (sub-word tokens -> integer ids):", kind="python")
                t.say(self.show_tokens(user_text), kind="python", indent=7)
                t.say("The FULL text the model receives (tools are just text, too):", kind="python")
                t.say(prompt, indent=7)
            else:
                t.say("Same prompt as last time, plus this new text at the end:", kind="python")
                t.say(prompt[len(self._previous_prompt):], indent=7)
            self._previous_prompt = prompt

        self._ensure_on_gpu()
        with t.stage("LLM", "llm", self.model_id, f"{ids.shape[1]:,} tokens", "new tokens -> text") as ev:
            t.say("streaming the model's output, one token at a time:", kind="llm")
            t.write("       ")
            start = time.perf_counter()
            with torch.no_grad():
                out = self.model.generate(
                    ids.to(self.device), attention_mask=torch.ones_like(ids).to(self.device),
                    max_new_tokens=max_new_tokens,
                    do_sample=temperature > 0, temperature=temperature if temperature > 0 else None, top_p=None,
                    pad_token_id=self.tokenizer.eos_token_id,
                    streamer=_Streamer(self.tokenizer, t) if t.verbosity >= 1 else None)
            new_ids = out[0, ids.shape[1]:]
            seconds = time.perf_counter() - start
            text = self.tokenizer.decode(new_ids, skip_special_tokens=True).strip()
            t.write("\n")
            self.hit_limit = len(new_ids) >= max_new_tokens  # cut off mid-sentence (see CUT OFF in agent.py)
            ev.summary = (f"wrote {len(new_ids)} new tokens in {seconds:.1f}s ({len(new_ids) / seconds:.0f} tokens/s)"
                          + (": the limit, cut off" if self.hit_limit else ""))
            ev.details = {"output_text": text,
                          "output_with_special_tokens": self.tokenizer.decode(new_ids, skip_special_tokens=False)}
        return text

    def samples(self, messages, tools, max_new_tokens, n, temperature=0.7, seed=1234):
        """
        n answers to the same prompt in one batch, each drawn at random (temperature > 0), for VOTE in agent.py.
        Greedy decoding gives one answer every time; sampling gives n different attempts at the same question.
        """
        t = self.tracer
        prompt = self.render(messages, tools)
        ids = self.tokenize(prompt)
        self._ensure_on_gpu()
        with t.stage("LLM", "llm", self.model_id, f"{ids.shape[1]:,} tokens, {n} times", f"{n} answers") as ev:
            torch.manual_seed(seed)  # the same "random" attempts every run: good for teaching
            start = time.perf_counter()
            with torch.no_grad():
                out = self.model.generate(
                    ids.to(self.device), attention_mask=torch.ones_like(ids).to(self.device),
                    max_new_tokens=max_new_tokens, do_sample=True, temperature=temperature, top_p=0.95,
                    num_return_sequences=n, pad_token_id=self.tokenizer.eos_token_id)
            seconds = time.perf_counter() - start
            new = out[:, ids.shape[1]:]
            written = int((new != self.tokenizer.eos_token_id).sum())
            texts = [self.tokenizer.decode(row, skip_special_tokens=True).strip() for row in new]
            ev.summary = (f"wrote {written} new tokens in {seconds:.1f}s: {n} answers sampled at temperature "
                          f"{temperature}, in one batch")
            ev.details = {"output_text": "\n----\n".join(texts), "samples": texts}
        return texts

    # ---- GPU memory juggling (the agent may have parked the weights in RAM) ----------------
    def _ensure_on_gpu(self):
        if self.device == "cuda" and not self.on_gpu:
            with self.tracer.stage("GPU MEMORY", "python", "model.to('cuda')", "LLM weights in CPU RAM",
                                   "LLM weights in GPU memory") as ev:
                self.model.to(self.device)
                self.on_gpu = True
                ev.summary = f"moved the LLM back onto the GPU ({gpu_free_gb():.1f} GB still free)"


class _Streamer(TextStreamer):
    """Prints tokens as they are generated, in the tracer's LLM colour."""

    def __init__(self, tokenizer, tracer):
        super().__init__(tokenizer, skip_prompt=True, skip_special_tokens=True)
        self.tracer = tracer

    def on_finalized_text(self, text, stream_end=False):
        self.tracer.write(text.replace("\n", "\n       "), kind="llm")


def gpu_free_gb():
    return torch.cuda.mem_get_info()[0] / 1e9 if torch.cuda.is_available() else 0.0
