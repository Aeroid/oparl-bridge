"""oparl-bridge: OParl 1.1 gateway for ALLRIS municipal information systems."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response

from oparl_bridge.api.md import router as md_router
from oparl_bridge.api.routes import router as oparl_router
from oparl_bridge.api.ui import router as ui_router
from oparl_bridge.config import settings
from oparl_bridge.db.session import init_db

_STATIC = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _lifespan(app: FastAPI):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        logger.info("oparl-bridge started. ALLRIS base: %s", settings.allris_base_url)
        if settings.wikidata_id:
            import asyncio

            from oparl_bridge.wikidata import warm_cache
            asyncio.ensure_future(warm_cache(settings.wikidata_id))
        yield
    return lifespan(app)


def create_app(
    *,
    enable_oparl: bool = True,
    enable_md: bool = True,
    enable_spa: bool = True,
) -> FastAPI:
    """Create the FastAPI application, optionally disabling frontends."""
    if not (enable_oparl or enable_md or enable_spa):
        raise ValueError("At least one frontend must be enabled (--no-oparl --no-md --no-spa all set)")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        logger.info("oparl-bridge started. ALLRIS base: %s", settings.allris_base_url)
        if settings.wikidata_id:
            import asyncio

            from oparl_bridge.wikidata import warm_cache
            asyncio.ensure_future(warm_cache(settings.wikidata_id))
        yield

    _app = FastAPI(
        title="oparl-bridge",
        description=(
            "OParl 1.1-compatible API gateway for ALLRIS municipal information systems. "
            "Exposes data from Apache Wicket-based ALLRIS instances as standard OParl REST API."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    _app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    if enable_oparl:
        _app.include_router(oparl_router)

    if enable_md:
        _app.include_router(md_router)

    if enable_spa:
        _app.include_router(ui_router)

        @_app.get("/", include_in_schema=False)
        async def root():
            return FileResponse(_STATIC / "index.html")

    else:
        @_app.get("/", include_in_schema=False)
        async def landing():
            links: list[str] = []
            if enable_oparl:
                links.append('<li><a href="/oparl/v1.1/">OParl 1.1 API</a> &mdash; maschinenlesbare Ratsdaten</li>')
                links.append('<li><a href="/docs">OpenAPI-Dokumentation</a></li>')
            if enable_md:
                links.append('<li><a href="/md/">Markdown-Ansicht</a> &mdash; LLM- und Crawler-freundlich</li>')
                links.append('<li><a href="/llms.txt">llms.txt</a> &mdash; vollständiger Inhaltsindex</li>')
                links.append('<li><a href="/sitemap.xml">sitemap.xml</a></li>')
            items = "\n".join(links) if links else "<li>(keine Frontends aktiv)</li>"
            html = (
                "<!doctype html><html lang=de><meta charset=utf-8>"
                f"<title>{settings.system_name}</title>"
                "<style>body{{font-family:sans-serif;max-width:600px;margin:3rem auto;padding:0 1rem}}</style>"
                f"<h1>{settings.body_name}</h1>"
                f"<p>{settings.system_name}</p>"
                f"<ul>{items}</ul>"
            )
            return Response(content=html, media_type="text/html; charset=utf-8")

    @_app.get("/index.html", include_in_schema=False)
    async def index_html():
        return RedirectResponse("/", status_code=301)

    @_app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        if settings.favicon_b64 and settings.favicon_b64.startswith("data:"):
            header, _, b64data = settings.favicon_b64.partition(";base64,")
            mime = header.removeprefix("data:")
            import base64 as _b64
            content = _b64.b64decode(b64data)
            return Response(content=content, media_type=mime)
        ICO = (
            b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00"
            b"\x30\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00"
            b"\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00"
            b"\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\xff\xff\xff\x00\x00\x00"
        )
        return Response(content=ICO, media_type="image/x-icon")

    return _app


# Default instance — all frontends enabled; used by `uvicorn oparl_bridge.main:app`
app = create_app()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="oparl-bridge API server")
    parser.add_argument("--no-oparl", action="store_true", help="Disable OParl API frontend")
    parser.add_argument("--no-md", action="store_true", help="Disable Markdown/crawler frontend")
    parser.add_argument("--no-spa", action="store_true", help="Disable browser SPA frontend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    _app = create_app(
        enable_oparl=not args.no_oparl,
        enable_md=not args.no_md,
        enable_spa=not args.no_spa,
    )
    uvicorn.run(_app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
