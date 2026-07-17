import os
import re
import json
import time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from collections import defaultdict

import numpy as np
import torch
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sentence_transformers import SentenceTransformer
from transformers import pipeline
import google.generativeai as genai
from app.scripts.email_notifier import send_risk_email

from app.db.database import SessionLocal


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"
BART_MODEL_NAME      = "facebook/bart-large-mnli"
GEMINI_MODEL_NAME    = "gemini-2.5-flash"


BGE_THRESHOLD           = 0.63
BGE_HIGH_CONF_THRESHOLD = 0.70
BART_THRESHOLD          = 0.68

MAX_LABELS     = 2
LLM_DELAY      = 0.2


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("email_risk_pipeline")


# ---------------------------------------------------------------------------
# Signal definitions
# ---------------------------------------------------------------------------

SIGNAL_DEFINITIONS = {
    "customer escalation": (
        "Customer expresses strong dissatisfaction, anger, frustration, "
        "or repeated complaints about service, unresolved issues, or lack of response."
    ),
    "refund dispute": (
        "Customer complaining about refund delays, deposit not returned, "
        "double charge, incorrect billing, or payment deduction issues."
    ),
    "issue tenant": (
        "Tenant reporting accommodation issues such as maintenance problems, "
        "electricity or water issues, disturbances, landlord disputes or unsafe conditions."
    ),
    "unwanted activity": (
        "Report of suspicious activity, fraud, unauthorized account access, "
        "harassment, intimidation or security concern."
    ),
    "kyc issues": (
        "Customer facing identity verification problems such as document rejection, "
        "KYC failure, verification delay or ID mismatch."
    ),
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

RISK_PATTERNS = [
    r"\b(fraud|cheat(ed|ing)?|stolen|scam(med)?|deceiv(ed|ing)?)\b",
    r"\b(fir|police|legal\s*action|court|lawyer|advocate|consumer\s*(forum|court))",
    r"\b(threaten(ing)?|threat(s)?|blackmail|intimidat)",
    r"\b(refund\s*(not|still|pending)|money\s*(not)\s*return)",
    r"\b(frustrated|disgusted|worst|horrible|pathetic|useless|fed\s*up|terrible)\b",
    r"\b(complain(t|ed|ing)?|escalat(e|ed|ing|ion)|unresolved|not\s*respond(ing)?)",
]

SYSTEM_PATTERNS = [
    # --- Existing ---
    r"ticket updated",
    r"your .* ticket has been created",
    r"greetings from",
    r"thank you for writing",
    r"customer happiness officer",
    r"dashboard",
    r"ticket id",

    # --- Booking / Activity Structured Emails ---
    r"new activity below are the details",
    r"prop name:",
    r"travel from date:",
    r"travel to date:",
    r"rent:\s*\d+",
    r"deposit:\s*\d+",
    r"amount:\s*\d+",
    r"nights:\s*\d+",
    r"num guests:",
    r"contact no:",
    r"email id:",
    r"source:\s*(web|app|portal)",

    # --- Payment / Transactional ---
    r"invoice generated",
    r"payment (received|successful|confirmed)",
    r"rent receipt",
    r"booking (confirmed|successful)",
    r"transaction id",
    r"order id",

    # --- OTP / Auth ---
    r"\botp\b",
    r"verification code",
    r"login code",
    r"do not share this code",

    # --- Notifications ---
    r"this is an automated message",
    r"do not reply",
    r"no-reply",
    r"noreply",
    r"system generated",
    r"auto[- ]?generated",

    
    r"risk detected!",
    r"label:\s*(customer escalation|refund dispute|issue tenant|unwanted activity|kyc issues)",
    r"confidence:\s*\d+\.\d+",
    r"decision stage:\s*(semantic|keyword)_(bge|bart|llm)",
]

NORMAL_PATTERNS = [
    r"booking\s*id",
    r"invoice\s*generated",
    r"payment\s*received",
    r"rent\s*receipt",
    r"otp",
]



RISK_REGEX   = [re.compile(p, re.I) for p in RISK_PATTERNS]
SYSTEM_REGEX = [re.compile(p, re.I) for p in SYSTEM_PATTERNS]
NORMAL_REGEX = [re.compile(p, re.I) for p in NORMAL_PATTERNS]


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------

_cached_models:    Optional[Dict] = None
_proto_embeddings: Optional[Dict] = None

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

def has_risk_signal(text: str) -> bool:
    return any(p.search(text) for p in RISK_REGEX)


# 🔥 UPDATED SYSTEM DETECTION (threshold-based)
def is_system_email(text: str) -> bool:
    matches = sum(1 for p in SYSTEM_REGEX if p.search(text))
    return matches >= 2


def is_normal_regex(text: str) -> bool:
    return any(p.search(text) for p in NORMAL_REGEX)



def is_alert_email(text: str) -> bool:
    text = text.lower()
    return (
        "risk detected!" in text and
        "label:" in text and
        "confidence:" in text
    )


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def get_models() -> Dict:
    global _cached_models
    if _cached_models:
        return _cached_models

    embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)

    bart = pipeline(
        "zero-shot-classification",
        model=BART_MODEL_NAME,
        device=0 if torch.cuda.is_available() else -1,
    )

    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    gemini = genai.GenerativeModel(GEMINI_MODEL_NAME)

    _cached_models = {
        "embedding": embedding_model,
        "bart":      bart,
        "gemini":    gemini,
    }
    logger.info("Models loaded successfully.")
    return _cached_models


# ---------------------------------------------------------------------------
# Prototype embeddings
# ---------------------------------------------------------------------------

def build_prototypes() -> Dict:
    global _proto_embeddings
    if _proto_embeddings:
        return _proto_embeddings

    emb_model  = get_models()["embedding"]
    proto_vecs = emb_model.encode(
        list(SIGNAL_DEFINITIONS.values()),
        normalize_embeddings=True,
    )

    _proto_embeddings = {
        key: proto_vecs[i]
        for i, key in enumerate(SIGNAL_DEFINITIONS.keys())
    }
    return _proto_embeddings


# ---------------------------------------------------------------------------
# BGE candidate selection
# ---------------------------------------------------------------------------

def bge_candidate_labels(
    email_embedding: np.ndarray,
) -> Tuple[List[Tuple[str, float]], float]:

    proto  = build_prototypes()
    scores = {label: float(np.dot(email_embedding, vec)) for label, vec in proto.items()}

    candidates = sorted(
        [(k, v) for k, v in scores.items() if v >= BGE_THRESHOLD],
        key=lambda x: x[1],
        reverse=True,
    )
    max_similarity = max(scores.values()) if scores else 0.0
    return candidates, max_similarity


# ---------------------------------------------------------------------------
# BART binary validation
# ---------------------------------------------------------------------------

def bart_binary_validate(
    email_text: str,
    label: str,
    bart_model,
) -> Tuple[str, float]:

    hypothesis = SIGNAL_DEFINITIONS[label]
    result     = bart_model(
        email_text[:2048],
        candidate_labels=[hypothesis, "normal system email"],
        hypothesis_template="This email is about {}.",
    )
    return result["labels"][0], float(result["scores"][0])


# ---------------------------------------------------------------------------
# LLM classification  (now returns reason too)
# ---------------------------------------------------------------------------

def llm_classify(
    email_text: str,
    gemini_model,
) -> Tuple[List[str], bool, str]:
    """
    Returns:
        labels  – list of matched risk labels (empty = normal)
        success – True if Gemini responded correctly
        reason  – short explanation from Gemini
    """
    prompt = f"""
You are a senior risk analyst reviewing customer emails from a rental property platform.

Your task is to identify whether the email contains a customer complaint or risk issue.

Categories and definitions:

customer escalation:
Customer expressing strong dissatisfaction, anger, frustration, or repeated complaints
about unresolved service issues or lack of response.

refund dispute:
Customer complaining about refund delays, deposit not returned, double charges,
incorrect billing or payment deductions.

issue tenant:
Tenant reporting accommodation problems such as maintenance failure, electricity issues,
water leakage, landlord issues or unsafe property conditions.

unwanted activity:
Report of suspicious activity, fraud, harassment, unauthorized account access,
threats or intimidation.

kyc issues:
Problems related to identity verification such as document rejection,
verification failure or KYC delay.

Rules:
- System generated emails (ticket creation, greetings, notifications) are NORMAL.
- If email contains no complaint return empty list.
- Email can belong to multiple categories.
- Only return labels that clearly apply.

Return STRICT JSON only, no markdown, no extra text:

{{"labels":["label1","label2"],"reason":"short explanation"}}

Email:
{email_text[:3000]}
"""

    for attempt in range(3):
        try:
            response = gemini_model.generate_content(prompt)
            raw      = response.text.strip()

            start  = raw.find("{")
            end    = raw.rfind("}")
            parsed = json.loads(raw[start:end + 1])

            labels = parsed.get("labels", [])
            reason = parsed.get("reason", "")

            time.sleep(LLM_DELAY)
            return labels, True, reason

        except Exception as e:
            logger.warning("Gemini attempt %d failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    return [], False, ""


# ---------------------------------------------------------------------------
# DB fetch  — ONLY last 100 unprocessed emails
# ---------------------------------------------------------------------------

def fetch_email_rows():
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT r.id, r.source_id, r.chunks, r.bge_embedding
            FROM rag_embeddings r
            JOIN (
                SELECT source_id
                FROM rag_embeddings
                WHERE source = 'emails'
                  AND is_processed = FALSE
                GROUP BY source_id
                ORDER BY MAX(updated_at) DESC
                LIMIT 100
            ) latest ON r.source_id = latest.source_id
            WHERE r.source = 'emails'
              AND r.bge_embedding IS NOT NULL
            ORDER BY r.source_id, r.updated_at DESC
        """)).fetchall()

        return rows

    finally:
        db.close()


def aggregate_chunks(rows) -> List[Dict]:
    grouped = defaultdict(list)
    for r in rows:
        grouped[r.source_id].append(r)

    emails = []
    for sid, chunks in grouped.items():
        chunks.sort(key=lambda x: x.id)

        texts, embeddings, ids = [], [], []
        for r in chunks:
            texts.append(r.chunks or "")
            ids.append(r.id)

            emb = r.bge_embedding
            if isinstance(emb, str):
                emb = json.loads(emb)
            embeddings.append(np.array(emb, dtype=np.float32))

        avg  = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(avg)
        if norm > 0:
            avg = avg / norm

        emails.append({
            "source_id": sid,
            "chunk_ids": ids,
            "text":      "\n".join(texts),
            "embedding": avg.astype(np.float32),
        })

    return emails


# ---------------------------------------------------------------------------
# Per-email classification
# ---------------------------------------------------------------------------

def process_email(
    email: Dict,
    models: Dict,
) -> Tuple[List[str], str, float, str]:

    email_text = email["text"]
    embedding  = email["embedding"]
    bart       = models["bart"]

    # 🔥 NEW ALERT BLOCK (FIRST PRIORITY)
    if is_alert_email(email_text):
        return ["normal"], "internal_alert", 0.0, ""

    # --- Fast exits ---
    if is_system_email(email_text):
        return ["normal"], "system", 0.0, ""

    if is_normal_regex(email_text):
        return ["normal"], "regex", 0.0, ""

    def _classify(path_prefix: str):
        candidates, similarity = bge_candidate_labels(embedding)

        high_conf_labels = [
            label for label, score in candidates
            if score >= BGE_HIGH_CONF_THRESHOLD
        ]

        low_conf_candidates = [
            (label, score) for label, score in candidates
            if score < BGE_HIGH_CONF_THRESHOLD
        ]

        # BART validation
        bart_labels = []
        for label, _ in low_conf_candidates:
            predicted, score = bart_binary_validate(email_text, label, bart)
            if predicted != "normal system email" and score >= BART_THRESHOLD:
                bart_labels.append((label, score))

        bart_labels.sort(key=lambda x: x[1], reverse=True)

        seen, combined = set(), []
        for label in high_conf_labels + [l for l, _ in bart_labels]:
            if label not in seen:
                seen.add(label)
                combined.append(label)

        # 🔥 LLM GUARD (MOST IMPORTANT FIX)
        if combined and similarity < 0.65:
            labels, success, reason = llm_classify(email_text, models["gemini"])
            if success:
                return labels or ["normal"], f"{path_prefix}_llm_guard", similarity, reason

        if combined:
            if high_conf_labels and bart_labels:
                stage = f"{path_prefix}_bge_bart"
            elif high_conf_labels:
                stage = f"{path_prefix}_bge"
            else:
                stage = f"{path_prefix}_bart"

            return combined[:MAX_LABELS], stage, similarity, ""

        # LLM fallback
        labels, success, reason = llm_classify(email_text, models["gemini"])
        if success and labels:
            return labels, f"{path_prefix}_llm", similarity, reason

        return ["normal"], f"{path_prefix}_fallback", similarity, ""

    if has_risk_signal(email_text):
        return _classify("keyword")

    return _classify("semantic")

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

UPSERT_SQL = text("""
    INSERT INTO email_risk_analysis_v2 (
        rag_embedding_id,
        source_id,
        risk_label,
        similarity_score,
        llm_called,
        llm_success,
        llm_reason,
        decision_stage,
        processed_at,
        alert_sent
    ) VALUES (
        :rag_embedding_id,
        :source_id,
        :risk_label,
        :similarity_score,
        :llm_called,
        :llm_success,
        :llm_reason,
        :decision_stage,
        :processed_at,
        FALSE
    )
    ON CONFLICT (source_id) DO NOTHING
    RETURNING id;
""")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    models = get_models()
    rows   = fetch_email_rows()

    if not rows:
        logger.info("No new emails to process.")
        return

    emails = aggregate_chunks(rows)
    logger.info("Processing %d new emails.", len(emails))

    db = SessionLocal()

    try:
        for email in emails:
            source_id = email["source_id"]

            logger.info("Processing email: %s", source_id)

            try:
                labels, stage, similarity, reason = process_email(email, models)

                logger.info(
                    "Result | source_id=%s | labels=%s | stage=%s | similarity=%.3f",
                    source_id, labels, stage, similarity
                )

                # ============================================================
                # CASE 1: NORMAL EMAIL
                # ============================================================
                if labels == ["normal"]:
                    logger.info("Normal email processed: %s", source_id)

                    db.execute(
                        text("""
                            UPDATE rag_embeddings
                            SET is_processed = TRUE,
                                processed_at = :ts
                            WHERE source_id = :sid
                        """),
                        {"sid": source_id, "ts": datetime.utcnow()}
                    )
                    db.commit()
                    continue

                # ============================================================
                # CASE 2: RISK EMAIL
                # ============================================================
                llm_called  = "llm" in stage
                llm_success = llm_called and "fallback" not in stage

                inserted_id = None

                for cid in email["chunk_ids"]:
                    try:
                        result = db.execute(UPSERT_SQL, {
                            "rag_embedding_id": cid,
                            "source_id":        source_id,
                            "risk_label":       json.dumps({"labels": labels, "stage": stage}),
                            "similarity_score": float(similarity),
                            "llm_called":       llm_called,
                            "llm_success":      llm_success,
                            "llm_reason":       reason or stage,
                            "decision_stage":   stage,
                            "processed_at":     datetime.utcnow(),
                        }).fetchone()

                        if result and inserted_id is None:
                            inserted_id = result[0]

                    except SQLAlchemyError as e:
                        logger.error(
                            "DB insert failed | source_id=%s | chunk_id=%s | error=%s",
                            source_id, cid, e
                        )
                        db.rollback()
                        break

                # ============================================================
                # HANDLE DUPLICATES
                # ============================================================
                if not inserted_id:
                    logger.info("Duplicate detected (already processed): %s", source_id)

                    db.execute(
                        text("""
                            UPDATE rag_embeddings
                            SET is_processed = TRUE,
                                processed_at = :ts
                            WHERE source_id = :sid
                        """),
                        {"sid": source_id, "ts": datetime.utcnow()}
                    )
                    db.commit()
                    continue

                # ============================================================
                # COMMIT RISK RECORD
                # ============================================================
                try:
                    db.commit()
                except SQLAlchemyError as e:
                    logger.error("Commit failed for %s: %s", source_id, e)
                    db.rollback()
                    continue

                logger.info(
                    "Risk record saved | source_id=%s | labels=%s | stage=%s",
                    source_id, labels, stage
                )

                # ============================================================
                # SEND ALERT EMAIL
                # ============================================================
                try:
                    send_risk_email(
                        label=", ".join(labels),
                        score=similarity,
                        email_text=email["text"],
                        llm_reason=reason,
                        stage=stage
                    )

                    db.execute(
                        text("""
                            UPDATE email_risk_analysis_v2
                            SET alert_sent = TRUE
                            WHERE id = :id
                        """),
                        {"id": inserted_id}
                    )
                    db.commit()

                    logger.info("Alert sent for %s", source_id)

                except Exception as e:
                    db.rollback()
                    logger.error(
                        "Alert failed (record saved) | source_id=%s | error=%s",
                        source_id, e
                    )

                # ============================================================
                # FINAL STEP: ALWAYS MARK AS PROCESSED
                # ============================================================
                try:
                    db.execute(
                        text("""
                            UPDATE rag_embeddings
                            SET is_processed = TRUE,
                                processed_at = :ts
                            WHERE source_id = :sid
                        """),
                        {"sid": source_id, "ts": datetime.utcnow()}
                    )
                    db.commit()

                except Exception as e:
                    logger.error(
                        "Failed to mark processed | source_id=%s | error=%s",
                        source_id, e
                    )
                    db.rollback()

            except Exception as e:
                logger.error(
                    "Unexpected failure processing email | source_id=%s | error=%s",
                    source_id, e
                )
                db.rollback()

    finally:
        db.close()

if __name__ == "__main__":
    run_pipeline()