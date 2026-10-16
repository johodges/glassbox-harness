# One-time setup of the local TRELLIS server (no Anaconda; about 20 GB of disk and 30-60 minutes).
#   1. creates a WSL distro called "trellis" (Ubuntu 22.04), separate from the rest of this PC
#   2. runs install.sh inside it: CUDA compiler, a Python venv, TRELLIS, its GPU extensions, the weights
# To remove all of it again:  wsl --unregister trellis
$ErrorActionPreference = "Stop"
$env:WSL_UTF8 = "1"  # makes wsl.exe print plain UTF-8 instead of UTF-16

$distros = wsl --list --quiet | ForEach-Object { $_.Trim() }
if ($distros -notcontains "trellis") {
    wsl --install Ubuntu-22.04 --name trellis --no-launch
}
wsl -d trellis -u root --cd "$PSScriptRoot" -- bash ./install.sh
exit $LASTEXITCODE
