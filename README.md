# ros-maintainer-agent-harness

A development harness and policy gateway designed for AI coding agents assisting with ROS 2 maintenance and development.

---

## 📖 Overview

When maintaining ROS 2 repositories with the assistance of an AI coding agent, the goal is to enable the agent to work autonomously on local tasks (compiling code, executing test suites, fixing bugs, refactoring, and diagnosing CI failures) while providing practical guardrails to help avoid accidental or unvetted remote side-effects.

### Core Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CONTAINER SANDBOX (e.g. ROS 2 Devcontainer)                │
│  - AI Coding Agent (Antigravity, Claude Code, Cursor, etc.) │
│  - Full local autonomy: colcon build, pytest, local git     │
│  - Read-Only GitHub token (clone, fetch, read issues/PRs)   │
└──────────────────────────────┬──────────────────────────────┘
                               │ Model Context Protocol (MCP)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  HOST POLICY GATEWAY (ros-maintainer-agent-harness)        │
│  - Runs on Maintainer Host (127.0.0.1 / stdio)              │
│  - Holds Read/Write Token, SSH Signing Key, Jenkins Auth    │
│  - Evaluates declarative policies (~/.config/.../policy.yml)│
│  - Records structured action audit log (audit.jsonl)        │
│  - Maintains human-readable session timeline (timeline.md)  │
└───────────────┬─────────────────────────────┬───────────────┘
                │ Guarded API                 │ Guarded CI
                ▼                             ▼
        GitHub Repositories             ci.ros2.org
```

1. **Containerized Execution Sandbox**: The agent runs entirely inside a container (such as a standard ROS 2 devcontainer), isolated from the maintainer's host system credentials and SSH keys.
2. **Read-Only Container Access**: The container is provided only a read-only GitHub token, allowing it to fetch code, pull branches, and read discussions, but preventing it from directly pushing code or creating comments.
3. **Host-Side Policy Gateway (MCP Server)**: A lightweight service running on the maintainer's host machine exposes a controlled set of tools via the Model Context Protocol (MCP). This service holds the maintainer's write credentials and enforces rules regarding what actions the agent can perform, when it can perform them, and what requires explicit maintainer approval.

---

## 🛡️ Policy & Guardrail Model

### Built-in Invariants
* **No Conversational Comments**: The harness provides no general comment posting tool, preventing accidental impersonation.
* **No Auto-Merging**: Merging pull requests is strictly reserved for maintainers.
* **No Direct Base Branch Pushes**: Pushes to `main`, `master`, `rolling`, `jazzy`, etc. are blocked at the gateway level.
* **No Unapproved PR/Issue Creation**: Opening PRs or issues requires explicit maintainer review and approval.

### Configurable Maintainer Policies (`policy.yaml`)
* **Branch Push Allowlist**: Regex patterns for allowable feature branches (e.g. `^<username>/.*$` or `^fix/.*$`).
* **External Fork Protection**: Require explicit maintainer confirmation before pushing to 3rd-party contributor forks.
* **Repository Scope**: Allowlisted GitHub organizations and repositories (`ros2/*`, `ros-tooling/*`).
* **Jenkins CI Controls**: Maximum concurrent runs per PR, cooldown intervals, and auto-cancellation of superseded builds.

---

## 📁 Workspace Layout

The harness manages workspaces using **Linked Git Worktrees** and session overlay directories to support parallel, isolated agent sessions with minimal disk usage:

```
~/ros_maintainer_ws/
├── config/                     # Configuration and maintainer preferences
│   ├── policy.yaml             # Enforced push/CI policies
│   └── maintainer_rules.md     # Human-readable maintainer conventions & preferences
│
├── tools/                      # Shared helper scripts & utilities (available across ALL sessions)
│   ├── bin/                    # Executable scripts (mounted into $PATH for all containers)
│   ├── README.md               # Tool catalog describing what each script does and how to use it
│   └── requirements.txt        # Shared Python dependencies for tools
│
├── shared_repos/               # Central primary Git clones (central .git object database)
│   ├── ros2/                   # e.g. rclcpp.git, rmw_implementation.git
│   └── ros-tooling/            # e.g. ros-github-scripts.git
│
├── sessions/                   # Isolated active task workspaces (one per conversation/PR)
│   ├── session-pr-160/         # Conversation 1
│   │   ├── src/                # Linked git worktrees pointing to shared_repos/
│   │   ├── build/              # Dedicated colcon build directory (bind-mounted to host)
│   │   ├── install/            # Dedicated colcon install directory (bind-mounted to host)
│   │   ├── log/                # Dedicated test and build logs (bind-mounted to host)
│   │   ├── scratch/            # Temporary session-specific scripts and notes
│   │   └── timeline.md         # Chronological session narrative & status updates
│   │
│   └── session-pr-99/          # Conversation 2
│       ├── ...
│       └── timeline.md
│
└── audit/                      # Central gateway action logs
    └── audit.jsonl             # Structured machine-readable audit trail of all remote actions
```

---

---

## 🔧 MCP Tools Exposed by Host Gateway

| Tool Name | Scope | Description |
| :--- | :--- | :--- |
| `log_status` | Local / Timeline | Records progress notes or milestone events to `sessions/<id>/timeline.md`. |
| `git_push` | Remote / Guarded | Pushes local branch to remote repository under strict branch allowlists, base distro protection, and external fork approval guards. |
| `launch_jenkins_ci` | Remote / CI | Launches Jenkins CI on `ci.ros2.org` with concurrency limits and cooldown enforcement. |
| `find_restarted_ci` | Remote / CI | Discovers rescheduled/queued Jenkins jobs and optionally updates GitHub PR status comment markdown. |
| `create_pull_request` | Remote / PR | Creates a GitHub Pull Request with mandatory interactive maintainer approval. |
| `check_policy` | Policy Pre-flight | Tests whether a branch name, repo, or CI launch complies with maintainer policy before attempting it. |
| `create_session` | Workspace | Provisions an isolated session workspace with build/install/log overlays and linked Git worktree. |
| `list_sessions` | Workspace | Lists active sessions and their attached Git worktrees. |
| `prune_session` | Workspace | Cleans up session directory and linked worktrees with audit logging. |
| `get_maintainer_rules` | Rules | Returns human-readable maintainer style, CI, and git conventions from `maintainer_rules.md`. |
| `add_maintainer_rule` | Rules | Appends a new preference rule into `maintainer_rules.md` and records audit entry. |
| `list_approval_requests`| Approvals | Lists pending or resolved maintainer approval requests. |
| `respond_approval_request`| Approvals | Approves or rejects a pending maintainer approval ticket. |

---

## 💻 CLI Usage

```bash
# 1. Initialize Maintainer Workspace
ros-maintainer-harness init

# 2. Run Host MCP Server Gateway (stdio mode for AI tool integration)
ros-maintainer-harness serve --transport stdio

# Or run standing background daemon (SSE mode)
ros-maintainer-harness serve --transport sse --port 8765

# 3. Create and manage session worktrees
ros-maintainer-harness session create session-pr-160 --topic "Fix memory leak" --repo ~/ros2_maintainer_ws/shared_repos/ros2/rclcpp --branch wjwwood/fix_leak
ros-maintainer-harness session list
ros-maintainer-harness session prune session-pr-160

# 4. View and check policies
ros-maintainer-harness policy show
ros-maintainer-harness policy check --branch wjwwood/fix_leak --repo ros2/rclcpp

# 5. Inspect audit log and approval tickets
ros-maintainer-harness audit show -n 20
ros-maintainer-harness approval list --status PENDING
ros-maintainer-harness approval approve req-abcd1234 --comment "Looks good to push"
```

---

## ⚙️ MCP Client Configuration

To connect your AI assistant (e.g. Antigravity, Claude Code, Cursor, Claude Desktop) to the host gateway:

```json
{
  "mcpServers": {
    "ros-maintainer-harness": {
      "command": "ros-maintainer-harness",
      "args": ["serve", "--transport", "stdio", "--workspace", "~/ros_maintainer_ws"]
    }
  }
}
```

---

## 📚 Documentation

For in-depth architectural details, trust boundaries, multi-session Git worktree management, and policy specifications, see:
* [Architecture & Design Plan](docs/design.md)

---

## 📜 License

This project is licensed under the [Apache License, Version 2.0](LICENSE).

