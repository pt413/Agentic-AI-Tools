

from datetime import datetime, timezone, timedelta
import traceback
import pytz
import boto3
import requests
import time
import os
import aiofiles
from dotenv import load_dotenv

from fastapi import APIRouter, Request, Depends, HTTPException, Query, UploadFile, File, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from urllib.parse import urlparse
from botocore.exceptions import ClientError
import urllib.parse
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from botocore.exceptions import ClientError
import boto3
import urllib.parse
import traceback
import os

import model
import schemas
from app.db.database import get_db
from config import AWS_ACCESS_KEY, AWS_SECRET_KEY, S3_BUCKET, REGION

# Load environment variables
load_dotenv()

# ==================== CONFIGURATION ====================

ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
if not ASSEMBLYAI_API_KEY:
    raise RuntimeError("ASSEMBLYAI_API_KEY is missing!")

ASSEMBLYAI_URL = "https://api.assemblyai.com/v2/transcript"
ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
HEADERS = {"authorization": ASSEMBLYAI_API_KEY}

# Initialize S3 client (singleton)
s3_client = boto3.client(
    "s3",
    region_name=REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
)

router = APIRouter()

# ==================== S3 UTILITY FUNCTIONS ====================

def generate_presigned_url(bucket: str, key: str, expiry: int = 3600) -> str:
    """Generate S3 presigned URL for download"""
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiry
        )
    except Exception as e:
        print(f"[ERROR] Failed to generate presigned URL: {e}")
        raise


# def extract_bucket_key_from_s3_url(s3_url: str) -> tuple:
#     """Extract bucket and key from S3 URL"""
#     try:
#         parsed = urlparse(s3_url)
#         # Handle both s3:// and https:// URLs
#         if parsed.scheme == 's3':
#             bucket = parsed.netloc
#             key = parsed.path.lstrip('/')
#         else:
#             # https://bucket.s3.region.amazonaws.com/key format
#             bucket = parsed.netloc.split('.')[0]
#             key = parsed.path.lstrip('/')
        
#         if not bucket or not key:
#             raise ValueError(f"Invalid S3 URL format: {s3_url}")
        
#         return bucket, key
#     except Exception as e:
#         print(f"[ERROR] Failed to parse S3 URL {s3_url}: {e}")
#         raise



def extract_bucket_key_from_s3_url(file_url: str):
    """
    Works for both:
      - https://bucket.s3.amazonaws.com/key
      - https://bucket.s3.ap-south-1.amazonaws.com/key
    """
    parsed = urllib.parse.urlparse(file_url)
    host_parts = parsed.netloc.split(".")
    
    # Example host_parts:
    # ['149561018306filebucket1', 's3', 'ap-south-1', 'amazonaws', 'com']
    if len(host_parts) < 3 or host_parts[1] != "s3":
        raise ValueError(f"Invalid S3 URL format: {file_url}")
    
    bucket = host_parts[0]
    key = parsed.path.lstrip("/")  # remove leading '/'
    
    if not bucket or not key:
        raise ValueError(f"Could not parse bucket/key from URL: {file_url}")
    
    return bucket, key


# ==================== TRANSCRIPTION UTILITY FUNCTIONS ====================

async def upload_file_to_assemblyai(file: UploadFile) -> str:
    """Upload file to AssemblyAI and return upload URL"""
    file_bytes = await file.read()
    if len(file_bytes) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 200MB)")
    
    # Validate file is not empty
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="File is empty")
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[INFO] Uploading file to AssemblyAI (attempt {attempt + 1}/{max_retries})")
            resp = requests.post(
                ASSEMBLYAI_UPLOAD_URL, 
                headers=HEADERS, 
                data=file_bytes,
                timeout=120  # 2 minute timeout for upload
            )
            resp.raise_for_status()
            upload_url = resp.json().get("upload_url")
            
            if not upload_url:
                raise ValueError("No upload_url in response")
            
            print(f"[SUCCESS] File uploaded to AssemblyAI: {upload_url}")
            return upload_url
            
        except requests.exceptions.Timeout:
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Upload timeout after retries")
            print(f"[WARN] Upload timeout, retrying in 2 seconds...")
            time.sleep(2)
            
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"[ERROR] AssemblyAI upload failed: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
            print(f"[WARN] Upload failed, retrying in 2 seconds...")
            time.sleep(2)


def poll_transcription(transcript_id: str, max_retries: int = 120) -> dict:
    """
    Poll AssemblyAI API until transcription completes
    
    Args:
        transcript_id: The transcript ID to poll
        max_retries: Maximum polling attempts (default: 120 * 3s = 6 minutes)
    
    Returns:
        Complete transcription data
    """
    print(f"[INFO] Starting to poll transcript: {transcript_id}")
    
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                f"{ASSEMBLYAI_URL}/{transcript_id}", 
                headers=HEADERS,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            
            transcript_status = data.get("status")
            
            # Log progress every 10 attempts
            if attempt % 10 == 0:
                print(f"[INFO] Polling attempt {attempt + 1}/{max_retries}, status: {transcript_status}")
            
            if transcript_status == "completed":
                # Validate we got text back
                text = data.get("text", "").strip()
                if not text:
                    print("[WARN] Transcription completed but text is empty")
                    # Check if audio was too short or silent
                    audio_duration = data.get("audio_duration", 0)
                    if audio_duration < 1:
                        raise HTTPException(
                            status_code=400,
                            detail="Audio file too short or silent"
                        )
                
                print(f"[SUCCESS] Transcription completed: {len(text)} characters")
                return data
                
            elif transcript_status == "error":
                error_msg = data.get('error', 'Unknown error')
                print(f"[ERROR] Transcription failed: {error_msg}")
                raise HTTPException(
                    status_code=500, 
                    detail=f"Transcription error: {error_msg}"
                )
            
            elif transcript_status in ["queued", "processing"]:
                # Normal statuses, continue polling
                time.sleep(3)
                
            else:
                # Unexpected status
                print(f"[WARN] Unexpected status: {transcript_status}")
                time.sleep(3)
            
        except requests.exceptions.Timeout:
            print(f"[WARN] Polling timeout on attempt {attempt + 1}")
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Polling timeout")
            time.sleep(5)
            
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Polling request failed: {str(e)}")
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail=f"Polling failed: {e}")
            time.sleep(5)
    
    raise HTTPException(
        status_code=500, 
        detail=f"Transcription timeout after {max_retries * 3} seconds"
    )


def load_word_boosts(folder: str = "app/word_boost", max_words: int = 15) -> List[str]:
    """Load word boost list from text files"""
    words = []
    if not os.path.exists(folder):
        print(f"[WARN] Word boost folder not found: {folder}")
        return words
    
    try:
        for file_name in os.listdir(folder):
            if file_name.endswith(".txt"):
                file_path = os.path.join(folder, file_name)
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            word_list = line.split()
                            if len(word_list) <= max_words:
                                words.append(line)
                            else:
                                # Split long phrases into chunks
                                for i in range(0, len(word_list), max_words):
                                    words.append(" ".join(word_list[i:i+max_words]))
        
        # Remove duplicates while preserving order
        words = list(dict.fromkeys(words))
        print(f"[INFO] Loaded {len(words)} word boost entries")
        
    except Exception as e:
        print(f"[ERROR] Failed to load word boosts: {e}")
    
    return words


def analyze_speaker_patterns(utterances: list) -> Dict[str, Any]:
    """Analyze speaker patterns from transcription utterances"""
    if not utterances:
        return {"warning": "No utterances available"}
    
    speaker_stats = {}
    
    for i, utt in enumerate(utterances):
        if not utt or 'speaker' not in utt:
            continue
        
        speaker = utt['speaker']
        text = utt.get('text', '').strip()
        start = utt.get('start', 0)
        end = utt.get('end', 0)
        duration = end - start
        
        if speaker not in speaker_stats:
            speaker_stats[speaker] = {
                'count': 0,
                'total_duration': 0,
                'total_words': 0
            }
        
        stats = speaker_stats[speaker]
        stats['count'] += 1
        stats['total_duration'] += duration
        stats['total_words'] += len(text.split())
    
    # Round durations for cleaner output
    for speaker in speaker_stats:
        speaker_stats[speaker]['total_duration'] = round(
            speaker_stats[speaker]['total_duration'], 2
        )
    
    return speaker_stats


def format_speaker_transcript_advanced(data: dict) -> Dict[str, Any]:
    """Format transcription with speaker analysis"""
    utterances = data.get("utterances")
    text = data.get("text", "").strip()
    
    if not text:
        return {
            "text": "",
            "speaker_analysis": {"warning": "No transcription text generated"}
        }
    
    if not utterances:
        return {
            "text": text,
            "speaker_analysis": {"warning": "No speaker diarization data available"}
        }
    
    stats = analyze_speaker_patterns(utterances)
    
    return {
        "text": text,
        "speaker_analysis": stats
    }