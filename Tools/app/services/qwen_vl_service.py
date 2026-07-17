'''import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"

class QwenVLService:
    _instance = None

    def __init__(self):
        print("🚀 Loading Qwen VL model (ONE TIME)...")

        self.processor = AutoProcessor.from_pretrained(
            MODEL_ID,
            trust_remote_code=True
        )

        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.float16,
            device_map="auto",
            trust_remote_code=True,
        )

        self.model.eval()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = QwenVLService()
        return cls._instance

    def extract_name(self, image_path: str) -> str:
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((1024, 1024))

        prompt = (
            "Extract ONLY the full name from this Indian ID card.\n"
            "Return only the name. If not visible return NULL."
        )

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
            add_generation_prompt=True
        )

        inputs = self.processor(
            text=text_input,
            images=[image],
            return_tensors="pt"
        )

        inputs = {
            k: v.to(self.device) if hasattr(v, "to") else v
            for k, v in inputs.items()
        }

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=40,
                do_sample=False,
            )

        generated_ids = output_ids[0][inputs["input_ids"].shape[1]:]

        text = self.processor.decode(
            generated_ids,
            skip_special_tokens=True
        )

        return text.strip()'''







import re
import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"


def clean_output(text: str) -> str:
    if not text:
        return "NULL"

    text = text.strip()
    text = re.sub(r"(name\s*[:\-]\s*)", "", text, flags=re.IGNORECASE)

    prefixes = [
        "name:",
        "full name:",
        "person's name:",
        "the name is:",
    ]

    lower_text = text.lower()
    for prefix in prefixes:
        if lower_text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    text = text.strip("\"' ")
    text = text.split("\n")[0].strip()
    text = re.sub(r"\s+", " ", text)

    if text.lower() in {"null", "none", "not visible", "not found", "unknown", ""}:
        return "NULL"

    return text


class QwenVLService:
    _instance = None

    def __init__(self):
        print("🚀 Loading Qwen VL model (ONE TIME)...")

        self.processor = AutoProcessor.from_pretrained(
            MODEL_ID,
            trust_remote_code=True
        )

        if torch.cuda.is_available():
            print("✅ Using GPU")
            try:
                self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    MODEL_ID,
                    torch_dtype=torch.float16,
                    device_map="auto",
                    trust_remote_code=True,
                    attn_implementation="flash_attention_2",
                )
            except Exception:
                print("⚠️ Flash Attention not available, falling back...")
                self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    MODEL_ID,
                    torch_dtype=torch.float16,
                    device_map="auto",
                    trust_remote_code=True,
                )
            self.device = "cuda"
        else:
            print("⚠️ Using CPU")
            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                MODEL_ID,
                torch_dtype=torch.float32,
                trust_remote_code=True,
            ).to("cpu")
            self.device = "cpu"

        self.model.eval()

        if self.device == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = True

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = QwenVLService()
        return cls._instance

    #def extract_name(self, image_path: str) -> str:
    def extract_name(self, image_path: str, prompt: str = None) -> str:    
        image = Image.open(image_path).convert("RGB")
        #image.thumbnail((1024, 1024))
        image.thumbnail((1400, 1400))

        '''prompt = (
            "You are an OCR system specialized in Indian ID cards.\n"
            "Extract ONLY the full name of the person.\n"
            "Rules:\n"
            "- Return only the name\n"
            "- Do not return labels like Name:\n"
            "- Do not return father name or other text\n"
            "- Do not explain\n"
            "- Do not output symbols or punctuation-only text\n"
            "- If no name is visible, return NULL\n"
        )'''
        if not prompt:
            prompt = (
                "You are an OCR system specialized in Indian ID cards.\n"
                "Extract ONLY the full name of the person.\n"
                "Rules:\n"
                "- Return only the name\n"
                "- Do not return labels like Name:\n"
                "- Do not return father name or other text\n"
                "- Do not explain\n"
                "- Do not output symbols or punctuation-only text\n"
                "- If no name is visible, return NULL\n"
            )

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
            add_generation_prompt=True
        )

        inputs = self.processor(
            text=text_input,
            images=[image],
            return_tensors="pt"
        )

        inputs = {
            k: v.to(self.device) if hasattr(v, "to") else v
            for k, v in inputs.items()
        }

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=40,
                do_sample=False,
            )

        generated_ids = output_ids[0][inputs["input_ids"].shape[1]:]

        text = self.processor.decode(
            generated_ids,
            skip_special_tokens=True
        )

        text = clean_output(text)

        # extra safety
        if text == "NULL":
            return text

        if not re.search(r"[A-Za-z]", text):
            return "NULL"

        if len(text.split()) > 5:
            return "NULL"

        return text
    

    