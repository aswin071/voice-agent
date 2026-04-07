"""API Routers package.

Exports all API routers for mounting in the main FastAPI application.
"""

from api.routers.agent import router as agent_router
from api.routers.auth import router as auth_router
from api.routers.bookings import router as bookings_router
from api.routers.notifications import router as notifications_router
from api.routers.voice import router as voice_router

# Export routers with their mount prefixes
router = {
    "auth": auth_router,
    "bookings": bookings_router,
    "voice": voice_router,
    "notifications": notifications_router,
    "agent": agent_router,
}
