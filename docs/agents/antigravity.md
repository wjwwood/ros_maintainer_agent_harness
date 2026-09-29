# Using Antigravity & Gemini with the Harness

Antigravity and Gemini CLI provide agentic workflows that integrate natively with the Model Context Protocol (MCP) and automatically discover workspace rule files (`GEMINI.md` and `AGENTS.md`).

## Prerequisites

- Antigravity IDE or Gemini CLI installed.
- Docker or Podman installed on the host.
- A read-only fine-grained GitHub Personal Access Token for container sandboxes (or explicit opt-in to unauthenticated container mode; see [GitHub Token Setup](../github_tokens.md)).

## One-Time Workspace Setup

Before starting a conversation in Antigravity or Gemini, initialize your maintainer workspace, configure your container token, and register the MCP server globally:

```bash
# 1. Initialize workspace (writes config/, tools/, AGENTS.md, GEMINI.md, CLAUDE.md)
ros-maintainer-harness -w ~/maintainer_ws init

# 2. Configure container GitHub token (or pass --no-token for unauthenticated mode)
ros-maintainer-harness -w ~/maintainer_ws token-setup --container-token <READONLY_PAT>

# 3. Register the MCP server in ~/.gemini/config/mcp_config.json
ros-maintainer-harness -w ~/maintainer_ws mcp-install --target gemini

# 4. Verify readiness
ros-maintainer-harness -w ~/maintainer_ws doctor
```

> [!IMPORTANT]
> Antigravity reads global MCP server registrations from `~/.gemini/config/mcp_config.json`. Running `ros-maintainer-harness mcp-install` updates this file automatically so the `ros-maintainer-harness` MCP tools are available in every conversation.

## Starting a Conversation

You can start an Antigravity or Gemini conversation in either of two ways:

### Option A: Open a Specific Session Directory (Single-PR Focus)

1. Scaffold the session from a Pull Request:
   ```bash
   ros-maintainer-harness -w ~/maintainer_ws session from-pr ros2/rclcpp#160
   ```
2. Launch Antigravity or Gemini in the session directory:
   ```bash
   ros-maintainer-harness -w ~/maintainer_ws session launch pr-rclcpp-160 --agent antigravity
   ```
   Because `sessions/pr-rclcpp-160/` contains auto-generated `GEMINI.md`, `AGENTS.md`, and `TASK.md` files, Antigravity automatically loads the session rules on startup.

### Option B: Open the Maintainer Workspace Root (Multi-PR Coordinator & Subagents)

1. Open `~/maintainer_ws` (or this repository) as your workspace in Antigravity.
2. Antigravity automatically discovers `GEMINI.md` and `AGENTS.md` at the workspace root.
3. Prompt the agent naturally, for example:
   ```text
   Use the maintainer harness in ~/maintainer_ws to review and test PR ros2/rclcpp#160.
   ```
4. Following the rules in `GEMINI.md` and `AGENTS.md`, the agent will:
   - Run `check_environment` / `ros-maintainer-harness doctor` first (and prompt you if `ROS_CONTAINER_GITHUB_TOKEN` has not been configured yet).
   - Scaffold the session via `scaffold_session_from_pr`.
   - Inspect the PR diff in `sessions/<id>/src/<repo>` for security issues before building.
   - Start the session container (`start_session_container` / `session up`) and execute all `colcon build` and `colcon test` commands inside the container via `exec_in_session` (or `session exec`), including when delegating work to subagents via `invoke_subagent`.

## How Containerized Execution Works for Host Agents & Subagents

When Antigravity or its spawned subagents (`invoke_subagent`) run on the host OS, their standard shell tool executes on the host. To ensure untrusted PR code is never compiled or executed on the host machine:
- Every session has a dedicated container (`ros-harness-<session_id>`) managed by `session up`, `session exec`, and `session down` (or the MCP tools `start_session_container`, `exec_in_session`, and `stop_session_container`).
- The container bind-mounts both the session directory (`/workspace`) and `shared_repos/` at its host path so linked Git worktrees resolve seamlessly inside the container.
- All `colcon build` and `colcon test` commands run inside `ros-harness-<session_id>` with `/opt/ros/$ROS_DISTRO/setup.bash` automatically sourced.

