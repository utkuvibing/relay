"""Authenticated loopback API and replayable ledger event stream."""

from __future__ import annotations

import asyncio
import hmac
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from relay.context import ConfigError
from relay.core.bus import MessageRejected
from relay.core.rooms import RoomError, RoomLookupError
from relay.core.state_machine import StateMachineError
from relay.harness.errors import UnsupportedCapability
from relay.server.operations import OperationError, RelayOperations
from relay.storage.models import MessageType


class TaskInput(BaseModel):
    title: str


class MessageInput(BaseModel):
    by: str
    recipient: str
    content: str
    room_id: str | None = None
    task_id: str | None = None
    type: MessageType = MessageType.NOTE


class DiscussionInput(BaseModel):
    topic: str
    protocol: str | None = None


class ApprovalInput(BaseModel):
    by: str


def create_app(root: Path, token: str | None = None) -> FastAPI:
    """Build the API for exactly one initialized workspace."""
    secret = token if token is not None else os.environ.get("RELAY_SERVER_TOKEN", "")
    if not secret:
        raise OperationError("RELAY_SERVER_TOKEN must be set")
    operations = RelayOperations(root)
    app = FastAPI(title="Relay", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter(prefix="/v1")

    @app.middleware("http")
    async def authenticate_http(request: Request, call_next: RequestResponseEndpoint) -> Response:
        authorization = request.headers.get("authorization")
        if authorization is None or not hmac.compare_digest(authorization, f"Bearer {secret}"):
            return JSONResponse(
                status_code=401,
                content={"detail": {"code": "unauthorized", "message": "invalid token"}},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    def call(action: str, **kwargs: Any) -> Any:
        try:
            method = getattr(operations, action)
            return method(**kwargs)
        except OperationError as exc:
            raise HTTPException(
                status_code=404 if exc.code == "not_found" else 409,
                detail={"code": exc.code, "message": str(exc)},
            ) from None
        except RoomLookupError as exc:
            raise HTTPException(
                status_code=404, detail={"code": "not_found", "message": str(exc)}
            ) from None
        except UnsupportedCapability as exc:
            raise HTTPException(
                status_code=409, detail={"code": "unavailable_capability", "message": str(exc)}
            ) from None
        except (ConfigError, MessageRejected, RoomError, StateMachineError) as exc:
            raise HTTPException(
                status_code=409, detail={"code": "policy_refused", "message": str(exc)}
            ) from None
        except (ValueError, sqlite3.IntegrityError):
            raise HTTPException(
                status_code=409, detail={"code": "refused", "message": "operation refused"}
            ) from None

    @router.post("/tasks", status_code=201)
    def create_task(body: TaskInput) -> dict[str, Any]:
        return call("create_task", title=body.title)

    @router.post("/messages", status_code=201)
    def send_message(body: MessageInput) -> dict[str, Any]:
        return call(
            "send_message",
            by=body.by,
            recipient=body.recipient,
            content=body.content,
            room_id=body.room_id,
            task_id=body.task_id,
            message_type=body.type,
        )

    @router.post("/discussions")
    async def start_discussion(body: DiscussionInput) -> dict[str, Any]:
        try:
            return await operations.start_discussion(body.topic, body.protocol)
        except OperationError as exc:
            raise HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc)}
            ) from None
        except UnsupportedCapability as exc:
            raise HTTPException(
                status_code=409, detail={"code": "unavailable_capability", "message": str(exc)}
            ) from None
        except (ConfigError, RoomError, ValueError):
            raise HTTPException(
                status_code=409, detail={"code": "refused", "message": "discussion refused"}
            ) from None

    @router.get("/rooms/{selector}")
    def room(selector: str) -> dict[str, Any]:
        return call("room", selector=selector)

    @router.get("/rooms/{selector}/graph")
    def room_graph(selector: str) -> dict[str, Any]:
        return call("room_graph", selector=selector)

    @router.post("/tasks/{task_id}/approve")
    def approve(task_id: str, body: ApprovalInput) -> dict[str, Any]:
        return call("approve", task_id=task_id, by=body.by)

    @router.get("/status")
    def status() -> dict[str, Any]:
        return call("status")

    @router.get("/agents")
    def agents() -> list[dict[str, Any]]:
        return call("agents")

    @router.get("/events")
    def events(last_sequence: int = Query(default=0, ge=0)) -> list[dict[str, Any]]:
        return call("events_after", last_sequence=last_sequence)

    app.include_router(router)

    @app.websocket("/v1/events/ws")
    async def event_stream(websocket: WebSocket) -> None:
        authorization = websocket.headers.get("authorization")
        if authorization is None or not hmac.compare_digest(authorization, f"Bearer {secret}"):
            await websocket.close(code=1008)
            return
        try:
            last_sequence = int(websocket.query_params.get("last_sequence", "0"))
        except ValueError:
            await websocket.close(code=1008)
            return
        if last_sequence < 0:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                for event in operations.events_after(last_sequence):
                    await websocket.send_json(event)
                    sequence = event["sequence"]
                    if isinstance(sequence, int):
                        last_sequence = sequence
                try:
                    incoming = await asyncio.wait_for(websocket.receive(), timeout=0.25)
                except TimeoutError:
                    continue
                if incoming["type"] == "websocket.disconnect":
                    return
                await websocket.close(code=1003)
                return
        except WebSocketDisconnect:
            return

    return app
