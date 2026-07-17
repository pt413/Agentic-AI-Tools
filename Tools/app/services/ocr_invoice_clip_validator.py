import numpy as np
import torch
from PIL import Image
from transformers import CLIPProcessor, CLIPModel

model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

LABELS = [
    "an invoice document",
    "a tax invoice",
    "a bill document",
    "a receipt document",
    "a purchase order document",
    "a document page",
    "a selfie photo",
    "a portrait photo",
    "a random image",
    "a food image",
    "a car image",
    "a landscape photo",
]

INVOICE_LABELS = {
    "an invoice document",
    "a tax invoice",
    "a bill document",
    "a receipt document",
}

def validate_invoice_image(image_np, threshold: float = 0.20):
    """
    CLIP disabled.
    Always allow image to continue into OCR pipeline.
    """
    return {
        "is_invoice": True,
        "clip_score": 1.0,
        "threshold": threshold,
        "top_label": "clip_disabled",
        "top_score": 1.0,
        "all_scores": {},
    }

'''def validate_invoice_image(image_np, threshold: float = 0.20):
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
        label_probs = {label: float(probs[i]) for i, label in enumerate(LABELS)}

        clip_score = max(label_probs[label] for label in INVOICE_LABELS)
        top_label = max(label_probs, key=label_probs.get)
        top_score = label_probs[top_label]

        return {
            "is_invoice": clip_score >= threshold,
            "clip_score": round(clip_score, 4),
            "threshold": threshold,
            "top_label": top_label,
            "top_score": round(top_score, 4),
            "all_scores": {k: round(v, 4) for k, v in label_probs.items()},
        }

    except Exception as e:
        return {
            "is_invoice": False,
            "clip_score": 0.0,
            "threshold": threshold,
            "top_label": None,
            "top_score": 0.0,
            "error": str(e),
        }'''