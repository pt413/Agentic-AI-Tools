from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime
import uuid
import re
import json

from app.db.database import get_db
from app.platform.tenant.registry_factory import DEFAULT_REGISTRY_FACTORY

router = APIRouter(prefix="/api/llm", tags=["llm-platform"])

def _resolve_auth_profile_id(db: Session, headers: dict) -> Optional[str]:
    """Auto-create or find auth profile from headers"""
    if not headers or not isinstance(headers, dict):
        return None
    
    auth_value = headers.get("Authorization") or headers.get("authorization")
    if not auth_value:
        return None
    
    if auth_value.lower().startswith("bearer "):
        auth_type = "bearer"
        token_value = auth_value[7:]
    elif auth_value.lower().startswith("basic "):
        auth_type = "basic"
        token_value = auth_value[6:]
    else:
        auth_type = "api_key"
        token_value = auth_value
    
    existing = db.execute(
        text("""
            SELECT auth_profile_id FROM llm_auth_profile
            WHERE token_value = :token_value AND auth_type = :auth_type
            LIMIT 1
        """),
        {"token_value": token_value, "auth_type": auth_type}
    ).scalar()
    
    if existing:
        return existing
    
    profile_id = f"auto_{auth_type}_{str(uuid.uuid4())[:8]}"
    db.execute(
        text("""
            INSERT INTO llm_auth_profile
            (auth_profile_id, auth_type, header_name, token_value, extra)
            VALUES (:auth_profile_id, :auth_type, :header_name, :token_value, :extra)
        """),
        {
            "auth_profile_id": profile_id,
            "auth_type": auth_type,
            "header_name": "Authorization",
            "token_value": token_value,
            "extra": None
        }
    )
    return profile_id


# -------------------------------------------------------------------
# Pydantic Schemas
# -------------------------------------------------------------------

class ManifestCreate(BaseModel):
    tenant_id: str
    schema_version: str = "1.0"
    created_by: str
    status: str = "draft"


class ManifestUpdate(BaseModel):
    schema_version: Optional[str] = None
    status: Optional[str] = None
    created_by: Optional[str] = None


class EntityCreate(BaseModel):
    name: str
    type: str
    required: bool
    allowed_ops: Optional[List[str]] = None


class ToolCreate(BaseModel):
    tool_id: str
    capability_role: str
    lookup_mode: str
    output_domain: str
    side_effects: str
    cost_hint: Optional[str] = None
    is_memory_safe: bool = True
    max_result_size: Optional[int] = None


class ToolUpdate(BaseModel):
    description: Optional[str] = None
    tool_status: Optional[str] = None
    capability_role: Optional[str] = None
    cost_hint: Optional[str] = None
    is_memory_safe: Optional[bool] = None
    lookup_mode: Optional[str] = None
    output_domain: Optional[str] = None
    side_effects: Optional[str] = None
    max_result_size: Optional[int] = None


class ToolParamCreate(BaseModel):
    name: str
    type: str
    required: bool = True
    is_primary_id: bool = False


class ToolOutputCreate(BaseModel):
    name: str
    type: str
    cardinality: str = "one"
    is_primary: bool = False
    is_join_key: bool = False


class JargonCreate(BaseModel):
    entity_name: str
    phrase: str
    normalized: Optional[str] = None


class IntentCreate(BaseModel):
    intent_id: str
    tools: List[str]
    entities: List[str]


class ExecutionSpecCreate(BaseModel):
    executor_type: str
    timeout_ms: int = 5000
    retry_count: int = 0
    retry_backoff_ms: int = 0


class HTTPTemplateCreate(BaseModel):
    method: str
    url_template: str
    query_template: Optional[Dict] = None
    body_template: Optional[Dict] = None
    headers_template: Optional[Dict] = None
    auth_profile_id: Optional[str] = None


class AuthProfileCreate(BaseModel):
    auth_profile_id: str
    auth_type: str
    header_name: str
    token_value: str
    extra: Optional[Dict] = None


class RuntimePolicyCreate(BaseModel):
    cache_ttl_seconds: Optional[int] = None
    negative_cache_ttl: Optional[int] = None
    on_error: str = "fail"
    fallback_payload: Optional[Dict] = None


class ResponseMappingCreate(BaseModel):
    output_name: str
    json_path: str
    required: bool = False


# -------------------------------------------------------------------
# Manifest APIs
# -------------------------------------------------------------------

@router.get("/tenant-manifests")
def get_all_manifests(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None, description="Filter by status"),
    tenant_id: Optional[str] = Query(None, description="Filter by tenant ID")
):
    query = """
        SELECT manifest_id, tenant_id, manifest_version, 
               schema_version, status, 
               COALESCE(created_by, 'System') as created_by,
               created_at, activated_at
        FROM llm_tenant_manifest
        WHERE 1 = 1
    """
    params = {}
    
    if status:
        query += " AND status = :status"
        params["status"] = status
    
    if tenant_id:
        query += " AND tenant_id = :tenant_id"
        params["tenant_id"] = tenant_id
    
    query += " ORDER BY created_at DESC"
    
    rows = db.execute(text(query), params).mappings().all()
    
    # Convert to list of dicts with proper serialization
    result = []
    for row in rows:
        manifest_dict = dict(row)
        # Ensure created_at is serializable
        if manifest_dict.get('created_at'):
            manifest_dict['created_at'] = manifest_dict['created_at'].isoformat()
        if manifest_dict.get('activated_at'):
            manifest_dict['activated_at'] = manifest_dict['activated_at'].isoformat()
        result.append(manifest_dict)
    
    return result


@router.post("/tenant-manifests")
def create_manifest(payload: ManifestCreate, db: Session = Depends(get_db)):
    # ✅ Check if tenant has any active manifests
    has_active = db.execute(
        text("""
            SELECT 1 FROM llm_tenant_manifest
            WHERE tenant_id = :tenant_id AND status = 'active'
        """),
        {"tenant_id": payload.tenant_id}
    ).scalar()
    
    # ✅ If no active manifest exists, auto-activate this one
    initial_status = 'active' if not has_active else payload.status
    
    version_result = db.execute(
        text("""
            SELECT COALESCE(MAX(manifest_version), 0) + 1 as next_version
            FROM llm_tenant_manifest
            WHERE tenant_id = :tenant_id
        """),
        {"tenant_id": payload.tenant_id}
    ).fetchone()
    
    next_version = version_result[0] if version_result else 1
    
    result = db.execute(
        text("""
            INSERT INTO llm_tenant_manifest
            (tenant_id, manifest_version, schema_version, status, created_by)
            VALUES (:tenant_id, :manifest_version, :schema_version, :status, :created_by)
            RETURNING manifest_id, manifest_version, schema_version, status, created_at
        """),
        {
            "tenant_id": payload.tenant_id,
            "manifest_version": next_version,
            "schema_version": payload.schema_version,
            "status": initial_status,  # ✅ Use auto-determined status
            "created_by": payload.created_by
        }   
    ).mappings().first()
    
    db.commit()
    
    # ✅ Clear cache if activated
    if initial_status == 'active':
        DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(payload.tenant_id)
    
    return dict(result)


@router.delete("/tenant-manifests/{manifest_id}")
def delete_tenant_manifest(manifest_id: str, db: Session = Depends(get_db)):
    """
    Delete a tenant manifest by ID.
    This will cascade delete all related tools, parameters, outputs, etc.
    """
    try:
        # Check if manifest exists
        manifest = db.execute(
            text("SELECT manifest_id, tenant_id FROM llm_tenant_manifest WHERE manifest_id = :manifest_id"),
            {"manifest_id": manifest_id}
        ).fetchone()
        
        if not manifest:
            raise HTTPException(status_code=404, detail="Manifest not found")
        
        tenant_id = manifest[1]
        
        # Delete in correct order to handle foreign key constraints
        # 1. Delete tool parameters
        db.execute(
            text("""
                DELETE FROM llm_manifest_tool_param 
                WHERE tool_id IN (
                    SELECT tool_id FROM llm_manifest_tool WHERE manifest_id = :manifest_id
                )
            """),
            {"manifest_id": manifest_id}
        )
        
        # 2. Delete tool outputs
        db.execute(
            text("""
                DELETE FROM llm_manifest_tool_output 
                WHERE tool_id IN (
                    SELECT tool_id FROM llm_manifest_tool WHERE manifest_id = :manifest_id
                )
            """),
            {"manifest_id": manifest_id}
        )
        
        # 3. Delete execution specs
        db.execute(
            text("""
                DELETE FROM llm_tool_execution_spec 
                WHERE tool_id IN (
                    SELECT tool_id FROM llm_manifest_tool WHERE manifest_id = :manifest_id
                )
            """),
            {"manifest_id": manifest_id}
        )
        
        # 4. Delete HTTP templates
        db.execute(
            text("""
                DELETE FROM llm_http_execution_template 
                WHERE tool_id IN (
                    SELECT tool_id FROM llm_manifest_tool WHERE manifest_id = :manifest_id
                )
            """),
            {"manifest_id": manifest_id}
        )
        
        # 5. Delete tools
        db.execute(
            text("DELETE FROM llm_manifest_tool WHERE manifest_id = :manifest_id"),
            {"manifest_id": manifest_id}
        )
        
        # 6. Finally delete the manifest
        db.execute(
            text("DELETE FROM llm_tenant_manifest WHERE manifest_id = :manifest_id"),
            {"manifest_id": manifest_id}
        )
        
        db.commit()
        
        # Clear cache
        DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(tenant_id)
        
        return {
            "status": "success",
            "message": "Manifest deleted successfully",
            "manifest_id": manifest_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"Error deleting manifest: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete manifest: {str(e)}"
        )


@router.get("/manifests/{manifest_id}/tools")
def list_tools(manifest_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT tool_id, capability_role, lookup_mode, 
                   output_domain, side_effects, cost_hint,
                   is_memory_safe, max_result_size, created_at,tool_status
            FROM llm_manifest_tool
            WHERE manifest_id = :manifest_id
            ORDER BY created_at
        """),
        {"manifest_id": manifest_id}
    ).mappings().all()
    
    return list(rows)


@router.post("/manifests/{manifest_id}/tools")
def add_tool(manifest_id: str, payload: ToolCreate, db: Session = Depends(get_db)):
    manifest_exists = db.execute(
        text("SELECT 1 FROM llm_tenant_manifest WHERE manifest_id = :manifest_id"),
        {"manifest_id": manifest_id}
    ).scalar()
    
    if not manifest_exists:
        raise HTTPException(status_code=404, detail="Manifest not found")
    
    exists = db.execute(
        text("""
            SELECT 1 FROM llm_manifest_tool
            WHERE manifest_id = :manifest_id AND tool_id = :tool_id
        """),
        {"manifest_id": manifest_id, "tool_id": payload.tool_id}
    ).scalar()
    
    if exists:
        raise HTTPException(400, f"Tool '{payload.tool_id}' already exists")
    
    result = db.execute(
        text("""
            INSERT INTO llm_manifest_tool
            (tool_id, manifest_id, capability_role, lookup_mode,
             output_domain, side_effects, cost_hint,
             is_memory_safe, max_result_size)
            VALUES
            (:tool_id, :manifest_id, :capability_role, :lookup_mode,
             :output_domain, :side_effects, :cost_hint,
             :is_memory_safe, :max_result_size)
            RETURNING tool_id, capability_role, lookup_mode, output_domain, side_effects
        """),
        {**payload.dict(), "manifest_id": manifest_id}
    ).mappings().first()
    
    db.commit()
    return dict(result) 


@router.get("/tools/{tool_id}")
def get_tool(tool_id: str, db: Session = Depends(get_db)):
    tool = db.execute(
        text("""
            SELECT t.tool_id, t.capability_role, t.lookup_mode,
                   t.output_domain, t.side_effects, t.cost_hint,
                   t.is_memory_safe, t.max_result_size, t.created_at,
                   t.description, t.tool_status,
                   tm.tenant_id, tm.manifest_id
            FROM llm_manifest_tool t
            JOIN llm_tenant_manifest tm ON t.manifest_id = tm.manifest_id
            WHERE t.tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).mappings().first()
    
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    return dict(tool)


@router.put("/tools/{tool_id}/update")
def update_tool(
    tool_id: str, 
    payload: ToolUpdate, 
    db: Session = Depends(get_db)
):
    """Update tool configuration"""
    
    # Check if tool exists and get tenant info
    tool_info = db.execute(
        text("""
            SELECT t.tool_id, tm.tenant_id, tm.manifest_id
            FROM llm_manifest_tool t
            JOIN llm_tenant_manifest tm ON t.manifest_id = tm.manifest_id
            WHERE t.tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).fetchone()
    
    if not tool_info:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    tenant_id = tool_info[1]
    
    # Build update query dynamically based on provided fields
    update_fields = []
    update_values = {"tool_id": tool_id}
    
    if payload.description is not None:
        update_fields.append("description = :description")
        update_values["description"] = payload.description
    
    if payload.tool_status is not None:
        update_fields.append("tool_status = :tool_status")
        update_values["tool_status"] = payload.tool_status
    
    if payload.capability_role is not None:
        update_fields.append("capability_role = :capability_role")
        update_values["capability_role"] = payload.capability_role
    
    if payload.cost_hint is not None:
        update_fields.append("cost_hint = :cost_hint")
        update_values["cost_hint"] = payload.cost_hint
    
    if payload.is_memory_safe is not None:
        update_fields.append("is_memory_safe = :is_memory_safe")
        update_values["is_memory_safe"] = payload.is_memory_safe
    
    if payload.lookup_mode is not None:
        update_fields.append("lookup_mode = :lookup_mode")
        update_values["lookup_mode"] = payload.lookup_mode
    
    if payload.output_domain is not None:
        update_fields.append("output_domain = :output_domain")
        update_values["output_domain"] = payload.output_domain
    
    if payload.side_effects is not None:
        update_fields.append("side_effects = :side_effects")
        update_values["side_effects"] = payload.side_effects
    
    if payload.max_result_size is not None:
        update_fields.append("max_result_size = :max_result_size")
        update_values["max_result_size"] = payload.max_result_size
    
    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    # Execute update
    update_query = f"""
        UPDATE llm_manifest_tool
        SET {', '.join(update_fields)}
        WHERE tool_id = :tool_id
        RETURNING tool_id, description, tool_status, capability_role, 
                  cost_hint, is_memory_safe, lookup_mode, output_domain, 
                  side_effects, max_result_size, created_at
    """
    
    result = db.execute(text(update_query), update_values).mappings().first()
    
    db.commit()
    
    # Clear cache for this tenant
    DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(tenant_id)
    
    return dict(result)


@router.delete("/tools/{tool_id}")
def delete_tool(tool_id: str, db: Session = Depends(get_db)):
   
    manifest_info = db.execute(
        text("""
            SELECT t.manifest_id, tm.tenant_id
            FROM llm_manifest_tool t
            JOIN llm_tenant_manifest tm ON t.manifest_id = tm.manifest_id
            WHERE t.tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).fetchone()
    
    if not manifest_info:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    db.execute(
        text("DELETE FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    )
    
    db.commit()
    
    DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(manifest_info[1])
    
    return {"status": "deleted", "tool_id": tool_id}


# -------------------------------------------------------------------
# Tool Parameters APIs
# -------------------------------------------------------------------

@router.get("/tools/{tool_id}/parameters")
def get_tool_parameters(tool_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT param_id, name, type, required, is_bind_key as is_primary_id, is_entity_hint
            FROM llm_manifest_tool_param
            WHERE tool_id = :tool_id
            ORDER BY required DESC, name
        """),
        {"tool_id": tool_id}
    ).mappings().all()
    
    return list(rows)


@router.post("/tools/{tool_id}/parameters")
def add_tool_parameter(
    tool_id: str, 
    payload: ToolParamCreate, 
    db: Session = Depends(get_db)
):
    tool_exists = db.execute(
        text("SELECT 1 FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if not tool_exists:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    exists = db.execute(
        text("""
            SELECT 1 FROM llm_manifest_tool_param
            WHERE tool_id = :tool_id AND name = :name
        """),
        {"tool_id": tool_id, "name": payload.name}
    ).scalar()
    
    if exists:
        raise HTTPException(400, f"Parameter '{payload.name}' already exists")
    
    result = db.execute(
        text("""
            INSERT INTO llm_manifest_tool_param
            (tool_id, name, type, required, is_bind_key, is_entity_hint)
            VALUES (:tool_id, :name, :type, :required, :is_primary_id, :is_primary_id)
            RETURNING param_id, name, type, required, is_bind_key as is_primary_id, is_entity_hint
        """),
        {**payload.dict(), "tool_id": tool_id}
    ).mappings().first()
    
    db.commit()
    return dict(result)


@router.delete("/tools/{tool_id}/parameters/{param_id}")
def delete_tool_parameter(
    tool_id: str, 
    param_id: str, 
    db: Session = Depends(get_db)
):
    """Delete a specific parameter from a tool"""
    tenant_info = db.execute(
        text("""
            SELECT tm.tenant_id
            FROM llm_manifest_tool t
            JOIN llm_tenant_manifest tm ON t.manifest_id = tm.manifest_id
            WHERE t.tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).scalar()
    
    if not tenant_info:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    deleted = db.execute(
        text("""
            DELETE FROM llm_manifest_tool_param
            WHERE tool_id = :tool_id AND param_id = :param_id
            RETURNING 1
        """),
        {"tool_id": tool_id, "param_id": param_id}
    ).scalar()
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Parameter not found")
    
    db.commit()
    
    DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(tenant_info)
    
    return {"status": "deleted", "tool_id": tool_id, "param_id": param_id}


# -------------------------------------------------------------------
# Tool Outputs APIs
# -------------------------------------------------------------------

@router.get("/tools/{tool_id}/outputs")
def get_tool_outputs(tool_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT output_id, name, is_primary, is_join_key, guaranteed
            FROM llm_manifest_tool_output
            WHERE tool_id = :tool_id
            ORDER BY is_primary DESC, name
        """),
        {"tool_id": tool_id}
    ).mappings().all()
    
    return list(rows)


@router.post("/tools/{tool_id}/outputs")
def add_tool_output(
    tool_id: str, 
    payload: ToolOutputCreate, 
    db: Session = Depends(get_db)
):
    tool_exists = db.execute(
        text("SELECT 1 FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if not tool_exists:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    exists = db.execute(
        text("""
            SELECT 1 FROM llm_manifest_tool_output
            WHERE tool_id = :tool_id AND name = :name
        """),
        {"tool_id": tool_id, "name": payload.name}
    ).scalar()
    
    if exists:
        raise HTTPException(400, f"Output '{payload.name}' already exists")
    
    # Step 1: Create field in llm_manifest_field
    # # field_id = str(uuid.uuid4())
    # field_id = field_name
    field_name = payload.name     # ✅ ADD THIS LINE
    field_id = field_name         # ✅ KEEP THIS

    tool_domain = db.execute(
        text("SELECT output_domain FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()

    db.execute(
        text("""
            INSERT INTO llm_manifest_field
            (field_id, owning_domain, value_type, semantic_role, semantic_config, created_at)
            VALUES (:field_id, :owning_domain, :value_type, :semantic_role, :semantic_config, :created_at)
        """),
        {
            "field_id": field_id,
            "owning_domain": tool_domain,
            "value_type": payload.type,
            "semantic_role": {
                "integer": "metric", "number": "metric", "float": "metric",
                "boolean": "boolean", "date": "timeline", "datetime": "timeline",
                "uuid": "identity", "email": "people", "url": "meta",
                "json": "meta", "json_array": "meta", "array": "meta",
            }.get(payload.type, "text"),
            "semantic_config": "{}",
            "created_at": datetime.utcnow()
        }
    )

    # Step 2: Insert into llm_manifest_tool_output with field_id
    output_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO llm_manifest_tool_output
            (output_id, field_id, tool_id, name, is_primary, is_join_key, guaranteed, created_at)
            VALUES (:output_id, :field_id, :tool_id, :name, :is_primary, :is_join_key, :guaranteed, :created_at)
        """),
        {
            "output_id": output_id,
            "field_id": field_id,
            "tool_id": tool_id,
            "name": payload.name,
            "is_primary": payload.is_primary,
            "is_join_key": payload.is_join_key,
            "guaranteed": payload.is_primary,
            "created_at": datetime.utcnow()
        }
    )

    db.commit()
    return {
        "output_id": output_id,
        "name": payload.name,
        "type": payload.type,
        "cardinality": payload.cardinality,
        "is_primary": payload.is_primary,
        "is_join_key": payload.is_join_key
    }


@router.delete("/tools/{tool_id}/outputs/{output_id}")
def delete_tool_output(
    tool_id: str,
    output_id: str,
    db: Session = Depends(get_db)
):
    """Delete a specific output from a tool"""
    tenant_info = db.execute(
        text("""
            SELECT tm.tenant_id
            FROM llm_manifest_tool t
            JOIN llm_tenant_manifest tm ON t.manifest_id = tm.manifest_id
            WHERE t.tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).scalar()

    if not tenant_info:
        raise HTTPException(status_code=404, detail="Tool not found")

    deleted = db.execute(
        text("""
            DELETE FROM llm_manifest_tool_output
            WHERE tool_id = :tool_id AND output_id = :output_id
            RETURNING 1
        """),
        {"tool_id": tool_id, "output_id": output_id}
    ).scalar()

    if not deleted:
        raise HTTPException(status_code=404, detail="Output not found")

    db.commit()

    DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(tenant_info)

    return {"status": "deleted", "tool_id": tool_id, "output_id": output_id}


@router.get("/tools/{tool_id}/execution")
def get_execution_spec(tool_id: str, db: Session = Depends(get_db)):
    spec = db.execute(
        text("""
            SELECT executor_type, timeout_ms, retry_count, retry_backoff_ms,
                   created_at, updated_at
            FROM llm_tool_execution_spec
            WHERE tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).mappings().first()
    
    if not spec:
        return None
    
    return dict(spec)
    

@router.post("/tools/{tool_id}/execution")
def add_execution_spec(
    tool_id: str, 
    payload: ExecutionSpecCreate, 
    db: Session = Depends(get_db)
):
    tool_exists = db.execute(
        text("SELECT 1 FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if not tool_exists:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    exists = db.execute(
        text("SELECT 1 FROM llm_tool_execution_spec WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if exists:
        raise HTTPException(400, "Execution spec already exists")
    
    result = db.execute(
        text("""
            INSERT INTO llm_tool_execution_spec
            (tool_id, executor_type, timeout_ms, retry_count, retry_backoff_ms)
            VALUES (:tool_id, :executor_type, :timeout_ms, :retry_count, :retry_backoff_ms)
            RETURNING executor_type, timeout_ms, retry_count, retry_backoff_ms
        """),
        {**payload.dict(), "tool_id": tool_id}
    ).mappings().first()
    
    db.commit()
    return dict(result)


@router.put("/tools/{tool_id}/execution")
def update_execution_spec(
    tool_id: str, 
    payload: ExecutionSpecCreate, 
    db: Session = Depends(get_db)
):
    exists = db.execute(
        text("SELECT 1 FROM llm_tool_execution_spec WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if not exists:
        return add_execution_spec(tool_id, payload, db)
    
    result = db.execute(
        text("""
            UPDATE llm_tool_execution_spec
            SET executor_type = :executor_type,
                timeout_ms = :timeout_ms,
                retry_count = :retry_count,
                retry_backoff_ms = :retry_backoff_ms,
                updated_at = NOW()
            WHERE tool_id = :tool_id
            RETURNING executor_type, timeout_ms, retry_count, retry_backoff_ms
        """),
        {**payload.dict(), "tool_id": tool_id}
    ).mappings().first()
    
    db.commit()
    return dict(result)


# -------------------------------------------------------------------
# HTTP Template APIs
# -------------------------------------------------------------------

@router.get("/tools/{tool_id}/http-template")
def get_http_template(tool_id: str, db: Session = Depends(get_db)):
    template = db.execute(
        text("""
            SELECT t.method as method, t.url_template as url_template, t.query_template as query_template, 
                   t.body_template as body_template, t.headers_template as headers_template, t.auth_profile_id as auth_profile_id,
                     ap.auth_type as auth_type, ap.header_name as header_name, ap.token_value as token_value,
                   t.created_at
            FROM llm_http_execution_template t join llm_auth_profile ap on t.auth_profile_id = ap.auth_profile_id
            WHERE tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).mappings().first()
    
    if not template:
        return None
    
    return dict(template)


@router.post("/tools/{tool_id}/http-template")
def add_http_template(
    tool_id: str, 
    payload: HTTPTemplateCreate, 
    db: Session = Depends(get_db)
):
    tool_exists = db.execute(
        text("SELECT 1 FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if not tool_exists:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    exists = db.execute(
        text("SELECT 1 FROM llm_http_execution_template WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if exists:
        raise HTTPException(400, "HTTP template already exists")
    
    raw_headers = payload.headers_template or {}
    auth_profile_id = payload.auth_profile_id
    
    if not auth_profile_id and raw_headers:
        auth_profile_id = _resolve_auth_profile_id(db, raw_headers)
    
    clean_headers = {
        k: v for k, v in raw_headers.items()
        if k.lower() not in ("authorization", "x-api-key")
    }
    
    result = db.execute(
        text("""
            INSERT INTO llm_http_execution_template
            (tool_id, method, url_template, query_template, 
             body_template, headers_template, auth_profile_id)
            VALUES (:tool_id, :method, :url_template, :query_template,
                    :body_template, :headers_template, :auth_profile_id)
            RETURNING method, url_template, query_template, body_template, headers_template, auth_profile_id
        """),
        {
            **payload.dict(exclude={"headers_template", "auth_profile_id"}),
            "tool_id": tool_id,
            "headers_template": clean_headers,
            "auth_profile_id": auth_profile_id
        }
    ).mappings().first()
    
    db.commit()
    return dict(result)


# -------------------------------------------------------------------
# Auth Profile APIs
# -------------------------------------------------------------------

@router.get("/auth-profiles")
def list_auth_profiles(db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT auth_profile_id, auth_type, header_name, 
                   created_at, updated_at
            FROM llm_auth_profile
            ORDER BY created_at
        """)
    ).mappings().all()
    
    return list(rows)


@router.post("/auth-profiles")
def add_auth_profile(payload: AuthProfileCreate, db: Session = Depends(get_db)):
    exists = db.execute(
        text("SELECT 1 FROM llm_auth_profile WHERE auth_profile_id = :auth_profile_id"),
        {"auth_profile_id": payload.auth_profile_id}
    ).scalar()
    
    if exists:
        raise HTTPException(400, f"Auth profile '{payload.auth_profile_id}' already exists")
    
    result = db.execute(
        text("""
            INSERT INTO llm_auth_profile
            (auth_profile_id, auth_type, header_name, token_value, extra)
            VALUES (:auth_profile_id, :auth_type, :header_name, :token_value, :extra)
            RETURNING auth_profile_id, auth_type, header_name, created_at
        """),
        payload.dict()
    ).mappings().first()
    
    db.commit()
    return dict(result)


# -------------------------------------------------------------------
# Runtime Policy APIs
# -------------------------------------------------------------------

@router.get("/tools/{tool_id}/runtime-policy")
def get_runtime_policy(tool_id: str, db: Session = Depends(get_db)):
    policy = db.execute(
        text("""
            SELECT *
            FROM llm_manifest_tool_param
            WHERE tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).mappings().first()
    
    if not policy:
        return None
    
    return dict(policy)


@router.post("/tools/{tool_id}/runtime-policy")
def add_runtime_policy(
    tool_id: str, 
    payload: RuntimePolicyCreate, 
    db: Session = Depends(get_db)
):
    tool_exists = db.execute(
        text("SELECT * FROM llm_manifest_tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).fetchone()
    
    if not tool_exists:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    exists = db.execute(
        text("SELECT 1 FROM llm_tool_runtime_policy WHERE tool_id = :tool_id"),
        {"tool_id": tool_id}
    ).scalar()
    
    if exists:
        raise HTTPException(400, "Runtime policy already exists")
    
    result = db.execute(
        text("""
            INSERT INTO llm_tool_runtime_policy
            (tool_id, cache_ttl_seconds, negative_cache_ttl, 
             on_error, fallback_payload)
            VALUES (:tool_id, :cache_ttl_seconds, :negative_cache_ttl,
                    :on_error, :fallback_payload)
            RETURNING cache_ttl_seconds, negative_cache_ttl, on_error
        """),
        {**payload.dict(), "tool_id": tool_id}
    ).mappings().first()
    
    db.commit()
    return dict(result)


# -------------------------------------------------------------------
# Validation + Activation APIs
# -------------------------------------------------------------------
@router.post("/manifests/{manifest_id}/activate")
def activate_manifest(manifest_id: str, db: Session = Depends(get_db)):
    tenant = db.execute(
        text("""
            SELECT tenant_id
            FROM llm_tenant_manifest
            WHERE manifest_id = :mid
        """),
        {"mid": manifest_id}
    ).scalar()
    
    if not tenant:
        raise HTTPException(404, "Manifest not found")
    
    db.execute(
        text("""
            UPDATE llm_tenant_manifest
            SET status = 'deprecated'
            WHERE tenant_id = :tenant
              AND status = 'active'
        """),
        {"tenant": tenant}
    )
    
    db.execute(
        text("""
            UPDATE llm_tenant_manifest
            SET status = 'active',
                activated_at = NOW()
            WHERE manifest_id = :mid
        """),
        {"mid": manifest_id}
    )
    
    db.commit()
    
    DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(tenant)
    
    return {"status": "activated", "manifest_id": manifest_id}


# -------------------------------------------------------------------
# Cache Clear API
# -------------------------------------------------------------------
@router.post("/cache/clear/{tenant_id}")
def clear_tenant_cache(tenant_id: str):
    """Clear cache for a specific tenant"""
    DEFAULT_REGISTRY_FACTORY.clear_cache_for_tenant(tenant_id)
    return {
        "status": "success",
        "message": f"Cache cleared for tenant: {tenant_id}",
        "tenant_id": tenant_id
    }


# -------------------------------------------------------------------
# Test Execution API
# -------------------------------------------------------------------

@router.post("/execute/{tool_id}")
def execute_tool(
    tool_id: str, 
    parameters: Dict[str, Any], 
    db: Session = Depends(get_db)
):
    tool = db.execute(
        text("""
            SELECT t.*, tm.tenant_id
            FROM llm_manifest_tool t
            JOIN llm_tenant_manifest tm ON t.manifest_id = tm.manifest_id
            WHERE t.tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).mappings().first()
    
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    spec = db.execute(
        text("""
            SELECT * FROM llm_tool_execution_spec
            WHERE tool_id = :tool_id
        """),
        {"tool_id": tool_id}
    ).mappings().first()
    
    if not spec:
        raise HTTPException(status_code=400, detail="Tool has no execution specification")
    
    executor_type = spec["executor_type"]
    
    if executor_type == "static":
        static_response = db.execute(
            text("""
                SELECT response FROM llm_static_execution_template
                WHERE tool_id = :tool_id
            """),
            {"tool_id": tool_id}
        ).scalar()
        
        if not static_response:
            raise HTTPException(status_code=400, detail="No static response configured")
        
        filters = db.execute(
            text("""
                SELECT param_name, target_path, operator
                FROM llm_static_execution_filter
                WHERE tool_id = :tool_id
            """),
            {"tool_id": tool_id}
        ).fetchall()
        
        response = static_response
        
    elif executor_type == "http":
        http_template = db.execute(
            text("""
                SELECT * FROM llm_http_execution_template
                WHERE tool_id = :tool_id
            """),
            {"tool_id": tool_id}
        ).mappings().first()
        
        if not http_template:
            raise HTTPException(status_code=400, detail="No HTTP template configured")
        
        response = {
            "simulated": True,
            "method": http_template["method"],
            "url": http_template["url_template"],
            "parameters": parameters
        }
    
    else:
        response = {
            "simulated": True,
            "executor_type": executor_type,
            "parameters": parameters
        }
    
    return {
        "success": True,
        "tool_id": tool_id,
        "executor_type": executor_type,
        "duration_ms": 150,
        "cached": False,
        "data": response
    }


# -------------------------------------------------------------------
# Health Check
# -------------------------------------------------------------------

@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        
        manifest_count = db.execute(
            text("SELECT COUNT(*) FROM llm_tenant_manifest")
        ).scalar()
        
        tool_count = db.execute(
            text("SELECT COUNT(*) FROM llm_manifest_tool")
        ).scalar()
        
        return {
            "status": "healthy",
            "database": "connected",
            "stats": {
                "manifests": manifest_count,
                "tools": tool_count
            },
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# HTTP Executor APIs
# -------------------------------------------------------------------

class HttpRequest(BaseModel):
    url: str
    method: str = "GET"
    headers: Optional[Dict[str, str]] = None
    query_params: Optional[Dict[str, Any]] = None
    body: Optional[Any] = None
    timeout: int = 30
    verify_ssl: bool = True


@router.get("/http_execute_connector")
async def execute_connector_get(
    url: str = Query(..., description="URL to call"),
    method: str = Query("GET", description="HTTP method"),
    header: Optional[str] = Query(None, description="Authorization header or JSON headers string"),
    query_params: Optional[str] = Query(None, description="Query parameters as JSON string"),
    body: Optional[str] = Query(None, description="Request body as JSON string"),
    timeout: int = Query(30, description="Request timeout in seconds"),
    verify_ssl: bool = Query(True, description="Verify SSL certificates")
):
    print("url:", url)
    print("body:", body)

    return await _execute_http_request(
        url=url,
        method=method.upper(),  
        header=header,
        query_params=query_params,
        body=body,
        timeout=timeout,
        verify_ssl=verify_ssl
    )


@router.post("/http_execute_connector")
async def execute_connector_post(request: HttpRequest):
    print("request:", request)
    return await _execute_http_request(
        url=request.url,
        method=request.method.upper(),
        header=None,
        query_params=request.query_params,
        body=request.body,
        timeout=request.timeout,
        verify_ssl=request.verify_ssl,
        headers_dict=request.headers
    )


async def _execute_http_request(
    url: str,
    method: str = "GET",
    header: Optional[str] = None,
    query_params: Optional[Any] = None,
    body: Optional[Any] = None,
    timeout: int = 30,
    verify_ssl: bool = True,
    headers_dict: Optional[Dict[str, str]] = None
):
    import requests
    
    print("body request:", body)
    headers = {}
    
    if header:
        try:
            parsed_headers = json.loads(header)
            if isinstance(parsed_headers, dict):
                headers.update(parsed_headers)
        except (json.JSONDecodeError, TypeError):
            headers["Authorization"] = header
    
    if headers_dict:
        headers.update(headers_dict)
    
    if "User-Agent" not in headers:
        headers["User-Agent"] = "HTTP-Executor/1.0"
    
    final_url = url
    if query_params:
        if isinstance(query_params, str):
            try:
                query_params = json.loads(query_params)
            except json.JSONDecodeError:
                if "=" in query_params:
                    params_dict = {}
                    for param in query_params.split("&"):
                        if "=" in param:
                            key, value = param.split("=", 1)
                            params_dict[key] = value
                    query_params = params_dict
        
        if query_params and isinstance(query_params, dict):
            from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
            url_parts = list(urlparse(url))
            query = dict(parse_qs(url_parts[4]))
            query.update({k: [v] for k, v in query_params.items()})
            url_parts[4] = urlencode(query, doseq=True)
            final_url = urlunparse(url_parts)
    
    request_kwargs = {
        "headers": headers,
        "timeout": timeout,
        "verify": verify_ssl
    }
    
    request_info = {
        "url": final_url,
        "method": method,
        "headers": headers,
        "query_params": query_params if query_params else None,
        "body": body,
        "timeout": timeout,
        "verify_ssl": verify_ssl
    }
    
    print(f"HTTP Request: {method} {final_url}")
    print(f"Headers: {headers}")
    
    try:
        response_data = None
        status_code = None
        
        if method == "GET":
            response = requests.get(final_url, **request_kwargs)
            status_code = response.status_code
            try:
                response_data = response.json()
            except:
                response_data = response.text
        
        elif method == "POST":
            if body:
                if isinstance(body, dict) or isinstance(body, list):
                    request_kwargs["json"] = body
                else:
                    request_kwargs["data"] = body
            
            response = requests.post(final_url, **request_kwargs)
            status_code = response.status_code
            try:
                response_data = response.json()
            except:
                response_data = response.text
        
        elif method == "PUT":
            if body:
                if isinstance(body, dict) or isinstance(body, list):
                    request_kwargs["json"] = body
                else:
                    request_kwargs["data"] = body
            
            response = requests.put(final_url, **request_kwargs)
            status_code = response.status_code
            try:
                response_data = response.json()
            except:
                response_data = response.text
        
        elif method == "PATCH":
            if body:
                if isinstance(body, dict) or isinstance(body, list):
                    request_kwargs["json"] = body
                else:
                    request_kwargs["data"] = body
            
            response = requests.patch(final_url, **request_kwargs)
            status_code = response.status_code
            try:
                response_data = response.json()
            except:
                response_data = response.text
        
        elif method == "DELETE":
            response = requests.delete(final_url, **request_kwargs)
            status_code = response.status_code
            try:
                response_data = response.json()
            except:
                response_data = response.text
        
        elif method == "HEAD":
            response = requests.head(final_url, **request_kwargs)
            status_code = response.status_code
            response_data = {"headers": dict(response.headers)}
        
        elif method == "OPTIONS":
            response = requests.options(final_url, **request_kwargs)
            status_code = response.status_code
            try:
                response_data = response.json()
            except:
                response_data = response.text
        
        else:
            return {
                "success": False,
                "error": f"Unsupported HTTP method: {method}",
                "supported_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                "request": request_info
            }
        
        result = {
            "success": True,
            "status_code": status_code,
            "status_text": response.reason if hasattr(response, 'reason') else None,
            "data": response_data,
            "response_headers": dict(response.headers) if hasattr(response, 'headers') else None,
            "request": request_info,
            "timing": {
                "elapsed_seconds": response.elapsed.total_seconds() if hasattr(response, 'elapsed') else None
            }
        }
        
        if status_code >= 400:
            result["success"] = False
            result["error"] = f"HTTP {status_code}: {response.reason}" if hasattr(response, 'reason') else f"HTTP {status_code}"
        
        return result
        
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": f"Request timeout after {timeout} seconds",
            "request": request_info
        }
    
    except requests.exceptions.ConnectionError as e:
        return {
            "success": False,
            "error": f"Connection failed: {str(e)}",
            "request": request_info
        }
    
    except requests.exceptions.HTTPError as e:
        return {
            "success": False,
            "error": f"HTTP error: {str(e)}",
            "request": request_info
        }
    
    except requests.RequestException as e:
        return {
            "success": False,
            "error": f"Request failed: {str(e)}",
            "request": request_info
        }
    
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "request": request_info
        }


@router.get("/http_test")
async def test_http_endpoint(url: str = Query(...)):
    import requests
    try:
        response = requests.get(url, timeout=10, verify=True)
        return {
            "reachable": True,
            "status_code": response.status_code,
            "url": url
        }
    except Exception as e:
        return {
            "reachable": False,
            "error": str(e),
            "url": url
        }


# -------------------------------------------------------------------
# Tool Execution via Engine
# -------------------------------------------------------------------

from app.platform.tenant.execution_engine import ExecutionEngine


@router.post("/execute_tool")
async def execute_tool_via_engine(
    tool_id: str = Body(..., embed=True, description="Tool ID to execute"),
    parameters: Dict[str, Any] = Body(..., embed=True, description="Parameters for the tool"),
    tenant: str = Body(..., embed=True, description="Tenant identifier")
):
    engine = ExecutionEngine()
    
    try:
        result = await engine.execute(
            tenant_id=tenant,        
            tool_id=tool_id,      
            args=parameters,       
            execution_context={}    
        )
        print("result", result)
        
        return result
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "tool_id": tool_id,
            "tenant": tenant
        }


# -------------------------------------------------------------------
# Load Manifest
# -------------------------------------------------------------------

from app.platform.tenant.manifest_db_loader import load_tenant_manifest_from_db


@router.get("/load_manifest")
async def load_manifest(tenant_id: str = Query(..., description="Tenant identifier")):
    return load_tenant_manifest_from_db(tenant_id)


# -------------------------------------------------------------------
# Helper Functions
# -------------------------------------------------------------------

def _detect_field_type(value: Any) -> str:
    if value is None:
        return "string"
    
    if isinstance(value, bool):
        return "boolean"
    
    if isinstance(value, int):
        return "integer"
    
    if isinstance(value, float):
        return "number"
    
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
            if 'T' in value or ' ' in value:
                return "datetime"
            else:
                return "date"
        except (ValueError, AttributeError):
            pass
        
        if re.match(r'^https?://', value):
            return "url"
        
        if re.match(r'^[^@]+@[^@]+\.[^@]+$', value):
            return "email"
        
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', value, re.I):
            return "uuid"
        
        return "string"
    
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return "json_array"
        return "array"
    
    if isinstance(value, dict):
        return "json"
    
    return "string"


def _extract_domain_from_url(url: str) -> str:
    try:
        if '://' in url:
            url = url.split('://', 1)[1]
        
        domain = url.split('/', 1)[0]
        
        return domain.lower()
    except:
        return "api"


def _extract_parameters_from_url(url: str) -> List[Dict[str, Any]]:
    parameters = []
    
    try:
        from urllib.parse import urlparse, parse_qs
        
        parsed = urlparse(url)
        
        path_parts = parsed.path.strip('/').split('/')
        for part in path_parts:
            match = re.match(r'^\{(.+)\}$', part)
            if match:
                param_name = match.group(1)
                is_identity = param_name.endswith('_id')
                
                parameters.append({
                    "name": param_name,
                    "type": "integer" if is_identity else "string",
                    "required": True,
                    "is_entity_hint": is_identity,
                    "is_bind_key": is_identity
                })
            
            elif part.startswith(':'):
                param_name = part[1:]
                is_identity = param_name.endswith('_id')
                
                parameters.append({
                    "name": param_name,
                    "type": "integer" if is_identity else "string",
                    "required": True,
                    "is_entity_hint": is_identity,
                    "is_bind_key": is_identity
                })
        
        query_params = parse_qs(parsed.query)
        for param_name in query_params.keys():
            clean_name = re.sub(r'[\[\]]', '', param_name)
            is_identity = clean_name.endswith('_id')
            
            parameters.append({
                "name": clean_name,
                "type": "integer" if is_identity else "string",
                "required": False,
                "is_entity_hint": is_identity,
                "is_bind_key": is_identity
            })
        
        if not parameters:
            domain = _extract_domain_from_url(url)
            
            parameters.append({
                "name": f"{domain}_id",
                "type": "integer",
                "required": False,
                "is_entity_hint": True,
                "is_bind_key": True
            })
        
        parameters.append({
            "name": "lookup",
            "type": "json",
            "required": False,
            "is_entity_hint": False,
            "is_bind_key": False,
            "description": "JSON object for filtering/searching"
        })
        
    except Exception as e:
        print(f"Error extracting parameters: {e}")
        parameters = [
            {
                "name": "id",
                "type": "integer",
                "required": False,
                "is_entity_hint": True,
                "is_bind_key": True
            },
            {
                "name": "lookup",
                "type": "json",
                "required": False,
                "is_entity_hint": False,
                "is_bind_key": False
            }
        ]
    
    return parameters


def _analyze_response_pattern(data: Any) -> Dict[str, Any]:
    analysis = {
        "structure": "unknown",
        "field_count": 0,
        "nested_objects": False,
        "has_array": False,
        "identities": [],
        "suggestions": []
    }
    
    if isinstance(data, list):
        analysis["structure"] = "array"
        analysis["has_array"] = True
        if data:
            first_item = data[0]
            analysis["field_count"] = len(first_item) if isinstance(first_item, dict) else 1
            
            if isinstance(first_item, dict):
                for key, value in first_item.items():
                    if key.endswith('_id'):
                        analysis["identities"].append({
                            "name": key,
                            "domain": key.replace('_id', ''),
                            "type": _detect_field_type(value)
                        })
                    
                    if isinstance(value, dict):
                        analysis["nested_objects"] = True
                        analysis["suggestions"].append({
                            "type": "nested",
                            "message": f"Field '{key}' contains nested object",
                            "data": {
                                "field": key,
                                "type": "json",
                                "nested_fields": list(value.keys())
                            }
                        })
    
    elif isinstance(data, dict):
        analysis["structure"] = "object"
        analysis["field_count"] = len(data)
        
        for key, value in data.items():
            if key.endswith('_id'):
                analysis["identities"].append({
                    "name": key,
                    "domain": key.replace('_id', ''),
                    "type": _detect_field_type(value)
                })
            
            if isinstance(value, dict):
                analysis["nested_objects"] = True
                analysis["suggestions"].append({
                    "type": "nested",
                    "message": f"Field '{key}' contains nested object",
                    "data": {
                        "field": key,
                        "type": "json",
                        "nested_fields": list(value.keys())
                    }
                })
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                analysis["nested_objects"] = True
                analysis["suggestions"].append({
                    "type": "nested_array",
                    "message": f"Field '{key}' contains array of objects",
                    "data": {
                        "field": key,
                        "type": "json_array"
                    }
                })
    
    return analysis


# -------------------------------------------------------------------
# Analysis & Config Generation APIs
# -------------------------------------------------------------------

@router.post("/analyze_response")
async def analyze_response(response_data: Dict[str, Any]):
    suggestions = []
    data = response_data.get("data", {})
    
    if not data:
        return {"suggestions": suggestions, "analysis": {}}
    
    sample_object = data
    if isinstance(data, list) and len(data) > 0:
        sample_object = data[0]
    
    analysis = _analyze_response_pattern(data)
    
    if isinstance(sample_object, dict):
        for key, value in sample_object.items():
            field_type = _detect_field_type(value)
            is_identity = key.endswith("_id")
            
            suggestions.append({
                "type": "output",
                "message": f"Found field: {key} ({field_type})",
                "data": {
                    "name": key,
                    "type": field_type,
                    "is_primary": key == "id",
                    "is_join_key": is_identity,
                    "domain": key.replace("_id", "") if is_identity else "general",
                    "guaranteed": True
                }
            })
            
            if is_identity:
                suggestions.append({
                    "type": "parameter",
                    "message": f"Found identity parameter: {key}",
                    "data": {
                        "name": key,
                        "type": "integer" if field_type == "integer" else "string",
                        "required": False,
                        "is_entity_hint": True,
                        "is_bind_key": True
                    }
                })
                
                suggestions.append({
                    "type": "identity",
                    "message": f"Identity link: {key} → {key.replace('_id', '')} domain",
                    "data": {
                        "input": key,
                        "output": key
                    }
                })
    
    if analysis["structure"] == "array":
        suggestions.append({
            "type": "info",
            "message": "Response returns array - tool will return multiple items",
            "data": {
                "max_result_size": 100,
                "cardinality": "many"
            }
        })
    
    if analysis["nested_objects"]:
        suggestions.append({
            "type": "output",
            "message": "Add JSON blob output for full object",
            "data": {
                "name": analysis.get("domain", "data"),
                "type": "json",
                "is_primary": False,
                "is_join_key": False,
                "domain": analysis.get("domain", "general"),
                "guaranteed": True
            }
        })
    
    return {
        "suggestions": suggestions,
        "analysis": analysis
    }


@router.post("/generate_config")
async def generate_tool_config(
    request_data: Dict[str, Any],
    response_data: Dict[str, Any]
):
    try:
        url = request_data.get("url", "")
        print("urllll ", url)
        method = request_data.get("method", "GET")
        domain = _extract_domain_from_url(url)
        
        if domain == "entity":
            tool_id = f"custom_tool_{int(uuid.uuid4().int % 10000)}"
        elif "graphql" in url.lower():
            tool_id = f"gql_{domain}_query"
        elif "search" in url.lower() or "filter" in url.lower():
            tool_id = f"search_{domain}"
        else:
            tool_id = f"gdr_{domain}_search"
        
        tool_id = re.sub(r'[^a-zA-Z0-9_]', '_', tool_id)
        
        # data = response_data.get("data", {})
        data = response_data.get("data", {})

# 🔥 FIX: extract only actual object
        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], list) and len(data["results"]) > 0:
                data = data["results"][0]
        sample_object = data
        is_array = False
        
        if isinstance(data, list):
            is_array = True
            if len(data) > 0:
                sample_object = data[0]
            else:
                sample_object = {}
        
        parameters = _extract_parameters_from_url(url)
        
        if method in ["POST", "PUT", "PATCH"] and request_data.get("body"):
            try:
                body_data = request_data["body"]
                if isinstance(body_data, dict):
                    for key, value in body_data.items():
                        is_identity = key.endswith('_id')
                        
                        parameters.append({
                            "name": key,
                            "type": _detect_field_type(value),
                            "required": False,
                            "is_entity_hint": is_identity,
                            "is_bind_key": is_identity
                        })
            except Exception:
                pass
        
        # ✅ FIX: Only add outputs from response, no automatic domain blob
        outputs = []
        if isinstance(sample_object, dict):
            for key, value in sample_object.items():
                field_type = _detect_field_type(value)
                is_identity = key.endswith("_id")
                
                outputs.append({
                    "name": key,
                    "type": field_type,
                    "cardinality": "one",
                    "is_primary": key in ["id", f"{domain}_id", "_id"],
                    "is_join_key": is_identity,
                    "domain": domain if is_identity else "general",
                    "guaranteed": True
                })
        
        # ✅ DON'T automatically add domain blob - let user decide
        
        input_identities = [
            p["name"] for p in parameters 
            if p.get("is_bind_key") and p["name"].endswith("_id")
        ]
        
        output_identities = [
            o["name"] for o in outputs 
            if o.get("name", "").endswith("_id")
        ]
        
        has_lookup = any(p.get("name") == "lookup" for p in parameters)
        has_identity_params = len(input_identities) > 0
        
        if has_lookup or not has_identity_params:
            lookup_mode = "discover"
        else:
            lookup_mode = "by_id"
        
        capability_role = "primary"
        if "join" in url.lower() or "expand" in url.lower():
            capability_role = "join"
        elif "aggregate" in url.lower() or "stats" in url.lower():
            capability_role = "aggregate"
        
        cost_hint = "moderate"
        if "graphql" in url.lower():
            cost_hint = "expensive"
        elif "search" in url.lower() or "filter" in url.lower():
            cost_hint = "cheap"
        
        http_template = {
            "method": method,
            "url_template": url,
            "headers_template": request_data.get("headers", {}),
            "query_template": request_data.get("query_params", {})
        }
        
        if method in ["POST", "PUT", "PATCH"]:
            http_template["body_template"] = request_data.get("body", {})
        
        response_path = None
        if isinstance(data, dict) and "data" in data:
            response_path = "data"
        elif isinstance(data, dict) and "results" in data:
            response_path = "results"
        
        if response_path:
            http_template["response_path"] = response_path
        
        config = {
            "tool_id": tool_id,
            "name": f"{domain.capitalize()} {'Query' if 'graphql' in url.lower() else 'Search'}",
            "description": f"Fetches {domain} details via {method} request",
            "capability_role": capability_role,
            "lookup_mode": lookup_mode,
            "output_domain": domain,
            "side_effects": "read" if method == "GET" else "write",
            "cost_hint": cost_hint,
            "is_memory_safe": True,
            "max_result_size": 1000 if is_array else 100,
            "parameters": parameters,
            "outputs": outputs,
            "input_identities": input_identities,
            "output_identities": output_identities,
            "http_template": http_template,
            "execution_config": {
                "executor_type": "http",
                "timeout_ms": 10000 if "graphql" in url.lower() else 5000,
                "retry_count": 3 if method == "GET" else 1,
                "retry_backoff_ms": 1000,
                "max_fanout_final": 50 if is_array else 10,
                "max_fanout_intermediate": 20 if is_array else 5
            },
            "metadata": {
                "generated_at": datetime.utcnow().isoformat(),
                "source_url": url,
                "response_structure": "array" if is_array else "object",
                "field_count": len(sample_object) if isinstance(sample_object, dict) else 0
            }
        }
        
        return config
        
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to generate tool config: {str(e)}"
        )


@router.post("/validate")
async def validate_tool_config_endpoint(config: Dict[str, Any]):
    errors = []
    warnings = []
    suggestions = []
    
    try:
        if not config.get("tool_id"):
            errors.append("Tool ID is required")
        
        if not config.get("output_domain"):
            errors.append("Output domain is required")
        
        tool_id = config.get("tool_id", "")
        if tool_id:
            if not re.match(r"^[a-zA-Z0-9_]+$", tool_id):
                errors.append("Tool ID must contain only letters, numbers, and underscores")
            
            if len(tool_id) > 100:
                errors.append("Tool ID must be less than 100 characters")
            
            if not re.match(r"^(gdr_|connector\.search_|gql_|search_|custom_)", tool_id):
                warnings.append("Tool ID doesn't follow standard naming conventions")
                suggestions.append({
                    "type": "naming",
                    "message": "Consider prefixing with gdr_, connector.search_, or gql_",
                    "suggestion": f"gdr_{config.get('output_domain', 'entity')}_search"
                })
        
        parameters = config.get("parameters", [])
        if not isinstance(parameters, list):
            errors.append("Parameters must be a list")
        else:
            for i, param in enumerate(parameters):
                if not param.get("name"):
                    errors.append(f"Parameter {i+1} missing name")
                if "type" not in param:
                    errors.append(f"Parameter '{param.get('name')}' missing type")
        
        identity_params = [p for p in parameters if p.get("is_bind_key")]
        lookup_mode = config.get("lookup_mode", "by_id")
        
        if lookup_mode == "by_id" and len(identity_params) == 0:
            errors.append("By-id tools need at least one identity parameter (_id field)")
            suggestions.append({
                "type": "parameter",
                "message": "Add an identity parameter for by-id lookup",
                "suggestion": {
                    "name": f"{config.get('output_domain', 'entity')}_id",
                    "type": "integer",
                    "required": True,
                    "is_entity_hint": True,
                    "is_bind_key": True
                }
            })
        
        outputs = config.get("outputs", [])
        if not isinstance(outputs, list):
            errors.append("Outputs must be a list")
        else:
            primary_outputs = [o for o in outputs if o.get("is_primary")]
            
            if len(primary_outputs) == 0:
                errors.append("At least one output must be marked as primary")
            
            for i, output in enumerate(outputs):
                if not output.get("name"):
                    errors.append(f"Output {i+1} missing name")
                if "type" not in output:
                    errors.append(f"Output '{output.get('name')}' missing type")
        
        input_identities = config.get("input_identities", [])
        output_identities = config.get("output_identities", [])
        
        if len(input_identities) == 0:
            warnings.append("Tool has no input identities - may not be discoverable")
        
        if len(output_identities) == 0:
            warnings.append("Tool has no output identities - cannot be used in identity graph")
        
        has_json_output = any(o.get("type") == "json" for o in outputs)
        if not has_json_output:
            suggestions.append({
                "type": "output",
                "message": "Add JSON output for full object access",
                "suggestion": {
                    "name": config.get("output_domain", "data"),
                    "type": "json",
                    "is_primary": False,
                    "is_join_key": False,
                    "domain": config.get("output_domain", "general"),
                    "guaranteed": True
                }
            })
        
        http_template = config.get("http_template", {})
        if not http_template.get("url_template"):
            errors.append("URL template is required")
        else:
            url = http_template["url_template"]
            if not re.match(r'^https?://', url):
                warnings.append("URL template should start with http:// or https://")
        
        exec_config = config.get("execution_config", {})
        if exec_config.get("timeout_ms", 0) > 60000:
            warnings.append("Timeout exceeds 60 seconds - consider reducing")
        
        if exec_config.get("max_fanout_final", 0) > 1000:
            warnings.append("Max fanout final is very high - may cause performance issues")
        
        if config.get("side_effects") == "write" and config.get("cost_hint") == "cheap":
            warnings.append("Write operations with cheap cost hint may not be appropriate")
        
        if config.get("max_result_size", 0) > 10000:
            warnings.append("Max result size is very high - consider pagination")
        
    except Exception as e:
        errors.append(f"Validation error: {str(e)}")
    
    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "suggestions": suggestions,
        "summary": {
            "parameter_count": len(parameters),
            "output_count": len(outputs),
            "input_identities": len(input_identities),
            "output_identities": len(output_identities)
        }
    }


def validate_tool_config(tool_config: Dict[str, Any]) -> Dict[str, Any]:
    errors = []
    warnings = []
    
    print(f"DEBUG: Validating tool: {tool_config.get('tool_id', 'unknown')}")
    
    tool_id = tool_config.get("tool_id")
    if not tool_id:
        errors.append("Tool ID is required")
    elif not isinstance(tool_id, str):
        errors.append("Tool ID must be a string")
    
    output_domain = tool_config.get("output_domain")
    if not output_domain:
        errors.append("Output domain is required")
    elif not isinstance(output_domain, str):
        errors.append("Output domain must be a string")
    
    http_template = tool_config.get("http_template", {})
    if not isinstance(http_template, dict):
        errors.append("http_template must be an object")
    else:
        url_template = http_template.get("url_template")
        if not url_template:
            errors.append("URL template is required")
        elif not isinstance(url_template, str):
            errors.append("URL template must be a string")
        
        method = http_template.get("method")
        if not method:
            errors.append("HTTP method is required")
        elif not isinstance(method, str):
            errors.append("HTTP method must be a string")
        elif method.upper() not in ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]:
            warnings.append(f"Unusual HTTP method: {method}")
    
    parameters = tool_config.get("parameters", [])
    if not isinstance(parameters, list):
        errors.append("parameters must be an array")
    else:
        param_names = []
        for param in parameters:
            if not isinstance(param, dict):
                errors.append("Each parameter must be an object")
                continue
            
            param_name = param.get("name")
            if not param_name:
                errors.append("Parameter name is required")
            elif not isinstance(param_name, str):
                errors.append("Parameter name must be a string")
            else:
                param_names.append(param_name.lower())
        
        seen = set()
        duplicates = [name for name in param_names if name in seen or seen.add(name)]
        if duplicates:
            warnings.append(f"Duplicate parameter names: {list(set(duplicates))}")
    
    outputs = tool_config.get("outputs", [])
    if not isinstance(outputs, list):
        errors.append("outputs must be an array")
    else:
        output_names = []
        for output in outputs:
            if not isinstance(output, dict):
                errors.append("Each output must be an object")
                continue
            
            output_name = output.get("name")
            if not output_name:
                errors.append("Output name is required")
            elif not isinstance(output_name, str):
                errors.append("Output name must be a string")
            else:
                output_names.append(output_name.lower())
        
        seen = set()
        duplicates = [name for name in output_names if name in seen or seen.add(name)]
        if duplicates:
            warnings.append(f"Duplicate output names: {list(set(duplicates))}")
    
    print(f"DEBUG: Validation result - errors: {len(errors)}, warnings: {len(warnings)}")
    
    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }


# -------------------------------------------------------------------
# Bulk Create Tools
# -------------------------------------------------------------------

def bulk_create_tools(
    db: Session,
    manifest_id: str,
    tools: List[Dict[str, Any]]
):
    results = []

    for i, tool_config in enumerate(tools):
        tool_id = tool_config.get("tool_id", f"unknown_{i}")
        print(f"DEBUG: Processing tool {i}: {tool_id}")

        try:
            validation = validate_tool_config(tool_config)
            if not validation["is_valid"]:
                results.append({
                    "index": i, "tool_id": tool_id,
                    "success": False, "errors": validation["errors"]
                })
                continue

            exists = db.execute(
                text("""
                    SELECT 1 FROM llm_manifest_tool
                    WHERE tool_id = :tool_id AND manifest_id = :manifest_id
                """),
                {"tool_id": tool_id, "manifest_id": manifest_id}
            ).scalar()

            if exists:
                results.append({
                    "index": i, "tool_id": tool_id,
                    "success": False, "error": f"Tool '{tool_id}' already exists"
                })
                continue

            try:
                db.execute(
                    text("""
                        INSERT INTO llm_manifest_tool
                        (tool_id, manifest_id, capability_role, lookup_mode, output_domain,
                         side_effects, cost_hint, is_memory_safe, max_result_size,
                         description, tool_status, created_at)
                        VALUES
                        (:tool_id, :manifest_id, :capability_role, :lookup_mode, :output_domain,
                         :side_effects, :cost_hint, :is_memory_safe, :max_result_size,
                         :description, :tool_status, :created_at)
                    """),
                    {
                        "tool_id": tool_id,
                        "manifest_id": manifest_id,
                        "capability_role": tool_config.get("capability_role", "primary"),
                        "lookup_mode": tool_config.get("lookup_mode", "by_id"),
                        "output_domain": tool_config.get("output_domain", ""),
                        "side_effects": tool_config.get("side_effects", "read"),
                        "cost_hint": tool_config.get("cost_hint", "moderate"),
                        "is_memory_safe": tool_config.get("is_memory_safe", True),
                        "max_result_size": tool_config.get("max_result_size", 1000),
                        "description": tool_config.get("description", ""),
                        "tool_status": tool_config.get("tool_status", "draft"),
                        "created_at": datetime.utcnow()
                    }
                )

                http_template = tool_config.get("http_template", {})
                if http_template:
                    raw_headers = http_template.get("headers_template") or {}
                    
                    auth_profile_id = tool_config.get("auth_profile_id")
                    if not auth_profile_id:
                        auth_profile_id = _resolve_auth_profile_id(db, raw_headers)
                    
                    clean_headers = {
                        k: v for k, v in raw_headers.items()
                        if k.lower() not in ("authorization", "x-api-key")
                    }
                    
                    db.execute(
                        text("""
                            INSERT INTO llm_http_execution_template
                            (tool_id, method, url_template, query_template, headers_template, 
                             body_template, auth_profile_id, created_at)
                            VALUES
                            (:tool_id, :method, :url_template, :query_template, :headers_template,
                             :body_template, :auth_profile_id, :created_at)
                        """),
                        {
                            "tool_id": tool_id,
                            "method": http_template.get("method", "GET"),
                            "url_template": http_template.get("url_template", ""),
                            "query_template": json.dumps(http_template.get("query_template") or {}),
                            "headers_template": json.dumps(clean_headers),
                            "body_template": json.dumps(http_template.get("body_template")) if http_template.get("body_template") else None,
                            "auth_profile_id": auth_profile_id,
                            "created_at": datetime.utcnow()
                        }
                    )

                exec_config = tool_config.get("execution_config", {})
                if exec_config:
                    db.execute(
                        text("""
                            INSERT INTO llm_tool_execution_spec
                            (tool_id, executor_type, timeout_ms, retry_count, retry_backoff_ms,
                             max_fanout_final, max_fanout_intermediate, created_at, updated_at)
                            VALUES
                            (:tool_id, :executor_type, :timeout_ms, :retry_count, :retry_backoff_ms,
                             :max_fanout_final, :max_fanout_intermediate, :created_at, :updated_at)
                        """),
                        {
                            "tool_id": tool_id,
                            "executor_type": "http",
                            "timeout_ms": exec_config.get("timeout_ms", 5000),
                            "retry_count": exec_config.get("retry_count", 0),
                            "retry_backoff_ms": exec_config.get("retry_backoff_ms", 0),
                            "max_fanout_final": exec_config.get("max_fanout_final", 40),
                            "max_fanout_intermediate": exec_config.get("max_fanout_intermediate", 20),
                            "created_at": datetime.utcnow(),
                            "updated_at": datetime.utcnow()
                        }
                    )

                for param in tool_config.get("parameters", []):
                    db.execute(
                        text("""
                            INSERT INTO llm_manifest_tool_param
                            (param_id, tool_id, name, type, required, is_entity_hint, is_bind_key)
                            VALUES (:param_id, :tool_id, :name, :type, :required, :is_entity_hint, :is_bind_key)
                        """),
                        {
                            "param_id": str(uuid.uuid4()),
                            "tool_id": tool_id,
                            "name": param.get("name", ""),
                            "type": param.get("type", "string"),
                            "required": param.get("required", False),
                            "is_entity_hint": param.get("is_entity_hint", False),
                            "is_bind_key": param.get("is_bind_key", False)
                        }
                    )

                # ✅ FIX: Only add user-specified outputs
                user_outputs = tool_config.get("outputs", [])

                if not user_outputs:
                    print(f"⚠️ Tool {tool_id} has no outputs specified")
                else:
                    for output in user_outputs:
                        field_id = str(uuid.uuid4())
                        db.execute(
                            text("""
                                INSERT INTO llm_manifest_field
                                (field_id, owning_domain, value_type, semantic_role, semantic_config, created_at)
                                VALUES (:field_id, :owning_domain, :value_type, :semantic_role, :semantic_config, :created_at)
                            """),
                            {
                                "field_id": field_id,
                                "owning_domain": tool_config.get("output_domain", "general"),
                                "value_type": output.get("type", "string"),
                                "semantic_role": {
                                    "integer": "metric",
                                    "number": "metric",
                                    "float": "metric",
                                    "boolean": "boolean",
                                    "date": "timeline",
                                    "datetime": "timeline",
                                    "uuid": "identity",
                                    "email": "people",
                                    "url": "meta",
                                    "json": "meta",
                                    "json_array": "meta",
                                    "array": "meta",
                                }.get(output.get("type", "string"), "text"),
                                "semantic_config": json.dumps({}),
                                "created_at": datetime.utcnow()
                            }
                        )
                        db.execute(
                            text("""
                                INSERT INTO llm_manifest_tool_output
                                (output_id, field_id, tool_id, name, is_primary, is_join_key, guaranteed, created_at)
                                VALUES
                                (:output_id, :field_id, :tool_id, :name, :is_primary, :is_join_key, :guaranteed, :created_at)
                            """),
                            {
                                "output_id": str(uuid.uuid4()),
                                "field_id": field_id,
                                "tool_id": tool_id,
                                "name": output.get("name", ""),
                                "is_primary": output.get("is_primary", False),
                                "is_join_key": output.get("is_join_key", False),
                                "guaranteed": output.get("guaranteed", False),
                                "created_at": datetime.utcnow()
                            }
                        )

                db.commit()
                print(f"DEBUG: Successfully saved tool: {tool_id}")

                results.append({
                    "index": i, "tool_id": tool_id,
                    "success": True, "message": "Tool created successfully"
                })

            except Exception as db_error:
                db.rollback()
                print(f"DEBUG: DB error for tool {tool_id}: {str(db_error)}")
                import traceback
                traceback.print_exc()
                results.append({
                    "index": i, "tool_id": tool_id,
                    "success": False, "error": str(db_error)
                })

        except Exception as e:
            print(f"DEBUG: Error processing tool {tool_id}: {str(e)}")
            results.append({
                "index": i, "tool_id": tool_id,
                "success": False, "error": str(e)
            })

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "manifest_id": manifest_id,
        "total": len(tools),
        "successful": success_count,
        "failed": len(tools) - success_count,
        "results": results
    }


@router.post("/bulk-create/{manifest_id}")
def create_bulk_tools(
    manifest_id: str,
    request: Dict[str, Any],
    db: Session = Depends(get_db)
):
    tools = request.get("tools", [])

    if not tools:
        raise HTTPException(status_code=422, detail="No tools provided")

    manifest_exists = db.execute(
        text("SELECT 1 FROM llm_tenant_manifest WHERE manifest_id = :manifest_id"),
        {"manifest_id": manifest_id}
    ).scalar()

    if not manifest_exists:
        raise HTTPException(status_code=404, detail=f"Manifest {manifest_id} not found")

    try:
        result_data = bulk_create_tools(db, manifest_id, tools)
        return {"success": True, "data": result_data}
    except Exception as e:
        print(f"DEBUG: Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# -------------------------------------------------------------------
# Templates & OpenAPI
# -------------------------------------------------------------------

@router.get("/templates")
async def get_tool_templates():
    return {
        "templates": [
            {
                "name": "REST API Get by ID",
                "description": "Fetch single entity by ID",
                "config": {
                    "lookup_mode": "by_id",
                    "capability_role": "primary",
                    "parameters": [
                        {
                            "name": "id",
                            "type": "integer",
                            "required": True,
                            "is_entity_hint": True,
                            "is_bind_key": True
                        }
                    ]
                }
            },
            {
                "name": "REST API Search",
                "description": "Search/filter entities",
                "config": {
                    "lookup_mode": "discover",
                    "capability_role": "primary",
                    "parameters": [
                        {
                            "name": "lookup",
                            "type": "json",
                            "required": False,
                            "is_entity_hint": False,
                            "is_bind_key": False
                        },
                        {
                            "name": "limit",
                            "type": "integer",
                            "required": False,
                            "is_entity_hint": False,
                            "is_bind_key": False
                        },
                        {
                            "name": "offset",
                            "type": "integer",
                            "required": False,
                            "is_entity_hint": False,
                            "is_bind_key": False
                        }
                    ]
                }
            },
            {
                "name": "GraphQL Query",
                "description": "GraphQL endpoint query",
                "config": {
                    "lookup_mode": "discover",
                    "capability_role": "rag",
                    "parameters": [
                        {
                            "name": "query",
                            "type": "string",
                            "required": True,
                            "is_entity_hint": False,
                            "is_bind_key": False
                        },
                        {
                            "name": "variables",
                            "type": "json",
                            "required": False,
                            "is_entity_hint": False,
                            "is_bind_key": False
                        }
                    ]
                }
            }
        ]
    }


@router.post("/extract_from_openapi")
async def extract_from_openapi(openapi_spec: Dict[str, Any]):
    tools = []
    
    try:
        paths = openapi_spec.get("paths", {})
        
        for path, methods in paths.items():
            print("paths", path)
            for method, spec in methods.items():
                if method.lower() in ["get", "post", "put", "patch", "delete"]:
                    parameters = []
                    for param in spec.get("parameters", []):
                        param_name = param.get("name", "")
                        param_type = param.get("schema", {}).get("type", "string")
                        
                        type_map = {
                            "integer": "integer",
                            "number": "number",
                            "boolean": "boolean",
                            "array": "json",
                            "object": "json"
                        }
                        
                        param_type = type_map.get(param_type, "string")
                        is_identity = param_name.endswith('_id') or param_name == 'id'
                        
                        parameters.append({
                            "name": param_name,
                            "type": param_type,
                            "required": param.get("required", False),
                            "is_entity_hint": is_identity,
                            "is_bind_key": is_identity,
                            "description": param.get("description", "")
                        })
                    
                    operation_id = spec.get("operationId", "")
                    if operation_id:
                        tool_id = f"api_{operation_id}"
                    else:
                        tool_id = re.sub(r'[^a-zA-Z0-9_]', '_', path.strip('/'))
                        tool_id = f"{method.lower()}_{tool_id}"
                    
                    domain = "entity"
                    if spec.get("tags"):
                        domain = spec["tags"][0].lower()
                    else:
                        domain = _extract_domain_from_url(f"http://example.com{path}")
                    
                    tools.append({
                        "tool_id": tool_id,
                        "name": spec.get("summary", f"{method.upper()} {path}"),
                        "description": spec.get("description", ""),
                        "capability_role": "primary",
                        "lookup_mode": "discover" if method.lower() == "get" else "by_id",
                        "output_domain": domain,
                        "side_effects": "read" if method.lower() == "get" else "write",
                        "cost_hint": "moderate",
                        "is_memory_safe": True,
                        "max_result_size": 100,
                        "parameters": parameters,
                        "input_identities": [p["name"] for p in parameters if p["is_bind_key"]],
                        "http_template": {
                            "method": method.upper(),
                            "url_template": f"{{{{base_url}}}}{path}",
                            "description": spec.get("description", "")
                        }
                    })
        
        return {
            "tools_found": len(tools),
            "tools": tools
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to extract from OpenAPI: {str(e)}"
        )


# -------------------------------------------------------------------
# Execution Trace APIs
# -------------------------------------------------------------------

@router.get("/execution-trace/recent-sessions")
async def get_recent_sessions(
    user_id: int = Query(..., description="User ID to filter sessions"),
    limit: int = Query(10, description="Number of sessions to return"),
    db = Depends(get_db)
):
    try:
        print(f"🔍 Fetching recent sessions for user_id: {user_id}")
        
        result = db.execute(
            text("""
                SELECT DISTINCT ON (session_id) 
                    session_id, user_query, created_at
                FROM llm_execution_trace 
                WHERE user_id = :user_id
                AND session_id IS NOT NULL
                ORDER BY session_id, created_at DESC
            """),
            {"user_id": user_id}
        ).fetchall()
        
        sessions = []
        for row in result:
            row_dict = dict(row._mapping)
            created_at = row_dict.get('created_at')
            sessions.append({
                "session_id": row_dict.get('session_id'),
                "user_query": row_dict.get('user_query'),
                "created_at": created_at.isoformat() if created_at else None
            })
        
        sessions.sort(key=lambda x: x['created_at'] or '', reverse=True)
        sessions = sessions[:limit]
        
        print(f"✅ Found {len(sessions)} sessions for user_id: {user_id}")
        return sessions
        
    except Exception as e:
        print(f"❌ Recent sessions error: {e}")
        import traceback
        traceback.print_exc()
        return []


@router.get("/execution-trace/search")
async def search_execution_trace(
    type: str = Query(..., description="Search type: 'session_id' or 'request_id'"),
    value: str = Query(..., description="Value to search for"),
    db = Depends(get_db)
):
    try:
        print(f"🔍 Searching {type} = {value}")
        
        if type == 'session_id':
            result = db.execute(
                text("""
                    SELECT 
                        request_id, session_id, user_query, created_at,
                        final_text, final_rich, rewrite_payload, meta,
                        preflight_result, planning_contract, tool_outputs, critic,
                        manifest, session_snapshot_pre, session_snapshot_post,
                        execution_mode, latency_metrics, trace_meta
                    FROM llm_execution_trace 
                    WHERE session_id = :value
                    ORDER BY created_at ASC
                """),
                {"value": value}
            ).fetchall()
            
            if not result or len(result) == 0:
                print(f"❌ No data found for session {value}")
                raise HTTPException(status_code=404, detail=f"No traces found for session_id: {value}")
            
            print(f"✅ Found {len(result)} records")
            
            requests = []
            for row in result:
                row_dict = dict(row._mapping)
                requests.append({
                    "request_id": row_dict.get('request_id') or "",
                    "session_id": row_dict.get('session_id'),
                    "user_query": row_dict.get('user_query'),
                    "final_text": row_dict.get('final_text'),
                    "final_rich": row_dict.get('final_rich'),
                    "rewrite_payload": row_dict.get('rewrite_payload'),
                    "meta": row_dict.get('meta'),
                    "preflight_result": row_dict.get('preflight_result'),
                    "planning_contract": row_dict.get('planning_contract'),
                    "tool_outputs": row_dict.get('tool_outputs'),
                    "critic": row_dict.get('critic'),
                    "manifest": row_dict.get('manifest'),
                    "session_snapshot_pre": row_dict.get('session_snapshot_pre'),
                    "session_snapshot_post": row_dict.get('session_snapshot_post'),
                    "execution_mode": row_dict.get('execution_mode'),
                    "latency_metrics": row_dict.get('latency_metrics'),
                    "trace_meta": row_dict.get('trace_meta')
                })
            
            return requests
            
        elif type == 'request_id':
            result = db.execute(
                text("""
                    SELECT 
                        request_id, session_id, user_query, created_at,
                        final_text, final_rich, rewrite_payload, meta,
                        preflight_result, planning_contract, tool_outputs, critic,
                        manifest, session_snapshot_pre, session_snapshot_post,
                        execution_mode, latency_metrics, trace_meta
                    FROM llm_execution_trace 
                    WHERE request_id = :value
                    LIMIT 1
                """),
                {"value": value}
            ).fetchone()
            
            if not result:
                raise HTTPException(status_code=404, detail=f"No trace found for request_id: {value}")
            
            row_dict = dict(result._mapping)
            
            return {
                "request_id": row_dict.get('request_id') or "",
                "session_id": row_dict.get('session_id'),
                "user_query": row_dict.get('user_query'),
                "final_text": row_dict.get('final_text'),
                "final_rich": row_dict.get('final_rich'),
                "rewrite_payload": row_dict.get('rewrite_payload'),
                "meta": row_dict.get('meta'),
                "preflight_result": row_dict.get('preflight_result'),
                "planning_contract": row_dict.get('planning_contract'),
                "tool_outputs": row_dict.get('tool_outputs'),
                "critic": row_dict.get('critic'),
                "manifest": row_dict.get('manifest'),
                "session_snapshot_pre": row_dict.get('session_snapshot_pre'),
                "session_snapshot_post": row_dict.get('session_snapshot_post'),
                "execution_mode": row_dict.get('execution_mode'),
                "latency_metrics": row_dict.get('latency_metrics'),
                "trace_meta": row_dict.get('trace_meta')
            }
        
        else:
            raise HTTPException(status_code=400, detail="Type must be 'session_id' or 'request_id'")
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Search error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))