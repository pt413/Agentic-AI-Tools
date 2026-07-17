from __future__ import annotations

from fastapi import APIRouter

from app.routes.analytics_capabilities.booking_routes import router as booking_router
from app.routes.analytics_capabilities.communication_routes import router as communication_router
from app.routes.analytics_capabilities.conversation_route import router as conversation_router
from app.routes.analytics_capabilities.staff_routes import router as staff_router
from app.routes.analytics_capabilities.timeline_routes import router as timeline_router
from app.routes.analytics_capabilities.ui_routes import router as ui_router


router = APIRouter(prefix="/analytics/capabilities", tags=["Analytics Capabilities"])
router.include_router(ui_router)
router.include_router(timeline_router)
router.include_router(booking_router)
router.include_router(communication_router)
router.include_router(conversation_router)
router.include_router(staff_router)


__all__ = ["router"]
