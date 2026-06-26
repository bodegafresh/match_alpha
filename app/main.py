from asyncio import TimeoutError as AsyncTimeoutError

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import competitions, health, jobs, quant, web
from app.core.config import get_settings
from app.core.time import iso_utc
from app.core.logging import configure_logging


def database_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "status": "DATABASE_ERROR",
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "checked_at": iso_utc(),
        },
    )


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")

    @app.exception_handler(SQLAlchemyError)
    async def handle_sqlalchemy_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        return database_error_response(exc)

    @app.exception_handler(AsyncTimeoutError)
    async def handle_async_timeout_error(_: Request, exc: AsyncTimeoutError) -> JSONResponse:
        return database_error_response(exc)

    @app.exception_handler(TimeoutError)
    async def handle_timeout_error(_: Request, exc: TimeoutError) -> JSONResponse:
        return database_error_response(exc)

    @app.exception_handler(OSError)
    async def handle_os_error(_: Request, exc: OSError) -> JSONResponse:
        return database_error_response(exc)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(competitions.router, prefix=settings.api_prefix)
    app.include_router(web.router, prefix=settings.api_prefix)
    app.include_router(quant.router, prefix=settings.api_prefix)
    app.include_router(jobs.router, prefix=settings.api_prefix)
    return app


app = create_app()
