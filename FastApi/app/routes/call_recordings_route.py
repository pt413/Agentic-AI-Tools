
from datetime import datetime, timezone, timedelta
import traceback
import pytz
import boto3
import requests
import time
import os
import aiofiles
from dotenv import load_dotenv

from fastapi import (
    APIRouter,
    Request,
    Depends,
    HTTPException,
    Query,
    UploadFile,
    File,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from urllib.parse import urlparse
from botocore.exceptions import ClientError

from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from botocore.exceptions import ClientError
import boto3
import urllib.parse
import traceback
import os

from app.db.database import get_db
from app.model.audio_file_model import AudioFile, Location
from app.model.sales_data import SalesData
import schemas
from app.utils.call_recordings_utils import (
    extract_bucket_key_from_s3_url,
    generate_presigned_url,
    format_speaker_transcript_advanced,
    load_word_boosts,
    poll_transcription,
)
from app.config import AWS_ACCESS_KEY, AWS_SECRET_KEY, S3_BUCKET, REGION
from app.schemas.call_recordings_route_schema import *

router = APIRouter(prefix="/api", tags=["Calls_recordings_transcript"])

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

# ==================== ENHANCED PYDANTIC MODELS ====================


class AudioIDRequest(BaseModel):
    """Request model for audio ID operations"""

    id: int = Field(..., description="Unique identifier of the audio file", example=123)

    class Config:
        json_schema_extra = {"example": {"id": 123}}


class TranscribeUrlRequest(BaseModel):
    """Request model for transcribing from S3 URL"""

    file_url: str = Field(
        ...,
        description="S3 URL of the audio file to transcribe",
        example="https://bucket-name.s3.region.amazonaws.com/audio.mp3",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "file_url": "https://my-bucket.s3.us-east-1.amazonaws.com/recordings/call_123.mp3"
            }
        }


class PresignedUrlRequest(BaseModel):
    """Request model for generating presigned S3 URLs"""

    filename: str = Field(
        ..., description="Name of the file to upload", example="recording_123.mp3"
    )
    file_type: str = Field(
        ..., description="MIME type of the file", example="audio/mpeg"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "filename": "call_recording_20231115.mp3",
                "file_type": "audio/mpeg",
            }
        }


class PresignedUrlResponse(BaseModel):
    """Response model for presigned URL generation"""

    upload_url: str = Field(..., description="Presigned URL for uploading the file")
    file_url: str = Field(
        ..., description="Final S3 URL where the file will be accessible"
    )


class ErrorResponse(BaseModel):
    """Standard error response model"""

    error: str = Field(..., description="Error message")
    details: Optional[str] = Field(None, description="Additional error details")


class TranscriptionResponse(BaseModel):
    """Response model for transcription operations"""

    status: str = Field(..., description="Transcription status", example="completed")
    transcript_id: str = Field(..., description="AssemblyAI transcript ID")
    text: str = Field(..., description="Transcribed text")
    language: str = Field(..., description="Detected language code", example="en")
    speaker_analysis: Optional[Dict[str, Any]] = Field(
        None, description="Speaker diarization analysis"
    )


class FileExistsResponse(BaseModel):
    """Response model for file existence check"""

    exists: bool = Field(..., description="Whether the file exists in S3")
    s3Key: str = Field(..., description="S3 key that was checked")


class SalesNumberCheckResponse(BaseModel):
    """Response model for sales number verification"""

    username: Optional[str] = Field(None, description="Username of the sales person")
    salesPhoneNumber: str = Field(..., description="Sales phone number")
    admin_Team: Optional[str] = Field(None, description="Admin team name")
    exists: bool = Field(..., description="Whether the sales number exists")


class LocationRequest(BaseModel):
    """Request model for saving location data"""

    sales_number: str = Field(
        ..., description="Sales person phone number", example="9876543210"
    )
    address: str = Field(
        ...,
        description="Location address or coordinates",
        example="Mumbai, Maharashtra",
    )
    timestamp: Optional[int] = Field(
        None, description="Timestamp in milliseconds", example=1699876543210
    )

    class Config:
        json_schema_extra = {
            "example": {
                "sales_number": "9876543210",
                "address": "Andheri West, Mumbai, Maharashtra 400053",
                "timestamp": 1699876543210,
            }
        }


class CallLogRequest(BaseModel):
    """Request model for saving call logs from Android"""

    sales_number: str = Field(
        ..., description="Sales person phone number", example="9164729897"
    )
    call_id: str = Field(..., description="Unique Android CallLog ID", example="6602")
    name: Optional[str] = Field(None, description="Contact name", example="John Doe")
    number: str = Field(
        ..., description="Customer phone number", example="918088044937"
    )
    date_ms: str = Field(
        ..., description="Call timestamp in milliseconds", example="1731372103511"
    )
    duration_sec: str = Field(..., description="Call duration in seconds", example="45")
    type: str = Field(
        ..., description="Call type: incoming, outgoing, missed", example="outgoing"
    )
    acc_id: Optional[str] = Field(
        None, description="Account/Team ID", example="Finance"
    )
    file_url: Optional[str] = Field("", description="S3 URL of call recording")
    filename: Optional[str] = Field("", description="Recording filename")

    class Config:
        json_schema_extra = {
            "example": {
                "sales_number": "9164729897",
                "call_id": "6602",
                "name": "John Doe",
                "number": "918088044937",
                "date_ms": "1731372103511",
                "duration_sec": "45",
                "type": "outgoing",
                "acc_id": "Finance",
                "file_url": "",
                "filename": "",
            }
        }


# ==================== FILE & LOCATION ENDPOINTS ====================


@router.get(
    "/",
    tags=["Health Check"],
    summary="API Health Check",
    description="Check if the Audio Upload API is running and operational",
    response_description="API status message",
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "API is running",
            "content": {
                "application/json": {
                    "example": {"message": "Audio Upload API is running!"}
                }
            },
        }
    },
)
def index():
    """
    Root endpoint for health checking.

    Returns a simple message confirming the API is running.
    """
    return {"message": "Audio Upload API is running!"}


@router.post(
    "/get-presigned-url",
    tags=["S3 Storage"],
    summary="Generate Presigned S3 URL",
    description="Generate a presigned S3 URL for uploading audio files directly to S3 from mobile devices",
    response_description="Presigned upload URL and final file URL",
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Presigned URL generated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "upload_url": "https://bucket.s3.amazonaws.com/file?signature=...",
                        "file_url": "https://bucket.s3.amazonaws.com/file.mp3",
                    }
                }
            },
        },
        400: {"model": ErrorResponse, "description": "Invalid request parameters"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_presigned_url(request: PresignedUrlRequest):
    """
    Generate a presigned S3 URL for uploading files.

    **Request Body:**
    - **filename**: Name of the file to upload
    - **file_type**: MIME type (e.g., "audio/mpeg", "audio/wav")

    **Returns:**
    - **upload_url**: Presigned URL to PUT the file
    - **file_url**: Final S3 URL where file will be accessible
    """
    try:
        filename = request.filename
        file_type = request.file_type

        if not filename or not file_type:
            return JSONResponse(
                {"error": "Both 'filename' and 'file_type' are required."},
                status_code=400,
            )

        upload_url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": S3_BUCKET,
                "Key": filename,
                "ContentType": file_type,
            },
            ExpiresIn=3600,
        )

        file_url = f"https://{S3_BUCKET}.s3.{REGION}.amazonaws.com/{filename}"

        print(f"[INFO] Generated presigned URL for file: {filename}")

        return JSONResponse({"upload_url": upload_url, "file_url": file_url})

    except Exception as e:
        print(f"[ERROR] Failed to generate presigned URL: {str(e)}")
        return JSONResponse(
            {"error": f"Internal server error: {str(e)}"}, status_code=500
        )


@router.post(
    "/save-location",
    tags=["Location Tracking"],
    summary="Save Location Data",
    description="Save GPS location data from mobile devices for sales team tracking",
    response_description="Location save confirmation",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Location already exists (duplicate)"},
        201: {"description": "Location saved successfully"},
        400: {"model": ErrorResponse, "description": "Missing required fields"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def save_location(request: LocationCreate, db: Session = Depends(get_db)):
    """
    Save location details sent from mobile devices.

    **Request Body:**
    - **sales_number**: Phone number of the sales person
    - **address**: Location address/coordinates
    - **timestamp**: Optional timestamp in milliseconds

    **Features:**
    - Prevents duplicate location entries
    - Converts timestamps to IST timezone
    - Returns existing record ID if duplicate found
    """
    try:
        data = request.dict()
        print(f"[DEBUG] Received location data: {data}")

        sales_number = data.get("sales_number")
        address = data.get("address")

        if not sales_number or not address:
            return JSONResponse(
                {"error": "sales_number and address are required"},
                status_code=400,
            )

        # Check for existing record
        existing_location = (
            db.query(Location)
            .filter(
                Location.sales_phone_number == sales_number,
                Location.location == address,
            )
            .first()
        )

        if existing_location:
            print(f"[INFO] Duplicate location ignored for {sales_number}")
            return JSONResponse(
                {
                    "message": "Location already exists",
                    "sales_number": sales_number,
                    "location": address,
                    "id": existing_location.id,
                    "status": "duplicate",
                },
                status_code=200,
            )

        # Handle timestamp (optional)
        timestamp = data.get("timestamp")
        if timestamp:
            try:
                ts = int(timestamp)
                if ts > 1e12:  # Convert ms → s if needed
                    ts /= 1000
                time_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                time_utc = datetime.now(tz=timezone.utc)
        else:
            time_utc = datetime.now(tz=timezone.utc)

        # Convert to IST
        ist = pytz.timezone("Asia/Kolkata")
        time_ist = time_utc.astimezone(ist)

        # Create new location record
        new_location = Location(
            sales_phone_number=sales_number,
            location=address,
            time=time_ist,
        )

        db.add(new_location)
        db.commit()
        db.refresh(new_location)

        print(f"[SUCCESS] Location saved for {sales_number}")

        return JSONResponse(
            {
                "message": "Location saved successfully",
                "sales_number": sales_number,
                "location": address,
                "id": new_location.id,
                "status": "created",
            },
            status_code=201,
        )

    except Exception as e:
        db.rollback()
        print(f"[ERROR] Exception in save_location: {str(e)}")
        print(traceback.format_exc())
        return JSONResponse(
            {"error": f"Internal server error: {str(e)}"},
            status_code=500,
        )


@router.post("/save-call-log", response_model=CallLogResponse)
async def save_call_log(request: Request, db: Session = Depends(get_db)):
    """Save call log from Android app"""
    try:
        data = await request.json()
        print(f"[DEBUG] Received call log data: {data}")

        # ✅ NOW validate with updated schema
        payload = schemas.CallLogCreate(**data)
        
        # Extract fields
        call_id = payload.call_id
        sales_number = payload.sales_number
        contact_name = payload.name
        customer_phone = payload.number  # ✅ Use 'number'
        date_ms = payload.date_ms  # ✅ Use 'date_ms'
        duration_sec = payload.duration_sec
        call_type = payload.type  # ✅ Use 'type'
        acc_id = payload.acc_id
        file_url = payload.file_url
        filename = payload.filename

        # Validate required fields
        if not call_id or not sales_number:
            return JSONResponse(
                {"error": "call_id and sales_number are required"},
                status_code=400
            )

        # Get sales user details
        sales_data = (
            db.query(SalesData.username, SalesData.admin_Team)
            .filter(SalesData.salesPhoneNumber == sales_number)
            .first()
        )
        
        db_username, db_admin_team = sales_data if sales_data else (None, None)
        username = db_username or contact_name or "Unknown"
        admin_team = db_admin_team or acc_id

        # Clean customer phone number
        if customer_phone:
            customer_phone = (
                customer_phone.replace("+", "")
                .replace(" ", "")
                .replace("-", "")
            )
            if len(customer_phone) > 10:
                customer_phone = customer_phone[-10:]
        else:
            customer_phone = "Unknown"

        # Convert timestamp (ms) → datetime
        try:
            timestamp_s = int(date_ms) / 1000
            call_date = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        except (ValueError, TypeError) as e:
            print(f"[ERROR] Invalid date_ms: {date_ms}")
            return JSONResponse(
                {"error": "Invalid date_ms timestamp"},
                status_code=400
            )

        # Parse duration
        try:
            call_duration = int(duration_sec or 0)
        except (ValueError, TypeError):
            call_duration = 0

        # Normalize call_type
        call_type_normalized = call_type.capitalize() if call_type else "Incoming"

        # Normalize file info
        if not filename or filename.lower() == "null" or filename.strip() == "":
            filename = None
        
        if not file_url or file_url.lower() == "null" or file_url.strip() == "":
            file_url = None

        # Check for existing call - ✅ Updated to use AudioFile model
        existing_call = (
            db.query(AudioFile)
            .filter(
                AudioFile.emp_phone_number == sales_number,
                AudioFile.call_id == call_id
            )
            .first()
        )

        if existing_call:
            print(f"[INFO] Found existing call with call_id: {call_id}")
            return schemas.CallLogResponse(
                message="Call log already exists",
                existing_id=existing_call.id,
                status="duplicate",
                has_recording=bool(existing_call.audio_url)
            )

        # Missed calls have 0 duration
        if call_type_normalized.lower() == "missed":
            call_duration = 0

        # Convert UTC → IST
        ist = pytz.timezone("Asia/Kolkata")
        uploaded_at_ist = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(ist)

        # CREATE NEW RECORD - ✅ Updated field names
        new_call = AudioFile(
            emp_phone_number=sales_number,
            call_id=call_id,
            emp_name=username,
            customer_phone_number=customer_phone,
            call_datetime=call_date,
            call_duration=call_duration,
            call_type=call_type_normalized,
            department=admin_team,
            filename=filename,
            audio_url=file_url,
            uploaded_at=uploaded_at_ist,
            transcript_text=None,
            status=0  # ✅ NEW FIELD
        )

        db.add(new_call)
        db.commit()
        db.refresh(new_call)

        print(f"[SUCCESS] Call log saved: call_id={call_id}, DB ID={new_call.id}")

        return schemas.CallLogResponse(
            message="Call log saved successfully",
            inserted_id=new_call.id,
            status="created",
            has_recording=bool(file_url)
        )

    except IntegrityError as e:
        db.rollback()
        print(f"[ERROR] Integrity error: {e}")
        return JSONResponse(
            {"error": "Duplicate call_id detected"},
            status_code=409
        )

    except Exception as e:
        db.rollback()
        print(f"[ERROR] Exception in save_call_log: {str(e)}")
        print(traceback.format_exc())
        return JSONResponse(
            {"error": f"Internal server error: {str(e)}"},
            status_code=500
        )


@router.post(
    "/save-file-link",
    tags=["Call Management"],
    summary="Save File Link",
    description="Save audio file link with metadata including optional transcription text",
    response_description="File link save confirmation",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "File already exists"},
        201: {"description": "File link saved successfully"},
        400: {
            "model": ErrorResponse,
            "description": "Invalid date format or missing fields",
        },
        409: {"model": ErrorResponse, "description": "Database constraint violation"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def save_file_link(request: Request, db: Session = Depends(get_db)):
    """
    Save file link with metadata and optional transcribed_text.

    **Request Body:**
    - **file_url**: S3 URL of the audio file
    - **sales_number**: Sales person phone number
    - **customer_phone**: Customer phone number
    - **date** or **call_timestamp**: Timestamp in milliseconds
    - **duration**: Call duration in seconds
    - **call_type**: Type of call
    - **filename**: Optional filename
    - **call_id**: Optional Android call ID
    - **transcribed_text**: Optional transcription

    **Features:**
    - Prevents duplicate file URLs
    - Updates existing records with transcription
    - Auto-converts timestamps to IST
    """
    try:
        data = await request.json()
        print(f"[DEBUG] Received data: {data}")

        # Handle timestamp/date
        call_timestamp = data.get("call_timestamp")
        call_date_raw = data.get("date") or call_timestamp

        if not call_date_raw:
            return JSONResponse(
                {"error": "date or call_timestamp is required"}, status_code=400
            )

        # Parse timestamp safely
        try:
            timestamp = int(call_date_raw)
            if timestamp > 1e12:  # milliseconds → seconds
                timestamp /= 1000
            call_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid date format"}, status_code=400)

        # Normalize phone number
        phone_number = data.get("customer_phone") or data.get("number")
        if phone_number:
            phone_number = "".join(filter(str.isdigit, phone_number))
            if len(phone_number) > 10:
                phone_number = phone_number[-10:]

        # Normalize call type
        call_type_raw = str(data.get("call_type") or data.get("type", "")).strip()
        call_type = (
            call_type_raw.split(".")[-1].lower()
            if "." in call_type_raw
            else call_type_raw.lower() or "unknown"
        )

        # Check for duplicates by file_url - ✅ Updated to use audio_url
        file_url = data.get("file_url")

        # Get transcribed text from request
        transcribed_text = data.get("transcribed_text", None)
        if transcribed_text and (
            transcribed_text.lower() == "null" or transcribed_text.strip() == ""
        ):
            transcribed_text = None

        if transcribed_text:
            print(f"[INFO] Transcribed text received: {transcribed_text[:100]}...")

        # ✅ Updated field name: audio_url
        existing = (
            db.query(AudioFile)
            .filter(AudioFile.audio_url == file_url)
            .first()
        )

        if existing:
            print(f"[INFO] File already exists with ID: {existing.id}")

            # Update with transcribed text if not already set - ✅ Updated field name
            if transcribed_text and not existing.transcript_text:
                existing.transcript_text = transcribed_text
                db.commit()
                db.refresh(existing)
                print(f"[INFO] Updated transcribed text for file ID: {existing.id}")

                return JSONResponse(
                    {
                        "message": "File already exists, updated with transcription",
                        "existing_id": existing.id,
                        "file_url": existing.audio_url,  # ✅ Updated field name
                        "status": "duplicate",
                        "updated": True,
                    },
                    status_code=200,
                )

            return JSONResponse(
                {
                    "message": "File already exists",
                    "existing_id": existing.id,
                    "file_url": existing.audio_url,  # ✅ Updated field name
                    "status": "duplicate",
                    "updated": False,
                },
                status_code=200,
            )

        # Duration
        try:
            call_log_duration = int(data.get("duration", 0))
        except (ValueError, TypeError):
            call_log_duration = 0

        if call_type == "missed":
            call_log_duration = 0

        # Uploaded time IST
        ist = pytz.timezone("Asia/Kolkata")
        uploaded_at_ist = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(ist)

        # Clean filename
        filename = data.get("filename")
        filename = filename if filename and filename.lower() != "null" else None

        # Get sales user info
        sales_number = data.get("sales_number") or data.get("salesNumber")
        sales_data = (
            db.query(SalesData.username, SalesData.admin_Team)
            .filter(SalesData.salesPhoneNumber == sales_number)
            .first()
        )
        username, admin_team = sales_data if sales_data else (None, None)

        # GET call_id if provided
        call_id = data.get("call_id")

        # Create new audio file record - ✅ Updated field names
        new_audio = AudioFile(
            emp_phone_number=sales_number,
            call_id=call_id,
            emp_name=username,
            customer_phone_number=phone_number,
            call_datetime=call_date,
            call_duration=call_log_duration,
            call_type=call_type,
            department=admin_team,
            audio_url=file_url,  # ✅ Updated field name
            filename=filename,
            uploaded_at=uploaded_at_ist,
            transcript_text=transcribed_text,  # ✅ Updated field name
            status=0  # ✅ NEW FIELD
        )

        db.add(new_audio)
        db.commit()
        db.refresh(new_audio)

        print(
            f"[SUCCESS] File link saved with ID: {new_audio.id}, has_transcription: {bool(transcribed_text)}"
        )

        return JSONResponse(
            {
                "message": "File link saved successfully",
                "file_url": file_url,
                "inserted_id": new_audio.id,
                "status": "created",
                "has_transcription": bool(transcribed_text),
            },
            status_code=201,
        )

    except IntegrityError as e:
        db.rollback()
        print(f"[ERROR] Integrity error: {e}")
        return JSONResponse(
            {"error": "Database constraint violation", "details": str(e)},
            status_code=409,
        )

    except Exception as e:
        db.rollback()
        print(f"[ERROR] Exception in save_file_link: {str(e)}")
        print(traceback.format_exc())
        return JSONResponse(
            {
                "error": f"Internal server error: {str(e)}",
                "type": type(e).__name__,
                "trace": traceback.format_exc(),
            },
            status_code=500,
        )


@router.get(
    "/get-latest-call-date",
    tags=["Call Management"],
    summary="Get Latest Call Date",
    description="Retrieve the most recent call record for a specific sales number",
    response_description="Latest call details or empty if not found",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Latest call data returned"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
def get_latest_call_date(
    sales_number: str = Query(
        ...,
        description="Sales phone number to check for latest call",
        example="9876543210",
    ),
    db: Session = Depends(get_db),
):
    """
    Get the latest call date and details for a sales number.

    **Query Parameters:**
    - **sales_number**: Phone number of the sales person

    **Returns:**
    - Latest call information including upload time, filename, and file URL
    - Empty response if no records found
    """
    try:
        # ✅ Updated field name: emp_phone_number
        latest_record = (
            db.query( AudioFile)
            .filter(AudioFile.emp_phone_number == sales_number)
            .order_by(AudioFile.uploaded_at.desc())
            .first()
        )

        if not latest_record:
            return {
                "sales_phone_number": sales_number,
                "message": "No records found",
                "uploaded_at": "",
            }

        # ✅ Updated field names
        return {
            "id": latest_record.id,
            "sales_phone_number": latest_record.emp_phone_number,
            "uploaded_at": latest_record.uploaded_at,
            "filename": latest_record.filename,
            "file_url": latest_record.audio_url,
        }

    except Exception as e:
        print(f"[ERROR] Exception in get_latest_call_date: {str(e)}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get(
    "/get-all-files",
    tags=["Call Management"],
    summary="Get All Audio Files",
    description="Retrieve all audio file records from the database",
    response_model=List[AudioFileOut],
    response_description="List of all audio files with metadata",
    status_code=status.HTTP_200_OK,
)
def get_all_files(db: Session = Depends(get_db)):
    """
    Get all audio files with complete metadata.

    **Returns:**
    - List of all audio file records
    - Includes call details, transcriptions, and file URLs
    - Empty list if no files exist
    """
    files = db.query(AudioFile).all()
    result = []

    for f in files:
        # ✅ Updated field names to match AudioFileOut schema
        result.append(
            {
                "id": f.id,
                "call_id": f.call_id,
                "emp_phone_number": f.emp_phone_number,
                "emp_name": f.emp_name,
                "customer_phone_number": f.customer_phone_number,
                "call_datetime": f.call_datetime,
                "call_duration": f.call_duration if f.call_duration is not None else 0,
                "call_type": f.call_type,
                "department": f.department,
                "audio_url": f.audio_url,
                "filename": f.filename,
                "transcript_text": f.transcript_text,
                "uploaded_at": f.uploaded_at,
                "status": f.status,  # ✅ NEW FIELD
            }
        )

    return result


@router.get(
    "/check-sales-number",
    tags=["Sales Management"],
    summary="Verify Sales Number",
    description="Check if a sales phone number exists in the system and retrieve associated details",
    response_model=SalesNumberCheckResponse,
    response_description="Sales number verification result",
    status_code=status.HTTP_200_OK,
)
def check_sales_number(
    sales_number: str = Query(
        ..., description="Sales phone number to verify", example="9876543210"
    ),
    db: Session = Depends(get_db),
):
    """
    Verify if a sales number exists in the system.

    **Query Parameters:**
    - **sales_number**: Phone number to verify

    **Returns:**
    - **exists**: Boolean indicating if number exists
    - **username**: Sales person's username (if exists)
    - **admin_Team**: Assigned team name (if exists)
    - **salesPhoneNumber**: The queried phone number
    """
    record = (
        db.query(
            SalesData.username,
            SalesData.salesPhoneNumber,
            SalesData.admin_Team,
        )
        .filter(SalesData.salesPhoneNumber == sales_number)
        .first()
    )

    if record:
        return {
            "username": record.username,
            "salesPhoneNumber": record.salesPhoneNumber,
            "admin_Team": record.admin_Team,
            "exists": True,
        }
    else:
        return {"salesPhoneNumber": sales_number, "exists": False}


@router.get(
    "/check-file-exists",
    tags=["S3 Storage"],
    summary="Check S3 File Existence",
    description="Verify if a file exists in the S3 bucket using its key",
    response_model=FileExistsResponse,
    response_description="File existence verification result",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "File existence check completed"},
        500: {"model": ErrorResponse, "description": "S3 error occurred"},
    },
)
async def check_file_exists(
    s3Key: str = Query(
        ..., description="S3 object key to check", example="recordings/call_123.mp3"
    ),
):
    """
    Check if a file exists in S3 bucket.

    **Query Parameters:**
    - **s3Key**: The S3 object key (file path) to verify

    **Returns:**
    - **exists**: True if file exists, False otherwise
    - **s3Key**: The checked S3 key

    **Use Case:**
    - Verify file upload completion before processing
    - Prevent redundant uploads
    """
    try:
        s3_client.head_object(Bucket=S3_BUCKET, Key=s3Key)
        return {"exists": True, "s3Key": s3Key}
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return {"exists": False, "s3Key": s3Key}
        else:
            raise HTTPException(status_code=500, detail=f"S3 error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking file: {str(e)}")







@router.post(
    "/transcribe/file_url",
    tags=["Transcription"],
    summary="Transcribe from S3 URL",
    description="Transcribe audio file directly from S3 URL with speaker diarization",
    response_model=TranscriptionResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Transcription with speaker diarization completed"},
        400: {"model": ErrorResponse, "description": "Invalid S3 URL"},
        500: {"model": ErrorResponse, "description": "Transcription failed"},
    },
)
async def transcribe_from_url(request: TranscribeUrlRequest):
    """
    Transcribe audio file directly from S3 URL with speaker diarization.

    **Features:**
    - **Speaker Diarization**: Identifies 2 speakers (sales & customer)
    - **Language Detection**: Auto-detects language
    - **Word Boost**: Uses custom vocabulary for better accuracy
    - **Speaker Analysis**: Provides statistics per speaker

    **Input:**
    - **file_url**: S3 URL of the audio file

    **Returns:**
    - **status**: Transcription status
    - **transcript_id**: AssemblyAI identifier
    - **text**: Full transcribed text
    - **speaker_analysis**: Stats for each speaker
    - **language**: Detected language code
    """
    try:
        file_url = request.file_url

        # Validate URL format
        if not file_url or not file_url.startswith(("http://", "https://", "s3://")):
            raise HTTPException(
                status_code=400, detail="Invalid file_url. Must be a valid S3/HTTP URL"
            )

        print(f"[INFO] Transcribing from URL: {file_url}")

        # Generate presigned URL for secure S3 access
        try:
            bucket, key = extract_bucket_key_from_s3_url(file_url)
            presigned_url = generate_presigned_url(bucket, key, expiry=7200)
            print(f"[INFO] Generated presigned URL")
        except Exception as e:
            print(f"[WARN] Could not generate presigned URL: {e}. Using direct URL")
            presigned_url = file_url

        # Load word boosts
        word_boost_list = load_word_boosts()

        # Prepare payload
        payload = {
            "audio_url": presigned_url,
            "speaker_labels": True,
            "speakers_expected": 2,
            "language_detection": True,
            "punctuate": True,
            "format_text": True,
            "speech_model": "best",
        }

        # Add word boost only if available
        if word_boost_list:
            payload["word_boost"] = word_boost_list
            payload["boost_param"] = "high"

        print(
            f"[INFO] Sending transcription request with {len(word_boost_list)} word boosts"
        )

        resp = requests.post(
            ASSEMBLYAI_URL,
            headers={**HEADERS, "content-type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()

        transcript_id = resp.json().get("id")
        if not transcript_id:
            raise HTTPException(status_code=500, detail="No transcript ID received")

        print(f"[INFO] Transcript ID: {transcript_id}, polling for completion...")

        # Poll with extended timeout for longer files
        data = poll_transcription(transcript_id, max_retries=120)

        # Format result
        result = format_speaker_transcript_advanced(data)

        if not result["text"]:
            print("[WARN] Empty transcription text")

        print(f"[SUCCESS] Transcription completed")

        return JSONResponse(
            {
                "status": "completed",
                "transcript_id": transcript_id,
                "file_url": file_url,
                "text": result["text"],
                "speaker_analysis": result["speaker_analysis"],
                "language": data.get("language_code", "en"),
                "audio_duration": data.get("audio_duration", 0),
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Transcription failed: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


@router.post(
    "/transcribe/all",
    tags=["Transcription"],
    summary="Batch Transcribe All Pending Files",
    description="Transcribe all audio files in database that don't have transcribed_text",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Batch transcription completed with summary"},
        500: {"model": ErrorResponse, "description": "Batch process failed"},
    },
)
async def transcribe_all(db: Session = Depends(get_db)):
    """
    Transcribe all audio files without transcribed_text in batch.

    **Process:**
    1. Queries all audio files missing transcription
    2. Processes each file sequentially
    3. Updates database with transcribed text
    4. Returns detailed summary

    **Features:**
    - Speaker diarization for each file
    - Error handling per file (failures don't stop batch)
    - Automatic retry logic with 3-second polling
    - Rate limiting protection (2-second delay between files)

    **Returns:**
    - **summary**: Array of results per file
    - **total_processed**: Number of files processed
    - **total_successful**: Successfully transcribed count
    - **total_failed**: Failed transcription count
    - **total_skipped**: Skipped file count
    """
    results = []
    total_successful = 0
    total_failed = 0
    total_skipped = 0

    try:
        # Get all audio files without transcription - ✅ Updated field names
        audio_files = (
            db.query(AudioFile)
            .filter(AudioFile.transcript_text == None)
            .filter(AudioFile.audio_url != None)  # Only files with URLs
            .order_by(AudioFile.id.asc())
            .all()
        )

        if not audio_files:
            print("[INFO] No audio files pending transcription")
            return {
                "summary": [],
                "message": "No audio files pending transcription",
                "total_processed": 0,
                "total_successful": 0,
                "total_failed": 0,
                "total_skipped": 0,
            }

        print(f"[INFO] Found {len(audio_files)} audio files to transcribe")

        for audio_file in audio_files:
            try:
                file_id = audio_file.id
                file_url = audio_file.audio_url  # ✅ Updated field name

                if not file_url or file_url.lower() == "null":
                    print(f"[WARN] Audio file ID {file_id} has no file_url, skipping")
                    results.append(
                        {
                            "id": file_id,
                            "status": "skipped",
                            "reason": "No file_url available",
                        }
                    )
                    total_skipped += 1
                    continue

                print(f"[INFO] Processing file ID: {file_id}")

                # Generate presigned URL
                try:
                    bucket, key = extract_bucket_key_from_s3_url(file_url)
                    presigned_url = generate_presigned_url(bucket, key, expiry=7200)
                except Exception as e:
                    print(f"[WARN] Using direct URL for ID {file_id}: {e}")
                    presigned_url = file_url

                # Load word boosts
                word_boost_list = load_word_boosts()

                # Prepare payload
                payload = {
                    "audio_url": presigned_url,
                    "speaker_labels": True,
                    "speakers_expected": 2,
                    "language_detection": True,
                    "punctuate": True,
                    "format_text": True,
                    "speech_model": "best",
                }

                if word_boost_list:
                    payload["word_boost"] = word_boost_list
                    payload["boost_param"] = "high"

                # Send transcription request
                resp = requests.post(
                    ASSEMBLYAI_URL,
                    headers={**HEADERS, "content-type": "application/json"},
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()

                transcript_id = resp.json().get("id")
                if not transcript_id:
                    raise ValueError("No transcript ID received")

                print(f"[INFO] Transcript ID for file {file_id}: {transcript_id}")

                # Poll for completion
                data = poll_transcription(transcript_id, max_retries=120)

                # Format result
                result = format_speaker_transcript_advanced(data)

                # Update database - ✅ Updated field name
                audio_file.transcript_text = result["text"]
                db.add(audio_file)
                db.commit()

                print(f"[SUCCESS] Transcription completed for file ID: {file_id}")

                results.append(
                    {
                        "id": file_id,
                        "status": "success",
                        "transcript_id": transcript_id,
                        "text_length": len(result["text"]),
                        "language": data.get("language_code", "en"),
                        "speaker_analysis": result["speaker_analysis"],
                    }
                )
                total_successful += 1

            except requests.exceptions.Timeout:
                db.rollback()
                print(f"[ERROR] Timeout for file ID {file_id}")
                results.append(
                    {"id": file_id, "status": "failed", "error": "Request timeout"}
                )
                total_failed += 1

            except HTTPException as e:
                db.rollback()
                print(f"[ERROR] HTTP error for file ID {file_id}: {e.detail}")
                results.append(
                    {"id": file_id, "status": "failed", "error": str(e.detail)}
                )
                total_failed += 1

            except Exception as e:
                db.rollback()
                print(f"[ERROR] Failed to transcribe file ID {file_id}: {str(e)}")
                results.append({"id": file_id, "status": "failed", "error": str(e)})
                total_failed += 1

            # Rate limiting: delay between requests
            time.sleep(2)

        return {
            "summary": results,
            "total_processed": len(results),
            "total_successful": total_successful,
            "total_failed": total_failed,
            "total_skipped": total_skipped,
            "message": f"Batch transcription completed: {total_successful} successful, {total_failed} failed, {total_skipped} skipped",
        }

    except Exception as e:
        print(f"[ERROR] Batch transcription failed: {str(e)}")
        traceback.print_exc()
        return JSONResponse(
            {
                "error": f"Batch transcription failed: {str(e)}",
                "summary": results,
                "total_successful": total_successful,
                "total_failed": total_failed,
            },
            status_code=500,
        )


@router.post(
    "/url",
    tags=["Transcription"],
    summary="Transcribe Audio by Database ID",
    description="Transcribe an audio file by its database ID with speaker diarization and caching support",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Transcription completed or cached result returned"},
        404: {"model": ErrorResponse, "description": "Audio file not found"},
        400: {"model": ErrorResponse, "description": "Audio file has no file_url"},
        500: {"model": ErrorResponse, "description": "Transcription failed"},
    },
)
async def transcribe_from_id(request: AudioIDRequest, db: Session = Depends(get_db)):
    """
    Transcribe an audio file by its database ID with intelligent caching.

    **Features:**
    - **Smart Caching**: Returns cached transcription immediately if available
    - **Speaker Diarization**: Identifies 2 speakers (sales rep & customer)
    - **Word Boost**: Uses custom vocabulary for domain-specific accuracy
    - **Language Detection**: Auto-detects language from audio
    - **Database Integration**: Automatically saves transcription for future use

    **Request Body:**
    - **id**: Database record ID of the audio file (integer)

    **Returns:**
    - New transcription with speaker analysis
    - Or cached transcription text (if previously processed)
    """
    try:
        # Query audio file
        audio_file = (
            db.query(AudioFile).filter(AudioFile.id == request.id).first()
        )

        if not audio_file:
            raise HTTPException(
                status_code=404, detail=f"Audio file with ID {request.id} not found"
            )

        # Return cached transcription if available - ✅ Updated field name
        if audio_file.transcript_text:
            print(f"[INFO] Returning cached transcription for audio ID: {request.id}")
            return audio_file.transcript_text

        # Validate file_url exists - ✅ Updated field name
        if not audio_file.audio_url:
            raise HTTPException(
                status_code=400, detail=f"Audio file ID {request.id} has no file_url"
            )

        print(f"[INFO] Starting transcription for audio ID: {request.id}")

        # Extract S3 details and generate presigned URL - ✅ Updated field name
        try:
            bucket, key = extract_bucket_key_from_s3_url(audio_file.audio_url)
            presigned_url = generate_presigned_url(bucket, key, expiry=7200)
            print(f"[INFO] Generated presigned URL for audio ID: {request.id}")
        except Exception as e:
            print(f"[WARN] Could not generate presigned URL: {e}. Using direct URL")
            presigned_url = audio_file.audio_url

        # Load word boosts
        word_boost_list = load_word_boosts()

        # Prepare transcription payload with speaker diarization
        payload = {
            "audio_url": presigned_url,
            "speaker_labels": True,
            "speakers_expected": 2,
            "language_detection": True,
            "punctuate": True,
            "format_text": True,
            "speech_model": "best",
        }

        # Add word boost only if available
        if word_boost_list:
            payload["word_boost"] = word_boost_list
            payload["boost_param"] = "high"

        # Submit to AssemblyAI
        try:
            print(f"[INFO] Submitting transcription request for audio ID: {request.id}")
            resp = requests.post(
                ASSEMBLYAI_URL,
                headers={**HEADERS, "content-type": "application/json"},
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()

            transcript_id = resp.json().get("id")
            if not transcript_id:
                raise HTTPException(
                    status_code=500, detail="No transcript ID received from AssemblyAI"
                )

            print(f"[INFO] Transcript ID: {transcript_id}, polling for completion...")

            # Poll with extended timeout (120 retries = 6 minutes)
            data = poll_transcription(transcript_id, max_retries=120)

        except requests.exceptions.Timeout:
            raise HTTPException(status_code=500, detail="AssemblyAI request timeout")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] AssemblyAI request failed: {str(e)}")
            raise HTTPException(
                status_code=500, detail=f"AssemblyAI request failed: {str(e)}"
            )
        except HTTPException:
            raise
        except Exception as e:
            print(f"[ERROR] Transcription error: {str(e)}")
            traceback.print_exc()
            raise HTTPException(
                status_code=500, detail=f"Transcription failed: {str(e)}"
            )

        # Format transcription with speaker analysis
        result = format_speaker_transcript_advanced(data)

        # Validate we got text back
        if not result["text"]:
            print(f"[WARN] Empty transcription for audio ID: {request.id}")

        # Save to database for caching - ✅ Updated field name
        try:
            audio_file.transcript_text = result["text"]
            db.add(audio_file)
            db.commit()
            db.refresh(audio_file)
            print(f"[SUCCESS] Transcription saved for audio ID: {request.id}")
        except Exception as e:
            db.rollback()
            print(f"[ERROR] Failed to save transcription to DB: {str(e)}")
            # Don't fail the request, just log the error

        return {
            "status": "completed",
            "transcript_id": transcript_id,
            "text": result["text"],
            "speaker_analysis": result["speaker_analysis"],
            "language": data.get("language_code", "en"),
            "audio_duration": data.get("audio_duration", 0),
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Unexpected error in transcribe_from_id: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# ==================== DELETE ENDPOINTS ====================


@router.delete(
    "/delete-file/{file_id}",
    tags=["Call Management"],
    summary="Delete File and Record by ID",
    status_code=status.HTTP_200_OK
)
async def delete_file_by_id(file_id: int, db: Session = Depends(get_db)):
    """
    Delete audio file from S3 and database by record ID.
    
    **Path Parameters:**
    - **file_id**: Database record ID of the audio file to delete
    """
    try:
        # Fetch record - ✅ Updated table name
        audio_file = (
            db.query(AudioFile)
            .filter(AudioFile.id == file_id)
            .first()
        )

        if not audio_file:
            raise HTTPException(
                status_code=404, 
                detail=f"Record with ID {file_id} not found"
            )

        file_url = audio_file.audio_url  # ✅ Updated field name
        s3_deleted = False

        print(f"[INFO] Found record {file_id} with file_url: {file_url}")

        # Delete from S3
        if file_url and file_url.strip() and file_url.lower() != "null":
            try:
                bucket, key = extract_bucket_key_from_s3_url(file_url)
                print(f"[INFO] Attempting to delete from S3 => Bucket: {bucket}, Key: {key}")

                # Optional: verify existence
                try:
                    s3_client.head_object(Bucket=bucket, Key=key)
                    print("[INFO] File exists on S3, proceeding to delete...")
                except ClientError as e:
                    if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                        print(f"[WARN] File not found on S3 (already deleted): {file_url}")
                        s3_deleted = True
                    else:
                        raise

                # Perform deletion
                if not s3_deleted:
                    response = s3_client.delete_object(Bucket=bucket, Key=key)
                    print(f"[DEBUG] S3 delete response: {response}")
                    s3_deleted = True
                    print(f"[SUCCESS] Deleted file from S3: {file_url}")

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                print(f"[ERROR] ClientError while deleting from S3: {error_code}")
                raise HTTPException(
                    status_code=500,
                    detail=f"S3 deletion failed: {str(e)}"
                )
            except Exception as e:
                print(f"[ERROR] Unexpected S3 deletion error: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"S3 deletion error: {str(e)}"
                )
        else:
            print("[INFO] No valid file_url found, skipping S3 deletion")
            s3_deleted = True

        # Delete from database
        try:
            db.delete(audio_file)
            db.commit()
            print(f"[SUCCESS] Deleted record from database: ID={file_id}")

        except Exception as e:
            db.rollback()
            print(f"[ERROR] Database deletion failed: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete from database: {str(e)}"
            )

        return {
            "message": "File and record deleted successfully",
            "file_id": file_id,
            "file_url": file_url,
            "s3_deletion": s3_deleted,
            "db_deletion": True
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Unexpected error: {str(e)}")
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )


@router.delete(
    "/delete-files/{sales_phone_number}",
    tags=["Call Management"],
    summary="Delete All Files by Sales Phone Number",
    status_code=status.HTTP_200_OK
)
async def delete_files_by_sales_phone_number(
    sales_phone_number: str,
    db: Session = Depends(get_db)
):
    """
    Delete all audio files from S3 and database for a given sales phone number.

    **Path Parameters:**
    - **sales_phone_number**: The sales phone number whose audio files need to be deleted

    **Returns:**
    - Summary of how many files were deleted and any failures
    """
    try:
        # Fetch all records for that sales number - ✅ Updated field name
        audio_files = (
            db.query(AudioFile)
            .filter(AudioFile.emp_phone_number == sales_phone_number)
            .all()
        )

        if not audio_files:
            raise HTTPException(
                status_code=404,
                detail=f"No records found for sales_phone_number {sales_phone_number}"
            )

        print(f"[INFO] Found {len(audio_files)} records for sales_phone_number={sales_phone_number}")

        s3_deleted_count = 0
        s3_failed = []
        deleted_ids = []

        # Loop through each record and delete from S3
        for audio_file in audio_files:
            file_url = audio_file.audio_url  # ✅ Updated field name
            
            if not file_url or file_url.strip().lower() == "null":
                print(f"[INFO] Skipping record {audio_file.id} - no valid file_url")
                deleted_ids.append(audio_file.id)
                continue

            try:
                bucket, key = extract_bucket_key_from_s3_url(file_url)
                print(f"[INFO] Deleting from S3 => Bucket: {bucket}, Key: {key}")

                s3_client.delete_object(Bucket=bucket, Key=key)
                print(f"[SUCCESS] Deleted file from S3: {file_url}")
                s3_deleted_count += 1

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code in ("404", "NoSuchKey"):
                    print(f"[WARN] File not found on S3 (already deleted): {file_url}")
                    s3_deleted_count += 1
                else:
                    print(f"[ERROR] Failed to delete from S3 ({file_url}): {str(e)}")
                    s3_failed.append(file_url)
            except Exception as e:
                print(f"[ERROR] Unexpected S3 error for {file_url}: {e}")
                s3_failed.append(file_url)

            deleted_ids.append(audio_file.id)

        # Delete all matching DB records
        try:
            for audio_file in audio_files:
                db.delete(audio_file)
            db.commit()
            print(f"[SUCCESS] Deleted all {len(deleted_ids)} DB records for {sales_phone_number}")

        except Exception as e:
            db.rollback()
            print(f"[ERROR] Database deletion failed: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Database deletion failed: {str(e)}"
            )

        # Return summary
        return {
            "message": f"Deleted {len(deleted_ids)} records for sales_phone_number={sales_phone_number}",
            "sales_phone_number": sales_phone_number,
            "records_found": len(audio_files),
            "s3_deleted": s3_deleted_count,
            "s3_failed": s3_failed,
            "db_deleted": len(deleted_ids)
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Unexpected server error: {str(e)}")
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )