#!/usr/bin/env bash
# Starts the local TRELLIS server. start.ps1 runs this inside the "trellis" WSL distro.
cd "$(dirname "$0")"
source ./env.sh
export HF_HUB_OFFLINE=1           # the weights are already in /opt/trellis/cache: no internet needed
export PYTHONWARNINGS=ignore      # hide library deprecation warnings, so the window shows the progress
export TRANSFORMERS_VERBOSITY=error  # TRELLIS imports transformers, but its image pipeline never uses it
exec /opt/trellis/venv/bin/python -u server.py  # -u: print progress immediately
