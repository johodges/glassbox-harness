#!/usr/bin/env bash
# One-time setup on Linux or macOS, using plain Python + venv (no Anaconda).
#   bash setup.sh
# Needs Python 3.12, 3.13 or 3.14 on the PATH (python3.12, python3.13, ...), or set PYTHON=/path/to/python3.
# Linux: an NVIDIA GPU with a recent driver; PyTorch comes from PyTorch's CUDA index (override with TORCH_INDEX).
# macOS: no CUDA; the harness runs on the CPU (see the README's Quick start for which models to download).
set -euo pipefail
cd "$(dirname "$0")"

# The Python to build .venv from: $PYTHON if set, else the first of python3.12 (the version tested), python3.13,
# python3.14 and python3 on the PATH.
if [ -z "${PYTHON:-}" ]; then
    for candidate in python3.12 python3.13 python3.14 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then PYTHON=$candidate; break; fi
    done
fi
if [ ! -d .venv ]; then
    if ! "${PYTHON:-python3}" -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] <= (3, 14) else 1)' 2>/dev/null; then
        echo "Needs Python 3.12, 3.13 or 3.14 (found: ${PYTHON:-none}). Install one, or set PYTHON=/path/to/python3.12." >&2
        exit 1
    fi
    "$PYTHON" -m venv .venv
fi
PY=.venv/bin/python
"$PY" -c "import sys; print('Python', sys.version.split()[0], 'in .venv')"
"$PY" -m pip install --upgrade pip

# PyTorch first: the CUDA build comes from PyTorch's own package index, not PyPI.
if [ "$(uname)" = "Darwin" ]; then
    "$PY" -m pip install torch torchvision
else
    "$PY" -m pip install torch torchvision --index-url "${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
fi

# kokoro (speech) and misaki, its pronunciation library, declare Python < 3.13 but work on 3.13 and 3.14: from 3.13
# on, pip is told to install them anyway.
IGNORE=""
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'; then IGNORE="--ignore-requires-python"; fi

# Everything else. The constraint file stops pip from swapping in a different torch build.
"$PY" -m pip freeze | grep -i '^torch' > torch-constraint.txt
"$PY" -m pip install -r requirements.txt -c torch-constraint.txt $IGNORE
rm torch-constraint.txt

"$PY" -c "import torch; print('CUDA available:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '(the harness will run on the CPU)')"

# The LaTeX tools run TeX Live's programs; nothing else needs them
MISSING=""
for tool in latexmk pdflatex bibtex latexdiff; do command -v "$tool" > /dev/null || MISSING="$MISSING $tool"; done
if [ -n "$MISSING" ]; then echo; echo "Not on the PATH:$MISSING. The LaTeX tools need TeX Live (see the README)."; fi

echo
echo "Next: download the models (~65 GB, once):   .venv/bin/python download_models.py"
echo "Then try:   .venv/bin/python run_agent.py \"Paint a watercolor fox and narrate a one-line story about it\""
