"""Local LangGraph coordinator whose agents are Codex CLI processes."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Protocol, TypedDict
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

CODING_AGENTS = ("implementer", "debugger", "test_writer", "refactorer", "researcher")
AGENT_GUIDANCE = {
    "implementer": "Implement the specified behavior with a focused patch and run relevant checks.",
    "debugger": "Find the root cause before changing code; fix it and verify the failing case.",
    "test_writer": "Add or improve behavioral tests for the assigned task and run them.",
    "refactorer": "Improve the existing code while preserving behavior and run focused checks.",
    "researcher": "Inspect and report findings or a plan without changing files.",
}


def route_coding_task(task: dict[str, str], context: str) -> dict[str, str]:
    """Ask the local decision service to choose a bounded coding role."""
    base = os.environ.get("JEV_LITE_URL", "http://127.0.0.1:8876").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("JEV_LITE_URL must be an HTTP loopback origin.")
    payload = json.dumps({"task": task["title"] + "\n" + task["instructions"],
                          "context": context[:8000], "agents": list(CODING_AGENTS)}).encode()
    try:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=90)
        try:
            connection.request("POST", "/api/coding-route", payload,
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"router returned HTTP {response.status}")
            decision = json.loads(response.read())
        finally:
            connection.close()
    except Exception as exc:
        raise RuntimeError("Jev Lite routing failed; start its local server and retry the run.") from exc
    if not isinstance(decision, dict) or decision.get("agent") not in CODING_AGENTS or not isinstance(decision.get("assignment"), str):
        raise ValueError("Jev Lite returned an invalid coding-agent decision.")
    if decision["assignment"] != AGENT_GUIDANCE[decision["agent"]]:
        raise ValueError("Jev Lite returned unexpected specialist guidance.")
    return {"agent": decision["agent"], "assignment": AGENT_GUIDANCE[decision["agent"]]}


class AgentRunner(Protocol):
    def run(self, role: str, prompt: str, cwd: Path, writable: bool = False) -> str: ...


class CodexRunner:
    """Run one real Codex agent per graph node, with per-role model selection."""

    def __init__(self) -> None:
        self.binary = os.environ.get("CODEX_BIN", "codex")
        self.codex_home = os.environ.get("CODEX_AGENT_HOME", str(Path(__file__).parent / "codex-home"))
        self.timeout = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "900"))
        self.models = {
            role: os.environ.get(f"CODEX_MODEL_{role.upper()}")
            for role in ("orchestrator", "planner", "workflow", "worker", *CODING_AGENTS)
        }

    def run(self, role: str, prompt: str, cwd: Path, writable: bool = False) -> str:
        command = [self.binary, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox",
                   "workspace-write" if writable else "read-only"]
        model = self.models[role] or (self.models["worker"] if role in CODING_AGENTS else None)
        if model:
            command.extend(["--config", f"model={json.dumps(model)}"])
        if role in (*CODING_AGENTS, "worker"):
            command.extend(["--config", 'model_reasoning_effort="low"'])
        with tempfile.TemporaryDirectory(prefix="codex-result-") as directory:
            output = Path(directory) / "answer.txt"
            command.extend(["--output-last-message", str(output), "-"])
            try:
                result = subprocess.run(command, input=prompt, text=True, cwd=cwd,
                                        env={**os.environ, "CODEX_HOME": self.codex_home},
                                        capture_output=True, timeout=self.timeout, check=False)
            except FileNotFoundError as exc:
                raise RuntimeError(f"Codex CLI not found: {self.binary}; set CODEX_BIN") from exc
            if result.returncode:
                raise RuntimeError(f"{role} Codex failed (exit {result.returncode}): "
                                   f"{result.stderr[-1500:]}")
            return output.read_text().strip() if output.exists() else result.stdout.strip()


def json_object(answer: str) -> dict[str, Any]:
    """Accept a JSON object, optionally wrapped in a Markdown code fence."""
    answer = answer.strip()
    answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer)
    value = json.loads(answer)
    if not isinstance(value, dict):
        raise ValueError("Agent must return a JSON object")
    return value


class GraphState(TypedDict, total=False):
    request: str
    repo: str
    worktree: str
    scope: str
    tasks: list[dict[str, str]]
    index: int
    results: list[dict[str, str]]
    workflow: str
    final: str
    approved: bool


def build_graph(runner: AgentRunner, router=route_coding_task):
    def orchestrator(state: GraphState) -> dict[str, Any]:
        scope = runner.run("orchestrator", "You are the Codex orchestrator. Scope this request "
            "for a planner and workflow agent. Do not change files. Give concise constraints "
            "and acceptance criteria.\nRequest: " + state["request"], Path(state["worktree"]))
        return {"scope": scope, "results": [], "index": 0}

    def planner(state: GraphState) -> dict[str, Any]:
        answer = runner.run("planner", "You are the Codex planner. Inspect the repository, "
            "do not edit. Split the request into 1-8 small, sequentially executable coding "
            "tasks. Reply ONLY JSON: {\"tasks\":[{\"title\":\"...\",\"instructions\":\"...\"}]}. "
            "Tasks must be concrete and together satisfy the request.\nRequest: "
            + state["request"] + "\nOrchestrator scope: " + state["scope"],
            Path(state["worktree"]))
        tasks = json_object(answer).get("tasks")
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 8 or any(
            not isinstance(task, dict) or not isinstance(task.get("title"), str)
            or not isinstance(task.get("instructions"), str) for task in tasks
        ):
            raise ValueError("Planner must return 1-8 tasks with title and instructions")
        return {"tasks": tasks}

    def workflow(state: GraphState) -> dict[str, Any]:
        answer = runner.run("workflow", "You are the Codex workflow agent. Inspect the "
            "repository and the planner's task list without editing. Identify ordering, "
            "dependencies, and useful tests for the lightweight workers. Keep it concise.\n"
            + json.dumps(state["tasks"]), Path(state["worktree"]))
        return {"workflow": answer}

    def approval(state: GraphState) -> dict[str, Any]:
        decision = interrupt({"scope": state["scope"], "tasks": state["tasks"],
                              "workflow": state["workflow"]})
        return {"approved": decision is True}

    def route_approval(state: GraphState) -> str:
        return "worker" if state.get("approved") else "cancelled"

    def cancelled(state: GraphState) -> dict[str, Any]:
        return {"final": "Cancelled before coding agents were run."}

    def worker(state: GraphState) -> dict[str, Any]:
        task = state["tasks"][state["index"]]
        previous = state["results"]
        decision = router(task, "Overall request: " + state["request"] +
                          "\nWorkflow: " + state["workflow"] +
                          "\nPrior reports: " + json.dumps(previous))
        if decision.get("agent") not in CODING_AGENTS or not isinstance(decision.get("assignment"), str):
            raise ValueError("Invalid coding-agent route")
        role = decision["agent"]
        writable = role != "researcher"
        answer = runner.run(role, "You are a lightweight Codex coding agent. Complete "
            "only the assigned task in this isolated worktree. Preserve existing edits from "
            "earlier workers. If assigned researcher, do not change files or run mutating commands. "
            "Run relevant focused checks when appropriate. Report changes and checks.\n"
            "Specialist focus: " + decision["assignment"] + "\n"
            "Overall request: " + state["request"] + "\nWorkflow: " + state["workflow"]
            + "\nPrevious results: " + json.dumps(previous)
            + "\nYour task: " + json.dumps(task), Path(state["worktree"]), writable=writable)
        return {"results": previous + [{"title": task["title"], "agent": role,
                                         "assignment": decision["assignment"], "report": answer}],
                "index": state["index"] + 1}

    def finish(state: GraphState) -> dict[str, Any]:
        diff = subprocess.run(["git", "status", "--short"], cwd=state["worktree"],
                              text=True, capture_output=True, check=True).stdout
        answer = runner.run("orchestrator", "You are the Codex orchestrator. Review completion "
            "against the original request. Do not edit. Summarize what was done, validation, "
            "and gaps. The changes stay in the isolated worktree for user review.\nRequest: "
            + state["request"] + "\nWorker reports: " + json.dumps(state["results"])
            + "\nGit status: " + diff, Path(state["worktree"]))
        return {"final": answer}

    graph = StateGraph(GraphState)
    for name, fn in (("orchestrator", orchestrator), ("planner", planner),
                     ("workflow", workflow), ("approval", approval),
                     ("cancelled", cancelled), ("worker", worker), ("finish", finish)):
        graph.add_node(name, fn)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", "planner")
    graph.add_edge("planner", "workflow")
    graph.add_edge("workflow", "approval")
    graph.add_conditional_edges("approval", route_approval,
                                {"worker": "worker", "cancelled": "cancelled"})
    graph.add_edge("cancelled", END)
    graph.add_conditional_edges("worker", lambda state: "worker" if
                                state["index"] < len(state["tasks"]) else "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=InMemorySaver())


class TaskRequest(BaseModel):
    repo: str
    request: str = Field(min_length=3)


class ApprovalRequest(BaseModel):
    approved: bool


class TaskService:
    def __init__(self, runner: AgentRunner | None = None, router=route_coding_task):
        self.graph = build_graph(runner or CodexRunner(), router)
        self.records: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.root = Path(os.environ.get("CODEX_TASK_ROOT", tempfile.gettempdir())) / "codex-langgraph"

    def create(self, payload: TaskRequest) -> str:
        repo = Path(payload.repo).expanduser().resolve(strict=True)
        if not repo.is_dir():
            raise ValueError("repo must be a directory")
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                              text=True, capture_output=True, check=True).stdout.strip()
        if Path(root).resolve() != repo:
            raise ValueError("repo must be the Git repository root")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                text=True, capture_output=True, check=True).stdout
        if status.strip():
            raise ValueError("repo must be clean; uncommitted changes are not copied to the isolated worktree")
        task_id = uuid.uuid4().hex
        worktree = self.root / task_id
        self.root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
                       cwd=repo, capture_output=True, text=True, check=True)
        base_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                     capture_output=True, text=True, check=True).stdout.strip()
        with self.lock:
            self.records[task_id] = {"id": task_id, "status": "running", "repo": str(repo),
                                     "worktree": str(worktree), "base_commit": base_commit, "events": []}
        return task_id

    def publish(self, record: dict[str, Any]) -> str | None:
        """Commit the isolated result and fast-forward the original, still-clean checkout."""
        repo, worktree = Path(record["repo"]), Path(record["worktree"])
        with self.publish_lock:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                  capture_output=True, text=True, check=True).stdout.strip()
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    capture_output=True, text=True, check=True).stdout
            if head != record["base_commit"] or status.strip():
                raise RuntimeError("Desktop repository changed during the run; isolated worktree preserved for review")
            subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
            staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=worktree)
            if staged.returncode == 0:
                return None
            if staged.returncode != 1:
                raise RuntimeError("Could not inspect staged worktree changes")
            subprocess.run(["git", "commit", "-m", "Apply LangGraph coding run " + record["id"]],
                           cwd=worktree, check=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                                    capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "merge", "--ff-only", commit], cwd=repo, check=True)
            return commit

    def run(self, task_id: str, request: str) -> None:
        record = self.records[task_id]
        try:
            initial: GraphState = {"request": request, "repo": record["repo"],
                                   "worktree": record["worktree"]}
            for event in self.graph.stream(initial, config={"configurable": {"thread_id": task_id},
                                                            "recursion_limit": 32}):
                with self.lock:
                    record["events"].append({"approval_required": True} if "__interrupt__" in event else event)
            snapshot = self.graph.get_state({"configurable": {"thread_id": task_id}})
            with self.lock:
                record["status"] = "awaiting_approval"
                record["plan"] = {key: snapshot.values[key] for key in ("scope", "tasks", "workflow")}
        except Exception as exc:
            with self.lock:
                record["status"] = "failed"
                record["error"] = str(exc)

    def begin_approval(self, task_id: str, approved: bool) -> None:
        with self.lock:
            record = self.records.get(task_id)
            if record is None:
                raise ValueError("Task not found")
            if record["status"] != "awaiting_approval":
                raise ValueError("Task is not awaiting approval")
            record["status"] = "running" if approved else "cancelling"

    def resume(self, task_id: str, approved: bool) -> None:
        record = self.records[task_id]
        try:
            for event in self.graph.stream(Command(resume=approved),
                                           config={"configurable": {"thread_id": task_id},
                                                   "recursion_limit": 32}):
                with self.lock:
                    record["events"].append(event)
            commit = self.publish(record) if approved else None
            with self.lock:
                record["status"] = "completed" if approved else "cancelled"
                record["result"] = record["events"][-1]["finish" if approved else "cancelled"]["final"]
                if approved:
                    record["commit"] = commit
        except Exception as exc:
            with self.lock:
                record["status"] = "failed"
                record["error"] = str(exc)

    def approve(self, task_id: str, approved: bool) -> None:
        self.begin_approval(task_id, approved)
        self.resume(task_id, approved)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.records.get(task_id)
            return json.loads(json.dumps(record)) if record else None


app = FastAPI(title="Codex LangGraph Orchestrator")
service = TaskService()


@app.post("/tasks", status_code=202)
async def create_task(payload: TaskRequest):
    try:
        task_id = await asyncio.to_thread(service.create, payload)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    asyncio.create_task(asyncio.to_thread(service.run, task_id, payload.request))
    return {"id": task_id, "status": "running"}


@app.post("/tasks/{task_id}/approval", status_code=202)
async def approve_task(task_id: str, payload: ApprovalRequest):
    try:
        service.begin_approval(task_id, payload.approved)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    asyncio.create_task(asyncio.to_thread(service.resume, task_id, payload.approved))
    return service.get(task_id)


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    record = service.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return record
