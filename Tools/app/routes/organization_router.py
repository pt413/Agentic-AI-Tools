from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.model.organization import Organization
from app.schemas.organization_schema import OrganizationCreate, OrganizationResponse
from typing import List

router = APIRouter(prefix="/api/organization", tags=["Organization"])


# ✅ POST — Create new organization
@router.post("/register", response_model=OrganizationResponse)
def create_organization(org_data: OrganizationCreate, db: Session = Depends(get_db)):
    # Check if domain already exists
    if org_data.domain:
        existing = db.query(Organization).filter(Organization.domain == org_data.domain).first()
        if existing:
            raise HTTPException(status_code=400, detail="Organization with this domain already exists")

    new_org = Organization(
        name=org_data.name,
        domain=org_data.domain,
        hierarchy=org_data.hierarchy,
        details=org_data.details
    )

    db.add(new_org)
    db.commit()
    db.refresh(new_org)
    return new_org


# ✅ GET — Get all organizations
@router.get("/", response_model=List[OrganizationResponse])
def get_all_organizations(db: Session = Depends(get_db)):
    orgs = db.query(Organization).all()
    return orgs


# ✅ GET — Get organization by ID
@router.get("/{org_id}", response_model=OrganizationResponse)
def get_organization_by_id(org_id: int, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org
