# Codex agents orchestrated by LangGraph

This local API runs **real Codex CLI processes** in LangGraph nodes: an orchestrator scopes and reports, a planner splits a change into up to eight small tasks, a workflow agent sets execution guidance, and a specialist Codex worker executes each task sequentially. Planning pauses for approval before any coding worker runs. Jev Lite chooses a bounded role for each approved task: `implementer`, `debugger`, `test_writer`, `refactorer`, or read-only `researcher`. The planner's concrete task and a fixed role-specific focus become the worker assignment. The graph records each choice and passes prior worker reports to the next worker. Approved changes are made in a detached, isolated Git worktree, then committed and fast-forwarded into the original local repository when the run finishes. Nothing is pushed.

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export CODEX_BIN=/absolute/path/to/codex # omit if codex is on PATH
export CODEX_MODEL_ORCHESTRATOR=gpt-6-sol
export CODEX_MODEL_PLANNER=gpt-6-sol
export CODEX_MODEL_WORKFLOW=gpt-6-sol
export CODEX_MODEL_WORKER=gpt-6-luna
.venv/bin/uvicorn agent_service:app --host 127.0.0.1 --port 8765
```

Start Jev Lite first at `http://127.0.0.1:8876` (`python3 server.py` in its directory). The graph calls its `/api/coding-route` endpoint before every coding worker and fails the run if routing is unavailable or invalid; it never silently guesses a role. `JEV_LITE_URL` can point to another **HTTP loopback origin**. Restart this API after updating its source so the running process loads the router. The specialist roles use `CODEX_MODEL_WORKER` by default; `CODEX_MODEL_IMPLEMENTER`, `CODEX_MODEL_DEBUGGER`, `CODEX_MODEL_TEST_WRITER`, `CODEX_MODEL_REFACTORER`, and `CODEX_MODEL_RESEARCHER` can override each one. Writing roles work in the isolated worktree; `researcher` runs in a read-only sandbox.

The included `codex-home/config.toml` is a minimal Codex configuration that stores no key; uncomment its provider block to use your own gateway. This minimal configuration keeps unrelated personal Codex hooks and MCP servers out of every worker. Set `CODEX_AGENT_HOME` to another Codex configuration directory if desired. Ensure `OPENAI_API_KEY` is available to the service process; never put the key in requests or these files. `CODEX_TASK_ROOT` selects where isolated worktrees go (default: system temporary directory). `CODEX_TIMEOUT_SECONDS` sets the per-agent timeout (default: 900).

Codex has a separate `jev_codex_langgraph` MCP registration for `mcp_bridge.py`. Call `classify_coding_task` first: Jev Lite returns `simple` / `codex` for narrow, clear tasks and `complex` / `langgraph` for multi-step, broad, or uncertain tasks. Simple tasks stay in the current Codex chat. Complex tasks use `start_routed_coding_run`, `get_routed_coding_run`, and `approve_routed_coding_run` against this loopback API. The older `codex_langgraph` MCP remains untouched. Restart Codex if the new tools are not visible. The global Codex agent instructions prefer this classification route; those instructions are a default workflow, not a filesystem permission boundary. If Jev Lite is unavailable, the tools fail instead of guessing a route.

```sh
curl -sS http://127.0.0.1:8765/tasks -H 'Content-Type: application/json' \
  -d '{"repo":"/absolute/path/to/git/repo","request":"Implement the requested change"}'
curl -sS http://127.0.0.1:8765/tasks/TASK_ID
curl -sS http://127.0.0.1:8765/tasks/TASK_ID/approval -H 'Content-Type: application/json' \
  -d '{"approved":true}'
```

Poll the task until `status` is `awaiting_approval`; review its `plan` before approving. The approval call returns immediately while workers run in the background. `approved:false` cancels without a coding worker. A completed record contains status, graph events (including each selected agent and assignment), the worktree path, the final report, and the local Git `commit` (null when no files changed). The repository must remain clean and at its starting commit while agents work; if it changes, publication fails and the isolated worktree remains available for manual review. A completed run does not push to a remote. Task state and checkpoints live in memory and are lost when the server restarts, but worktrees remain available. Run locally only: this API has no authentication. Workers execute sequentially to avoid conflicting edits in a shared worktree; each is a fresh, low-reasoning Codex process, with prior reports forwarded through LangGraph state. The graph caps tasks at eight. Git worktrees require a clean repository with a commit; uncommitted changes are not copied. Jev Lite uses a local conventional language model; its routing judgment is not guaranteed correct or calibrated.

To test: `python3 -m pytest tests` (install `pytest` separately).
