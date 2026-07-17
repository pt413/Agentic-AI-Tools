import json
import re
import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"


class QwenVLInvoiceService:
    _instance = None

    def __init__(self):
        print("🚀 Loading Qwen VL Invoice model (ONE TIME)...")

        self.processor = AutoProcessor.from_pretrained(
            MODEL_ID,
            trust_remote_code=True,
        )

        if torch.cuda.is_available():
            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                MODEL_ID,
                torch_dtype=torch.float16,
                device_map="auto",
                trust_remote_code=True,
            )
            self.device = "cuda"
        else:
            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                MODEL_ID,
                torch_dtype=torch.float32,
                trust_remote_code=True,
            ).to("cpu")
            self.device = "cpu"

        self.model.eval()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = QwenVLInvoiceService()
        return cls._instance

    def _generate_from_image(self, image_path: str, prompt: str, max_new_tokens: int = 900) -> str:
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((1400, 1400))

        messages = [{
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": prompt},
            ],
        }]

        text_input = self.processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        inputs = self.processor(
            text=text_input,
            images=[image],
            return_tensors="pt",
        )

        inputs = {
            k: v.to(self.device) if hasattr(v, "to") else v
            for k, v in inputs.items()
        }

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )

        generated_ids = output_ids[0][inputs["input_ids"].shape[1]:]
        text = self.processor.decode(generated_ids, skip_special_tokens=True)
        return text.strip()

    def _parse_json_output(self, raw: str) -> dict:
        if not raw:
            return {
                "document_type": "invoice",
                "parse_error": "Empty response from model",
                "raw_output": raw,
            }

        cleaned = raw.strip()

        # remove markdown fences if present
        cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^```\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            return json.loads(cleaned)
        except Exception:
            pass

        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass

        return {
            "document_type": "invoice",
            "parse_error": "Model did not return valid JSON",
            "raw_output": raw,
        }

    def extract_invoice_details(self, image_path: str) -> dict:
        prompt = """
You are an invoice extraction system.

Extract the invoice into STRICT JSON only.
Do not explain anything.
Do not include markdown fences.
If a field is missing, use null.
If line items are unclear, return an empty array.

Return JSON with exactly this schema:
{
  "document_type": "invoice",
  "vendor_name": null,
  "vendor_address": null,
  "vendor_gstin": null,
  "buyer_name": null,
  "buyer_address": null,
  "buyer_gstin": null,
  "invoice_number": null,
  "invoice_date": null,
  "due_date": null,
  "po_number": null,
  "currency": null,
  "subtotal": null,
  "tax_amount": null,
  "discount_amount": null,
  "shipping_amount": null,
  "total_amount": null,
  "payment_terms": null,
  "line_items": [
    {
      "description": null,
      "quantity": null,
      "unit_price": null,
      "tax_rate": null,
      "line_total": null
    }
  ]
}
"""
        raw = self._generate_from_image(
            image_path=image_path,
            prompt=prompt,
            max_new_tokens=900
        )

        return self._parse_json_output(raw)