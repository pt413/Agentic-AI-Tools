'''import numpy as np
from PIL import Image
import torch
from transformers import CLIPProcessor, CLIPModel

model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

LABELS = [
    "an Indian Aadhaar identity card",
    "an Indian PAN card",
    "an Indian driving licence card",
    "an Indian passport identity page",
    "a plastic government identity card",

    "a selfie photo of a person",
    "a food image",
    "a dog",
    "a car",
    "a landscape",

    "a printed document page",
    "a book page full of text",
    "a scanned article page",
    "random photo"
]

DOC_LABELS = [
    "an Indian Aadhaar identity card",
    "an Indian PAN card",
    "an Indian driving licence card",
    "an Indian passport identity page",
    "a plastic government identity card"
]

def validate_document(image_np, threshold=0.30):

    try:
        image = Image.fromarray(image_np).convert("RGB")

        inputs = processor(
            text=LABELS,
            images=image,
            return_tensors="pt",
            padding=True
        )

        # 🔥 FORCE CPU
        inputs = {k: v.to("cpu") for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        probs = outputs.logits_per_image.softmax(dim=1).cpu().numpy()[0]

        # 🔥 BETTER: use max instead of sum
        doc_score = max(
            float(probs[i])
            for i, label in enumerate(LABELS)
            if label in DOC_LABELS
        )

        is_doc = doc_score > threshold

        print("CLIP SCORE:", round(doc_score, 3), "VALID:", is_doc)

        return is_doc, doc_score

    except Exception as e:
        print("CLIP VALIDATION ERROR:", str(e))
        return False, 0.0'''








import numpy as np
from PIL import Image
import torch
from transformers import CLIPProcessor, CLIPModel

model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

LABELS = [
    "an Indian Aadhaar identity card",
    "an Indian PAN card",
    "an Indian driving licence card",
    "an Indian passport identity page",
    "a plastic government identity card",

    "a selfie photo of a person",
    "a food image",
    "a dog",
    "a car",
    "a landscape",

    "a printed document page",
    "a book page full of text",
    "a scanned article page",
    "random photo"
]

DOC_LABELS = [
    "an Indian Aadhaar identity card",
    "an Indian PAN card",
    "an Indian driving licence card",
    "an Indian passport identity page",
    "a plastic government identity card"
]

def validate_document(image_np, threshold=0.20):
    try:
        image = Image.fromarray(image_np).convert("RGB")

        inputs = processor(
            text=LABELS,
            images=image,
            return_tensors="pt",
            padding=True
        )

        inputs = {k: v.to("cpu") for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        probs = outputs.logits_per_image.softmax(dim=1).cpu().numpy()[0]

        doc_score = max(
            float(probs[i])
            for i, label in enumerate(LABELS)
            if label in DOC_LABELS
        )

        # Relaxed validation:
        # reject only when score is below 0.20
        is_doc = doc_score >= threshold

        print("CLIP SCORE:", round(doc_score, 3), "VALID:", is_doc)

        return is_doc, float(doc_score)

    except Exception as e:
        print("CLIP VALIDATION ERROR:", str(e))
        return False, 0.0
    
