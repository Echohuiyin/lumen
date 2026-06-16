"""FastAPI-powered MCP-style server orchestrating crash sessions."""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import AppConfig
from .crash_session import CrashCommandResult, CrashSessionError, CrashSessionManager


LOG = logging.getLogger(__name__)


class SessionCreateRequest(BaseModel):
    vmcore_path: Path = Field(..., description="Path to the vmcore dump")
    vmlinux_path: Path = Field(..., description="Path to the uncompressed vmlinux image")


class CommandRequest(BaseModel):
    command: str = Field(..., description="Crash command to execute")


class CommandResponse(BaseModel):
    command: str
    output: str
    success: bool

    @classmethod
    def from_result(cls, result: CrashCommandResult) -> "CommandResponse":
        return cls(command=result.command, output=result.output, success=result.success)


class SessionRegistry:
    """In-memory registry that keeps active crash sessions."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._sessions: Dict[str, CrashSessionManager] = {}

    def create_session(self, vmcore_path: Path, vmlinux_path: Path) -> str:
        session_id = str(uuid.uuid4())
        session = CrashSessionManager(
            vmcore_path=vmcore_path,
            vmlinux_path=vmlinux_path,
            config=self.config,
        )
        session.start()
        with self._lock:
            self._sessions[session_id] = session
        LOG.info("Started crash session %s", session_id)
        return session_id

    def get(self, session_id: str) -> CrashSessionManager:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session.stop()
            LOG.info("Closed crash session %s", session_id)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Instantiate and configure the MCP server."""

    config = config or AppConfig()
    registry = SessionRegistry(config)
    app = FastAPI(title="OC-AiCrash MCP Server", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/session")
    def _create_session(req: SessionCreateRequest) -> dict:
        try:
            session_id = registry.create_session(req.vmcore_path, req.vmlinux_path)
            return {"session_id": session_id}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CrashSessionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/session/{session_id}/command")
    def _run_command(session_id: str, req: CommandRequest) -> CommandResponse:
        try:
            session = registry.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

        try:
            result = session.run_command(req.command)
            return CommandResponse.from_result(result)
        except CrashSessionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/session/{session_id}/baseline")
    def _collect_baseline(session_id: str) -> dict:
        try:
            session = registry.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

        try:
            results = session.collect_baseline()
        except CrashSessionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "results": [CommandResponse.from_result(r).dict() for r in results]
        }

    @app.delete("/session/{session_id}")
    def _close_session(session_id: str) -> dict:
        registry.close(session_id)
        return {"status": "closed"}

    return app


__all__ = ["create_app"]
