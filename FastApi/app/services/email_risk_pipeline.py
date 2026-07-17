"""
Email Risk Classification Pipeline (Production)

Flow per email:
    Gate 1 → Junk filter    (DKIM, MIME headers, base64 blobs)
    Gate 2 → Normal filter   (regex: transactional/system emails)
    Gate 3 → Semantic filter  (embedding similarity to risk prototypes)
    Stage 4 → BART classify   (zero-shot, only if Gates 1-3 passed)
    Stage 5 → LLM validate    (Gemini, only when BART is uncertain)

Only RISKY emails are stored with real labels.
Normal/junk emails are marked 'skipped' so they aren't re-fetched.

Query risky results:
    SELECT * FROM email_risk_analysis WHERE risk_label != 'skipped'
"""

import os
import re
import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional
from contextlib import contextmanager
from collections import defaultdict

import torch
import numpy as np
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sentence_transformers import SentenceTransformer
from transformers import pipeline as hf_pipeline
import google.generativeai as genai
from app.db.database import get_db
from app.db.database import SessionLocal


load_dotenv()




SIMILARITY_THRESHOLD = 0.70
BART_CONFIDENCE_THRESHOLD = 0.70       
BART_MARGIN_THRESHOLD = 0.12           
BART_NORMAL_OVERRIDE_THRESHOLD = 0.75  
LLM_RATE_DELAY_SECONDS = 0.2
BATCH_INSERT_SIZE = 50
BART_MAX_CHARS = 2048                  

EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"
BART_MODEL_NAME = "facebook/bart-large-mnli"
GEMINI_MODEL_NAME = "gemini-2.5-flash"




CANDIDATE_LABELS = {
    "fraud": "accusation of financial cheating, stolen money, or intentional payment fraud by staff",
    "legal_escalation": "explicit mention of filing FIR, police complaint, consumer forum, or legal action",
    "threat": "explicit threat of physical harm, intimidation, or coercion by the sender",
    "refund_dispute": "complaint specifically about a refund or payment not being returned",
    "customer_escalation": "angry or aggressive complaint about service without legal or fraud accusation",
    "normal": "informational, transactional, booking confirmation, invoice, rent receipt, or system generated email",
}

LABEL_KEYS = list(CANDIDATE_LABELS.keys())
RISK_LABELS = [k for k in LABEL_KEYS if k != "normal"]
REVERSE_MAP = {v: k for k, v in CANDIDATE_LABELS.items()}




JUNK_PATTERNS = [
    r"(?i)^DKIM-Signature:",
    r"(?i)^X-Google-DKIM-Signature:",
    r"(?i)^(Received|Return-Path|X-Received|ARC-):",
    r"(?i)\bv=1;\s*a=rsa-sha256",
    r"(?i)\bc=relaxed/relaxed",
    r"(?i)\bbh=[A-Za-z0-9+/=]{20,}",
    r"(?i)\bb=[A-Za-z0-9+/=]{40,}",
    r"(?i)^Content-Type:\s*multipart/",
    r"(?i)^MIME-Version:",
    r"(?i)boundary=",
    r"(?i)^Message-ID:\s*<",
    r"(?i)^(From|To|Cc|Bcc|Subject|Date|Reply-To):\s",
    r"[A-Za-z0-9+/=]{60,}",
]

JUNK_COMPILED = [re.compile(p, re.MULTILINE) for p in JUNK_PATTERNS]


def is_junk_or_header(full_text: str) -> bool:
    """Detect raw email headers, DKIM, MIME, base64 blobs."""
    stripped = full_text.strip()
    if len(stripped) == 0:
        return True

    junk_matches = sum(1 for p in JUNK_COMPILED if p.search(stripped))
    if junk_matches >= 3:
        return True

    total_len = len(stripped)
    if total_len > 100:
        alnum_ratio = sum(
            1 for c in stripped if c.isalnum() or c.isspace()
        ) / total_len
        if alnum_ratio < 0.50:
            return True

    return False




NORMAL_PATTERNS = [
    # IDs and statuses
    r"(?i)\b(booking\s*id|invoice\s*id|order\s*id|transaction\s*id)\s*[:;]?\s*\d+",
    r"(?i)\bpayment\s*status\s*[:;]?\s*(success|completed|paid|received|confirmed)",
    r"(?i)\bnew\s*activity\s*below\s*are\s*the\s*details",
    # System emails
    r"(?i)\b(auto[- ]?generated|do\s*not\s*reply|no[- ]?reply|system\s*generated)",
    # Booking / reservation
    r"(?i)\b(booking\s*confirm|reservation\s*confirm|check[- ]?in\s*detail)",
    r"(?i)\byour\s*(booking|reservation|payment|invoice)\s*(has\s*been|is)\s*(confirmed|processed|received|created)",
    r"(?i)\b(welcome\s*to|thank\s*you\s*for\s*(your\s*)?(booking|reservation|payment|registr|staying))",
    # Receipts
    r"(?i)\b(rent\s*receipt|payment\s*receipt|invoice\s*attached|invoice\s*generated)",
    # Stay / lease / extension
    r"(?i)\bsuccessfully\s*(extended|renewed|booked|confirmed|registered|checked)",
    r"(?i)\b(new\s*)?move[- ]?out\s*date\s*[:;]?",
    r"(?i)\bapplicable\s*(monthly\s*)?rent\s*[:;]?",
    r"(?i)\b(stay\s*extended|lease\s*renewed|tenancy\s*confirm|extension\s*confirm)",
    r"(?i)\bcongratulations\s*[,!]?\s*(you\s*have|your)",
    r"(?i)\b(monthly\s*rent|rent\s*amount|security\s*deposit)\s*[:;]?\s*\d+",
    # Move-in / onboarding
    r"(?i)\b(move[- ]?in\s*date|check[- ]?in\s*date|onboarding\s*detail)",
    r"(?i)\b(key\s*handover|property\s*handover|agreement\s*sign)",
    # Maintenance / tickets
    r"(?i)\b(maintenance\s*scheduled|service\s*request\s*(created|resolved|closed))",
    r"(?i)\b(ticket\s*(id|number|created|resolved|closed))\s*[:;]?\s*\d*",
    # Payments / invoices
    r"(?i)\b(payment\s*(due|reminder|received|credited|debited|processed))",
    r"(?i)\b(invoice\s*(generated|created|due|sent|attached))",
    r"(?i)\b(emi|installment)\s*(due|paid|received|processed)",
    # OTP / verification
    r"(?i)\b(otp|verification\s*code|one[- ]?time\s*password)\b",
    # Polite system closers
    r"(?i)\bfeel\s*free\s*to\s*(reply|contact|reach|raise\s*a\s*ticket)",
    r"(?i)\braise\s*a\s*ticket",
    r"(?i)\b(if\s*you\s*have\s*any\s*(further\s*)?questions)",
    # Greetings without complaint
    r"(?i)^(hi|hello|dear)\s+\w+\s*,?\s*(greetings?|good\s*(morning|afternoon|evening))",
    r"(?i)^dear\s+\w+\s*,\s*congratulations",
]

NORMAL_COMPILED = [re.compile(p) for p in NORMAL_PATTERNS]

# Risk signals — if ANY present, do NOT short-circuit to normal
RISK_SIGNALS = [
    r"(?i)\b(harassment|harassing|harass(ed|ment)?)\b",
    r"(?i)\b(fraud|cheat(ed|ing)?|stolen|scam(med)?|mislead(ing)?)\b",
    r"(?i)\b(frustrated|frustration|frustrating|tired|annoyed|upset|disgust(ed|ing)?|worst|horrible|pathetic|useless|fed\s*up|terrible|bad|unprofessional)\b",
    r"(?i)\b(intimidat(ed|ing)?|threaten(ing)?|threat(s)?|blackmail|harm\s+(you|him|her|them))\b",
    r"(?i)\b(fir\b|police|legal\s*action|court|lawyer|advocate|consumer\s*(forum|court)|legal)\b",
    r"(?i)\b(refund\s*(not|never|still|pending)|money\s*(not|never)\s*return)\b",
    r"(?i)\b(complain(t|ed|ing)?|escalat(e|ed|ing|ion)|unresolved|not\s*respond(ing)?)\b",
    r"(?i)\b(cheat\s*me|fraud\s*with\s*me|stealing\s*my|looting)\b",
]

RISK_COMPILED = [re.compile(p) for p in RISK_SIGNALS]


def is_likely_normal(full_text: str) -> bool:
    """Matches transactional pattern AND has no risk signal."""
    has_normal = any(p.search(full_text) for p in NORMAL_COMPILED)
    has_risk = any(p.search(full_text) for p in RISK_COMPILED)
    return has_normal and not has_risk




PROTOTYPE_EXAMPLES = {
    "fraud": [
        "they cheated me financially",
        "my money was stolen by staff",
        "payment fraud happened",
        "deposit not returned intentionally",
        "caretaker wants to fraud with me",
        "someone redirected my payment",
        "they are looting my money",
    ],
    "legal_escalation": [
        "I will file FIR against you",
        "I will go to police station",
        "legal action will be taken",
        "I will take this to court",
        "I am contacting consumer forum",
        "I informed the local police station",
        "I will approach consumer court",
    ],
    "threat": [
        "I will harm you physically",
        "this is your final warning or else",
        "I will create serious trouble for you",
        "your supervisor is a dangerous man he threatened me",
        "he is threatening and intimidating me",
        "I will make sure you regret this",
    ],
    "refund_dispute": [
        "refund amount not received",
        "when will my refund be credited",
        "payment not returned after checkout",
        "how much refund will I get",
        "deducted amount not refunded yet",
        "waiting for refund since months",
    ],
    "customer_escalation": [
        "I am very frustrated with your service",
        "issue not resolved after many complaints",
        "I have complained many times no response",
        "nobody is responding to my queries",
        "worst service ever experienced",
        "pathetic response from your team",
    ],
}




logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("email_risk_pipeline")




_cached_models: Optional[Dict] = None
_cached_proto_embeddings: Optional[Dict] = None


def get_models() -> Dict:
    """Return cached models. Loads only on first call."""
    global _cached_models
    if _cached_models is not None:
        return _cached_models

    logger.info("Loading models (first time)...")

    embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)

    bart_classifier = hf_pipeline(
        "zero-shot-classification",
        model=BART_MODEL_NAME,
        device=0 if torch.cuda.is_available() else -1,
    )

    api_key = os.getenv("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(GEMINI_MODEL_NAME)

    _cached_models = {
        "embedding": embedding_model,
        "bart": bart_classifier,
        "gemini": gemini_model,
    }

    logger.info("Models loaded and cached.")
    return _cached_models


def get_proto_embeddings() -> Dict:
    """Return cached prototype embeddings. Builds only on first call."""
    global _cached_proto_embeddings
    if _cached_proto_embeddings is not None:
        return _cached_proto_embeddings

    models = get_models()
    logger.info("Building prototype embeddings (first time)...")

    proto = {}
    for label, examples in PROTOTYPE_EXAMPLES.items():
        embs = models["embedding"].encode(examples, normalize_embeddings=True)
        proto[label] = embs.astype(np.float32)

    _cached_proto_embeddings = proto
    logger.info("Prototype embeddings cached (%d labels).", len(proto))
    return _cached_proto_embeddings




def semantic_filter(email_embedding: np.ndarray, proto_embeddings: Dict):
    """
    Compare email embedding against prototype examples per label.
    Returns (best_label, best_score).
    """
    best_label = "normal"
    best_score = 0.0

    for label, proto_embs in proto_embeddings.items():
        scores = np.dot(proto_embs, email_embedding)
        max_score = float(np.max(scores))
        if max_score > best_score:
            best_score = max_score
            best_label = label

    return best_label, best_score




def bart_classify(text_input: str, bart_pipeline):
    """Run BART zero-shot. Returns (label, top1_score, top2_score)."""
    result = bart_pipeline(
        text_input[:BART_MAX_CHARS],
        candidate_labels=list(CANDIDATE_LABELS.values()),
        hypothesis_template="This email is about {}.",
    )

    top1_desc = result["labels"][0]
    top1_score = float(result["scores"][0])
    top2_score = float(result["scores"][1]) if len(result["scores"]) > 1 else 0.0

    return REVERSE_MAP[top1_desc], top1_score, top2_score




def validate_with_llm(
    email_text: str,
    bart_label: str,
    bart_score: float,
    gemini_model,
) -> tuple:
    """Ask Gemini to confirm or override BART. Returns (verdict, reason, success)."""
    prompt = f"""You are a senior email risk analyst for a property rental company (RentMyStay).

TASK: Validate or override the previous model's classification.

Previous model said: "{bart_label}" (confidence: {bart_score:.2f})

CRITICAL RULES:
1. System-generated emails about bookings, invoices, payments, rent receipts,
   stay extensions, OTPs, move-in/move-out dates, or routine notifications
   are ALWAYS "normal" — even if they mention money amounts or dates.
2. Raw email headers (DKIM, MIME, From/To lines) are ALWAYS "normal".
3. Only classify as a risk label if the email contains a CLEAR human-written
   complaint, threat, fraud accusation, or legal mention.
4. "fraud" = the sender accuses someone of cheating/stealing money.
5. "legal_escalation" = the sender explicitly says they will file FIR,
   go to police, contact consumer forum, or take legal action.
6. "threat" = the sender threatens physical harm or intimidation.
7. "refund_dispute" = the sender complains about a refund not being processed.
8. "customer_escalation" = angry complaint without legal/fraud/threat language.

Allowed labels: {', '.join(LABEL_KEYS)}

Respond ONLY in valid JSON (no markdown):
{{"verdict": "label", "reason": "explanation under 20 words"}}

Email:
\"\"\"{email_text[:3000]}\"\"\"
"""
    try:
        response = gemini_model.generate_content(prompt)
        raw = response.text.strip()

        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return None, None, False

        parsed = json.loads(raw[start : end + 1])
        time.sleep(LLM_RATE_DELAY_SECONDS)

        verdict = parsed.get("verdict", "").strip().lower()
        reason = parsed.get("reason", "")

        if verdict in LABEL_KEYS:
            return verdict, reason, True

        return None, None, False

    except Exception as e:
        logger.debug("LLM validation failed: %s", e)
        return None, None, False




def fetch_email_rows():
    """Fetch all unprocessed email chunks."""
    with get_db() as db:
        rows = db.execute(text("""
            SELECT r.id, r.source_id, r.chunks, r.bge_embedding
            FROM rag_embeddings r
            LEFT JOIN email_risk_analysis e
                ON e.rag_embedding_id = r.id
            WHERE r.source = 'emails'
              AND r.bge_embedding IS NOT NULL
              AND e.id IS NULL
        """)).fetchall()
    return rows


def aggregate_chunks(rows) -> List[Dict]:
    """
    Group chunks by source_id → one logical email.
    Sorts by id to preserve order, averages embeddings.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.source_id].append(row)

    aggregated = []

    for source_id, chunk_rows in grouped.items():
        chunk_rows.sort(key=lambda r: r.id)

        texts = []
        embeddings = []
        chunk_ids = []

        for row in chunk_rows:
            texts.append(row.chunks or "")
            chunk_ids.append(row.id)

            raw_emb = row.bge_embedding
            if raw_emb is not None:
                if isinstance(raw_emb, str):
                    raw_emb = json.loads(raw_emb)
                embeddings.append(np.array(raw_emb, dtype=np.float32))

        if not embeddings:
            continue

        full_text = "\n".join(texts).strip()
        if not full_text:
            continue

        avg_emb = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(avg_emb)
        if norm > 0:
            avg_emb = avg_emb / norm

        aggregated.append({
            "source_id": source_id,
            "chunk_ids": chunk_ids,
            "full_text": full_text,
            "avg_embedding": avg_emb.astype(np.float32),
        })

    return aggregated




def process_email(
    email_group: Dict,
    models: Dict,
    proto_embeddings: Dict,
) -> List[Dict]:
    """
    Full pipeline for one aggregated email.
    Returns list of result dicts (one per chunk_id).
    """
    full_text = email_group["full_text"]
    email_embedding = email_group["avg_embedding"]
    source_id = email_group["source_id"]
    chunk_ids = email_group["chunk_ids"]

    
    if is_junk_or_header(full_text):
        return _skip(chunk_ids, source_id, "junk_header")

    
    if is_likely_normal(full_text):
        return _skip(chunk_ids, source_id, "regex_normal")

    
    sem_label, sem_score = semantic_filter(email_embedding, proto_embeddings)
    if sem_score < SIMILARITY_THRESHOLD:
        return _skip(chunk_ids, source_id, "below_sim_threshold")

    
    bart_label, bart_top1, bart_top2 = bart_classify(full_text, models["bart"])
    margin = bart_top1 - bart_top2

    
    if bart_label == "normal":
        reason = "bart_normal_high" if bart_top1 >= BART_NORMAL_OVERRIDE_THRESHOLD else "bart_normal"
        return _skip(chunk_ids, source_id, reason)

    
    needs_llm = (
        bart_top1 < BART_CONFIDENCE_THRESHOLD
        or margin < BART_MARGIN_THRESHOLD
    )

    final_label = bart_label
    validated = False
    llm_verdict = "bart_only"

    if needs_llm:
        llm_label, llm_reason, success = validate_with_llm(
            full_text, bart_label, bart_top1, models["gemini"],
        )
        if success:
            final_label = llm_label
            validated = True
            llm_verdict = llm_reason

    
    if final_label == "normal":
        return _skip(chunk_ids, source_id, "llm_override_normal")

    
    return _risk(
        chunk_ids=chunk_ids,
        source_id=source_id,
        risk_label=final_label,
        risk_score=float(bart_top1),
        similarity_score=float(sem_score),
        validated_by_llm=validated,
        llm_verdict=llm_verdict,
    )


def _skip(chunk_ids: List[int], source_id: str, reason: str) -> List[Dict]:
    """Mark as processed but not risky."""
    now = datetime.utcnow()
    return [
        {
            "rag_embedding_id": cid,
            "source_id": source_id,
            "risk_label": "skipped",
            "risk_score": 0.0,
            "bart_model": BART_MODEL_NAME,
            "similarity_score": 0.0,
            "validated_by_llm": False,
            "llm_verdict": reason,
            "processed_at": now,
        }
        for cid in chunk_ids
    ]


def _risk(
    chunk_ids, source_id, risk_label, risk_score,
    similarity_score, validated_by_llm, llm_verdict,
) -> List[Dict]:
    """Build rows for a confirmed risky email."""
    now = datetime.utcnow()
    return [
        {
            "rag_embedding_id": cid,
            "source_id": source_id,
            "risk_label": risk_label,
            "risk_score": risk_score,
            "bart_model": BART_MODEL_NAME,
            "similarity_score": similarity_score,
            "validated_by_llm": validated_by_llm,
            "llm_verdict": llm_verdict,
            "processed_at": now,
        }
        for cid in chunk_ids
    ]




INSERT_SQL = text("""
    INSERT INTO email_risk_analysis
    (
        rag_embedding_id, source_id, risk_label, risk_score,
        bart_model, similarity_score, validated_by_llm,
        llm_verdict, processed_at
    )
    VALUES
    (
        :rag_embedding_id, :source_id, :risk_label, :risk_score,
        :bart_model, :similarity_score, :validated_by_llm,
        :llm_verdict, :processed_at
    )
""")


def insert_batch(results: List[Dict]):
    """Insert batch. Rolls back on error."""
    if not results:
        return
    with get_db() as db:
        try:
            for r in results:
                db.execute(INSERT_SQL, r)
            db.commit()
        except SQLAlchemyError as e:
            logger.error("Batch insert failed: %s", e)
            db.rollback()




def run_pipeline():
    """Main entry point. Call from cron, scheduler, or API."""
    models = get_models()
    proto_embeddings = get_proto_embeddings()

    
    rows = fetch_email_rows()
    logger.info("Fetched %d unprocessed chunks.", len(rows))

    if not rows:
        logger.info("Nothing to process.")
        return

    
    emails = aggregate_chunks(rows)
    total_emails = len(emails)
    total_chunks = sum(len(e["chunk_ids"]) for e in emails)
    logger.info(
        "Aggregated into %d unique emails (%d chunks).",
        total_emails, total_chunks,
    )

    
    batch: List[Dict] = []
    stats = {"risky": 0, "skipped": 0, "llm_calls": 0}

    for idx, email_group in enumerate(emails, start=1):
        results = process_email(email_group, models, proto_embeddings)

        if results:
            batch.extend(results)

            label = results[0]["risk_label"]
            if label == "skipped":
                stats["skipped"] += 1
            else:
                stats["risky"] += 1
            if results[0]["validated_by_llm"]:
                stats["llm_calls"] += 1

        if len(batch) >= BATCH_INSERT_SIZE:
            insert_batch(batch)
            batch.clear()

        if idx % 500 == 0:
            logger.info(
                "Progress: %d/%d | risky=%d skipped=%d llm=%d",
                idx, total_emails,
                stats["risky"], stats["skipped"], stats["llm_calls"],
            )

    if batch:
        insert_batch(batch)

    logger.info(
        "Pipeline complete: %d emails | %d risky | %d skipped | %d LLM calls",
        total_emails, stats["risky"], stats["skipped"], stats["llm_calls"],
    )