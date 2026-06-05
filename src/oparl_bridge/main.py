"""oparl-bridge: OParl 1.1 gateway for ALLRIS municipal information systems."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from oparl_bridge.api.routes import router
from oparl_bridge.config import settings
from oparl_bridge.db.session import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("oparl-bridge started. ALLRIS base: %s", settings.allris_base_url)
    yield


app = FastAPI(
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "name": "oparl-bridge",
        "oparl_endpoint": f"{settings.api_base_url}/oparl/v1.1",
        "allris_instance": settings.allris_base_url,
    }


def main():
    uvicorn.run("oparl_bridge.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
