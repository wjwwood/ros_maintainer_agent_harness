# OpenCode Container Architecture & Investigation

This document records the findings of the OpenCode architecture investigation (Issue #33, tested against `anomalyco/opencode` `v1.18.35`) and specifies how the Hub and Session containers run OpenCode behind a single user interface while keeping tool execution confined to each container.

---

## 1. Executive Summary & Recommended Architecture

**Recommendation**: Run an independent `opencode serve --hostname 0.0.0.0 --port 4096` process **inside** the Hub container and **inside** each Session container, published only on host loopback (`-p 127.0.0.1:<host_port>:4096`) and authenticated with a per-container `OPENCODE_SERVER_PASSWORD`.

1. **True OS/Container Confinement**: Every built-in OpenCode tool (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `webfetch`) and every MCP client call executes inside the `opencode serve` process inside that container.
2. **Single OpenCode UI Across Multiple Containers**: Both the OpenCode Web UI (`opencode web`) and the OpenCode Desktop app natively support registering multiple `http://127.0.0.1:<port>` server connections (`ServerConnection.Http`, persisted in `server.v3` storage) and switching between their projects and conversations from a single UI window (`See Servers` / project-server selector). Terminal users can also attach to any container from the host with `opencode attach http://127.0.0.1:<port> --dir <identical_host_path> -s <session_id>`.
3. **Why Option (a) (Single Host Server with Denied Built-In Tools) Is Rejected**: Upstream OpenCode's `SECURITY.md` explicitly states that its permission config (`permission: { bash: "deny", edit: "deny" }`) is a UX guardrail rather than a security sandbox. If `opencode serve` runs on the host, the agent process still shares the host filesystem and environment, and every file edit or shell command in a session would have to be marshaled through custom MCP wrapper tools instead of OpenCode's native `edit`, `read`, `glob`, `grep`, and `bash` tools.

---

## 2. Answers to Investigation Questions (Tested against OpenCode `v1.18.35`)

### Q0. Single host server vs. in-container servers behind one UI
| Property | Option A: One Host `opencode serve` + Per-Container MCP Tools | Option B (Selected): In-Container `opencode serve` per Hub/Session + Multi-Server UI |
| :--- | :--- | :--- |
| **Where built-in tools (`bash`, `edit`, `read`, `write`, `glob`, `grep`) run** | On the host OS (must be disabled via `permission` config, losing native file editing and grep/glob) | Inside the target container (`ros-harness-hub` or `ros-harness-<id>`) |
| **Isolation boundary** | Application-level config (`opencode.json` permissions; upstream `SECURITY.md` notes this is not a sandbox) | Linux namespaces, cgroups, and read-only bind mounts enforced by the container runtime |
| **Single UI support** | Single server list | Supported natively by OpenCode Web/Desktop multi-server switcher (`packages/app/src/context/server.tsx` and `prompt-project-selector.tsx`) and `opencode attach` |
| **Agent ergonomics** | Degraded (agent cannot use native `read`/`edit`/`bash` tools) | Full native OpenCode toolset inside the container |

### Q1. Server-side vs. client-side tool execution with `opencode serve` and `opencode attach`
When `opencode serve` runs inside a container and a user connects from the host via `opencode attach http://127.0.0.1:<port>` (or via the Web/Desktop UI):
- **100% of tool execution happens on the server (`opencode serve`) inside the container**: `bash`, `read`, `write`, `edit`, `patch`, `glob`, `grep`, `list`, `todowrite`, `webfetch`, and all MCP tool calls execute inside the container's filesystem and network namespace.
- **`opencode attach` is a pure HTTP/SSE TUI client**: Looking at `packages/opencode/src/cli/cmd/attach.ts`, `attach` simply passes `--url http://127.0.0.1:<port>`, optional `--dir`, `--session`, and `--password` Basic Auth headers to the TUI renderer, which communicates with the container over REST + Server-Sent Events (`/global/event`).

### Q2. Server authentication, `127.0.0.1` binding, and multi-client visibility
- **Authentication**: Setting `OPENCODE_SERVER_PASSWORD=<random_secret>` (and optional `OPENCODE_SERVER_USERNAME`, which defaults to `opencode`) in the container environment enables HTTP Basic Authentication across all `opencode serve` routes.
- **Loopback-Only Publishing**: Inside the container, `opencode serve` listens on `--hostname 0.0.0.0 --port 4096` so Docker's port proxy can forward traffic, while the host port mapping is restricted to `-p 127.0.0.1:<host_port>:4096`. No external network interface can reach the container's OpenCode server.
- **Multi-Client Visibility**: Because conversation state lives in the server's SQLite database inside the container and broadcasts events over `GET /global/event` (SSE), multiple clients (for example, the Web UI and a terminal running `opencode attach -s <session_id>`) can view and interact with the same conversation simultaneously.

### Q3. Project directory rooting and learning the OpenCode session ID
- **Project Rooting at Identical Path**: Starting `opencode serve` with `--workdir <ws>` (for the Hub) or `--workdir <ws>/sessions/<id>` (for a Session) roots the default project at the identical host path. Clients can also pass `--dir <ws>/sessions/<id>` or the `x-opencode-directory` HTTP header.
- **Programmatic Session Creation & Discovery**: The Host Launch Service can create and seed a conversation directly via the container's HTTP API (`http://127.0.0.1:<host_port>`):
  1. `GET /global/health` -> returns `{"healthy": true, "version": "1.18.35"}`.
  2. `POST /session` with JSON body `{"title": "PR ros2/rclcpp#160"}` -> returns `{"id": "ses_...", ...}`.
  3. `POST /session/{id}/prompt_async` with JSON body:
     ```json
     {
       "parts": [
         {
           "type": "text",
           "text": "Read AGENTS.md and TASK.md in this session directory and begin the maintainer review workflow."
         }
       ]
     }
     ```
  4. The launch service stores `opencode_session_id` (e.g. `ses_...`), `opencode_port`, and `opencode_url` in `session.json` (while keeping the password in private service state under `~/.local/state/ros_maintainer_agent_harness/`) so `status`, `next`, and `session attach <id>` can link directly to the live session.

### Q4. Injecting `opencode.json`, remote MCP bearer tokens, `AGENTS.md`, and disabling external telemetry/updates
OpenCode natively supports `{env:VAR_NAME}` interpolation inside `opencode.json` without writing secret token values to disk.

Minimal working `opencode.json` generated for the Hub (`<ws>/opencode.json`) and each Session (`<ws>/sessions/<id>/opencode.json`):

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

In addition, containers are launched with the following environment variables so OpenCode does not attempt background binary updates, LSP downloads, or default plugin installations at startup:
- `OPENCODE_DISABLE_AUTOUPDATE=true`
- `OPENCODE_DISABLE_LSP_DOWNLOAD=true`
- `OPENCODE_DISABLE_DEFAULT_PLUGINS=true`
- `ROS_MAINTAINER_GATEWAY_TOKEN` (injected via `docker run -e ROS_MAINTAINER_GATEWAY_TOKEN` environment inheritance so the secret never appears in `ps aux` or `opencode.json` on disk)

### Q5. Provider credentials and disk writes
- **Environment Variable Injection**: OpenCode reads standard LLM provider environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `OPENROUTER_API_KEY`) directly on startup.
- **No Host Auth File Mounts**: `~/.local/share/opencode/auth.json` and `~/.config/opencode` from the host are never mounted into containers.
- **Disk Writes**: When credentials are supplied via environment variables, OpenCode does not write the API keys back to `auth.json` or into the project workspace; it only writes conversation history to its container-local SQLite database under `/root/.local/share/opencode/` (or `$XDG_DATA_HOME/opencode`).

### Q6. Delivering OpenCode into `osrf/ros:*` images across `arm64` (OrbStack) and `amd64`
Upstream `anomalyco/opencode` publishes self-contained static musl Linux binaries on GitHub Releases for both `arm64` and `amd64`:
- `opencode-linux-arm64-musl.tar.gz` (single static binary, works on Ubuntu 20.04/22.04/24.04 and Debian regardless of container glibc version)
- `opencode-linux-x64-musl.tar.gz`

| Delivery Mechanism | Startup Time | Offline / Cached Use | Multi-Distro Maintenance | `arm64` (OrbStack) & `amd64` Support |
| :--- | :--- | :--- | :--- | :--- |
| **1. Static musl binary in `<ws>/tools/bin/opencode` (or host cache mounted `:ro`)** | Instant (~0s overhead at `session up`) | Works offline once downloaded once per arch | Zero per-distro image rebuilds; works with stock `osrf/ros:*` images | Native `linux-arm64-musl` and `linux-x64-musl` binaries |
| **2. Derived image per ROS distro (`FROM osrf/ros:<distro>-desktop`)** | Instant at `run`, ~15-30s per distro at `build` | Works offline after build | Requires building/tagging a derived image for every distro (`rolling`, `jazzy`, `humble`, etc.) | Requires matching base distro arch |
| **3. Install via `curl` / `npm` at container start** | Slow (+10-30s every `session up`) | Fails offline | No image build, but fragile on old Ubuntu/Node versions | Network-dependent on every start |

**Recommendation for #32 and #34**:
- **Hub container (#32)**: Install the static `opencode-linux-<arch>-musl` binary directly in `Dockerfile.hub` (baked into the `ros-harness-hub` image).
- **Session containers (#34)**: Provision the matching static `opencode` binary into `<ws>/tools/bin/opencode` (or a host cache mounted read-only into `/workspace/tools/bin/opencode`), with optional fallback to an pre-installed `opencode` binary in a custom image. Because `/workspace/tools/bin` is already on `$PATH` and mounted `:ro` into every session container, every `osrf/ros:<distro>-desktop` container gets `opencode` immediately without building a derived Docker image per ROS distribution and without network downloads on container start.

### Q7. Fallback evaluation (headless HTTP driver vs. host command-routing plugin)
Because in-container `opencode serve` + `opencode attach` / multi-server Web UI satisfies both hard container confinement and unified UI access, neither fallback is required as the primary architecture. However, because `opencode serve` exposes the clean REST API documented in Q3 (`POST /session`, `POST /session/:id/prompt_async`), the Host Launch Service uses that exact HTTP API in `start_session_conversation` (#37) to create the session and seed the initial `TASK.md` prompt automatically when a session container starts.

---

## 3. Concrete Design Specifications for Downstream Issues

1. **Issue #32 (`Dockerfile.hub` and `hub` CLI)**:
   - `Dockerfile.hub` installs `git`, `ca-certificates`, `curl`, `python3`, the harness wheel, and the architecture-matched `opencode` static binary, running `opencode serve --hostname 0.0.0.0 --port 4096` rooted at `<ws>`.
   - `hub start` publishes `-p 127.0.0.1:<hub_port>:4096` and passes `OPENCODE_SERVER_PASSWORD` and `ROS_MAINTAINER_GATEWAY_TOKEN` via environment inheritance.
   - `hub attach` runs `opencode attach http://127.0.0.1:<hub_port> --dir <ws>` with `OPENCODE_SERVER_PASSWORD` set in the environment.
2. **Issue #34 (Session OpenCode agent and `session attach`)**:
   - `start_session_container` allocates an available localhost port (`127.0.0.1:<session_port>:4096`), generates a random `OPENCODE_SERVER_PASSWORD` and `ROS_MAINTAINER_GATEWAY_TOKEN`, and starts `opencode serve --hostname 0.0.0.0 --port 4096` inside the session container rooted at `<ws>/sessions/<id>`.
   - `session attach <id>` invokes `opencode attach http://127.0.0.1:<session_port> --dir <ws>/sessions/<id> [-s <opencode_session_id>]`.
   - `session attach-info <id> --json` returns the loopback URL, directory, and `opencode_session_id`.
3. **Issue #35 (LLM Credentials)**:
   - Store LLM provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) in `~/.local/state/ros_maintainer_agent_harness/credentials.env` (`0600`) and pass them into Hub and Session containers via `docker run -e <KEY>` inheritance.
4. **Issue #36 (`opencode.json` and `AGENTS.md`)**:
   - Generate `<ws>/opencode.json` for the Hub and `<ws>/sessions/<id>/opencode.json` for each Session using `{env:ROS_MAINTAINER_GATEWAY_TOKEN}` in the remote MCP `Authorization` header so no token is ever written to disk.
5. **Issue #37 (`start_session_conversation`)**:
   - After starting the session container and waiting for `GET http://127.0.0.1:<session_port>/global/health`, call `POST /session` and `POST /session/{id}/prompt_async` with the initial `TASK.md` prompt, and save `opencode_session_id` in `session.json`.
