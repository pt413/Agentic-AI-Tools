from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.model.scraped_page import ScrapedPage
from app.db.database import get_db

router = APIRouter(prefix="/api/url_details", tags=["URL_Details"])

@router.get("/")
def get_url_summary(db: Session = Depends(get_db)):
    # Query to group by base domain and aggregate suffixes with word count and last_sync
    results = (
        db.query(
            func.substring(ScrapedPage.url, r'^(.*?\.com)').label("website_base"),
            func.json_agg(
                func.json_build_object(
                    "suffix",
                    func.substring(ScrapedPage.url, r'\.com(.*)$'),
                    "word_count",
                    func.cardinality(
                        func.regexp_split_to_array(func.trim(ScrapedPage.content), r'\s+')
                    ),
                    "last_sync",
                    ScrapedPage.last_synced,  # static value but further replace with DB field later
                )
            ).label("suffix_array"),
            func.count(func.distinct(func.substring(ScrapedPage.url, r'\.com(.*)$'))).label("suffix_count"),
        )
        .filter(ScrapedPage.url.ilike("%.com%"))
        .group_by(func.substring(ScrapedPage.url, r'^(.*?\.com)'))
        .order_by(func.substring(ScrapedPage.url, r'^(.*?\.com)'))
        .all()
    )

    data = [
        {
            "Website_Base": r.website_base,
            "Suffix_Count": r.suffix_count,
            "Suffix_Array": r.suffix_array,  # JSON array
        }
        for r in results
    ]

    return data
