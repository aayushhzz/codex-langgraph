import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

import agent_service
from agent_service import AGENT_GUIDANCE, TaskRequest, TaskService, build_graph, json_object


def fake_router(task, context):
    role = "debugger" if task["title"] == "one" else "test_writer"
    return {"agent": role, "assignment": AGENT_GUIDANCE[role]}


class FakeCodex:
    def __init__(self):
        self.calls = []

    def run(self, role, prompt, cwd, writable=False):
        self.calls.append((role, writable, str(cwd)))
        if role == "planner":
            return '{"tasks":[{"title":"one","instructions":"first"},' \
                   '{"title":"two","instructions":"second"}]}'
        return f"{role} done"


def test_graph_runs_codex_roles_and_each_small_task(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    fake = FakeCodex()
    graph = build_graph(fake, fake_router)
    config = {"configurable": {"thread_id": "two-tasks"}}
    graph.invoke({"request": "Implement two changes", "repo": str(tmp_path),
                  "worktree": str(tmp_path)}, config=config)
    assert [call[0] for call in fake.calls] == ["orchestrator", "planner", "workflow"]
    assert graph.get_state(config).next == ("approval",)
    result = graph.invoke(Command(resume=True), config=config)
    assert [call[:2] for call in fake.calls] == [
        ("orchestrator", False), ("planner", False), ("workflow", False),
        ("debugger", True), ("test_writer", True), ("orchestrator", False)]
    assert [task["title"] for task in result["results"]] == ["one", "two"]
    assert [task["agent"] for task in result["results"]] == ["debugger", "test_writer"]
    assert all(task["assignment"] == AGENT_GUIDANCE[task["agent"]] for task in result["results"])
    assert result["final"] == "orchestrator done"


def test_service_creates_isolated_worktree_and_tracks_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
    monkeypatch.setenv("CODEX_TASK_ROOT", str(tmp_path / "tasks"))
    service = TaskService(FakeCodex(), fake_router)
    task_id = service.create(TaskRequest(repo=str(repo), request="Make changes"))
    assert service.get(task_id)["worktree"] != str(repo)
    service.run(task_id, "Make changes")
    assert service.get(task_id)["status"] == "awaiting_approval"
    assert service.get(task_id)["events"][-1] == {"approval_required": True}
    service.approve(task_id, True)
    assert service.get(task_id)["status"] == "completed"
    assert [result["agent"] for result in service.graph.get_state({"configurable": {"thread_id": task_id}}).values["results"]] == ["debugger", "test_writer"]


def test_completed_run_publishes_code_and_local_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
    monkeypatch.setenv("CODEX_TASK_ROOT", str(tmp_path / "tasks"))

    class WritingCodex(FakeCodex):
        def run(self, role, prompt, cwd, writable=False):
            if writable:
                (cwd / "implemented.txt").write_text("implemented")
            return super().run(role, prompt, cwd, writable)

    service = TaskService(WritingCodex(), fake_router)
    task_id = service.create(TaskRequest(repo=str(repo), request="Implement code"))
    service.run(task_id, "Implement code")
    assert not (repo / "implemented.txt").exists()
    service.approve(task_id, True)
    record = service.get(task_id)
    assert record["status"] == "completed"
    assert (repo / "implemented.txt").read_text() == "implemented"
    assert record["commit"] == subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                               capture_output=True, text=True, check=True).stdout.strip()
    assert not subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout


def test_changed_desktop_repo_preserves_isolated_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
    monkeypatch.setenv("CODEX_TASK_ROOT", str(tmp_path / "tasks"))
    service = TaskService(FakeCodex(), fake_router)
    task_id = service.create(TaskRequest(repo=str(repo), request="Implement code"))
    service.run(task_id, "Implement code")
    (Path(service.get(task_id)["worktree"]) / "implemented.txt").write_text("result")
    (repo / "local.txt").write_text("local edit")
    service.approve(task_id, True)
    assert service.get(task_id)["status"] == "failed"
    assert "Desktop repository changed" in service.get(task_id)["error"]
    assert not (repo / "implemented.txt").exists()
    assert (Path(service.get(task_id)["worktree"]) / "implemented.txt").exists()


def test_rejection_runs_no_coding_agent(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    fake = FakeCodex()
    graph = build_graph(fake, fake_router)
    config = {"configurable": {"thread_id": "rejected"}}
    graph.invoke({"request": "Implement two changes", "worktree": str(tmp_path)}, config=config)
    result = graph.invoke(Command(resume=False), config=config)
    assert result["final"] == "Cancelled before coding agents were run."
    assert [call[0] for call in fake.calls] == ["orchestrator", "planner", "workflow"]


def test_http_run_waits_for_approval_and_rejects_duplicate(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
    monkeypatch.setenv("CODEX_TASK_ROOT", str(tmp_path / "tasks"))
    fake = FakeCodex()
    monkeypatch.setattr(agent_service, "service", TaskService(fake, fake_router))
    with TestClient(agent_service.app) as client:
        created = client.post("/tasks", json={"repo": str(repo), "request": "Make changes"})
        assert created.status_code == 202
        task_id = created.json()["id"]
        for _ in range(100):
            record = client.get(f"/tasks/{task_id}").json()
            if record["status"] == "awaiting_approval":
                break
            time.sleep(.01)
        assert record["status"] == "awaiting_approval"
        assert len(record["plan"]["tasks"]) == 2
        assert all(role not in ("debugger", "test_writer") for role, _, _ in fake.calls)
        rejected = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
        assert rejected.status_code == 202
        duplicate = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
        assert duplicate.status_code == 409
        for _ in range(100):
            record = client.get(f"/tasks/{task_id}").json()
            if record["status"] == "cancelled":
                break
            time.sleep(.01)
        assert record["status"] == "cancelled"
        assert all(role not in ("debugger", "test_writer") for role, _, _ in fake.calls)


def test_service_rejects_dirty_repo_before_creating_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
    (repo / "unsaved.txt").write_text("important")
    monkeypatch.setenv("CODEX_TASK_ROOT", str(tmp_path / "tasks"))
    with pytest.raises(ValueError, match="repo must be clean"):
        TaskService(FakeCodex(), fake_router).create(
            TaskRequest(repo=str(repo), request="Make changes"))
    assert not (tmp_path / "tasks").exists()


def test_planner_rejects_malformed_tasks(tmp_path):
    class BadPlanner(FakeCodex):
        def run(self, role, prompt, cwd, writable=False):
            return '{"tasks":[]}' if role == "planner" else super().run(role, prompt, cwd, writable)

    with pytest.raises(ValueError, match="1-8 tasks"):
        build_graph(BadPlanner(), fake_router).invoke(
            {"request": "change", "worktree": str(tmp_path)},
            config={"configurable": {"thread_id": "bad-plan"}})


def test_invalid_route_never_spawns_worker(tmp_path):
    fake = FakeCodex()
    graph = build_graph(fake, lambda task, context: {"agent": "unapproved", "assignment": "anything"})
    config = {"configurable": {"thread_id": "invalid-route"}}
    graph.invoke({"request": "change", "worktree": str(tmp_path)}, config=config)
    with pytest.raises(ValueError, match="Invalid coding-agent route"):
        graph.invoke(Command(resume=True), config=config)
    assert all(role not in ("worker", "unapproved") for role, _, _ in fake.calls)


def test_researcher_is_read_only(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    fake = FakeCodex()
    route = lambda task, context: {"agent": "researcher", "assignment": AGENT_GUIDANCE["researcher"]}
    graph = build_graph(fake, route)
    config = {"configurable": {"thread_id": "research"}}
    graph.invoke({"request": "Investigate an issue and report a plan",
                  "worktree": str(tmp_path)}, config=config)
    result = graph.invoke(Command(resume=True), config=config)
    assert ("researcher", False, str(tmp_path)) in fake.calls
    assert result["results"][0]["agent"] == "researcher"


def test_json_object_accepts_code_fence():
    assert json_object('```json\n{"tasks": []}\n```') == {"tasks": []}
