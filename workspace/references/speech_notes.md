# Notes: fitting speech to a length

The speech tool uses Kokoro-82M, a text-to-speech model with 82 million parameters. At normal speed it speaks about 2.5 words per second, so 40 seconds of speech needs about 100 words.

When a length is requested, the tool measures the audio and re-speaks it at between 0.8 and 1.25 times normal speed, up to three times, until it is within 2% of the target. Outside that range the voice starts to sound unnatural.

If the text is still more than 20% too short, or more than 5% too long, the tool returns an error that tells the LLM how many words to write. In the wildfire test the LLM wrote 24 words, then 60, then 89; the third version filled exactly 40 seconds.

Padding with silence has a cost. When 52 words left 15.3 seconds of silence, the Whisper speech recognizer that checks the audio "heard" a sentence that was never spoken.
