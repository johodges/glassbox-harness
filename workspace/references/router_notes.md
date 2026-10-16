# Notes: the tool router

The router is a zero-shot classifier (DeBERTa-v3, about 184 million parameters) that runs before the LLM. For every tool it asks whether the request implies a sentence such as "This request asks for a picture, image, drawing, painting or photo." Tools that score above 0.5 are described to the LLM; the others stay hidden.

Small models are eager tool users. With the router turned off, Llama 3.2 3B answered "What is the capital of France?" by drawing the Eiffel Tower. With the router on, no tool scored above 0.5, the LLM saw no tools, and it answered "Paris".

At first the router scored only the whole request. For "Draw a cartoon robot and compose a short upbeat tune for it", music scored 0.011, so the music tool was never offered. Scoring each clause separately raised music to 0.933, and both tools ran.

A tool can have several label sentences. The combine tool needed two: "putting sound onto a video" and "a video and its audio synchronized or of the same length". One sentence alone missed half the test prompts.

For files, a plain-Python rule runs before the classifier: a request that names a .pptx, .docx, .tex or .py file is always offered the tool for that file type.
