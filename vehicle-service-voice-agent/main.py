"""SpeedCare Voice Agent - Main FastAPI Application.

This is the entry point for the FastAPI backend service that provides:
- RESTful API for booking management, voice calls, notifications
- Agent conversational turn processing
- Authentication and authorization
- Health checks and metrics

Usage:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Environment:
    Requires .env.local file with database, Redis, API keys configured.
    See config.py for all required settings.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

from api.routers import agent, auth, bookings, notifications, voice
from config import get_settings

settings = get_settings()

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("speedcare.api")

# Prometheus metrics
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
ACTIVE_CONNECTIONS = Gauge(
    "active_connections",
    "Number of active connections",
)

# Global Redis pool
_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Get or create Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_POOL_MAX,
            decode_responses=True,
        )
    return _redis_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager - handles startup and shutdown."""
    # Startup
    logger.info(
        "api_startup",
        app_name=settings.APP_NAME,
        environment="production" if not settings.SENTRY_DSN else "development",
    )

    # Initialize Redis connection
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info("redis_connected", url=settings.REDIS_URL)
    except Exception as e:
        logger.error("redis_connection_failed", error=str(e))
        # Continue without Redis - some features will degrade

    # Test database connection
    try:
        from db import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("database_connected", url=settings.DATABASE_URL.split("@")[-1])
    except Exception as e:
        logger.error("database_connection_failed", error=str(e))
        raise

    yield

    # Shutdown
    logger.info("api_shutdown", app_name=settings.APP_NAME)

    # Close Redis connection
    global _redis_pool
    if _redis_pool:
        await _redis_pool.close()
        _redis_pool = None
        logger.info("redis_disconnected")

    # Close database engine
    from db import engine
    await engine.dispose()
    logger.info("database_disconnected")


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    description="Multilingual voice agent for vehicle service bookings",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS middleware
_allowed_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Log all requests with timing and request ID."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    start_time = time.monotonic()

    # Log request
    logger.info(
        "http_request",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else None,
    )

    try:
        response = await call_next(request)
        duration = time.monotonic() - start_time

        # Record metrics
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code,
        ).inc()
        REQUEST_LATENCY.labels(
            method=request.method,
            endpoint=request.url.path,
        ).observe(duration)

        # Add request ID to response
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "http_response",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )

        return response

    except Exception as e:
        duration = time.monotonic() - start_time
        logger.error(
            "http_error",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            error=str(e),
            duration_ms=round(duration * 1000, 2),
        )
        raise


# Exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle uncaught exceptions and return structured error response."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    logger.error(
        "unhandled_exception",
        request_id=request_id,
        error=str(exc),
        exc_info=True,
    )

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred. Please try again later.",
            },
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint for load balancers and monitoring."""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "checks": {},
    }

    # Check Redis
    try:
        if _redis_pool:
            await _redis_pool.ping()
            health_status["checks"]["redis"] = "ok"
        else:
            health_status["checks"]["redis"] = "disconnected"
    except Exception as e:
        health_status["checks"]["redis"] = f"error: {str(e)}"
        health_status["status"] = "degraded"

    # Check Database
    try:
        from db import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        health_status["checks"]["database"] = "ok"
    except Exception as e:
        health_status["checks"]["database"] = f"error: {str(e)}"
        health_status["status"] = "unhealthy"

    status_code = 200 if health_status["status"] == "healthy" else 503
    return JSONResponse(content=health_status, status_code=status_code)


# Readiness check (for Kubernetes)
@app.get("/ready")
async def readiness_check():
    """Readiness probe for Kubernetes."""
    try:
        from db import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception:
        return JSONResponse(
            content={"ready": False},
            status_code=503,
        )


# Liveness check (for Kubernetes)
@app.get("/live")
async def liveness_check():
    """Liveness probe for Kubernetes."""
    return {"alive": True}


# Metrics endpoint for Prometheus
@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# Include routers
app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(voice.router)
app.include_router(notifications.router)
app.include_router(agent.router)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "auth": "/api/v1/auth",
            "bookings": "/api/v1/bookings",
            "voice": "/api/v1/voice",
            "notifications": "/api/v1/notifications",
            "agent": "/api/v1/agent",
        },
    }


if __name__ == "__main__":
    import uvicorn
    import uuid
    from datetime import datetime

    print(f"Starting {settings.APP_NAME}...")
    print(f"Docs available at: http://localhost:8000/docs")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
