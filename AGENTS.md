# Agent Instructions for `ros_maintainer_agent_harness`

This repository contains the `ros_maintainer_agent_harness` package and CLI (`ros-maintainer-harness`).
When a conversation is started in this workspace, determine whether the user is asking you to:
1. **Use the harness** to investigate, review, build, test, or run CI on a ROS 2 Pull Request or package, OR
2. **Develop / modify the harness** codebase itself.

Follow the mandatory rules below for the active task.
*(Note: If `ros-maintainer-harness` is not on your non-interactive shell `$PATH`, run
`export PATH="$HOME/.local/bin:$PATH"` or invoke `~/.local/bin/ros-maintainer-harness` directly).*

---

## Part 1: Using the Harness for ROS 2 Maintainer Tasks

Whenever the user asks you to review a PR, investigate an issue, build/test a ROS 2 package, or use the harness:

### 1. Mandatory Pre-Flight Environment & GitHub Token Check (Run First!)
Before scaffolding any session, starting any container, or spawning subagents:
1. Choose the maintainer workspace directory (default: `~/ros_maintenance_ws` unless the user specifies another path).
   Initialize it if needed:
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws init
   ```
2. Run the environment doctor (via the MCP `check_environment` tool or CLI):
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws doctor
   ```
3. **GitHub Token Prompt Requirement**:
   - If `ROS_CONTAINER_GITHUB_TOKEN` is unconfigured (`container_token_configured: False`), **STOP and ask the user**
     how they want to configure GitHub authentication before proceeding:
     - **Option A (Recommended)**: Provide a fine-grained GitHub Personal Access Token with **zero write permissions**
       (read-only access to public repositories) and save it via:
       `ros-maintainer-harness -w ~/ros_maintenance_ws token-setup --container-token <TOKEN>`
     - **Option B**: Explicitly opt into unauthenticated container mode (subject to GitHub's 60 req/hr rate limit):
       `ros-maintainer-harness -w ~/ros_maintenance_ws token-setup --no-token`
   - **CRITICAL SECURITY RULE**: **NEVER** silently read `gh auth token` from the host and pass it into a container,
     subagent environment, or `.env` file.
4. **MCP Server Registration**:
   - If `global_mcp_configured` is `False`, install the global MCP configuration so `ros-maintainer-harness` tools
     are registered:
     ```bash
     ros-maintainer-harness -w ~/ros_maintenance_ws mcp-install --target all
     ```

### 2. Maintainer Hub & Spoke Coordinator Workflow
When acting as a root coordinator ("Maintainer Hub"):
- **"What is the status of things we're working on?"**:
  - Call the MCP tool `get_workspace_status()` (or `ros-maintainer-harness -w ~/ros_maintenance_ws status`).
  - Present active sessions, their status, clickable `[<session_id>](conversation://<id>)` and
    `[<session_id>](file://<session_dir>)` links, latest milestones, container state, CI runs, and pending approvals.
- **"What should I work on next?"**:
  - Call the MCP tool `get_next_actions()` (or `ros-maintainer-harness -w ~/ros_maintenance_ws next`).
  - Surface pending approvals (`P1`), blocked sessions (`P1`), failed/completed CI runs (`P2`), active sessions (`P3`),
    and candidate open GitHub PRs (`P4`).
- **"I want to work on `<owner>/<repo>#<number>`" (Dedicated Task Conversation)**:
  - Call `start_session_conversation(pr_ref="<owner>/<repo>#<number>", mode="auto")` (or CLI
    `ros-maintainer-harness -w ~/ros_maintenance_ws session start-conversation --pr <owner>/<repo>#<number>`) to scaffold
    the session, start the session container, and launch a dedicated top-level conversation via `agentapi new-conversation`
    (or use `mode="prompt_only"` with `invoke_subagent` and link its `conversationId` via `update_session_status`).

### 3. Scaffolding an Isolated Session Directly
Never clone target ROS repositories or run `colcon` inside the `ros_maintainer_agent_harness` repository directory.
Always create or scaffold an isolated session inside the maintainer workspace (`~/ros_maintenance_ws/sessions/<id>`):
- **From a Pull Request**:
  - MCP tool: `scaffold_session_from_pr(pr_ref="<owner>/<repo>#<number>")`
  - CLI: `ros-maintainer-harness -w ~/ros_maintenance_ws session from-pr <owner>/<repo>#<number>`
- **For a New Branch or Task**:
  - MCP tool: `create_session(session_id="<id>", distro="rolling", repo_path="...", branch="...")`
  - CLI: `ros-maintainer-harness -w ~/ros_maintenance_ws session create <id> --distro rolling`

### 4. Mandatory Pre-Build Diff Security Review
Before compiling or running any tests on an external Pull Request:
1. Inspect the git diff in `~/ros_maintenance_ws/sessions/<session_id>/src/<repo>` against the target base branch.
2. Check for:
   - Accidentally committed secrets, tokens, or private keys.
   - Modifications to `.github/workflows/`, CI scripts, or Dockerfiles.
   - Suspicious commands in `CMakeLists.txt`, `setup.py`, `package.xml`, or test files (e.g. unexpected network
     calls, shell execution, or access to paths outside the workspace).
3. If anything suspicious is found, **halt immediately** and report the finding to the user before building.

### 5. Mandatory Containerized Execution (Host Agents & Subagents)
**NEVER** run `colcon build`, `colcon test`, `pytest` (for target ROS packages), or untrusted repository code
directly on the host OS.
1. Start the detached session sandbox container:
   - MCP tool: `start_session_container(session_id="<session_id>")`
   - CLI: `ros-maintainer-harness -w ~/ros_maintenance_ws session up <session_id>`
2. **Automatic `PreToolUse` Hook Routing**:
   - The harness installs `PreToolUse` hooks (`.agents/hooks.json`, `.claude/settings.json`, and
     `~/.gemini/config/hooks.json`) that automatically intercept shell commands (`run_command` / `Bash`) when `Cwd`
     is inside `~/ros_maintenance_ws/sessions/<session_id>` (or in a linked session conversation) and rewrite them to
     execute inside `ros-harness-<session_id>`.
   - Inside a session conversation, set `Cwd` to `~/ros_maintenance_ws/sessions/<session_id>` and run `colcon`, `git`,
     `gh`, and `pytest` commands directly without prefixing `session exec`.
   - From the Hub conversation (or in agent environments without `PreToolUse` hooks), use the MCP tool
     `exec_in_session(session_id="<session_id>", command="...")` or CLI
     `ros-maintainer-harness -w ~/ros_maintenance_ws session exec <session_id> -- "<command>"`.
3. **Subagent Delegation Rule**:
   Whenever you spawn a subagent (e.g. via `invoke_subagent`) to investigate, build, or test a session, you **MUST**
   include in the subagent's prompt:
   - The exact `session_id` and `session_dir` (`~/ros_maintenance_ws/sessions/<session_id>`).
   - Instructions to read `<session_dir>/AGENTS.md` and `<session_dir>/TASK.md` first.
   - Instructions to set `Cwd` to `<session_dir>` so the `PreToolUse` hook routes shell commands into the container
     automatically (or use `exec_in_session` if hooks are unavailable).

### 6. MCP Gateway Tools & Progress Logging
- Record progress milestones to `<session_dir>/timeline.md` using the MCP `log_status` tool or `ros-session-status`,
  and update session state via `update_session_status`.
- Use the MCP gateway tools (`get_ci_status`, `get_ci_summary`, `launch_jenkins_ci`, `find_restarted_ci`,
  `git_push`, `create_pull_request`, `edit_pull_request`, `push_release`, `run_bloom_release`) for all CI queries,
  policy-guarded remote mutations, and ROS package releases.
- By default, `create_pull_request` (and `ros-maintainer-harness create-pr`) generates a pre-filled GitHub compare URL
  (`--web-url` / `web_url=True`, status `WEB_URL_READY`, no approval ticket required) so the maintainer can click the
  link, inspect both the diff and the PR title/description in the browser, and click "Create pull request" themselves.
  Only pass `web_url=False` (or `--api`) if the user explicitly asks to submit the PR directly via the GitHub API.
- For releasing ROS packages, use the 3-step workflow:
  1. Run `catkin_generate_changelog` + edit/commit `CHANGELOG.rst` + `catkin_prepare_release --no-push` inside the
     session container.
  2. Push the release commit and version tag via `push_release` (or `ros-maintainer-harness release push`, ticket-gated).
  3. Run `bloom-release` on the host via `run_bloom_release` (or `ros-maintainer-harness release bloom`, ticket-gated).

---

## Part 2: Developing `ros_maintainer_agent_harness` Itself

When modifying the harness source code or documentation in this repository:
- **Code Style & Linting**: All Python files in `src/` and `tests/` must pass `flake8` with a 120-character max line
  length:
  ```bash
  flake8 src/ tests/ --max-line-length=120 --statistics
  ```
- **Unit Tests**: Run `pytest -v` to verify all unit tests pass (`pyproject.toml` sets `pythonpath = ["src"]` so
  `pytest` tests the local working tree directly).
- **Non-Editable Host Installation (Isolate Live Sessions from In-Progress Edits)**:
  - **NEVER** install this repository in editable mode (`pip install -e .`) when live maintainer sessions exist,
    because `PreToolUse` hooks invoke `~/.local/bin/ros-maintainer-harness` on every tool call.
  - Only install a non-editable snapshot into `~/.local` after `pytest` and `flake8` pass (or after merging to `main`):
    ```bash
    pip install --user --no-build-isolation --no-deps --force-reinstall .
    ```
- **Commits**: Include DCO sign-off (`git commit -s`) on all commits.
- **Documentation Tone**: Keep documentation direct, technical, and concise. Avoid em-dashes (`--` or unicode
  em-dash); use commas, colons, or parentheticals instead.

