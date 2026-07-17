'''import os
import io
import numpy as np
from PIL import Image, ImageFile
from pdf2image import convert_from_bytes
from rapidocr_onnxruntime import RapidOCR


# FIX: Allow truncated images
# =====================================================
ImageFile.LOAD_TRUNCATED_IMAGES = True

# CONFIG
# =====================================================

SUPPORTED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tiff"}

# Initialize once
rapid_ocr = RapidOCR()


# FILE → IMAGE CONVERSION
# =====================================================

def convert_to_images(file_bytes: bytes, filename: str):
    ext = os.path.splitext(filename.lower())[1]

    if ext in SUPPORTED_IMAGE_EXT:
        image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        return [np.array(image)]

    if ext == ".pdf":
        pages = convert_from_bytes(
            file_bytes, 
            dpi=300
            #poppler_path=r"C:\poppler-25.12.0\Library\bin"
            )
        return [np.array(p.convert("RGB")) for p in pages]

    raise ValueError(f"Unsupported file format: {ext}")


# RAPID OCR
# =====================================================

def run_ocr(image_np: np.ndarray) -> dict:
    try:
        result, _ = rapid_ocr(image_np)

        if not result:
            return {
                "text": "",
                "raw_json": {
                    "engine": "rapidocr",
                    "error": "No text detected"
                }
            }

        extracted_text = [line[1] for line in result]
        full_text = "\n".join(extracted_text)

        return {
            "text": full_text,
            "raw_json": {
                "engine": "rapidocr"
            }
        }

    except Exception as e:
        return {
            "text": "",
            "raw_json": {
                "engine": "rapidocr",
                "error": str(e)
            }
        }'''














import os
import io
import re
import cv2
import pytesseract
import numpy as np
from PIL import Image, ImageFile, ImageOps
from pdf2image import convert_from_bytes
from rapidocr_onnxruntime import RapidOCR


# FIX: Allow truncated images
# =====================================================
ImageFile.LOAD_TRUNCATED_IMAGES = True

# CONFIG
# =====================================================
SUPPORTED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tiff"}

# Initialize once
rapid_ocr = RapidOCR()








'''def preprocess_for_ocr_global(image_np: np.ndarray) -> np.ndarray:
    img = cv2.resize(image_np, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    # enhance but keep RGB
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    # convert back to RGB
    enhanced = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    return enhanced'''




# =====================================================
# RAPIDOCR TEXT HELPERS
# =====================================================

def _extract_text_from_result(result) -> str:
    lines = []

    for line in result or []:
        try:
            txt = line[1]

            # some OCR engines may return tuple/list in line[1]
            if isinstance(txt, (list, tuple)):
                txt = txt[0]

            if txt:
                txt = str(txt).strip()
                if txt:
                    lines.append(txt)
        except Exception:
            continue

    return "\n".join(lines)


# =====================================================
# TESSERACT OSD HELPERS
# =====================================================

def _parse_osd_rotate_angle(osd_text: str) -> int:
    """
    Parse 'Rotate: X' from Tesseract OSD output.
    Returns 0/90/180/270.
    """
    match = re.search(r"Rotate:\s*(\d+)", osd_text or "")
    if not match:
        return 0

    angle = int(match.group(1))
    if angle in (0, 90, 180, 270):
        return angle
    return 0


def _detect_orientation_tesseract(image_np: np.ndarray) -> dict:
    """
    Use Tesseract OSD only for orientation detection.
    If it fails, return angle=0 and mark used=False.
    """
    try:
        osd = pytesseract.image_to_osd(image_np)
        
        angle = _parse_osd_rotate_angle(osd)

        return {
            "used": True,
            "angle": angle,
            "osd": osd,
            "error": None,
        }

    except pytesseract.TesseractError as e:
        return {
            "used": False,
            "angle": 0,
            "osd": None,
            "error": str(e),
        }

    except Exception as e:
        return {
            "used": False,
            "angle": 0,
            "osd": None,
            "error": str(e),
        }




'''def _detect_orientation_tesseract(image_np: np.ndarray) -> dict:
    try:
        # 🔥 PREPROCESS (VERY IMPORTANT)
        img = cv2.resize(image_np, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # improve contrast
        gray = cv2.equalizeHist(gray)

        rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)

        config = r'--dpi 300'

        osd = pytesseract.image_to_osd(rgb, config=config)

        angle = _parse_osd_rotate_angle(osd)

        return {
            "used": True,
            "angle": angle,
            "osd": osd,
            "error": None,
        }

    except pytesseract.TesseractError as e:
        return {
            "used": False,
            "angle": 0,
            "osd": None,
            "error": str(e),
        }

    except Exception as e:
        return {
            "used": False,
            "angle": 0,
            "osd": None,
            "error": str(e),
        }'''


def _rotate_by_osd_angle(image_np: np.ndarray, angle: int) -> np.ndarray:
    """
    Tesseract OSD 'Rotate: X' means image must be rotated by X degrees
    to become upright.
    """
    if angle == 90:
        return cv2.rotate(image_np, cv2.ROTATE_90_CLOCKWISE)
    elif angle == 180:
        return cv2.rotate(image_np, cv2.ROTATE_180)
    elif angle == 270:
        return cv2.rotate(image_np, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image_np


def normalize_orientation_for_ocr(image_np: np.ndarray) -> dict:
    """
    Orientation normalization flow:
    1. Try Tesseract OSD
    2. If success, rotate accordingly
    3. If fail, keep original image unchanged
    """
    osd_result = _detect_orientation_tesseract(image_np)

    #corrected = _rotate_by_osd_angle(image_np, osd_result["angle"])
    if osd_result["used"]:
        corrected = _rotate_by_osd_angle(image_np, osd_result["angle"])
    else:
        corrected = image_np   # 👉 SAFE fallback

    return {
        "image": corrected,
        "angle": osd_result["angle"],
        "osd_used": osd_result["used"],
        "osd_error": osd_result["error"],
        "osd_raw": osd_result["osd"],
    }


# =====================================================
# FILE → IMAGE CONVERSION
# =====================================================

def convert_to_images(file_bytes: bytes, filename: str):
    ext = os.path.splitext(filename.lower())[1]

    if ext in SUPPORTED_IMAGE_EXT:
        image = ImageOps.exif_transpose(
            Image.open(io.BytesIO(file_bytes))
        ).convert("RGB")

        fixed = normalize_orientation_for_ocr(np.array(image))

        if fixed["osd_used"]:
            print(f"[OSD] angle={fixed['angle']}")
        else:
            print(f"[OSD FALLBACK] using original image. error={fixed['osd_error']}")

        # it return only normal image
        return [fixed["image"]]

        #it return only preprocessed image
        #processed = preprocess_for_ocr_global(fixed["image"])
        #return [processed]
    
        #it return both original and preprocessed image
        #return [{
         #   "original": fixed["image"],
          #  "processed": preprocess_for_ocr_global(fixed["image"])
        #}]

    if ext == ".pdf":
        pages = convert_from_bytes(
            file_bytes,
            dpi=300
            # poppler_path=r"C:\poppler-25.12.0\Library\bin"
        )

        normalized_pages = []

        for idx, p in enumerate(pages, start=1):
            page = ImageOps.exif_transpose(p.convert("RGB"))
            fixed = normalize_orientation_for_ocr(np.array(page))

            if fixed["osd_used"]:
                print(f"[OSD][PDF page {idx}] angle={fixed['angle']}")
            else:
                print(
                    f"[OSD FALLBACK][PDF page {idx}] using original page. "
                    f"error={fixed['osd_error']}"
                )

            # normal pdf
            normalized_pages.append(fixed["image"])

            #preprocess pdf
            #processed = preprocess_for_ocr_global(fixed["image"])
            #normalized_pages.append(processed)


            #normalized_pages.append({
             #   "original": fixed["image"],
              #  "processed": preprocess_for_ocr_global(fixed["image"])
            #})

        return normalized_pages

    raise ValueError(f"Unsupported file format: {ext}")


# =====================================================
# RAPID OCR
# =====================================================

def run_ocr(image_np: np.ndarray) -> dict:
    try:
        result, _ = rapid_ocr(image_np)

        # 🔥 DPI + clarity boost (CRITICAL)
        '''img = cv2.resize(image_np, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # improve contrast
        gray = cv2.equalizeHist(gray)

        result, _ = rapid_ocr(gray)'''

        if not result:
            return {
                "text": "",
                "raw_json": {
                    "engine": "rapidocr",
                    "error": "No text detected"
                }
            }

        full_text = _extract_text_from_result(result)

        return {
            "text": full_text,
            "raw_json": {
                "engine": "rapidocr"
            }
        }

    except Exception as e:
        return {
            "text": "",
            "raw_json": {
                "engine": "rapidocr",
                "error": str(e)
            }
        }   








































'''
import os
import io
import numpy as np
from PIL import Image, ImageFile
from pdf2image import convert_from_bytes
from paddleocr import PaddleOCR


# FIX: Allow truncated images
# =====================================================
ImageFile.LOAD_TRUNCATED_IMAGES = True

# CONFIG
# =====================================================

SUPPORTED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tiff"}

# Initialize once
paddle_ocr = PaddleOCR(
    lang="en",   # change to "hi" if you want Hindi-heavy docs
)


# FILE → IMAGE CONVERSION
# =====================================================

def convert_to_images(file_bytes: bytes, filename: str):
    ext = os.path.splitext(filename.lower())[1]

    if ext in SUPPORTED_IMAGE_EXT:
        image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        return [np.array(image)]

    if ext == ".pdf":
        pages = convert_from_bytes(
            file_bytes,
            dpi=300
            # poppler_path=r"C:\poppler-25.12.0\Library\bin"
        )
        return [np.array(p.convert("RGB")) for p in pages]

    raise ValueError(f"Unsupported file format: {ext}")


# PADDLE OCR
# =====================================================

def run_ocr(image_np: np.ndarray) -> dict:
    try:
        result = paddle_ocr.predict(image_np)

        if not result:
            return {
                "text": "",
                "raw_json": {
                    "engine": "paddleocr",
                    "error": "No text detected"
                }
            }

        page = result[0]
        extracted_text = page.get("rec_texts", [])

        if not extracted_text:
            return {
                "text": "",
                "raw_json": {
                    "engine": "paddleocr",
                    "error": "No text detected"
                }
            }

        cleaned_lines = [
            str(line).strip()
            for line in extracted_text
            if str(line).strip()
        ]
        full_text = "\n".join(cleaned_lines)

        return {
            "text": full_text,
            "raw_json": {
                "engine": "paddleocr",
                "rec_texts": cleaned_lines,
                "rec_scores": page.get("rec_scores", [])
            }
        }

    except Exception as e:
        return {
            "text": "",
            "raw_json": {
                "engine": "paddleocr",
                "error": str(e)
            }
        }'''
