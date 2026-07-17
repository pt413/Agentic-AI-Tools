from fastapi import APIRouter
from app.schemas.adverse_lookup_schema import AdverseLookupRequest
from app.services.web_rag.adverse_lookup.query_builder import build_queries
from app.services.web_rag.adverse_lookup.lookup_runner import run_lookup
from app.services.web_rag.adverse_lookup.result_filter import filter_results
from app.services.web_rag.adverse_lookup.response_formatter import format_response

router = APIRouter(prefix="/illegal", tags=["Adverse Lookup"])

@router.post("/lookup")
def adverse_lookup(payload: AdverseLookupRequest):
    queries = build_queries(
        phone=payload.phone,
        email=payload.email
    )

    DOMAINS = [
        "indiankanoon.org",
        "consumercomplaints.in",
        "complaintboard.in",
        "complaintsboard.com",
        "scamadvisor.com",
        "ripoffreport.com",
        "scammer.info",
        "reddit.com",
        "thehindu.com",
        "indianexpress.com",
        "timesofindia.indiatimes.com"
    ]

    raw_chunks = run_lookup(
        queries,
        top_k=5,
        include_domains=DOMAINS
    )

    identifier = payload.email or payload.phone
    filtered = filter_results(raw_chunks, identifier)

    return format_response(identifier, filtered)
