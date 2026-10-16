"""Plain-Python helpers for video files: read, write, and stretch to a length. No ML in here."""

import numpy as np


def read_video(path):
    """MP4 -> (frames as a (n, height, width, 3) uint8 array, frames per second)."""
    import imageio.v3 as iio
    frames = np.stack(list(iio.imiter(str(path))))
    return frames, iio.immeta(str(path))["fps"]


def write_video(frames, path, fps):
    """uint8 frames -> H.264 MP4 that browsers and PowerPoint can play."""
    import imageio.v2 as imageio
    imageio.mimwrite(str(path), list(frames), fps=fps, codec="libx264", quality=7, macro_block_size=16)


def join_with_crossfade(video, segment, overlap):
    """
    Append a segment whose first `overlap` frames re-create the end of `video` (it was generated
    from them). Blending across that overlap hides any small jump in colour or detail at the seam.
    """
    weights = np.linspace(0, 1, overlap + 2)[1:-1].reshape(-1, 1, 1, 1)
    seam = (video[-overlap:] * (1 - weights) + segment[:overlap] * weights).round().astype(np.uint8)
    return np.concatenate([video[:-overlap], seam, segment[overlap:]])


def stretch_frames(frames, n):
    """Make a clip n frames long by repeating or dropping frames evenly: a slight slow-down or speed-up."""
    return frames[np.linspace(0, len(frames) - 1, n).round().astype(int)]


def fit_frames(frames, n):
    """
    Make a clip exactly n frames long: trim it, or loop it forward then backward ("ping-pong"),
    so there is no jump back to the first frame at the seam.
    """
    if n <= len(frames):
        return frames[:n]
    cycle = np.concatenate([frames, frames[-2:0:-1]]) if len(frames) > 2 else frames
    return np.concatenate([cycle] * -(-n // len(cycle)))[:n]
