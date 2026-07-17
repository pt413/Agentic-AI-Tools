from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Body
from fastapi.responses import JSONResponse
import requests, time, os, aiofiles
#rom elevenlabs.client import ElevenLabs
from typing import  List
import requests
import aiohttp
from io import BytesIO
from dotenv import load_dotenv
from pydantic import BaseModel
from urllib.parse import urlparse
from typing import List, Dict, Any
import boto3
from app.model.audio_file_model import AudioFile
from app.model.test_transcription import TestTranscription
from app.db.database import  get_db
from sqlalchemy.orm import Session
import google.generativeai as genai
import json
import re
from datetime import datetime, timedelta
from app.services.whisper_service import transcribe_audio_from_url
import asyncio
# #NLLB Code
# LANG_MAP = {
#     "hi": "hin_Deva",
#     "te": "tel_Telu",
#     "kn": "kan_Knda",
#     "ta": "tam_Taml",
#     "ml": "mal_Mlym",
#     "pa": "pan_Guru",
#     "or": "ory_Orya",
#     "bn": "ben_Beng",
#     "mr": "mar_Deva",
#     "gu": "guj_Gujr"
# }

# # IndicTrans2 model
# import torch
# from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# MODEL_PATH = "/models/nllb"
# device = "cuda" if torch.cuda.is_available() else "cpu"

# tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)

# indi_model = AutoModelForSeq2SeqLM.from_pretrained(
#     MODEL_PATH,
#     torch_dtype=torch.float16 if device == "cuda" else torch.float32,
#     device_map="auto"
# )
# # thats it

# # any language to english translate
# def translate_to_english(text, lang):

#     # skip translation if already english
#     if lang and lang.startswith("en"):
#         return text

#     src_lang = LANG_MAP.get(lang, "eng_Latn")

#     tokenizer.src_lang = src_lang

#     inputs = tokenizer(text, return_tensors="pt").to(indi_model.device)

#     with torch.no_grad():
#         tokens = indi_model.generate(
#             **inputs,
#             forced_bos_token_id=tokenizer.convert_tokens_to_ids("eng_Latn"),
#             max_length=512
#         )

#     return tokenizer.batch_decode(tokens, skip_special_tokens=True)[0]


# ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
# if not ASSEMBLYAI_API_KEY:
#     raise RuntimeError("ASSEMBLYAI_API_KEY is missing!")

# ASSEMBLYAI_URL = "https://api.assemblyai.com/v2/transcript"
# ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
# HEADERS = {"authorization": ASSEMBLYAI_API_KEY}

#elevenlabs = ElevenLabs(
 # api_key=os.getenv("ELEVAN_LABS_API_KEY"),
#)

router = APIRouter(prefix="/api/transcribe", tags=["Transcription"])

s3_client = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY"),
    aws_secret_access_key=os.getenv("AWS_SECRET_KEY"),
    region_name=os.getenv("REGION", "us-east-1")
)

class AudioIDRequest(BaseModel):
    id: int

class CustomerNumberRequest(BaseModel):
    customer_number: str

class SalesNumberRequest(BaseModel):
    emp_phone_number: str 
    
# ---------------- Utility functions ----------------

# async def upload_file_to_assemblyai(file: UploadFile) -> str:
#     file_bytes = await file.read()
#     if len(file_bytes) > 200 * 1024 * 1024:
#         raise HTTPException(status_code=400, detail="File too large (max 200MB)")
#     try:
#         resp = requests.post(ASSEMBLYAI_UPLOAD_URL, headers=HEADERS, data=file_bytes)
#         resp.raise_for_status()
#         return resp.json()["upload_url"]
#     except requests.exceptions.RequestException as e:
#         raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

# def poll_transcription(transcript_id: str, max_retries: int = 60) -> dict:
#     for attempt in range(max_retries):
#         try:
#             resp = requests.get(f"{ASSEMBLYAI_URL}/{transcript_id}", headers=HEADERS)
#             print("resp done")
#             resp.raise_for_status()
#             data = resp.json()
#             print("data done")
#             status = data.get("status")
#             print(status)
#             if status == "completed":
#                 return data
#             elif status == "error":
#                 print("AssemblyAI error:", data.get("error"))
#                 raise HTTPException(
#                     status_code=500, 
#                     detail=f"Transcription error: {data.get('error', 'Unknown')}"
#                 )
#             time.sleep(3)
#         except requests.exceptions.RequestException as e:
#             if attempt == max_retries - 1:
#                 raise HTTPException(status_code=500, detail=f"Polling failed: {e}")
#             time.sleep(3)
#     raise HTTPException(status_code=500, detail="Transcription timeout")

def infer_roles_gemini(utterances):
    """
    Uses Gemini to determine which speaker (A/B) is Admin or Customer based on conversation text.
    """
    context_text = "\n".join([f"{u['speaker']}: {u['text']}" for u in utterances])

    prompt = f"""
    You are analyzing a two-person conversation labeled as A and B.

    Your task:
    1. Identify which speaker is the Admin (the support agent, caretaker, sales, operations, or company representative),
    and which speaker is the Customer (the client, user, or person asking for help or property).
    2. Admins typically greet, explain, offer, or ask for customer details.
    3. Customers typically inquire, request, or provide their requirements or responses.
    4. Return strictly in JSON format only, no explaination, no markdowns.
    5. Strictly don't change the words or paraphrase the sentences or words.
    Now analyze the following conversation and infer roles:

    Output only valid JSON in this exact format:
    {{
      "Admin": "A" or "B",
      "Customer": "A" or "B",
      "ExactConversation": [
         {{ "speaker": "A" or "B", "text": "..." }},
         ...
      ]
    }}

    Conversation:
    {context_text}
    """

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        raw_text=response.text.strip()
        raw_text = re.sub(r"^```(?:json)?", "", raw_text)
        raw_text = re.sub(r"```$", "", raw_text)
        raw_text = raw_text.strip()
        data = json.loads(raw_text)
        return data
    except Exception:
        print("⚠️ Gemini response parsing failed")
        return {"Admin": "A", "Customer": "B", "ImprovedConversation": utterances}

# @router.post("/file")
# async def transcribe_file(file: UploadFile = File(...)):
#     """
#     Frontend uploads: simple transcription, no speaker diarization
#     """
#     try:
#         print(f"Received file: {file.filename}, content-type: {file.content_type}, size: {file.size}")
        
#         file_contents = await file.read()
#         print(f"Actual file size: {len(file_contents)} bytes")
        
#         await file.seek(0)
        
#         upload_url = await upload_file_to_assemblyai(file)
#         print(f"Upload URL obtained: {upload_url}")
        
#         payload = {
#             "audio_url": upload_url,
#             "language_detection": True,
#             "punctuate": True,
#             "format_text": True,
#             "speech_model": "best"
#         }
        
#         print("Sending to AssemblyAI...")
#         resp = requests.post(
#             ASSEMBLYAI_URL,
#             headers={**HEADERS, "content-type": "application/json"},
#             json=payload,
#             timeout=30
#         )
#         resp.raise_for_status()
        
#         transcript_id = resp.json()["id"]
#         print(f"Transcript ID: {transcript_id}")
        
#         data = poll_transcription(transcript_id)
#         print(f"Transcription completed: {len(data.get('text', ''))} characters")
        
#         return JSONResponse({"text": data.get("text", "")})
        
#     except Exception as e:
#         print("Transcription error details:", str(e))
#         import traceback
#         traceback.print_exc()
#         return JSONResponse({"text": "Error during transcription."}, status_code=500)

@router.post("/file")
async def transcribe_file(file: UploadFile = File(...)):

    try:
        contents = await file.read()

        import tempfile

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp.write(contents)
            temp_path = tmp.name

        from app.services.whisper_service import model

        segments, info = model.transcribe(temp_path)

        text = " ".join([segment.text for segment in segments])

        return {"text": text}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def load_word_boosts(folder: str = "app/word_boost", max_words: int = 15) -> List[str]:
    words = []
    if not os.path.exists(folder):
        return words
    for file_name in os.listdir(folder):
        if file_name.endswith(".txt"):
            with open(os.path.join(folder, file_name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        word_list = line.split()
                        if len(word_list) <= max_words:
                            words.append(line)
                        else:
                            for i in range(0, len(word_list), max_words):
                                words.append(" ".join(word_list[i:i+max_words]))
    return list(dict.fromkeys(words))


# Speaker analysis helper
def analyze_speaker_patterns(utterances: list, speaker_map: dict) -> Dict[str, Any]:
    speaker_stats = {}
    for utt in utterances:
        raw_speaker = utt.get("speaker")
        speaker = speaker_map.get(raw_speaker, raw_speaker)
        text = utt.get('text', '').strip()
        duration = utt.get('end', 0) - utt.get('start', 0)
        if speaker not in speaker_stats:
            speaker_stats[speaker] = {'count': 0, 'total_duration': 0, 'total_words': 0}
        stats = speaker_stats[speaker]
        stats['count'] += 1
        stats['total_duration'] += duration
        stats['total_words'] += len(text.split())
    return speaker_stats

# def format_speaker_transcript(data: dict) -> Dict[str, Any]:
#     utterances = data.get("utterances") or []
#     if not utterances:
#         return {"text": data.get("text") or "", "speaker_analysis": {"warning": "No speaker data"}}

#     inferred_data = infer_roles_gemini(utterances)
#     role_map = {
#         inferred_data.get("Admin"): "Admin",
#         inferred_data.get("Customer"): "Customer"
#     }
#     utterances = inferred_data.get("ImprovedConversation", utterances)
#     transcript_lines = []
#     for utt in utterances:
#         raw_speaker = utt.get("speaker", "Unknown")
#         speaker = role_map.get(raw_speaker, raw_speaker)
#         text = utt.get("text", "").strip()
#         if text:
#             transcript_lines.append(f"{speaker}: {text}")

#     speaker_text = "\n".join(transcript_lines)
#     stats = analyze_speaker_patterns(utterances, role_map)

#     return {
#         "text": speaker_text,
#         "speaker_analysis": stats,
#         "role_map": role_map
#     }


def format_speaker_transcript(data: dict) -> Dict[str, Any]:
  """ Return only the raw transcription text without any speaker roles or diarization. """ 
  raw_text = data.get("text", "") 
  return {"text": raw_text}  

@router.post("/url")
async def transcribe_from_id(request: AudioIDRequest, db: Session = Depends(get_db)):
    audio_file = db.query(AudioFile).filter(AudioFile.id == request.id).first()
    if not audio_file:
        raise HTTPException(status_code=404, detail="Audio file not found")
    if audio_file.transcript_text:
        return audio_file.transcript_text
    presigned_url = audio_file.audio_url
    # print(audio_file.id, ":", presigned_url)
    payload = {
        "audio_url": presigned_url,
        "speaker_labels": True,
        "language_detection": True,
        "punctuate": True,
        "format_text": True,
        "speech_model": "best",
        "word_boost": load_word_boosts(),
        "boost_param": "high"
    }

    try:
        #resp = requests.post(ASSEMBLYAI_URL, headers={**HEADERS, "content-type": "application/json"}, json=payload)
        data = transcribe_audio_from_url(presigned_url)
        raw_text = data["text"]
        text_in_english = data["translated_text"]
        lang = data["language"]
        transcript_id = "local_whisper"
        # print("DEBUG: AssemblyAI response status =", resp.status_code)
        # print("DEBUG RAW RESPONSE =", resp.text)
        # resp.raise_for_status()
        # print("resp for status done")
        # transcript_id = resp.json()["id"]
        # print(transcript_id)
        # data = poll_transcription(transcript_id)
        # print("polling done")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")

    # # result = format_speaker_transcript(data)
    # # audio_file.transcript_text = result["text"]
    # raw_text = data.get("text", "")
    # audio_file.transcript_text = raw_text
    # # audio_file.uploaded_at = audio_file.uploaded_at + timedelta(hours=5, minutes=30)
    # audio_file.status = 1
    # db.add(audio_file)
    # db.commit()
    audio_file.transcript_text = raw_text
    audio_file.status = 1
    audio_file.translated_text=text_in_english
    db.add(audio_file)
    db.commit()

    return {
        "status": "completed",
        "transcript_id": transcript_id,
        "text":raw_text,
        "translated_text": text_in_english
        # "text": result["text"],
        # "speaker_analysis": result["speaker_analysis"],
        # "language": data.get("language_code", "en")
    }

# 20 transcription per sales_person
@router.post("/transcribe_top20")
async def transcribe_top20_per_salesperson(
    request: SalesNumberRequest,
    db: Session = Depends(get_db)
):
    sales_phone_number = request.emp_phone_number
    audio_files = (
        db.query(AudioFile)
        .filter(
            AudioFile.emp_phone_number == sales_phone_number,
            AudioFile.audio_url.isnot(None),  
            AudioFile.transcript_text.is_(None)
        )
        .order_by(AudioFile.call_datetime.desc())  
        .limit(10)
        .all()
    )

    if not audio_files:
        raise HTTPException(
            status_code=404, 
            detail="No untranscribed audio files found for this salesperson"
        )

    results = []
    for audio_file in audio_files:
        try:
            audio = AudioIDRequest(id=audio_file.id)
            result=await transcribe_from_id(audio,db)
            results.append({
                "id": audio_file.id,
                "call_date": audio_file.call_datetime.isoformat() if audio_file.call_datetime else None,
                "status": "success",
                # "language": data.get("language_code", "en"),
                # "transcript_id": transcript_id
            })

        except Exception as e:
            results.append({
                "id": audio_file.id,
                "call_date": audio_file.call_datetime.isoformat() if audio_file.call_datetime else None,
                "status": "failed",
                "error": str(e)
            })
            continue

    # Return summary
    successful_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] == "failed")

    return {
        "sales_phone_number": sales_phone_number,
        "total_processed": len(results),
        "successful": successful_count,
        "failed": failed_count,
        "results": results
    }

# Transcribe 20 files per salesperson (auto)
@router.post("/transcribe_top20_all")
async def transcribe_top20_per_salesperson(
    db: Session = Depends(get_db)
):
    from sqlalchemy import distinct

    # 1️⃣ Get all unique salesperson numbers
    phone_numbers = (
        db.query(distinct(AudioFile.emp_phone_number))
        .filter(AudioFile.emp_phone_number.isnot(None))
        .all()
    )

    if not phone_numbers:
        raise HTTPException(
            status_code=404,
            detail="No salesperson phone numbers found"
        )

    final_results = []

    # 2️⃣ Process each salesperson
    for (sales_phone_number,) in phone_numbers:

        audio_files = (
            db.query(AudioFile)
            .filter(
                AudioFile.emp_phone_number == sales_phone_number,
                AudioFile.audio_url.isnot(None),
                AudioFile.transcript_text.is_(None)
            )
            .order_by(AudioFile.call_datetime.desc())
            .limit(20)   # ← 20 per salesperson
            .all()
        )

        if not audio_files:
            final_results.append({
                "sales_phone_number": sales_phone_number,
                "total_processed": 0,
                "successful": 0,
                "failed": 0,
                "message": "No untranscribed audio found"
            })
            continue

        results = []

        # 3️⃣ Transcribe files
        for audio_file in audio_files:
            try:
                audio = AudioIDRequest(id=audio_file.id)
                await transcribe_from_id(audio, db)

                results.append({
                    "id": audio_file.id,
                    "call_date": audio_file.call_datetime.isoformat() if audio_file.call_datetime else None,
                    "status": "success"
                })

            except Exception as e:
                results.append({
                    "id": audio_file.id,
                    "call_date": audio_file.call_datetime.isoformat() if audio_file.call_datetime else None,
                    "status": "failed",
                    "error": str(e)
                })

        # 4️⃣ Summary per salesperson
        successful_count = sum(1 for r in results if r["status"] == "success")
        failed_count = sum(1 for r in results if r["status"] == "failed")

        final_results.append({
            "sales_phone_number": sales_phone_number,
            "total_processed": len(results),
            "successful": successful_count,
            "failed": failed_count,
            "results": results
        })

    return {
        "total_salespersons": len(final_results),
        "summary": final_results
    }












# @router.post("/url_elevan_labs")
# async def transcribe_from_id_elevan_labs(request: AudioIDRequest, db: Session = Depends(get_db)):

#     audio_file = db.query(AudioFile).filter(AudioFile.id == request.id).first()
    
#     if not audio_file:
#         raise HTTPException(status_code=404, detail="Audio file not found")
#     if audio_file.raw_transcripts:
#         return audio_file.raw_transcripts
    
#     bucket, key = extract_bucket_key_from_s3_url(audio_file.audio_url)
#     presigned_url = generate_presigned_url(bucket, key)
#     # Download audio
#     try:
#         async with aiohttp.ClientSession() as session:
#             async with session.get(str(presigned_url), timeout=30) as resp:
#                 if resp.status != 200:
#                     raise HTTPException(status_code=400, detail=f"Error downloading audio: {resp.status}")
                
#                 audio_bytes = await resp.read()
#                 if not audio_bytes:
#                     raise HTTPException(status_code=400, detail="Audio file empty at URL")
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Failed to fetch audio: {str(e)}")

#     audio_buffer = BytesIO(audio_bytes)

#     #  Minimal required params for ElevenLabs STT
#     params = {
#         "model_id": "scribe_v1",
#         "file": audio_buffer,
#         "tag_audio_events": True,
#         "diarize": True,
#         "timestamps_granularity": "word",
#     }

#     #  Call ElevenLabs
#     try:
#         result = elevenlabs.speech_to_text.convert(**params)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"ElevenLabs STT error: {str(e)}")
    
    

#     audio_file.transcript_text_eleven_labs = result.text
#     audio_file.raw_eleven_labs_transcript = result.model_dump()
#     # audio_file.status = 1
#     db.add(audio_file)
#     db.commit()
#     # Return
#     return {"success": True, "result": result}

# #20 transcription per sales_person using eleven labs
# @router.post("/url_elevan_labs_top20")
# async def transcribe_from_salesperson_elevan_labs(request: SalesNumberRequest, db: Session = Depends(get_db)):

#     sales_phone_number = request.emp_phone_number

#     audio_files =( 
#         db.query(AudioFile).filter(
#         AudioFile.emp_phone_number == sales_phone_number,
#         AudioFile.audio_url.isnot(None),  
#         AudioFile.transcript_text_eleven_labs.is_(None) 
#         ).order_by(AudioFile.call_datetime.desc()).limit(20).all()
#         )
    
#     if not audio_files:
#         raise HTTPException(
#             status_code=404, 
#             detail="No untranscribed audio files found for this salesperson"
#         )
    
#     results = []

#     # Transcribe each one sequentially by calling transcribe_from_id
#     for audio_file in audio_files:
#         try:
#             audio = AudioIDRequest(id=audio_file.id)
#             result=await transcribe_from_id_elevan_labs(audio,db)
#             results.append({
#                 "id": audio_file.id,
#                 "call_date": audio_file.call_datetime.isoformat() if audio_file.call_datetime else None,
#                 "status": "success",
#                 # "language": data.get("language_code", "en"),
#                 # "transcript_id": transcript_id
#             })

#         except Exception as e:
#             results.append({
#                 "id": audio_file.id,
#                 "call_date": audio_file.call_datetime.isoformat() if audio_file.call_datetime else None,
#                 "status": "failed",
#                 "error": str(e)
#             })
#             continue

#     # Return summary
#     successful_count = sum(1 for r in results if r["status"] == "success")
#     failed_count = sum(1 for r in results if r["status"] == "failed")

#     return {
#         "sales_phone_number": sales_phone_number,
#         "total_processed": len(results),
#         "successful": successful_count,
#         "failed": failed_count,
#         "results": results
#     }


@router.post("/all")
async def transcribe_all(db: Session = Depends(get_db)):
    """
    Transcribe all audio files that do not yet have transcribed_text.
    Iterates from first to last ID in the table.
    """
    results = []

    first_id = db.query(AudioFile.id).order_by(AudioFile.id.asc()).first()
    last_id = db.query(AudioFile.id).order_by(AudioFile.id.desc()).first()

    if not first_id or not last_id:
        return {"summary": [], "message": "No audio files found"}

    start_id = first_id[0]
    end_id = last_id[0]

    for i in range(start_id, end_id + 1):
        try:
            audio_file = db.query(AudioFile).filter(AudioFile.id == i).first()
            if not audio_file:
                results.append({"id": i, "status": "skipped", "reason": "Record not found"})
                continue

            if audio_file.transcribed_text:
                results.append({"id": i, "status": "skipped", "reason": "Already transcribed"})
                continue

            request = AudioIDRequest(id=i)
            await transcribe_from_id(request, db)
            results.append({"id": i, "status": "success"})
        except Exception as e:
            print(f"Failed to transcribe ID {i}: {e}")
            results.append({"id": i, "status": "failed", "error": str(e)})

    return {"summary": results}

@router.post("/set_status")
async def mark_for_transcription(
    payload: CustomerNumberRequest,
    db: Session = Depends(get_db)
):
    """
    Marks all audio files for a specific customer number (ph_num)
    with status = 1.
    """
    customer_number = payload.customer_number

    updated_rows = (
        db.query(AudioFile)
        .filter(AudioFile.ph_num == customer_number)
        .update({"status": 1}, synchronize_session=False)
    )
    db.commit()

    if updated_rows == 0:
        raise HTTPException(status_code=404, detail="No records found for that customer number")

    return {"message": f"{updated_rows} records updated to status=1 for {customer_number}"}

@router.post("/run_pending_jobs")
async def run_pending_jobs(db: Session = Depends(get_db)):
    """
    Finds all records where status=1, transcribed_text is null,
    and file_url is not null, then transcribes them.
    Intended to be called by a cron job.
    """
    pending_files = (
        db.query(AudioFile)
        .filter(
            AudioFile.status == 1,
            AudioFile.raw_transcripts.is_(None),
            AudioFile.audio_url.isnot(None)
        )
        .all()
    )

    if not pending_files:
        return {"message": "No pending audio files to transcribe"}

    results = []

    for file in pending_files:
        try:
            request = AudioIDRequest(id=file.id)
            await transcribe_from_id(request, db)
            results.append({"id": file.id, "status": "success"})
        except Exception as e:
            results.append({"id": file.id, "status": "failed", "error": str(e)})

    return {"summary": results}


@router.post("/refresh_audio")
async def refresh_audio_by_sales_number(
    body: SalesNumberRequest,
    db: Session = Depends(get_db)
):
    """
    Refresh transcription for all audio files linked to a specific sales phone number.
    Runs transcription in background to avoid Cloudflare timeout.
    """

    sales_phone_number = body.emp_phone_number
    print("Received sales_phone_number:", sales_phone_number)

    if not sales_phone_number:
        raise HTTPException(status_code=400, detail="sales_phone_number is required")

    audio_ids = (
        db.query(AudioFile.id)
        .filter(AudioFile.emp_phone_number == sales_phone_number)
        .all()
    )

    if not audio_ids:
        raise HTTPException(status_code=404, detail=f"No audio found for {sales_phone_number}")

    id_list = [aid[0] for aid in audio_ids]
    print(f"Found {len(id_list)} audio files for {sales_phone_number}")

    async def background_job(ids):
        for audio_id in ids:
            try:
                request = AudioIDRequest(id=audio_id)
                await transcribe_from_id(request, db)
                print(f"Transcribed: {audio_id}")
            except Exception as e:
                print(f"Failed for {audio_id}: {e}")

    # 🔹 Run transcription in background
    asyncio.create_task(background_job(id_list))

    # 🔹 Return immediately (no timeout)
    return {
        "status": "started",
        "sales_phone_number": sales_phone_number,
        "total_files": len(id_list)
    }

@router.post("/set_status")
async def mark_for_transcription(
    payload: CustomerNumberRequest,
    db: Session = Depends(get_db)
):
    """
    Marks all audio files for a specific customer number (ph_num)
    with status = 1.
    """
    customer_number = payload.customer_number

    updated_rows = (
        db.query(AudioFile)
        .filter(AudioFile.ph_num == customer_number)
        .update({"status": 1}, synchronize_session=False)
    )
    db.commit()

    if updated_rows == 0:
        raise HTTPException(status_code=404, detail="No records found for that customer number")

    return {"message": f"{updated_rows} records updated to status=1 for {customer_number}"}


async def run_pending_transcription(db: Session = Depends(get_db)):
    """
    Finds all records where status=1, transcribed_text is null,
    and file_url is not null, then transcribes them.
    Intended to be called by a cron job.
    """
    pending_files = (
        db.query(AudioFile)
        .filter(
            AudioFile.sync_status == 1,
            AudioFile.transcript_text.is_(None),
            AudioFile.audio_url.isnot(None)
        )
        .all()
    )

    if not pending_files:
        return {"message": "No pending audio files to transcribe"}

    results = []

    for file in pending_files:
        try:
            request = AudioIDRequest(id=file.id)
            await transcribe_from_id(request, db)
            results.append({"id": file.id, "status": "success"})
        except Exception as e:
            results.append({"id": file.id, "status": "failed", "error": str(e)})

    return {"summary": results}












from sqlalchemy import or_


@router.post("/run_pending_jobs_top_k")
async def run_pending_jobs_top20(db: Session = Depends(get_db)):

    # Fetch latest pending files (raw_transcripts is None or "[PENDING]")
    pending_files = (
        db.query(AudioFile)
        .filter(
            or_(AudioFile.raw_transcripts.is_(None), AudioFile.raw_transcripts == "[PENDING]"),
            AudioFile.audio_url.isnot(None),
            AudioFile.audio_url != ''
        )
        .order_by(AudioFile.call_datetime.desc())
        .limit(200)
        .all()
    )

    if not pending_files:
        return {"message": "No pending audio files to transcribe"}

    results = []

    for file in pending_files:
        try:
            print(f"\n Processing ID: {file.id} | Date: {file.call_datetime}")
            # Skip very short files or empty audio
            if file.call_duration <= 1:

                print(f" Skipped (too short): ID {file.id}")

                file.raw_transcripts = " "
                file.status = 2  # mark as processed
                db.commit()

                results.append({
                    "id": file.id,
                    "call_date": file.call_datetime.isoformat() if file.call_datetime else None,
                    "status": "skipped",
                    "reason": "Audio too short or no audio"
                })
                continue

            # Prepare request to transcribe
            request = AudioIDRequest(id=file.id)

            try:
                await transcribe_from_id(request, db)

                # After successful transcription
                file.status = 2  # mark as processed
                db.commit()


                print(f" SUCCESS: ID {file.id}")
                results.append({
                    "id": file.id,
                    "call_date": file.call_datetime.isoformat() if file.call_datetime else None,
                    "status": "success"
                })

            except Exception as e_inner:
                error_msg = str(e_inner)

                # Handle files with no spoken audio
                if "language_detection cannot be performed on files with no spoken audio" in error_msg:
                    print(f" Skipped (no spoken audio): ID {file.id}")

                    file.raw_transcripts = " "
                    file.status = 2
                    db.commit()
                    results.append({
                        "id": file.id,
                        "call_date": file.call_datetime.isoformat() if file.call_datetime else None,
                        "status": "skipped",
                        "reason": "No spoken audio"
                    })
                else:
                    print(f" FAILED: ID {file.id} | Error: {error_msg}")
                    results.append({
                        "id": file.id,
                        "call_date": file.call_datetime.isoformat() if file.call_datetime else None,
                        "status": "failed",
                        "error": error_msg
                    })

        except Exception as e_outer:
            print(f" CRITICAL ERROR: ID {file.id} | {str(e_outer)}")
            results.append({
                "id": file.id,
                "call_date": file.call_datetime.isoformat() if file.call_datetime else None,
                "status": "failed",
                "error": str(e_outer)
            })

    # Count results
    successful_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")

    return {
        "total_processed": len(results),
        "successful": successful_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "results": results
    }
