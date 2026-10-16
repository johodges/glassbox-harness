# Notes: making long videos

The video tool uses Wan 2.1 VACE. The model was trained on clips of up to 81 frames, which is about 5 seconds at 16 frames per second, so longer videos are built from 5-second segments.

In "continue" mode (the default), segment 1 is made from text alone. Every later segment is given the last 9 frames of the video so far, plus a mask that marks those frames as fixed, and the model generates the 72 frames that follow. The 9 overlapping frames are crossfaded, so each segment adds 4.5 seconds. A 40-second video needs 9 segments. On a 16 GB laptop GPU each segment takes about 12 minutes; the full 40-second video took 109 minutes.

In "loop" mode, one 5-second clip is played forward and backward until it fills the requested length. It takes about 12 minutes in total, but the action repeats.

The LLM writes a storyboard: one short sentence per 5 seconds. A 3B model sometimes mixes narration or advice into the storyboard, and later segments can drift away from the opening shot.

A plain-Python repeat check compares small grey copies of frames at least 2 seconds apart. In a looped video, matching frames differ by 0.12 on a scale of 0 to 255; real motion differs by about 6.
