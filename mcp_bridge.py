"""Expose the local LangGraph coding service as Codex MCP tools."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

API = "http://127.0.0.1:8765"
JEV_API = "http://127.0.0.1:8876"
mcp = FastMCP("jev-codex-langgraph")


def task_path(task_id: str) -> str:
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise ValueError("task_id must be the 32-character ID returned by start_routed_coding_run")
    return "/tasks/" + task_id


def call_api(path: str, payload: dict | None = None, base: str = API) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(base + path, data=data,
                      headers={"Content-Type": "application/json"} if data else {},
                      method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(2000).decode(errors="replace")
        raise ValueError(f"LangGraph API returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Local decision API unavailable at {base}") from exc


@mcp.tool()
def classify_coding_task(task: str, context: str = "") -> dict:
    """Classify a coding request with Jev Lite before any edits. simple => handle directly in Codex; complex => start_routed_coding_run. If routing is unavailable, do not silently guess."""
    return call_api("/api/task-complexity", {"task": task, "context": context}, base=JEV_API)


@mcp.tool()
def start_routed_coding_run(repo: str, request: str) -> dict:
    """After classify_coding_task returns complex, start an isolated LangGraph run. Returns a run ID; planning then pauses for approval. Poll get_routed_coding_run before approving."""
    return call_api("/tasks", {"repo": repo, "request": request})


@mcp.tool()
def get_routed_coding_run(task_id: str) -> dict:
    """Read a run's status, planned tasks, role decisions, and result. Await status awaiting_approval before requesting user approval."""
    return call_api(task_path(task_id))


@mcp.tool()
def approve_routed_coding_run(task_id: str, approved: bool) -> dict:
    """Resume or cancel a planned coding run. Set approved=true only after the user has reviewed the returned plan and explicitly approved implementation."""
    return call_api(task_path(task_id) + "/approval", {"approved": approved})


if __name__ == "__main__":
    mcp.run(transport="stdio")
