# GitHub Token Setup & Best Practices

This guide explains how to configure GitHub credentials for both the **Host Policy Gateway** and the **Container Sandbox**, and provides advice on creating targeted, read-only tokens for AI coding agents.

---

## 1. The Two-Token Architecture

To maintain a secure boundary between your host credentials and the autonomous coding agent, the harness uses two distinct credential tiers:

```
┌────────────────────────────────────────────────────────┐
│ CONTAINER SANDBOX (AI Agent)                           │
│ - Restricted Read-Only Token                           │
│ - Permissions: Contents (Read-only), PRs (Read-only)   │
│ - CANNOT push code, CANNOT comment, CANNOT create PRs  │
└────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│ MAINTAINER HOST (Policy Gateway)                       │
│ - Full Maintainer Credentials (gh CLI / write token)   │
│ - SSH Signing Keys (id_ed25519)                        │
│ - Evaluates policy before executing any write actions  │
└────────────────────────────────────────────────────────┘
```

### Why You Should Never Pass Your Host Token into the Container
If you pass your personal GitHub write token or host `gh` CLI session into the container, the containment model is broken:
- Any prompt injection in a contributor's PR (such as malicious instructions embedded in a markdown file or issue body) could instruct the agent to run `git push` or tamper with repositories across your account.
- Accidental commands executed by the agent could push broken code to upstream branches or publish unreviewed comments.

By strictly separating the tokens, the agent inside the container **physically lacks the credentials** to modify remote repositories. All remote writes must be explicitly routed through the Host Policy Gateway.

---

## 2. Setting Up the Host Gateway Token (Privileged)

The Host Policy Gateway runs on your machine and uses your maintainer credentials to:
- Query PR metadata and changed files (`gh pr view` or GitHub REST API).
- Push approved branches to GitHub via Git/SSH.
- Create pull requests via GitHub CLI or API (with maintainer approval).

### Recommended: GitHub CLI Authentication
If you already use the GitHub CLI (`gh`), no extra setup is required:

```bash
gh auth login
```

The gateway automatically detects your active `gh` session and uses it for API queries and git operations.

### Alternative: Personal Access Token
If you prefer using an environment variable on the host:
```bash
export GITHUB_TOKEN="ghp_yourHostWriteTokenHere"
```

Ensure this token has:
- `repo` scope (for classic tokens), or
- `Contents: Read & write` and `Pull requests: Read & write` (for fine-grained tokens).

---

## 3. Creating a Targeted Read-Only Token for Containers

The container sandbox needs a token to:
1. Clone and fetch repository code without hitting GitHub's 60 requests/hour unauthenticated IP rate limit.
2. Allow the agent to read PR discussions, issue descriptions, or API metadata.

We strongly recommend creating a **Fine-Grained Personal Access Token (PAT)** with strictly read-only permissions:

### Step-by-Step Token Creation

1. Navigate to GitHub > **Settings** > **Developer Settings** > **Personal access tokens** > **Fine-grained tokens**.
2. Click **Generate new token**.
3. Configure the token parameters:
   - **Token name**: `ros-maintainer-agent-readonly`
   - **Expiration**: 30, 60, or 90 days.
   - **Resource owner**: Select the target organization (e.g. `ros2`, `ros-tooling`, or your personal account).
   - **Repository access**: Select **Only select repositories** (e.g. `ros2/rclcpp`) or **All repositories** under that organization.
4. Configure **Permissions**:
   - **Repository permissions**:
     - `Contents`: **Read-only** (allows cloning, fetching, and reading code).
     - `Pull requests`: **Read-only** (allows reading PR titles, descriptions, and comments).
     - `Metadata`: **Read-only** (automatically selected by GitHub).
   - **All other permissions**: Set to **No access**.
5. Click **Generate token** and copy the resulting string (`github_pat_...`).

---

## 4. Providing the Read-Only Token to Containers

Once generated, you can supply the read-only token to your session containers:

### Method A: Host Environment Variable (Recommended)
Add the token to your shell profile (e.g. `~/.bashrc` or `~/.zshrc`) under a distinct name:

```bash
export ROS_MAINTAINER_CONTAINER_GITHUB_TOKEN="github_pat_yourReadOnlyTokenHere"
```

When generating or editing `.devcontainer/devcontainer.json`, forward this variable into the container environment:

```json
"containerEnv": {
  "GITHUB_TOKEN": "${localEnv:ROS_MAINTAINER_CONTAINER_GITHUB_TOKEN}"
}
```

### Method B: Session `.env` File
You can also place a `.env` file in the session directory (`sessions/<session_id>/.env`):

```bash
GITHUB_TOKEN=github_pat_yourReadOnlyTokenHere
```

Docker and devcontainer runners will automatically load variables from `.env`.

---

## 5. What if No Token is Provided?

If you do not provide a token to the container:
- The agent can still clone and fetch public GitHub repositories via unauthenticated HTTPS (`https://github.com/ros2/rclcpp.git`).
- However, GitHub limits unauthenticated REST API requests to **60 requests per hour per IP address**.
- If the agent runs multiple API queries or inspects several PRs, it may hit rate-limiting errors (`403 API rate limit exceeded`).

Supplying a read-only fine-grained token increases this limit to **5,000 requests per hour** while keeping write operations blocked.

---

## 6. Token Security Checklist

- **Never mount `~/.config/gh` or `~/.git-credentials`** into the container.
- **Never mount host SSH private keys (`~/.ssh`)** into the container.
- **Use short expirations** (30–90 days) for container tokens and rotate them periodically.
- **Review audit logs**: Periodically inspect `audit/audit.jsonl` on the host to review what actions the gateway performed on behalf of the agent.
