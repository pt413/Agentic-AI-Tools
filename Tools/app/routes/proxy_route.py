# app/routes/proxy_route.py
import json
import logging
from datetime import datetime
import requests
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
from sqlalchemy.orm import Session
from typing import List,Dict,Any
from app.db.database import get_db
from app.model.call_log import CallLog
from app.model.sales_data import SalesData

router = APIRouter(prefix="/api", tags=["Proxy"])

BASE_URL = "https://www.rentmystay.com/V2"

# Cache for fallback data (in case external API fails)
admin_cache = {
    "data": [],
    "timestamp": None,
    "expires_in": 300,  # 5 minutes cache
}


@router.get("/get_all_admin_details/687ba37d3241d")
async def get_admins():
    try:
        headers = {"Accept": "application/json", "User-Agent": "FastAPI-Proxy"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{BASE_URL}/get_all_admin_details/687ba37d3241d", headers=headers)

        if resp.status_code == 200:
            try:
                data = resp.json()
                admin_cache["data"] = data
                admin_cache["timestamp"] = datetime.now()
                return JSONResponse(content=data, status_code=200)
            except json.JSONDecodeError:
                logging.error("External API returned invalid JSON")

        # If external API fails, check if we have cached data
        if (
            admin_cache["data"]
            and admin_cache["timestamp"]
            and (datetime.now() - admin_cache["timestamp"]).total_seconds() < admin_cache["expires_in"]
        ):
            logging.info("Returning cached admin data due to external API failure")
            return JSONResponse(content=admin_cache["data"], status_code=200)

        return JSONResponse(content=[], status_code=200)

    except httpx.RequestError:
        logging.warning("External API connection/timeout, returning cached data if available")
        if admin_cache["data"]:
            return JSONResponse(content=admin_cache["data"], status_code=200)
        return JSONResponse(content=[], status_code=200)
    except Exception as e:
        logging.error(f"Unexpected error in get_admins: {str(e)}")
        return JSONResponse(content=admin_cache.get("data", []), status_code=200)


@router.get("/get_call_logs_details/{date}/687ba37d3241d")
@router.get("/get_call_logs_details/{date}/687ba37d3241d/{phone}")
async def get_call_logs(date: str, phone: str | None = None):
    try:
        url = (
            f"{BASE_URL}/get_call_logs_details/{date}/687ba37d3241d/{phone}"
            if phone else f"{BASE_URL}/get_call_logs_details/{date}/687ba37d3241d"
        )

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)

        logging.info(f"Upstream GET {url} returned {resp.status_code}")

        if resp.status_code == 404:
            return {"status": True, "data": []}

        try:
            data = resp.json()
            return JSONResponse(content=data, status_code=resp.status_code)
        except json.JSONDecodeError:
            logging.warning(f"Non-JSON response for {url}")
            return PlainTextResponse(content=resp.text, status_code=resp.status_code)

    except httpx.TimeoutException:
        logging.error(f"Timeout when calling upstream {url}")
        return {"status": False, "error": "Request timeout"}
    except httpx.RequestError:
        logging.error(f"Connection error when calling upstream {url}")
        return {"status": False, "error": "Connection error"}
    except Exception as e:
        logging.exception(f"Unexpected error when calling upstream {url}")
        return {"status": False, "error": str(e)}


@router.get("/health/admin-api")
async def health_check():
    try:
        headers = {"Accept": "application/json", "User-Agent": "FastAPI-Proxy"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BASE_URL}/get_all_admin_details/687ba37d3241d", headers=headers)

        return {
            "external_api_status": resp.status_code,
            "external_api_accessible": resp.status_code == 200,
            "cache_available": bool(admin_cache["data"]),
            "cache_age": (datetime.now() - admin_cache["timestamp"]).total_seconds()
            if admin_cache["timestamp"] else None,
        }
    except Exception as e:
        return {
            "external_api_status": "error",
            "external_api_accessible": False,
            "error": str(e),
            "cache_available": bool(admin_cache["data"]),
            "cache_age": (datetime.now() - admin_cache["timestamp"]).total_seconds()
            if admin_cache["timestamp"] else None,
        }


@router.post("/refresh-admin-cache")
async def refresh_cache():
    try:
        headers = {"Accept": "application/json", "User-Agent": "FastAPI-Proxy"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{BASE_URL}/get_all_admin_details/687ba37d3241d", headers=headers)

        if resp.status_code == 200:
            data = resp.json()
            admin_cache["data"] = data
            admin_cache["timestamp"] = datetime.now()
            return {"success": True, "message": "Cache refreshed successfully"}
        return {"success": False, "message": f"External API returned {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/fetch-and-store-call-logs")
async def fetch_and_store_call_logs(db: Session = Depends(get_db)):
    """
    Fetch data from both APIs for TODAY, merge them, and store in PostgreSQL call_logs table
    Always uses current date and gets ALL data (no phone filtering)
    """
    try:
        current_date = datetime.now().strftime("%Y-%m-%d")
        
        logging.info(f"Starting sync for date: {current_date}")

        admin_headers = {"Accept": "application/json", "User-Agent": "FastAPI-Proxy"}
        async with httpx.AsyncClient(timeout=30) as client:
            admin_resp = await client.get(
                f"{BASE_URL}/get_all_admin_details/687ba37d3241d", 
                headers=admin_headers
            )
        
        if admin_resp.status_code != 200:
            return {"success": False, "error": f"Admin API returned {admin_resp.status_code}"}
        
        admin_data = admin_resp.json()
        
        call_logs_url = f"{BASE_URL}/get_call_logs_details/{current_date}/687ba37d3241d"
        
        async with httpx.AsyncClient(timeout=30) as client:
            call_logs_resp = await client.get(call_logs_url)
        
        if call_logs_resp.status_code != 200:
            return {"success": False, "error": f"Call logs API returned {call_logs_resp.status_code}"}
        
        call_logs_data = call_logs_resp.json()
        
        admin_mapping = {}
        if admin_data.get("status") and "data" in admin_data:
            for admin in admin_data["data"]:
                admin_mapping[admin.get("admin_uname")] = {
                    "admin_phoneNumber": admin.get("admin_phoneNumber"),
                    "admin_team": admin.get("admin_team")
                }
        
        stored_count = 0
        if call_logs_data.get("status") and "data" in call_logs_data:
            for call_log in call_logs_data["data"]:
                try:
                    username = call_log.get("username")
                    admin_info = admin_mapping.get(username, {})
                    
                    call_date = None
                    added_on = None
                    
                    if call_log.get("callDate"):
                        try:
                            call_date = datetime.strptime(call_log.get("callDate"), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            call_date = datetime.now()
                    
                    if call_log.get("added_on"):
                        try:
                            added_on = datetime.strptime(call_log.get("added_on"), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            added_on = datetime.now()
                    
                    db_call_log = CallLog(
                        username=username,
                        phNum=call_log.get("phNum")[-10:],
                        salesPhoneNumber=call_log.get("salesPhoneNumber")[-10:],
                        callType=call_log.get("callType"),
                        callDuration=call_log.get("callDuration", "0"),
                        lead_id=call_log.get("lead_id"),
                        timestamp=call_log.get("timestamp"),
                        callDate=call_date,
                        added_on=added_on,
                        admin_Team=admin_info.get("admin_team"),
                    )
                    
                    existing_log = db.query(CallLog).filter(
                        CallLog.username == username,
                        CallLog.phNum == call_log.get("phNum"),
                        CallLog.callDate == call_date,
                        CallLog.lead_id == call_log.get("lead_id")
                    ).first()
                    
                    if not existing_log:
                        db.add(db_call_log)
                        stored_count += 1
                        logging.info(f"Added call log for user: {username}, phone: {call_log.get('phNum')}")
                    else:
                        logging.debug(f"Skipped duplicate for user: {username}, phone: {call_log.get('phNum')}")
                        
                except Exception as e:
                    logging.error(f"Error processing call log {call_log}: {str(e)}")
                    continue
            
            db.commit()
            
            admin_cache["data"] = admin_data
            admin_cache["timestamp"] = datetime.now()
            
            logging.info(f"Sync completed. Stored {stored_count} new records out of {len(call_logs_data['data'])} total")
            
            return {
                "success": True,
                "message": f"Successfully stored {stored_count} new call logs in database",
                "sync_date": current_date,
                "total_fetched": len(call_logs_data["data"]),
                "stored_count": stored_count,
                "duplicates_skipped": len(call_logs_data["data"]) - stored_count
            }
        else:
            logging.warning(f"No call logs data found for date: {current_date}")
            return {
                "success": True,
                "message": "No call logs data found to sync",
                "sync_date": current_date,
                "total_fetched": 0,
                "stored_count": 0
            }
            
    except httpx.RequestError as e:
        logging.error(f"HTTP error during sync: {str(e)}")
        return {"success": False, "error": f"HTTP request failed: {str(e)}"}
    
    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {str(e)}")
        return {"success": False, "error": "Invalid JSON response from API"}
    
    except Exception as e:
        logging.error(f"Unexpected error during sync: {str(e)}")
        db.rollback()
        return {"success": False, "error": f"Unexpected error: {str(e)}"}

@router.post("/fetch-and-store-admin-details")
def fetch_and_store_admin_details(db: Session = Depends(get_db)):
    """
    Fetches admin details 
    upserts (inserts/updates) them into the sales_data table.
    """
    API_URL = "https://www.rentmystay.com/V2/get_all_admin_details/687ba37d3241d"
    try:
        response = requests.get(API_URL)
        response.raise_for_status()
        json_data = response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch data: {e}")

    if not json_data.get("status") or "data" not in json_data:
        raise HTTPException(status_code=400, detail="Invalid API response")

    admin_list = json_data["data"]

    inserted, updated = 0, 0

    for admin in admin_list:
        phone = admin.get("admin_phoneNumber")
        uname = admin.get("admin_uname")
        team = admin.get("admin_team")

        existing = db.query(SalesData).filter(SalesData.salesPhoneNumber == phone).first()

        if existing:
            changed = False
            if existing.username != uname:
                existing.username = uname
                changed = True
            if existing.admin_Team != team:
                existing.admin_Team = team
                changed = True

            if changed:
                updated += 1
        else:
            new_record = SalesData(
                salesPhoneNumber=phone[-10:],
                username=uname,
                admin_Team=team,
            )
            db.add(new_record)
            inserted += 1

    db.commit()

    return {
        "status": "success",
        "inserted_records": inserted,
        "updated_records": updated,
        "total_processed": len(admin_list),
    }
