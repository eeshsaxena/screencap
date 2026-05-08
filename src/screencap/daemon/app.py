"""Minimal ASGI app for the ScreenCap daemon."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.applications import Starlette


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    yield


def build_app() -> Starlette:
    return Starlette(routes=[], lifespan=lifespan)
