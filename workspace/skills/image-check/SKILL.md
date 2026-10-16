---
name: image-check
description: Draw a red bicycle, a blue car or a green tractor, an object in a named colour.
tools: [generate_image, look_at_image]
---
# Check a picture before showing it

1. Call generate_image.
2. Then call look_at_image on the new image with a question that ends with "Answer yes or no.", for example "Is the bicycle red? Answer yes or no."
3. Only if the answer is no, call generate_image once more with the colour and the object at the start of the prompt. If it is yes, do not draw again.
4. In your answer, say what look_at_image saw.
