# OpenCode Integration Guide

This guide explains how to use [OpenCode](https://opencode.ai) with `ros_maintainer_agent_harness` in the containerized hub-and-spoke deployment. For the full architectural analysis and security model, see [OpenCode Container Architecture](../opencode.md) and [Hub-and-Spoke Architecture](../hub_and_spoke.md).

---

## 1. How OpenCode Runs in the Harness

Each tier runs its own isolated `opencode serve` instance inside its container, bound exclusively to `127.0.0.1` on the host and protected by HTTP Basic Authentication (`OPENCODE_SERVER_PASSWORD`):

- **Hub Container (`ros-harness-hub`)**: Runs `opencode serve` rooted at `~/ros_maintenance_ws` (mounted at the identical host path, with `config/` and `audit/` read-only). It connects to the Host Launch Service (`http://host.docker.internal:8765/mcp`) using a `hub` bearer token.
- **Session Containers (`ros-harness-<session_id>`)**: Each session container runs `opencode serve` rooted at `~/ros_maintenance_ws/sessions/<session_id>`. All built-in tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`) execute inside the ROS distro container. It connects to the Host Launch Service using a session-scoped `session:<session_id>` bearer token.

Because `opencode serve` runs inside the container, all shell commands (`colcon build`, `colcon test`, `pytest`) and file edits stay confined to that container without needing host-side `PreToolUse` command-rewriting hooks.

---

## 2. Attaching from the Terminal or Single Web/Desktop UI

### Attaching via the CLI (`hub attach` and `session attach`)
Once the gateway, hub, or session container is running, you can attach an interactive OpenCode TUI client from your host terminal:

```bash
# Attach to the Hub coordinator conversation
ros-maintainer-harness -w ~/ros_maintenance_ws hub attach

# Attach to a specific PR session container
ros-maintainer-harness -w ~/ros_maintenance_ws session attach pr-rclcpp-160
```

To inspect the loopback URL, directory, and OpenCode session ID in JSON format:

```bash
ros-maintainer-harness -w ~/ros_maintenance_ws session attach-info pr-rclcpp-160 --json
```

### Connecting Multiple Container Servers in One OpenCode Web or Desktop Instance
OpenCode Web (`opencode web`) and OpenCode Desktop support connecting a single UI window to multiple `opencode serve` HTTP endpoints:

1. Run `ros-maintainer-harness status` or `session attach-info <id>` to view the `http://127.0.0.1:<port>` endpoint for the Hub and each active Session container.
2. In the OpenCode Web or Desktop app, open the server switcher (**See Servers**) and add the Hub and Session loopback URLs (`http://127.0.0.1:<port>`).
3. Switch between the Hub coordinator conversation and any active Session conversation directly within the same UI instance.

---

## 3. Generated `opencode.json` Configuration

The harness generates `opencode.json` in the workspace root (for the Hub) and in each session directory (`sessions/<id>/opencode.json`). It uses OpenCode's `{env:ROS_MAINTAINER_GATEWAY_TOKEN}` variable substitution so bearer tokens are injected at container start and never written to disk:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "share": "disabled",
  "lsp": false,
  "instructions": [
    "AGENTS.md",
    "MAINTAINER_RULES.md"
  ],
  "permission": {
    "read": "allow",
    "edit": "allow",
    "bash": "allow",
    "glob": "allow",
    "grep": "allow",
    "webfetch": "deny"
  },
  "mcp": {
    "ros-maintainer-harness": {
      "type": "remote",
      "url": "http://host.docker.internal:8765/mcp",
      "enabled": true,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:ROS_MAINTAINER_GATEWAY_TOKEN}"
      }
    }
  }
}
```
