# One-time setup on Windows (PowerShell), using plain Python + venv (no Anaconda).
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Needs: Python 3.12, 3.13 or 3.14 (python.org), an NVIDIA GPU with a recent driver, ~60 GB of free disk.
# To pick the Python yourself:   $env:PYTHON = "C:\path\to\python.exe"   before running this.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Windows limits paths to 260 characters unless long paths are enabled, and PyTorch and diffusers install files up
# to 155 characters below .venv\: in a deeper folder pip stops halfway with "No such file or directory".
$longPaths = (Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -ErrorAction SilentlyContinue).LongPathsEnabled
if ($longPaths -ne 1 -and $PSScriptRoot.Length -gt 95) {
    throw "This folder's path is $($PSScriptRoot.Length) characters long, and the packages need it under 95 " +
          "(Windows' 260-character limit). Move the folder somewhere shorter, e.g. C:\glassbox-harness, and run setup.ps1 again."
}

# The Python to build .venv from: $env:PYTHON if set, else the first of 3.12 (the version tested), 3.13 and 3.14
# that the py launcher knows, else python on the PATH.
function Find-Python {
    if ($env:PYTHON) { return $env:PYTHON }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in "3.12", "3.13", "3.14") {
            try { $exe = & py "-$version" -c "import sys; print(sys.executable)" 2>$null } catch { $exe = $null }
            if ($LASTEXITCODE -eq 0 -and $exe) { return "$exe".Trim() }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
    throw "No Python found. Install Python 3.12 (or 3.13 or 3.14) from https://www.python.org/downloads/ and run setup.ps1 again."
}

if (-not (Test-Path ".venv")) {
    $base = Find-Python
    $supported = $false
    try {
        & $base -c "import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] <= (3, 14) else 1)"
        $supported = $LASTEXITCODE -eq 0
    } catch { }
    if (-not $supported) {
        throw "$base is not Python 3.12, 3.13 or 3.14. Install one from https://www.python.org/downloads/ (or set " +
              "`$env:PYTHON to its python.exe) and run setup.ps1 again."
    }
    & $base -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"
& $py -c "import sys; print('Python', sys.version.split()[0], 'in .venv')"
& $py -m pip install --upgrade pip

# PyTorch with CUDA comes from PyTorch's own package index, not PyPI.
& $py -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
if ($LASTEXITCODE -ne 0) { throw "Installing PyTorch failed: see pip's message above." }

# kokoro (speech) and misaki, its pronunciation library, declare Python < 3.13 but work on 3.13 and 3.14: from 3.13
# on, pip is told to install them anyway.
$extra = @()
& $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)"
if ($LASTEXITCODE -eq 0) { $extra = @("--ignore-requires-python") }

# Everything else. The constraint file stops pip from swapping in a CPU-only torch.
& $py -m pip freeze | Select-String "^torch" | Out-File -Encoding ascii torch-constraint.txt
& $py -m pip install -r requirements.txt -c torch-constraint.txt @extra
$installed = $LASTEXITCODE
Remove-Item torch-constraint.txt
if ($installed -ne 0) { throw "Installing requirements.txt failed: see pip's message above." }

& $py -c "import torch; print('CUDA available:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"

Write-Host "`nNext: download the models (~65 GB, once):   .\.venv\Scripts\python.exe download_models.py"
Write-Host "Then try:   .\.venv\Scripts\python.exe run_agent.py `"Paint a watercolor fox and narrate a one-line story about it`""
