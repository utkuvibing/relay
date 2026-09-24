"""Opt-in REST transport for Phase 9 CLI operations."""

from __future__ import annotations

import os
from typing import Any

import httpx
import typer


def server_url() -> str | None:
    return os.environ.get("RELAY_SERVER_URL") or None


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = server_url()
    if url is None:
        raise RuntimeError("server mode is not enabled")
    token = os.environ.get("RELAY_SERVER_TOKEN")
    if not token:
        typer.echo("ERROR RELAY_SERVER_TOKEN must be set")
        raise typer.Exit(1)
    try:
        response = httpx.request(
            method,
            url.rstrip("/") + "/v1" + path,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=3600,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                detail = exc.response.json().get("detail", {})
                message = detail.get("message", str(exc.response.status_code))
            except (ValueError, AttributeError):
                message = str(exc.response.status_code)
        else:
            message = "server request failed"
        typer.echo(f"ERROR {message}")
        raise typer.Exit(1) from None
