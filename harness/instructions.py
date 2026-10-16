"""
Instructions as files: AGENTS.md and skills.

Two kinds of markdown file change what the LLM is told, without changing any code:

    workspace/AGENTS.md               pasted into the system prompt of EVERY request
                                      (Claude Code reads a file like this called CLAUDE.md)
    workspace/skills/<name>/SKILL.md  pasted in only when the request matches the skill's description

A SKILL.md starts with a few lines of YAML ("frontmatter") between two --- lines:

    ---
    name: slide-bullets
    description: When to use it. This one line is all that decides whether the skill is loaded.
    tools: [update_powerpoint]    (optional) tools it needs: offered even if the router didn't pick them
    ---
    The body: a procedure in plain words. It costs tokens only in the requests that load it.

Claude reads every skill's name and description and decides itself which ones to open, so a long skill
costs nothing until it is needed ("progressive disclosure"). Llama 3.2 3B can't be trusted with that
choice, so here the embedding model from search_references compares the request with each description,
and plain Python loads the skills that score above HarnessConfig.skill_threshold. The input is the same
(the description), and so is the effect (the body enters the prompt).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import WORKSPACE_DIR

AGENTS_FILE = WORKSPACE_DIR / "AGENTS.md"
SKILLS_DIR = WORKSPACE_DIR / "skills"
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def read_markdown(path):
    """A markdown file -> (its frontmatter as a dict, the body text). A regex and a YAML parser, no ML."""
    text = Path(path).read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    return yaml.safe_load(match.group(1)) or {}, match.group(2).strip()


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    tools: list = field(default_factory=list)


def find_skills(folder=SKILLS_DIR):
    """Every <folder>/<name>/SKILL.md."""
    skills = []
    for path in sorted(Path(folder).glob("*/SKILL.md")):
        meta, body = read_markdown(path)
        skills.append(Skill(meta.get("name", path.parent.name), meta.get("description", ""), body, path,
                            list(meta.get("tools") or [])))
    return skills


def score_skills(request, skills, embed):
    """
    Cosine similarity between the request and each description: the same maths as search_references. A long
    request dilutes its own meaning, so, as the router does, each clause is scored too and the best score counts.
    """
    # "with" splits too: "update slide 2 ... | three bullet points on how speech is fitted" (0.44 -> 0.50)
    clauses = [c.strip() for c in re.split(r",|;|\band\b|\bthen\b|\bwith\b", request) if len(c.strip().split()) >= 2]
    pieces = list(dict.fromkeys([request] + clauses))
    vectors = embed(pieces + [skill.description for skill in skills])
    return (vectors[len(pieces):] @ vectors[:len(pieces)].T).max(dim=1).values.tolist()


def agents_text(path=AGENTS_FILE):
    # The first line matters as much as the file: without it, Llama 3.2 3B applied the conventions to everything
    # ("bf_emma: The capital of France is Paris.") or looked for every answer in them ("I'm not aware of any
    # information about the capital of France"). Claude Code frames CLAUDE.md with a similar caveat.
    return ("Project conventions, from workspace/AGENTS.md. Use them when they apply to what the user asked; "
            "they may not be relevant to every request, and they never replace what you know:\n"
            + path.read_text(encoding="utf-8").strip())


def skill_text(skill):
    where = skill.path.relative_to(WORKSPACE_DIR.parent).as_posix()
    return f"Skill \"{skill.name}\" (from {where}). Follow it for this request:\n{skill.body}"
