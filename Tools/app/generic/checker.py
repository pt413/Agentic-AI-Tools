import os
import sys
import warnings


# Add parent folder to sys.path (if needed)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# from app.generic.checker import get_user_details

# # from bp_assist.invoice_page import fetch_booking_data
# from app.generic.external_data import fetch_booking_data
# # from bp_assist.get_booking_details import get_booking_details
# from app.generic.data_collecter import get_booking_details
# # from helpers.get_user_details import get_user_details
# # from app.generic.checker import get_user_details
# from app.generic import get_user_details
# # from helpers.booking_details import save_booking_detailsss
# from app.generic.checker import save_booking_detailsss
from app.generic.get_user_detailss import get_user_details
from app.generic.data_collecter import get_booking_details
from app.generic.external_data import fetch_booking_data
from app.generic.get_user_detailss import create_user_details 
from app.generic.get_user_detailss import save_booking_detailsss

# Ignore cryptography deprecation warnings
try:
    from cryptography.utils import CryptographyDeprecationWarning
except ImportError:
    CryptographyDeprecationWarning = DeprecationWarning

warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)

# Blueprint
# generic_bp = Blueprint('generic', __name__, url_prefix='/api/gen')
async def process_info(info: str):
    """Fetch booking details based on booking_id / email / phone"""
    if not info:
        return {"error": "Empty booking id or email."}

    if info.isdigit() and len(info) == 10:
        info = "91" + info

    user_details = await get_user_details(info)  # also async ideally
    print("User details:", user_details)
    if user_details:
        booking_id = user_details[0].get("booking_id", "").strip()
        all_data = await get_booking_details(booking_id)
        return all_data

    if info.isdigit() and len(info) <= 6:
        server_booking = await fetch_booking_data(info)
        print("Server booking:", server_booking)
        if server_booking:
            await save_booking_detailsss(server_booking)
            booking_id = server_booking.get("booking_id")

            # ✅ FIX — pass email separately
            # await create_user_details(
            #     email=server_booking.get("email", ""),  # or primary email
            #     details=server_booking
            # )

            all_data = await get_booking_details(booking_id)
            return all_data


    return {"error": f"Sorry, there is no booking for ({info})."}
