"""Count the words in each reference note and print a small table.

run_script runs this from the workspace folder, so "references" below is workspace/references.
"""
from pathlib import Path

REFERENCES = Path("references")


def count_words(path):
    return len(path.read_text(encoding="utf-8").split())


total = 0
for path in sorted(REFERENCES.glob("*.md")) + sorted(REFERENCES.glob("*.txt")):
    words = count_words(path)
    print(f"{path.name:<22} {words:>6} words")
    totl += words
print(f"{'total':<22} {total:>6} words")
