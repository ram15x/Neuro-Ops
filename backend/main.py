from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import os
from backend.middleware.request_id import RequestIDMiddleware
import asyncio
from datetime import datetime
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from backend.core.config import settings
from backend.core.logger import get_logger
from backend.models.database import init_db

from backend.api import auth, health, engine

logger = get_logger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="NeuroOps API",
    description="AI-Powered Infrastructure Operations Platform",
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# ========== MIDDLEWARE ==========
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from slowapi.middleware import SlowAPIMiddleware
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestIDMiddleware)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

# ========== GLOBAL EXCEPTION HANDLERS ==========
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status": "error", "code": exc.status_code}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status": "error"}
    )

# ========== ROUTER REGISTRATION ==========
app.include_router(health.router, prefix="/api/v1", tags=["Health"])
app.include_router(auth.router, prefix="/api/v1", tags=["Authentication"])
app.include_router(engine.router, prefix="/api/v1", tags=["Engine"])

# ========== STATIC FILES ==========
if os.path.exists("dashboard"):
    app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")

# ========== LIFESPAN EVENTS ==========
@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    init_db()
    logger.info(f"NeuroOps v{settings.APP_VERSION} started successfully")
    if settings.ENGINE_ENABLED:
        from backend.api.engine import get_runner
        get_runner().start()

# ========== ROOT ENDPOINTS ==========
@app.get("/")
async def root():
    return {
        "platform": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "api": "/api/v1",
        "docs": "/docs",
        "status": "operational"
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION}

@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint for the engine. Counts only, nothing about instances."""
    from fastapi.responses import PlainTextResponse
    from backend.api.engine import metrics_text
    return PlainTextResponse(metrics_text(), media_type="text/plain; version=0.0.4")


@app.get("/ready")
async def ready():
    """readiness: can the app reach its database"""
    from sqlalchemy import text
    from backend.models.database import engine as db_engine
    try:
        with db_engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:
        logger.error(f"not ready: {e}")
        return JSONResponse(status_code=503, content={"status": "not ready"})
    return {"status": "ready"}


@app.get("/ping")
async def ping():
    """Lightweight health check for ALB - no DB dependency"""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown - wait for in-flight requests to complete"""
    logger.info("NeuroOps shutting down gracefully...")
    await asyncio.sleep(3)  # Wait for in-flight requests
    logger.info("Shutdown complete")
