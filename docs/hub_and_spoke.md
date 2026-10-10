# Hub-and-Spoke Architecture & Threat Model

This document is the reference specification for the three-tier hub-and-spoke deployment of `ros_maintainer_agent_harness`. It defines the execution tiers, filesystem mounts, network boundaries, caller authorization matrix, and threat model.

---

## 1. Three-Tier Overview

The harness separates development of the harness itself, multi-session coordination, and per-PR build/test execution into three tiers:

| Tier | Where the Agent Runs | Working Directory | Filesystem Access | Docker Socket | Network & Credentials |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Dev** | Host OS (trusted) | Harness source checkout (for example `~/dev/ros_maintainer_agent_harness`) | Full host access for editing and testing the harness | Host access (trusted) | Host environment; runs `pytest`, `flake8`, and `redeploy` |
| **Hub** | Hub container (`ros-harness-hub`) | `~/ros_maintenance_ws` (mounted at the identical host path) | Workspace root (`rw`), with `config/` and `audit/` mounted read-only (`ro`); no access outside workspace | **None** | Calls host launch service (`http://host.docker.internal:8765/mcp`) with `hub` bearer token; holds LLM API key and read-only GitHub token |
| **Session** | Session container (`ros-harness-<id>`, e.g. `osrf/ros:jazzy-desktop`) | `~/ros_maintenance_ws/sessions/<id>` (mounted at the identical host path, linked at `/workspace`) | Own session directory (`rw`), `tools/` (`ro`), `shared_repos/` (`ro` by default), `MAINTAINER_RULES.md` (`ro`); no sibling sessions or `audit/` | **None** | Calls host launch service (`http://host.docker.internal:8765/mcp`) with `session:<id>` bearer token; holds LLM API key and read-only GitHub token |

Between the Hub and Session containers sits the **Host Launch Service** (`ros-maintainer-harness gateway`), a deterministic Python HTTP MCP server running on the host. It is not an AI agent. It is the only component that talks to the container engine (`docker` or `podman`) and the only component that holds GitHub write credentials, SSH keys, bloom credentials, and Jenkins API tokens.

```mermaid
flowchart TD
    subgraph HostOS ["Maintainer Host Machine"]
        DevAgent["Dev Tier Agent (Host)<br/>Edits harness repo, runs pytest & redeploy"]
        UI["Single OpenCode Web / Desktop / TUI Client<br/>Connects to 127.0.0.1:<hub_port> and 127.0.0.1:<session_port>"]
        Gateway["Host Launch Service & Policy Gateway<br/>(ros-maintainer-harness gateway, 127.0.0.1:8765)<br/>Holds write credentials, validates LaunchSpec & role tokens"]
        DockerDaemon["Container Engine (OrbStack / Docker / Podman)"]
        StateDir["~/.local/state/ros_maintainer_agent_harness/<br/>(pidfile, role tokens, LLM keys, host write tokens)"]

        subgraph HostWS ["~/ros_maintenance_ws/ (Identical-Path Host Workspace)"]
            ConfigDir["config/ (policy.yaml, maintainer_rules.md)"]
            AuditDir["audit/ (audit.jsonl, approvals.json, ci_runs.json)"]
            ToolsDir["tools/ (bin/, requirements.txt)"]
            SharedRepos["shared_repos/ (central git object cache)"]
            Session1Dir["sessions/pr-rclcpp-160/"]
            Session2Dir["sessions/pr-launch-712/"]
        end
    end

    subgraph HubContainer ["Hub Container (ros-harness-hub)"]
        HubServer["opencode serve (port 4096 -> host 127.0.0.1:<hub_port>)<br/>Role: hub"]
    end

    subgraph SessionContainer ["Session Container (ros-harness-pr-rclcpp-160)"]
        SessionServer["opencode serve (port 4096 -> host 127.0.0.1:<session_port>)<br/>Role: session:pr-rclcpp-160"]
        Colcon["colcon build / colcon test / pytest"]
        SessionServer --> Colcon
    end

    UI <-->|HTTP + Basic Auth on 127.0.0.1| HubServer
    UI <-->|HTTP + Basic Auth on 127.0.0.1| SessionServer

    HubServer -->|MCP over HTTP + Bearer Token (hub)| Gateway
    SessionServer -->|MCP over HTTP + Bearer Token (session:pr-rclcpp-160)| Gateway

    Gateway -->|Validated docker run / stop / inspect| DockerDaemon
    Gateway -->|Reads/writes state & tokens| StateDir
    Gateway -->|Appends audit records & tickets| AuditDir

    HostWS ==>|Bind mount: ws (rw), config/ (ro), audit/ (ro)| HubContainer
    Session1Dir ==>|Bind mount: sessions/pr-rclcpp-160 (rw)| SessionContainer
    ToolsDir -.->|Bind mount (ro)| SessionContainer
    SharedRepos -.->|Bind mount (ro)| SessionContainer
```

---

## 2. Core Architectural Decisions

### 2.1 Identical-Path Bind Mounts
Both the Hub container and Session containers mount host workspace directories at their **exact absolute host paths** (for example `/Users/maintainer/ros_maintenance_ws` on macOS with OrbStack, or `/home/maintainer/ros_maintenance_ws` on Linux):

1. **No Path Translation Layer**: When the Hub or a Session agent passes a path to the Host Launch Service, or when the launch service invokes `docker run -v <source>:<target>`, host paths and container paths match byte-for-byte. Session containers also keep a `/workspace` symlink pointing to `<ws>/sessions/<id>` for backward compatibility with existing `.devcontainer` configurations and helper tools.
2. **Git Metadata Resolution**: Linked git checkouts inside `sessions/<id>/src/<repo>` reference `shared_repos/<repo>` using absolute host paths (in `.git` worktree files or `.git/objects/info/alternates`). Mounting `shared_repos/` at the identical absolute path inside the container allows `git status`, `git diff`, `git log`, and `git commit` to resolve shared objects without rewriting git metadata.

### 2.2 No Docker Socket in Containers
Neither the Hub container nor any Session container ever mounts `/var/run/docker.sock`:

1. **Why**: Access to the Docker socket is equivalent to unrestricted root access on the host (a container with socket access can run `docker run --privileged -v /:/host` and modify the host filesystem or steal host credentials).
2. **How Containers Are Started Instead**: When the Hub agent wants to scaffold a PR or start a session container, it calls MCP tools on the Host Launch Service (`scaffold_session_from_pr`, `start_session_container`, `start_session_conversation`). The launch service constructs and validates a structured `LaunchSpec` on the host before invoking `docker run`:
   - Only allowlisted images (`DEFAULT_DISTRO_IMAGES`, the hub image, and explicit `policy.yaml` allowlist entries) are permitted.
   - All mount sources are canonicalized (`Path.resolve()`) and verified to reside inside the workspace root.
   - Privileged flags (`--privileged`, `--network=host`, `--pid=host`, `--ipc=host`, `--cap-add`) and sensitive mounts (`docker.sock`, `~/.ssh`, `~/.config/gh`, host `$HOME`) are rejected in code.
   - Container ports are bound exclusively to `127.0.0.1:<port>`.

### 2.3 Read-Only `config/` and `audit/` in the Hub
The Hub container mounts `<ws>` read-write so it can read session timelines and coordinate workspace state, but overlays `<ws>/config` and `<ws>/audit` as read-only (`:ro`) bind mounts:

- A compromised or prompt-injected Hub agent cannot weaken `config/policy.yaml` or alter `config/maintainer_rules.md`.
- The Hub agent cannot forge or self-approve tickets in `audit/approvals.json` or tamper with `audit/audit.jsonl`.
- Only the Host Launch Service (and the human maintainer via the host CLI) can write to `audit/`.

---

## 3. Tier Capabilities and Caller Authorization Matrix

The Host Launch Service issues a random bearer token whenever it starts the Hub container (`role = "hub"`) or a Session container (`role = "session:<session_id>"`). Token hashes and metadata live in `~/.local/state/ros_maintainer_agent_harness/` (outside the mounted workspace) and are revoked when a container stops.

Any `session_id` parameter passed by a `session:<id>` caller is verified against (or overridden by) the token's bound `<id>`, preventing one session from acting on another session's worktree, status, or CI runs.

| Operation / MCP Tool Category | `admin` (Host CLI / Dev) | `hub` (Hub Container) | `session:<id>` (Session Container) |
| :--- | :--- | :--- | :--- |
| **Environment & Status Queries** (`check_environment`, `get_workspace_status`, `get_next_actions`, `list_sessions`, `get_maintainer_rules`, `check_policy`, `list_approval_requests`) | Allowed | Allowed | Read-only self status (`get_maintainer_rules`, `check_policy`) |
| **Session Lifecycle & Container Launch** (`scaffold_session_from_pr`, `create_session`, `prune_session`, `start_session_container`, `stop_session_container`, `start_session_conversation`) | Allowed | Allowed (subject to `max_concurrent_sessions` and `LaunchSpec` policy) | **Denied** |
| **Cross-Session Command Execution** (`exec_in_session`) | Allowed | Configurable (`allow_hub_exec_in_session`, disabled by default once in-container session agents are active) | **Denied** (session agent already runs inside its own container) |
| **Session Progress & Timeline** (`log_status`, `update_session_status`) | Allowed | Allowed | Allowed for own `<id>` only |
| **CI Queries & Launching** (`get_ci_status`, `get_ci_summary`, `list_ci_runs`, `find_restarted_ci`, `launch_jenkins_ci`, `cancel_ci_run`) | Allowed | Allowed | Allowed for own `<id>` only (subject to CI cooldown and rate limits) |
| **Remote Git & PR Mutations** (`git_push`, `create_pull_request`, `edit_pull_request`) | Allowed (with policy/ticket) | **Denied** (mutations originate from a session checkout) | Allowed for own `<id>` only (subject to branch/fork policy and approval tickets) |
| **Package Releases** (`push_release`, `run_bloom_release`) | Allowed (with approval ticket) | **Denied** | Allowed for own `<id>` only when policy enables session release requests and a maintainer approval ticket is approved |
| **Approving Tickets & Editing Policy** (`approval approve|reject`, `rules add`, editing `policy.yaml`) | Host CLI only | **Denied** | **Denied** |

---

## 4. Threat Model & Remaining Trust Points

Containerization and the Host Launch Service bound what an agent can touch on disk and which remote mutations it can trigger, but they do not eliminate all risk. The following trust points remain and must be accounted for by maintainers:

1. **The Host Launch Service Is the Privileged Boundary**:
   - The launch service holds host write credentials and invokes `docker` on the host. Any bug in its caller authentication, path validation, or `LaunchSpec` enforcement directly affects host security.
2. **Read-Only `config/` and `audit/` Are Mandatory for Hub Confinement**:
   - Because the Hub container mounts the workspace root, its isolation depends on the read-only sub-mounts on `config/` and `audit/` and on storing launch service state/tokens outside `<ws>`. `hub inspect-mounts` and `doctor` verify that these mounts are `ro` at runtime.
3. **Prompt Injection and LLM Provider Credentials**:
   - Both the Hub and Session agents read untrusted external input (GitHub PR descriptions, review comments, commit messages, and source diffs) while holding LLM API credentials in their process environment.
   - A prompt injection in a PR could attempt to read the container's LLM API key from the environment and exfiltrate it over the network, or invoke MCP tools allowed to that role.
   - Mitigation: Use dedicated LLM API keys with strict spend limits stored outside the workspace (`~/.local/state/ros_maintainer_agent_harness/`), never mount host auth files (`~/.local/share/opencode/auth.json`, `~/.config/gh`, `~/.ssh`), and require maintainer approval tickets for external fork pushes, API PR creation/edits, and releases.
4. **Read-Only `shared_repos/` and Local `git commit` Behavior**:
   - Mounting `shared_repos/` read-only (`:ro`) prevents a session container from corrupting shared git objects or altering branches used by other sessions.
   - However, a standard `git worktree` stores its object database and branch refs under `shared_repos/<repo>/.git/`, so `git commit` inside a read-only `shared_repos/` mount fails unless either (a) the session checkout uses a session-local `.git` directory with `.git/objects/info/alternates` pointing to `shared_repos/<repo>/.git/objects` (via `git clone --reference`), or (b) the maintainer explicitly enables `--writable-shared-repos` after reviewing the PR diff.
5. **Untrusted Session Directory Contents**:
   - Everything inside `<ws>/sessions/<id>/` is writable by the session container. Host-side code in the launch service that inspects session files (`session.json`, `TASK.md`, or `.git/config` inside `sessions/<id>/src/<repo>`) must treat those files as untrusted input: never execute hooks from a session `.git/hooks` directory on the host, always pass `-c core.hooksPath=/dev/null` and `-c safe.directory=...` when running host `git` commands on a session repo, and validate all parsed values.
6. **The Dev Tier Is Unconfined by Design**:
   - The Dev conversation runs on the host so it can edit this repository, run unit tests, and execute `ros-maintainer-harness redeploy`. It must never be used to build or test untrusted target ROS PRs.
