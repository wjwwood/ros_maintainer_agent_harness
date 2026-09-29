# Using Antigravity & Gemini with the Harness

Antigravity and Gemini CLI provide agentic workflows that integrate natively with the Model Context Protocol (MCP) and automatically discover workspace rule files (`AGENTS.md`).

## Prerequisites

- Antigravity IDE or Gemini CLI installed.
- Docker or Podman installed on the host.
- A read-only fine-grained GitHub Personal Access Token for container sandboxes (or explicit opt-in to unauthenticated container mode; see [GitHub Token Setup](../github_tokens.md)).

## One-Time Workspace Setup

Before starting a conversation in Antigravity or Gemini, initialize your maintainer workspace, configure your container token, and register the MCP server globally:

```bash
# 1. Initialize workspace (writes config/, tools/, AGENTS.md, CLAUDE.md)
ros-maintainer-harness -w ~/ros_maintenance_ws init

# 2. Configure container GitHub token (or pass --no-token for unauthenticated mode)
ros-maintainer-harness -w ~/ros_maintenance_ws token-setup --container-token <READONLY_PAT>

# 3. Register the MCP server in ~/.gemini/config/mcp_config.json
ros-maintainer-harness -w ~/ros_maintenance_ws mcp-install --target gemini

# 4. Verify readiness
ros-maintainer-harness -w ~/ros_maintenance_ws doctor
```

> [!IMPORTANT]
> Antigravity reads global MCP server registrations from `~/.gemini/config/mcp_config.json`. Running `ros-maintainer-harness mcp-install` updates this file automatically so the `ros-maintainer-harness` MCP tools are available in every conversation.

## Starting a Conversation

You can start an Antigravity or Gemini conversation in either of two ways:

### Option A: Open a Specific Session Directory (Single-PR Focus)

1. Scaffold the session from a Pull Request:
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws session from-pr ros2/rclcpp#160
   ```
2. Launch Antigravity or Gemini in the session directory:
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws session launch pr-rclcpp-160 --agent antigravity
   ```
   Because `sessions/pr-rclcpp-160/` contains auto-generated `AGENTS.md` and `TASK.md` files, Antigravity automatically loads the session rules on startup.

### Option B: Open the Maintainer Workspace Root as a "Maintainer Hub" (Recommended)

1. Open `~/ros_maintenance_ws` (or this repository) as your workspace in Antigravity/Jetski.
2. Antigravity automatically discovers `AGENTS.md` at the workspace root.
3. Use a single long-lived **Maintainer Hub** conversation to coordinate your work across multiple PRs:
   - **Check Status Across All Tasks**:
     ```text
     What is the status of things we're still working on?
     ```
     Calls `get_workspace_status` (or `ros-maintainer-harness status`) to display all active sessions, clickable `[<session_id>](conversation://<id>)` links, latest milestones, container state, CI runs, and pending approvals.
   - **Triage What to Work on Next**:
     ```text
     What should I work on next?
     ```
     Calls `get_next_actions` (or `ros-maintainer-harness next`) to prioritize pending approval tickets, blocked sessions, failed/completed CI jobs, and open GitHub PRs not yet in an active session.
   - **Start a Dedicated Task Conversation for a PR**:
     ```text
     I want to work on ros2/rclcpp#160
     ```
     Calls `start_session_conversation(pr_ref="ros2/rclcpp#160")`, which scaffolds `sessions/pr-rclcpp-160/`, launches a dedicated top-level conversation via `agentapi new-conversation` (or spawns a background subagent via `invoke_subagent`), links the `conversation_id` in `session.json`, and returns a clickable `conversation://<id>` link in the Hub chat.

## How Containerized Execution Works for Host Agents & Subagents

When Antigravity or its spawned subagents (`invoke_subagent`) run on the host OS, their standard shell tool executes on the host. To ensure untrusted PR code is never compiled or executed on the host machine:
- Every session has a dedicated container (`ros-harness-<session_id>`) managed by `session up`, `session exec`, and `session down` (or the MCP tools `start_session_container`, `exec_in_session`, and `stop_session_container`).
- The container bind-mounts both the session directory (`/workspace`) and `shared_repos/` at its host path so linked Git worktrees resolve seamlessly inside the container.
- All `colcon build` and `colcon test` commands run inside `ros-harness-<session_id>` with `/opt/ros/$ROS_DISTRO/setup.bash` automatically sourced.

