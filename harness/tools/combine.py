import subprocess

import soundfile as sf

from .base import Tool, find_file
from .media import fit_frames, read_video, stretch_frames, write_video


class CombineTool(Tool):
    """A tool with no model in it at all: just code. To the LLM it looks like any other tool."""

    name = "combine_video_and_audio"
    description = ("Put an existing audio file (narration or music) onto an existing video, making ONE MP4 that "
                   "plays both together. Use it after generate_video and generate_speech or generate_music when the "
                   "user wants the video and the sound together, in sync, the same length, or as one file. The "
                   "video is fitted to the length of the audio.")
    parameters = {
        "type": "object",
        "properties": {
            "video_path": {"type": "string", "description": "The MP4 file made by generate_video."},
            "audio_path": {"type": "string", "description": "The WAV file made by generate_speech or generate_music."},
        },
        "required": ["video_path", "audio_path"],
    }
    model_id = "imageio + ffmpeg (no ML)"
    vram_gb = 0.0
    how_it_works = ("No model here: plain Python reads the video's frames and the audio's length, fits the video to "
                    "the audio (a slight retime if they are close; a loop or a trim if not), and ffmpeg writes the two "
                    "streams into one MP4 (H.264 video + AAC audio).")

    def run(self, ctx, video_path, audio_path):
        video, audio = find_file(video_path, ctx.run_dir), find_file(audio_path, ctx.run_dir)
        if video is None or audio is None:
            raise FileNotFoundError(f"cannot find {video_path if video is None else audio_path}")
        path = ctx.new_path("narrated_video", ".mp4")
        with ctx.tracer.stage("TOOL: combine_video_and_audio", "python", self.model_id, "video file + audio file",
                              "one MP4 with sound") as ev:
            ctx.tracer.say(self.how_it_works, kind="python")
            frames, fps = read_video(video)
            audio_seconds = sf.info(str(audio)).duration
            target = round(audio_seconds * fps)
            if len(frames) == target:
                fitted, change = frames, "kept as is"
            elif 0.85 <= target / len(frames) <= 1.15:  # close: retime it slightly, so the action never reverses
                fitted = stretch_frames(frames, target)
                change = f"retimed by {100 * (target / len(frames) - 1):+.0f}%"
            else:
                fitted = fit_frames(frames, target)
                change = "looped forward and backward" if len(fitted) > len(frames) else "trimmed"
            ctx.tracer.say(f"video {len(frames) / fps:.1f}s, audio {audio_seconds:.1f}s -> video {change} "
                           f"to {len(fitted) / fps:.1f}s", kind="python")
            silent = ctx.run_dir / "_silent_video.mp4"
            write_video(fitted, silent, fps)
            subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(silent), "-i", str(audio),
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                            "-shortest", str(path)], check=True)
            silent.unlink()
            note = f"one MP4 with picture and sound, {audio_seconds:.1f}s long (the video was {change} to match the audio)"
            ev.summary = note
            ev.artifacts.append(str(path))
        return {"artifacts": [{"type": "video", "path": path, "note": note}], "info": {}}


def _ffmpeg():
    import imageio_ffmpeg  # ships its own ffmpeg binary, so nothing extra to install
    return imageio_ffmpeg.get_ffmpeg_exe()
