# ros-maintainer-agent-harness

A development harness (sandboxed workspaces and tooling) and policy-enforcing MCP server for AI coding agents working on ROS 2.

## Overview

When using AI coding agents to help maintain ROS 2 repositories, you want the agent to have enough autonomy to inspect code, build packages, run tests, and diagnose failures without giving it unrestricted access to your credentials or remote repositories.

Giving an autonomous agent direct access to your personal SSH keys, GitHub write tokens, or Jenkins credentials risks unwanted pushes, unvetted comments, or triggering runaway CI jobs.

This harness separates the environment into two distinct halves:

1. **An isolated container sandbox** (a standard ROS 2 devcontainer) where the agent can build and test code locally. It only has read-only access to GitHub.
2. **A host-side policy gateway** (an MCP server) running on your local machine. It holds your write credentials, evaluates push and CI requests against configurable policies, logs an audit trail, and handles heavy operations like Jenkins polling.

A core principle of this setup is **local-first verification**: the agent performs as much compilation, linting, and regression testing as possible inside the local container sandbox before triggering remote Jenkins CI, minimizing strain on shared community build farm infrastructure (`ci.ros2.org`).

```
┌─────────────────────────────────────────────────────────────┐
│ CONTAINER SANDBOX (ROS 2 Devcontainer)                      │
│ - AI Agent (Claude Code, Cursor, Codex, etc.)               │
│ - Full local build/test autonomy: colcon build, pytest, git │
│ - Read-only GitHub access (clone, fetch, read issues/PRs)   │
│ - Shared helper tools mounted at /workspace/tools/bin       │
└──────────────────────────────┬──────────────────────────────┘
                               │ Model Context Protocol (MCP)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ HOST POLICY GATEWAY (ros-maintainer-harness serve)          │
│ - Runs on maintainer host (stdio or HTTP/SSE)               │
│ - Holds write credentials: SSH signing keys, GitHub, Jenkins│
│ - Enforces safety policies (policy.yaml)                    │
│ - Background CI monitor & JUnit failure summarizer          │
│ - Structured audit log (audit.jsonl) & session timeline     │
└───────────────┬─────────────────────────────┬───────────────┘
                │ Guarded API                 │ Guarded CI
                ▼                             ▼
        GitHub Repositories             ci.ros2.org
```

## Quick Start

### Installation

Install the package in editable mode:

```bash
pip install -e .
```

Prerequisites:
- Python 3.10+
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/) authenticated with appropriate scopes
- An OCI container runtime (Docker or Podman) to run devcontainer sessions
  - Running on the host does not require Docker-in-Docker (DinD).
  - If running the harness itself inside a container (e.g., GitHub Codespaces), use Docker-outside-of-Docker (mount `/var/run/docker.sock`) or enable DinD.

### Typical Workflow

#### 1. Initialize the workspace

Initialize the maintainer directory structure and default configuration:

```bash
ros-maintainer-harness init
```

By default this initializes the current directory (`.`), but you can specify a path with `--workspace` or set `$ROS_MAINTAINER_WS`.

#### 2. Create a session from a PR

You can set up an entire isolated investigation environment directly from a PR URL or shorthand:

```bash
ros-maintainer-harness session from-pr ros2/rclcpp#160
```

This single command:
- Fetches PR metadata (title, author, base/head branches, changed files).
- Auto-detects the target ROS 2 distribution from the base branch (e.g., `jazzy` or `rolling`).
- Clones the target repository into `shared_repos/` if not already present.
- Creates a new linked git worktree in `sessions/pr-rclcpp-160/src/rclcpp`.
- Generates `.devcontainer/devcontainer.json` configured for that ROS distribution.
- Writes editor/agent MCP configs (`mcp.json`, `.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`).
- Pre-populates `timeline.md` and generates a structured `TASK.md` goal prompt for the agent.

#### 3. Launch an agent in the session

Launch your preferred coding agent inside the session directory with the appropriate environment variables pre-configured:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent claude
```

Supported agents/editors include `claude`, `cursor`, `code`/`vscode`, or an interactive container `shell`. You can also pass `--dry-run` to inspect the command line and environment without launching.

#### 4. Monitor CI without burning agent tokens

Instead of having an LLM repeatedly poll Jenkins and consume context tokens reading multi-megabyte build logs, the harness can poll on the host and return structured failure summaries:

```bash
# Wait for a running build to finish
ros-maintainer-harness ci status ros2/rclcpp#160 --wait

# Get a compact summary of failed tests and compiler errors
ros-maintainer-harness ci summary ros2/rclcpp#160
```

#### 5. Review audit logs and maintainer approvals

All actions that interact with remote systems are logged to `audit/audit.jsonl`:

```bash
ros-maintainer-harness audit show -n 20
```

If an agent attempts an action that requires maintainer confirmation (such as pushing to an external contributor's fork or creating a PR), the gateway creates an approval ticket:

```bash
# List pending requests
ros-maintainer-harness approval list --status PENDING

# Approve or reject a ticket
ros-maintainer-harness approval approve req-abcd1234 --comment "Reviewed diff, approved to push"
ros-maintainer-harness approval reject req-abcd1234 --comment "Needs cleaner commit message"
```

## Policy & Guardrails

The host gateway enforces safety constraints defined in `config/policy.yaml`:

### Invariants
- **No direct base branch pushes**: Pushes to `main`, `master`, `rolling`, `jazzy`, `iron`, `humble`, etc. are blocked at the gateway level.
- **No unapproved PR creation**: Opening pull requests requires explicit maintainer review and approval.
- **No conversational commenting**: The harness does not provide tools for posting conversational comments on issues or PRs, preventing impersonation.
- **No automated merges**: Merging pull requests is strictly reserved for the maintainer.

### Configurable Policies (`policy.yaml`)
- **Branch naming rules**: Enforce branch naming conventions using regex patterns (e.g., `^<username>/.*$` or `^fix/.*$`).
- **Repository allowlist**: Restrict operations to specific organizations or repositories (e.g., `ros2/*`, `ros-tooling/*`).
- **External fork protection**: Require explicit maintainer approval before pushing commits to forks owned by third-party contributors.
- **CI rate limiting**: Limit concurrent Jenkins runs per PR and enforce cooldown intervals between rebuilds.

Test whether an action complies with policy:

```bash
ros-maintainer-harness policy check --branch wjwwood/fix_timer --repo ros2/rclcpp
```

## Workspace Structure

The workspace uses linked Git worktrees and session overlay directories so multiple tasks can run in parallel without cloning separate copies of large repositories:

```
~/ros_maintainer_ws/
├── config/                     # Configuration and maintainer preferences
│   ├── policy.yaml             # Enforced push and CI policies
│   └── maintainer_rules.md     # Maintainer conventions & preferences (mounted read-only)
│
├── tools/                      # Shared helper scripts mounted into all containers
│   ├── bin/                    # Executables added to $PATH in containers
│   │   ├── ros-find-restarted-ci
│   │   ├── ros-ci-for-pr
│   │   ├── ros-ci-status
│   │   └── ros-session-status
│   └── requirements.txt        # Shared Python dependencies
│
├── shared_repos/               # Bare or primary Git clones (shared object storage)
│   ├── ros2/
│   │   └── rclcpp.git
│   └── ros-tooling/
│
├── sessions/                   # Task workspaces (one per PR or issue)
│   ├── pr-rclcpp-160/
│   │   ├── src/                # Linked git worktrees pointing to shared_repos/
│   │   ├── build/              # Dedicated build directory
│   │   ├── install/            # Dedicated install directory
│   │   ├── log/                # Build and test logs
│   │   ├── scratch/            # One-off scripts and debug files
│   │   ├── timeline.md         # Chronological session log & milestones
│   │   ├── TASK.md             # Goal prompt and task context
│   │   └── .devcontainer/      # Devcontainer definition for target distro
│   │
│   └── pr-rmw-42/
│
└── audit/                      # Audit logs & approvals
    ├── audit.jsonl             # Machine-readable log of all remote actions
    ├── approvals.json          # Maintainer approval ticket database
    └── ci_runs.json            # Tracked CI jobs and status cache
```

## MCP Gateway Tools

When running `ros-maintainer-harness serve`, the host gateway exposes tools to connected AI coding agents over the Model Context Protocol:

- **Git & GitHub**: `git_push` (guarded by policy), `create_pull_request` (requires approval ticket).
- **CI Management**: `launch_jenkins_ci`, `get_ci_status` (supports host-side blocking wait), `get_ci_summary` (parses JUnit failures & compiler errors), `list_ci_runs`, `cancel_ci_run`, `find_restarted_ci`.
- **Sessions & Workspaces**: `scaffold_session_from_pr`, `create_session`, `list_sessions`, `prune_session`, `generate_mcp_config`, `get_session_launch_info`.
- **Maintainer Preferences & Audit**: `log_status` (records milestones in `timeline.md`), `get_maintainer_rules`, `add_maintainer_rule`, `check_policy`, `list_approval_requests`, `respond_approval_request`.

### Editor & Client Configuration

You can connect any MCP-compatible client directly to the gateway. For local stdio:

```json
{
  "mcpServers": {
    "ros-maintainer-harness": {
      "command": "ros-maintainer-harness",
      "args": ["serve", "--transport", "stdio", "--workspace", "/path/to/ros_maintainer_ws"]
    }
  }
}
```

Or run the gateway as a background daemon over SSE / HTTP:

```bash
ros-maintainer-harness serve --transport sse --port 8765
```

When sessions are created with `session create` or `session from-pr`, these client configs are generated automatically inside each session directory.

## Documentation

- **[End-to-End Walkthrough](docs/walkthrough.md)**: Complete step-by-step example of triaging and fixing a ROS 2 PR.
- **[AI Agent Integration Guides](docs/agents/index.md)**: Setup tutorials for specific agents:
  - [Claude Code](docs/agents/claude_code.md)
  - [Cursor](docs/agents/cursor.md)
  - [VS Code & Extensions](docs/agents/vscode.md)
  - [Interactive Shell](docs/agents/shell.md)
- **[Policy & Security Model](docs/policy_and_security.md)**: Trust boundaries, `policy.yaml` configuration, and maintainer approvals.
- **[GitHub Token Setup & Best Practices](docs/github_tokens.md)**: Two-token architecture, creating read-only tokens for the agent, and rate-limit management.
- **[Container Runtimes & Sandboxing](docs/containers.md)**: Docker/Podman setup, devcontainers, DinD/DooD, and worktree layouts.
- **[Shared Tools Catalog](docs/tools.md)**: Built-in utilities, design rationale, and guide for adding custom tools.
- **[Architecture & Design Plan](docs/design.md)**: Original design specification and system architecture.

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).
