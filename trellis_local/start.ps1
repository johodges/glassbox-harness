# Starts the local TRELLIS server and keeps it running in this window (Ctrl+C stops it).
# It runs inside the "trellis" WSL distro made by setup.ps1 and listens on http://127.0.0.1:7960.
# Then run the harness with:  .\.venv\Scripts\python.exe run_agent.py --trellis local "Create a 3D model of ..."
wsl -d trellis -u root --cd "$PSScriptRoot" -- bash ./start.sh
