# Settings shared by install.sh and start.sh (sourced, not run).
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=$CUDA_HOME/bin:$PATH
export HF_HOME=/opt/trellis/cache/huggingface   # TRELLIS-image-large weights
export TORCH_HOME=/opt/trellis/cache/torch      # DINOv2 (downloaded through torch.hub)
export U2NET_HOME=/opt/trellis/cache/u2net      # rembg's background-removal model
export ATTN_BACKEND=xformers                    # TRELLIS defaults to flash-attn, which is not installed
export SPCONV_ALGO=native                       # skip spconv's slow auto-tuning on the first run
export PYTHONPATH=/opt/trellis/TRELLIS
