"""
A local stand-in for the trellis-community/TRELLIS Hugging Face Space.

It runs inside the "trellis" WSL distro with its own Python (see install.sh), NOT in the
harness's .venv, and it offers the same Gradio API as the Space. So the harness talks to it
with exactly the same gradio_client calls; only the address changes:

    /start_session             (nothing)                -> (nothing)
    /preprocess_image          photo                    -> object cut out of its background
    /generate_and_extract_glb  cut-out + sampler knobs  -> (turntable video, GLB mesh, GLB mesh)

Start it with trellis_local\\start.ps1, then open http://127.0.0.1:7960 to try it by hand.

GPU memory: this process cannot see the harness's models, and the harness cannot park this
process's models. So the weights wait in CPU RAM and borrow the GPU only while a request runs.
"""

import os
import tempfile
import time

import gradio as gr
import imageio
import numpy as np
import torch
from trellis.pipelines import TrellisImageTo3DPipeline
from trellis.utils import postprocessing_utils, render_utils

PORT = int(os.environ.get("TRELLIS_PORT", 7960))
OUT_DIR = tempfile.mkdtemp(prefix="trellis_")

print("Loading TRELLIS-image-large (DINOv2 image encoder + flow transformers + decoders) into CPU RAM ...")
pipeline = TrellisImageTo3DPipeline.from_pretrained("microsoft/TRELLIS-image-large")
pipeline.cpu()
print(f"Loaded. GPU: {torch.cuda.get_device_name(0)}")


def start_session():
    """The Space makes a folder per visitor here. This server has one user, so there is nothing to do."""


def preprocess_image(image):
    """rembg's u2net (on the CPU) cuts the object out of its background, then it is cropped to 518x518."""
    start = time.time()
    cut_out = pipeline.preprocess_image(image)
    print(f"[preprocess_image] background removed in {time.time() - start:.1f}s")
    return cut_out


def generate_and_extract_glb(image, multiimages, seed, ss_guidance_strength, ss_sampling_steps,
                             slat_guidance_strength, slat_sampling_steps, multiimage_algo,
                             mesh_simplify, texture_size):
    """Image -> 3D. `multiimages` and `multiimage_algo` exist only to match the Space's API."""
    start, name = time.time(), time.strftime("%H%M%S")
    pipeline.cuda()  # borrow the GPU (~4 GB of weights move from RAM to GPU memory in about a second)
    torch.cuda.reset_peak_memory_stats()
    try:
        # 1. DINOv2 features of the image steer two flow transformers: first a coarse 64^3 voxel
        #    structure ("which cells are occupied?"), then a latent feature vector for each occupied cell.
        #    Decoders turn those latents into 3D Gaussians (for the video) and a mesh (for the GLB).
        outputs = pipeline.run(
            image, seed=int(seed), formats=["gaussian", "mesh"], preprocess_image=False,
            sparse_structure_sampler_params={"steps": int(ss_sampling_steps), "cfg_strength": ss_guidance_strength},
            slat_sampler_params={"steps": int(slat_sampling_steps), "cfg_strength": slat_guidance_strength})
        gaussians, mesh = outputs["gaussian"][0], outputs["mesh"][0]

        # 2. A turntable video: the coloured Gaussians on the left, the mesh's surface normals on the right.
        color = render_utils.render_video(gaussians, num_frames=120)["color"]
        normal = render_utils.render_video(mesh, num_frames=120)["normal"]
        video_path = os.path.join(OUT_DIR, f"{name}_preview.mp4")
        imageio.mimsave(video_path, [np.concatenate([c, n], axis=1) for c, n in zip(color, normal)], fps=15)

        # 3. The GLB: simplify the mesh, unwrap it to UV space, and bake the Gaussians' colours into a texture.
        glb = postprocessing_utils.to_glb(gaussians, mesh, simplify=mesh_simplify, texture_size=int(texture_size),
                                          verbose=False)
        glb_path = os.path.join(OUT_DIR, f"{name}_model.glb")
        glb.export(glb_path)
    finally:
        peak = torch.cuda.max_memory_allocated() / 1e9
        pipeline.cpu()            # give the GPU back ...
        torch.cuda.empty_cache()  # ... including PyTorch's cached blocks, so other processes can use it
    print(f"[generate_and_extract_glb] done in {time.time() - start:.0f}s, peak GPU memory {peak:.1f} GB")
    return video_path, glb_path, glb_path


# The same components and endpoint names as the Space, with a minimal page for trying it by hand.
with gr.Blocks(title="TRELLIS (local)") as demo:
    gr.Markdown("# TRELLIS image -> 3D, running on this PC\nThe agent harness calls the same API "
                "(see *Use via API* at the bottom of the page).")
    with gr.Row():
        with gr.Column():
            image_prompt = gr.Image(label="Image Prompt", type="pil", image_mode="RGBA", format="png", height=300)
            multiimage_prompt = gr.Gallery(label="Image Prompt", type="pil", visible=False)
            with gr.Accordion("Generation settings", open=False):
                seed = gr.Slider(0, np.iinfo(np.int32).max, value=0, step=1, label="Seed")
                ss_guidance_strength = gr.Slider(0.0, 10.0, value=7.5, step=0.1, label="Stage 1 guidance strength")
                ss_sampling_steps = gr.Slider(1, 50, value=12, step=1, label="Stage 1 sampling steps")
                slat_guidance_strength = gr.Slider(0.0, 10.0, value=3.0, step=0.1, label="Stage 2 guidance strength")
                slat_sampling_steps = gr.Slider(1, 50, value=12, step=1, label="Stage 2 sampling steps")
                multiimage_algo = gr.Radio(["stochastic", "multidiffusion"], value="stochastic", visible=False)
                mesh_simplify = gr.Slider(0.9, 0.98, value=0.95, step=0.01, label="Mesh simplify")
                texture_size = gr.Slider(512, 2048, value=1024, step=512, label="Texture size")
            generate_btn = gr.Button("Generate")
        with gr.Column():
            video_output = gr.Video(label="Generated 3D Asset", autoplay=True, loop=True, height=300)
            model_output = gr.Model3D(label="Extracted GLB", height=300)
            download_glb = gr.DownloadButton(label="Download GLB")

    demo.load(start_session, api_name="start_session")
    image_prompt.upload(preprocess_image, inputs=[image_prompt], outputs=[image_prompt], api_name="preprocess_image")
    generate_btn.click(
        generate_and_extract_glb,
        inputs=[image_prompt, multiimage_prompt, seed, ss_guidance_strength, ss_sampling_steps,
                slat_guidance_strength, slat_sampling_steps, multiimage_algo, mesh_simplify, texture_size],
        outputs=[video_output, model_output, download_glb], api_name="generate_and_extract_glb")

if __name__ == "__main__":
    # 127.0.0.1 inside WSL is forwarded to 127.0.0.1 on Windows, and to nothing else.
    demo.queue(default_concurrency_limit=1).launch(server_name="127.0.0.1", server_port=PORT,
                                                    allowed_paths=[OUT_DIR], show_error=True)
