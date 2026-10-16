# One-time setup on Windows (PowerShell), using plain Python + venv (no Anaconda).
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Needs: Python 3.12 (python.org installer), an NVIDIA GPU with a recent driver, ~60 GB of free disk.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip

# PyTorch with CUDA comes from PyTorch's own package index, not PyPI.
& $py -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130

# Everything else. The constraint file stops pip from swapping in a CPU-only torch.
& $py -m pip freeze | Select-String "^torch" | Out-File -Encoding ascii torch-constraint.txt
& $py -m pip install -r requirements.txt -c torch-constraint.txt
Remove-Item torch-constraint.txt

& $py -c "import torch; print('CUDA available:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"

Write-Host "`nNext: download the models (~65 GB, once):   .\.venv\Scripts\python.exe download_models.py"
Write-Host "Then try:   .\.venv\Scripts\python.exe run_agent.py `"Paint a watercolor fox and narrate a one-line story about it`""
