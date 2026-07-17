# app/generic/checker.py
from datetime import datetime
from bson import ObjectId
from app.db.mongo_db import bookking_details_coll

# -------------------
# FETCH USER DETAILS
# -------------------
async def get_user_details(data: str):
    """
    Find a user booking by email/phone/booking id
    """
    query = {
        "$or": [
            {"all_emails.primary": data},
            {"all_emails.others": data},
            {"all_contacts.primary": data},
            {"all_contacts.others": data},
            {"booking_id": data},  # consistent naming
        ]
    }

    cursor = bookking_details_coll.find(query)
    results = []
    async for doc in cursor:
        # convert _id to string for JSON response
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results or None


# -------------------
# CREATE OR UPDATE USER DETAILS
# -------------------
async def create_user_details(email: str, details: dict):
    """
    Insert/update booking details in MongoDB
    """
    created_docs = []
    bookings = details.get("bookings", [])

    for booking in bookings:
        booking_id = booking.get("booking_id")
        if not booking_id:
            continue

        existing = await bookking_details_coll.find_one({"booking_ID": booking_id})

        travel_from_date = booking.get("travel_from_date")
        travel_to_date = booking.get("travel_to_date")
        booking_status = booking.get("order_status", "Unknown")

        primary_mail = (
            details.get("primary_mail")
            or details.get("email")
            or email
        )
        primary_contact = (
            details.get("primary_contact_no")
            or details.get("contact_no")
        )

        other_mails = booking.get("all_emails", []) or []
        other_contacts = booking.get("all_contact_nums", []) or []

        if existing:
            # Keep ObjectId for query
            object_id = existing["_id"]

            # Work on a copy
            update_doc = dict(existing)

            # Make sure nested fields exist
            update_doc.setdefault("all_emails", {"primary": primary_mail, "others": []})
            update_doc.setdefault("all_contacts", {"primary": primary_contact, "others": []})

            # Emails update
            if primary_mail and primary_mail not in update_doc["all_emails"]["others"] \
                    and primary_mail != update_doc["all_emails"].get("primary"):
                update_doc["all_emails"]["others"].append(primary_mail)

            for m in other_mails:
                if m and m not in update_doc["all_emails"]["others"] \
                        and m != update_doc["all_emails"].get("primary"):
                    update_doc["all_emails"]["others"].append(m)

            # Contacts update
            if primary_contact and primary_contact not in update_doc["all_contacts"]["others"] \
                    and primary_contact != update_doc["all_contacts"].get("primary"):
                update_doc["all_contacts"]["others"].append(primary_contact)

            for c in other_contacts:
                if c and c not in update_doc["all_contacts"]["others"] \
                        and c != update_doc["all_contacts"].get("primary"):
                    update_doc["all_contacts"]["others"].append(c)

            if travel_from_date:
                update_doc["travel_from_date"] = travel_from_date
            if travel_to_date:
                update_doc["travel_to_date"] = travel_to_date
            update_doc["booking_status"] = booking_status
            update_doc["prop_name"] = booking.get("prop_name", "")
            update_doc["prop_id"] = booking.get("prop_id")
            update_doc["updated_at"] = datetime.utcnow()

            await bookking_details_coll.replace_one({"_id": object_id}, update_doc)
            update_doc["_id"] = str(object_id)
            created_docs.append(update_doc)

        else:
            # Insert new doc
            new_doc = {
                "booking_ID": booking_id,
                "all_emails": {
                    "primary": primary_mail,
                    "others": [m for m in other_mails if m != primary_mail],
                },
                "all_contacts": {
                    "primary": primary_contact,
                    "others": [c for c in other_contacts if c != primary_contact],
                },
                "travel_from_date": travel_from_date,
                "travel_to_date": travel_to_date,
                "prop_name": booking.get("prop_name", ""),
                "prop_id": booking.get("prop_id"),
                "booking_status": booking_status,
                "updated_at": datetime.utcnow(),
            }

            result = await bookking_details_coll.insert_one(new_doc)
            new_doc["_id"] = str(result.inserted_id)
            created_docs.append(new_doc)

    return created_docs or None
async def save_booking_detailsss(booking: dict):
    if not booking:
        print("⚠️ No booking to save")
        return False

    try:
        booking_id = booking.get("booking_id") or booking.get("booking_ID")
        if not booking_id:
            print("⚠️ Missing booking_id in booking")
            return False

        contacts = {
            "primary": booking.get("traveller_contact_num") or booking.get("tenant", {}).get("tenant_phone"),
            "others": []
        }
        emails = {
            "primary": booking.get("contact_email") or booking.get("tenant", {}).get("tenant_email"),
            "others": []
        }

        travel_from_date = None
        travel_to_date = None
        # Parse dates safely
        for key in ("travel_from_date", "travel_to_date"):
            if booking.get(key):
                try:
                    val = datetime.strptime(booking[key], "%Y-%m-%d")
                    if key == "travel_from_date":
                        travel_from_date = val
                    else:
                        travel_to_date = val
                except Exception as e:
                    print(f"Invalid {key} {booking.get(key)}: {e}")

        # ✅ Use booking_id everywhere (lowercase)
        update_doc = {
            "booking_id": booking_id,
            "all_contacts": contacts,
            "all_emails": emails,
            "travel_from_date": travel_from_date,
            "travel_to_date": travel_to_date,
            "booking_status": booking.get("booking_status") or booking.get("order_status"),
            "prop_name": booking.get("property_title") or booking.get("prop_name", ""),
            "updated_at": datetime.utcnow(),
            "tenant": booking.get("tenant", {}),
            "contact_email": emails["primary"]
        }

        print(f"Update doc for booking {booking_id}: {update_doc}")

        # ✅ Check and update with booking_id
        existing = await bookking_details_coll.find_one({"booking_id": booking_id})
        if existing:
            await bookking_details_coll.update_one(
                {"booking_id": booking_id},
                {"$set": update_doc}
            )
            print(f"✅ Updated booking {booking_id}")
        else:
            await bookking_details_coll.insert_one(update_doc)
            print(f"✅ Inserted new booking {booking_id}")

        return True

    except Exception as e:
        print(f"❌ Error saving booking: {e}")
        return False
