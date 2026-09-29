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
1. Choose the maintainer workspace directory (default: `~/maintainer_ws` unless the user specifies another path).
   Initialize it if needed:
   ```bash
   ros-maintainer-harness -w ~/maintainer_ws init
   ```
2. Run the environment doctor (via the MCP `check_environment` tool or CLI):
   ```bash
   ros-maintainer-harness -w ~/maintainer_ws doctor
   ```
3. **GitHub Token Prompt Requirement**:
   - If `ROS_CONTAINER_GITHUB_TOKEN` is unconfigured (`container_token_configured: False`), **STOP and ask the user**
     how they want to configure GitHub authentication before proceeding:
     - **Option A (Recommended)**: Provide a fine-grained GitHub Personal Access Token with **zero write permissions**
       (read-only access to public repositories) and save it via:
       `ros-maintainer-harness -w ~/maintainer_ws token-setup --container-token <TOKEN>`
     - **Option B**: Explicitly opt into unauthenticated container mode (subject to GitHub's 60 req/hr rate limit):
       `ros-maintainer-harness -w ~/maintainer_ws token-setup --no-token`
   - **CRITICAL SECURITY RULE**: **NEVER** silently read `gh auth token` from the host and pass it into a container,
     subagent environment, or `.env` file.
4. **MCP Server Registration**:
   - If `global_mcp_configured` is `False`, install the global MCP configuration so `ros-maintainer-harness` tools
     are registered:
     ```bash
     ros-maintainer-harness -w ~/maintainer_ws mcp-install --target all
     ```

### 2. Maintainer Hub & Spoke Coordinator Workflow
When acting as a root coordinator ("Maintainer Hub"):
- **"What is the status of things we're working on?"**:
  - Call the MCP tool `get_workspace_status()` (or `ros-maintainer-harness -w ~/maintainer_ws status`).
  - Present active sessions, their status, clickable `[<session_id>](conversation://<id>)` links, latest milestones,
    container state, CI runs, and pending approvals.
- **"What should I work on next?"**:
  - Call the MCP tool `get_next_actions()` (or `ros-maintainer-harness -w ~/maintainer_ws next`).
  - Surface pending approvals (`P1`), blocked sessions (`P1`), failed/completed CI runs (`P2`), active sessions (`P3`),
    and candidate open GitHub PRs (`P4`).
- **"I want to work on `<owner>/<repo>#<number>`" (Dedicated Task Conversation)**:
  - Call `start_session_conversation(pr_ref="<owner>/<repo>#<number>", mode="auto")` (or CLI
    `ros-maintainer-harness -w ~/maintainer_ws session start-conversation --pr <owner>/<repo>#<number>`) to scaffold
    the session and launch a dedicated top-level conversation via `agentapi new-conversation` (or use `mode="prompt_only"`
    with `invoke_subagent` and link its `conversationId` via `update_session_status`).

### 3. Scaffolding an Isolated Session Directly
Never clone target ROS repositories or run `colcon` inside the `ros_maintainer_agent_harness` repository directory.
Always create or scaffold an isolated session inside the maintainer workspace (`~/maintainer_ws/sessions/<id>`):
- **From a Pull Request**:
  - MCP tool: `scaffold_session_from_pr(pr_ref="<owner>/<repo>#<number>")`
  - CLI: `ros-maintainer-harness -w ~/maintainer_ws session from-pr <owner>/<repo>#<number>`
- **For a New Branch or Task**:
  - MCP tool: `create_session(session_id="<id>", distro="rolling", repo_path="...", branch="...")`
  - CLI: `ros-maintainer-harness -w ~/maintainer_ws session create <id> --distro rolling`

### 4. Mandatory Pre-Build Diff Security Review
Before compiling or running any tests on an external Pull Request:
1. Inspect the git diff in `~/maintainer_ws/sessions/<session_id>/src/<repo>` against the target base branch.
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
   - CLI: `ros-maintainer-harness -w ~/maintainer_ws session up <session_id>`
2. Execute all build, test, and diagnostic commands inside the session container:
   - MCP tool: `exec_in_session(session_id="<session_id>", command="colcon build --symlink-install")`
   - CLI:
     ```bash
     ros-maintainer-harness -w ~/maintainer_ws session exec <session_id> -- \
       "colcon build --symlink-install --cmake-args -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
     ros-maintainer-harness -w ~/maintainer_ws session exec <session_id> -- \
       "colcon test --event-handlers console_direct+ --return-code-on-test-failure"
     ros-maintainer-harness -w ~/maintainer_ws session exec <session_id> -- \
       "colcon test-result --all --verbose"
     ```
3. **Subagent Delegation Rule**:
   Whenever you spawn a subagent (e.g. via `invoke_subagent`) to investigate, build, or test a session, you **MUST**
   include in the subagent's prompt:
   - The exact `session_id` and `session_dir` (`~/maintainer_ws/sessions/<session_id>`).
   - Instructions to read `<session_dir>/AGENTS.md` and `<session_dir>/TASK.md` first.
   - Strict instructions to run all build/test commands inside the container via the MCP tool `exec_in_session`
     or `ros-maintainer-harness -w ~/maintainer_ws session exec <session_id> -- "<command>"`, **never** directly on
     the host OS.

### 6. MCP Gateway Tools & Progress Logging
- Record progress milestones to `<session_dir>/timeline.md` using the MCP `log_status` tool or `ros-session-status`,
  and update session state via `update_session_status`.
- Use the MCP gateway tools (`get_ci_status`, `get_ci_summary`, `launch_jenkins_ci`, `find_restarted_ci`,
  `git_push`, `create_pull_request`) for all CI queries and policy-guarded remote mutations.

---

## Part 2: Developing `ros_maintainer_agent_harness` Itself

When modifying the harness source code or documentation in this repository:
- **Code Style & Linting**: All Python files in `src/` and `tests/` must pass `flake8` with a 120-character max line
  length:
  ```bash
  flake8 src/ tests/ --max-line-length=120 --statistics
  ```
- **Unit Tests**: Run `pytest -v` to verify all unit tests pass.
- **Commits**: Include DCO sign-off (`git commit -s`) on all commits.
- **Documentation Tone**: Keep documentation direct, technical, and concise. Avoid em-dashes (`--` or unicode
  em-dash); use commas, colons, or parentheticals instead.
