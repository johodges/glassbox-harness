"""
Run the teaching harness from the command line.

    python run_agent.py "Paint a watercolor fox in a snowy forest and narrate a one-line story about it"
    python run_agent.py --verbose 2 "Compose a calm lo-fi piano tune"
    python run_agent.py --no-router "What is the capital of France?"
    python run_agent.py "Create a 3D model of a vintage brass teapot"   # TRELLIS on this PC (trellis_local/)
    python run_agent.py --trellis remote "Create a 3D model of a teapot"  # TRELLIS on Hugging Face instead
    python run_agent.py --no-tool-guard "Using the reference notes, update slide 3 of ..."  # see an injection get in
    python run_agent.py --yes "Run workspace/scripts/word_count.py"   # don't ask before running code
    python run_agent.py --no-agents-md --no-skills "Read aloud: hello class"  # without the instruction files
    python run_agent.py --effort high "A fire burns at 2 MW for 10 minutes. How much energy, in MJ?"  # 5 tries, a vote
    python run_agent.py "Run the tests in workspace/code_demo and fix the code so that the failing test passes."  # Qwen plans
    python run_agent.py --llm llm "Run the tests in workspace/code_demo and tell me which test fails."  # Llama, pinned
    python run_agent.py --precision nf4 "Read aloud: hello class"   # the planner's weights in 4 bits: 2.4 GB, not 6.4
    python run_agent.py                      # interactive: type prompts, models stay loaded between them
"""

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # phonemes and box characters on Windows

from harness import Agent, HarnessConfig
from harness.config import PRECISIONS


def main():
    p = argparse.ArgumentParser(description="A transparent agent harness: watch data flow between models.")
    p.add_argument("prompt", nargs="*", help="what you want (leave empty for interactive mode)")
    p.add_argument("--verbose", "-v", type=int, default=1, choices=[0, 1, 2],
                   help="0 = answer only, 1 = every stage (default), 2 = also full prompts every step")
    p.add_argument("--no-guard", action="store_true", help="skip the prompt-injection classifier")
    p.add_argument("--no-tool-guard", action="store_true",
                   help="don't check the passages and files tools read for instructions (indirect injection)")
    p.add_argument("--yes", action="store_true", help="approve every tool call without asking (default: ask y/n "
                                                      "before run_script runs code)")
    p.add_argument("--no-router", action="store_true", help="offer every tool to the LLM, no classifier")
    p.add_argument("--no-agents-md", action="store_true", help="leave workspace/AGENTS.md out of the system prompt")
    p.add_argument("--no-skills", action="store_true", help="never load a workspace/skills/*/SKILL.md")
    p.add_argument("--tools", help="comma-separated subset, e.g. generate_image,generate_speech")
    p.add_argument("--effort", choices=["low", "medium", "high", "max"], default=HarnessConfig.effort,
                   help="how much work a request gets: low = small budget, no checks; medium = the default; high = also "
                        "a vote over 5 answers; max = also think first and review (see EFFORT in harness/config.py)")
    p.add_argument("--llm", choices=["llm", "code_llm"], default=None,
                   help="one planner for every request: llm = Llama 3.2 3B, code_llm = Qwen2.5-Coder 7B in 4 bits (see "
                        "MODELS). Without it, Llama plans and the PLANNER stage hands code requests to Qwen")
    p.add_argument("--precision", choices=list(PRECISIONS), default=HarnessConfig.llm_precision,
                   help="the planner's weights: bf16 = 16 bits (default), int8 = 8 bits, nf4 = 4 bits (bitsandbytes, "
                        "quantized while loading; needs an NVIDIA GPU; see compare_quantization.py)")
    p.add_argument("--check-types", action="store_true", help="check_code also runs mypy, a type checker")
    p.add_argument("--max-steps", type=int, default=None, help="max LLM calls per request (default: from --effort)")
    p.add_argument("--temperature", type=float, default=0.0, help="0 = deterministic (greedy) decoding")
    p.add_argument("--seed", type=int, default=1234, help="seed for the generators")
    p.add_argument("--trellis", choices=["remote", "local"], default=HarnessConfig.trellis_backend,
                   help="where the 3D tool runs TRELLIS: local = your own server in WSL (default, started "
                        "automatically; see trellis_local/), remote = the Hugging Face Space")
    p.add_argument("--open", action="store_true", help="open the HTML report in a browser afterwards")
    p.add_argument("--no-color", action="store_true")
    a = p.parse_args()

    config = HarnessConfig(verbosity=a.verbose, use_guard=not a.no_guard, guard_tool_outputs=not a.no_tool_guard,
                           ask_before_acting=not a.yes, use_router=not a.no_router,
                           use_agents_md=not a.no_agents_md, use_skills=not a.no_skills,
                           effort=a.effort, max_steps=a.max_steps, temperature=a.temperature, seed=a.seed,
                           open_report=a.open, llm=a.llm or HarnessConfig.llm, route_planner=a.llm is None, llm_precision=a.precision, check_types=a.check_types,
                           color=not a.no_color, trellis_backend=a.trellis)
    if a.tools:
        config.enabled_tools = [t.strip() for t in a.tools.split(",")]
    agent = Agent(config)

    if a.prompt:
        agent.run(" ".join(a.prompt))
        return
    print("\nInteractive mode. Type a request (empty line or 'quit' to exit).")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if prompt.lower() in ("", "quit", "exit"):
            break
        agent.run(prompt)


if __name__ == "__main__":
    main()
