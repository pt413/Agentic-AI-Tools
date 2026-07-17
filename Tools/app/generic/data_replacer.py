# import os
# import sys
# import warnings
# from flask import Blueprint, jsonify

# # Add parent folder to sys.path (if needed)
# sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# # from app.generic.checker import get_user_details

# # # from bp_assist.invoice_page import fetch_booking_data
# # from app.generic.external_data import fetch_booking_data
# # # from bp_assist.get_booking_details import get_booking_details
# # from app.generic.data_collecter import get_booking_details
# # # from helpers.get_user_details import get_user_details
# # # from app.generic.checker import get_user_details
# # from app.generic import get_user_details
# # # from helpers.booking_details import save_booking_detailsss
# # from app.generic.checker import save_booking_detailsss
# from app.generic.get_user_detailss import get_user_details
# from app.generic.data_collecter import get_booking_details
# from app.generic.external_data import fetch_booking_data
# from app.generic.get_user_detailss import create_user_details 
# from app.generic.get_user_detailss import save_booking_detailsss

# # Ignore cryptography deprecation warnings
# try:
#     from cryptography.utils import CryptographyDeprecationWarning
# except ImportError:
#     CryptographyDeprecationWarning = DeprecationWarning

# warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)

# # Blueprint
# # generic_bp = Blueprint('generic', __name__, url_prefix='/api/gen')
# async def process_info():
#     """Fetch booking details based on booking_id / email / phone"""
    
