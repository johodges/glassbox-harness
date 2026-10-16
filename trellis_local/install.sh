#!/usr/bin/env bash
# Builds a local TRELLIS image-to-3D server inside the "trellis" WSL distro (Ubuntu 22.04).
# setup.ps1 runs this as root. Everything lands in /opt/trellis, nothing touches the harness's .venv:
#
#   /opt/trellis/TRELLIS   Microsoft's TRELLIS code (pinned commit)
#   /opt/trellis/venv      Python 3.10 + PyTorch 2.4.0/CUDA 12.1, the versions TRELLIS was released with
#   /opt/trellis/cache     model weights (TRELLIS-image-large, DINOv2, the background remover)
#
# Safe to re-run: finished steps are skipped or quick.
set -euo pipefail

ROOT=/opt/trellis
VENV=$ROOT/venv
PY=$VENV/bin/python
TRELLIS_COMMIT=442aa1e1afb9014e80681d3bf604e8d728a86ee7
UTILS3D_COMMIT=9a4eb15e4021b67b12c460c7057d642626897ec8
source "$(dirname "$0")/env.sh"   # CUDA, cache folders and TRELLIS settings shared with start.sh
export DEBIAN_FRONTEND=noninteractive MAX_JOBS=8

step() { echo; echo "=== $* ==="; }

step "1/6 system packages and the CUDA 12.1 compiler (nvcc) for building TRELLIS's GPU extensions"
# The GPU driver itself comes from Windows (/usr/lib/wsl/lib); only the compiler and headers are installed here.
apt-get update -qq
apt-get install -y -qq git wget python3-venv python3-dev build-essential ninja-build libgl1 libglib2.0-0 >/dev/null
if [ ! -x /usr/local/cuda-12.1/bin/nvcc ]; then
    wget -q -O /tmp/cuda-keyring.deb \
        https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
    dpkg -i /tmp/cuda-keyring.deb >/dev/null
    apt-get update -qq
    apt-get install -y -qq cuda-nvcc-12-1 cuda-cccl-12-1 cuda-nvrtc-dev-12-1 cuda-nvtx-12-1 \
        cuda-profiler-api-12-1 libcublas-dev-12-1 libcusparse-dev-12-1 libcusolver-dev-12-1 libcurand-dev-12-1 >/dev/null
fi
nvcc --version | tail -n 2

step "2/6 TRELLIS source code"
mkdir -p "$ROOT"
[ -d "$ROOT/TRELLIS" ] || git clone -q --recurse-submodules https://github.com/microsoft/TRELLIS.git "$ROOT/TRELLIS"
git -C "$ROOT/TRELLIS" checkout -q "$TRELLIS_COMMIT"
echo "TRELLIS at $(git -C "$ROOT/TRELLIS" log -1 --format='%h %cd')"

step "3/6 Python venv with PyTorch 2.4.0 + CUDA 12.1"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install -q --upgrade pip wheel setuptools
"$PY" -m pip install -q torch==2.4.0 torchvision==0.19.0 xformers==0.0.27.post2 \
    --index-url https://download.pytorch.org/whl/cu121
"$PY" -c "import torch; print('torch', torch.__version__, '| GPU:', torch.cuda.get_device_name(0))"

step "4/6 TRELLIS's Python dependencies (TRELLIS's setup.sh --basic --spconv --kaolin, plus gradio for the server)"
"$PY" -m pip install -q pillow imageio imageio-ffmpeg tqdm easydict opencv-python-headless scipy ninja \
    rembg onnxruntime trimesh open3d xatlas pyvista pymeshfix igraph transformers spconv-cu120 \
    "gradio>=6,<7"  # Gradio 6 matches the harness's gradio_client 2.x (tested with 6.30)
"$PY" -m pip install -q "git+https://github.com/EasternJournalist/utils3d.git@$UTILS3D_COMMIT"
# NVIDIA's prebuilt kaolin for this exact torch/CUDA pair (TRELLIS's mesh code imports one helper from it)
"$PY" -m pip install -q kaolin -f https://nvidia-kaolin.s3.us-east-2.amazonaws.com/torch-2.4.0_cu121.html

step "5/6 build the CUDA extensions: nvdiffrast (mesh rasteriser) and diff-gaussian-rasterization (splat renderer)"
mkdir -p "$ROOT/extensions"
if ! "$PY" -c "import nvdiffrast.torch" 2>/dev/null; then
    [ -d "$ROOT/extensions/nvdiffrast" ] || git clone -q https://github.com/NVlabs/nvdiffrast.git "$ROOT/extensions/nvdiffrast"
    "$PY" -m pip install -q --no-build-isolation "$ROOT/extensions/nvdiffrast"
fi
if ! "$PY" -c "import diff_gaussian_rasterization" 2>/dev/null; then
    [ -d "$ROOT/extensions/mip-splatting" ] || git clone -q https://github.com/autonomousvision/mip-splatting.git "$ROOT/extensions/mip-splatting"
    "$PY" -m pip install -q --no-build-isolation "$ROOT/extensions/mip-splatting/submodules/diff-gaussian-rasterization/"
fi
"$PY" -c "import nvdiffrast.torch, diff_gaussian_rasterization, spconv.pytorch, xformers; print('extensions import OK')"

step "6/6 download the weights: TRELLIS-image-large (2.9 GB), DINOv2 (1.2 GB), u2net background remover (170 MB)"
cd /tmp
"$PY" - <<'EOF'
import rembg
from trellis.pipelines import TrellisImageTo3DPipeline
TrellisImageTo3DPipeline.from_pretrained("microsoft/TRELLIS-image-large")  # also fetches DINOv2 via torch.hub
rembg.new_session("u2net")
print("weights ready")
EOF
"$PY" -m pip cache purge -q  # ~3 GB of downloaded wheels that are installed now

echo
echo "Done. Start the server from Windows with:  trellis_local\start.ps1"
