from sqlalchemy.orm import Session
from sqlalchemy import text, bindparam
from datetime import datetime, timedelta, timezone

class GeoService:
    DIST_SQL = """
    ST_Distance(
        ST_Point(:lng1, :lat1)::geography,
        ST_Point(:lng2, :lat2)::geography
    )
    """

    def __init__(self, db: Session):
        self.db = db

    def nearest_person(
        self,
        lat: float,
        lng: float,
        role: str | None = None,
        limit: int = 5,
        time_window_minutes: int = 15
    ):
        since_time = datetime.now(timezone.utc) - timedelta(minutes=time_window_minutes)
        sql = text("""
        WITH latest_location AS (
            SELECT DISTINCT ON (right(l.sales_phone_number, 10))
                right(l.sales_phone_number, 10) AS phone10,
                l.lat,
                l.lng,
                l.time,
                l.location
            FROM location l
            WHERE l.lat IS NOT NULL
            AND l.lng IS NOT NULL
            AND l.time >= :since_time
            ORDER BY right(l.sales_phone_number, 10), l.time DESC
        )
        SELECT
            u.name,
            u.phone,
            u.role,
            ll.location,
            ST_Distance(
                ST_Point(:lng1, :lat1)::geography,
                ST_Point(ll.lng, ll.lat)::geography
            ) AS distance_m
        FROM user_data u
        JOIN latest_location ll
        ON right(u.phone, 10) = ll.phone10
        WHERE (:role IS NULL OR u.role = :role)
        ORDER BY distance_m
        LIMIT :raw_limit
        """).bindparams(bindparam("role", value=role))
        rows = self.db.execute(
            sql,
            {
                "lat1": lat,
                "lng1": lng,
                "role": role,
                "since_time": since_time,
                "raw_limit": limit * 3,
            }
        ).fetchall()
        results = []
        seen_phones = set()
        for r in rows:
            phone10 = r.phone[-10:] if r.phone else None
            if not phone10 or phone10 in seen_phones:
                continue
            seen_phones.add(phone10)
            results.append({
                "type": "person",
                "name": r.name,
                "phone": phone10,
                "address": r.location,
                "role": r.role,
                "distance_m": round(r.distance_m, 2),
            })
            if len(results) >= limit:
                break
        return results

    def nearest_property(
        self,
        lat: float,
        lng: float,
        limit: int
    ):
        sql = text("""
            SELECT
                p.prop_id,
                p.name AS property_name,
                p.unit,
                p.bedrooms,
                p.daily_rent,
                b.bname,
                b.baddress,
                b.direction,
                b.caretaker,
                ST_Distance(
                    ST_Point(:lng1, :lat1)::geography,
                    ST_Point(b.glng, b.glat)::geography
                ) AS distance_m
            FROM properties p
            JOIN buildings b
            ON p.building_id = b.buid_id
            ORDER BY distance_m
            LIMIT :limit
            """)
        rows = self.db.execute(
            sql,
            {
                "lat1": lat,
                "lng1": lng,
                "limit": limit
            }
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "type": "property",
                "property_id": r.prop_id,
                "property_name": r.property_name,
                "unit": r.unit,
                "bedrooms": r.bedrooms,
                "daily_rent": r.daily_rent,
                "building": r.bname,
                "address": r.baddress,
                "direction": r.direction,
                "caretaker": r.caretaker,
                "distance_m": round(r.distance_m, 2)
            })
        return results
