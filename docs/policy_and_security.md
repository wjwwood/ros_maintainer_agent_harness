# Policy, Approvals & Security Model

This document outlines the security architecture, safety policies, maintainer approval workflow, and audit logging mechanisms enforced by the Host Policy Gateway.

---

## 1. Trust Boundaries & Threat Model

The core objective of the harness is to allow an autonomous AI coding agent to edit code, compile packages, run tests, and diagnose issues without granting it unrestricted access to your credentials or remote repositories.

```
┌────────────────────────────────────────────────────────┐
│ CONTAINER SANDBOX (Untrusted / Autonomous Agent)       │
│ - Has root inside container                            │
│ - Has full local filesystem access to session workspace│
│ - Holds only Read-Only GitHub token (clone, fetch)     │
│ - Has NO SSH keys, NO GitHub write token, NO Jenkins pw│
└───────────────────────────┬────────────────────────────┘
                            │ MCP Requests with 'reason'
                            ▼
┌────────────────────────────────────────────────────────┐
│ HOST POLICY GATEWAY (Trusted / Maintainer Host)        │
│ - Holds SSH signing keys, GitHub write token, Jenkins  │
│ - Evaluates requests against config/policy.yaml        │
│ - Creates approval tickets for sensitive actions       │
│ - Writes tamper-evident audit trail (audit.jsonl)      │
└────────────────────────────────────────────────────────┘
```

### Containment & Threat Model Posture

This architecture is intended to raise the bar against accidental misuse and minimize credential exposure. It provides defense-in-depth, but no guarantees of absolute safety against intentional evasion or container escape vulnerabilities.

- **Limiting Credential Exposure**: Host SSH private keys and write tokens are kept on the host machine and are not mounted into the container. An agent operating inside the container has no direct access to host authentication material.
- **Accident Prevention**: The agent cannot run `git push origin main` or trigger runaway Jenkins rebuilds via simple shell commands, as the container lacks write credentials to do so. Read-only remote operations like `git fetch` or `git clone` can work directly (via public access or a scoped read-only token), but all mutating or write-like remote operations (such as pushing branches, triggering CI, or opening pull requests) must pass through the Host Gateway.
- **Untrusted PR Code Execution**: Pull requests from external contributors contain untrusted code. If a PR modifies build scripts (`CMakeLists.txt`, `setup.py`) or test cases, running `colcon build` or `colcon test` executes that code locally inside the container sandbox. While the sandbox lacks host write credentials, untrusted code could attempt to exfiltrate container tokens over the network or abuse local compute. For this reason, inspecting the PR diff must always occur *before* building or running tests, and any suspicious modifications must be reported to the maintainer immediately.

---

## 2. Invariant Rules (Hardcoded Protections)

Certain rules are non-configurable invariants enforced directly in code:

1. **No Direct Base Branch Pushes**: Pushes to base branches (`main`, `master`, `rolling`, `jazzy`, `iron`, `humble`, etc.) are unconditionally rejected unless performed via the explicit ticket-gated `release push` (`push_release`) workflow for a verified local version tag.
2. **No Conversational Commenting**: The gateway does not expose a tool for posting freeform comments on GitHub issues or PRs. This prevents the agent from hallucinating or impersonating the maintainer in public discussions.
3. **No Auto-Merging**: Merging pull requests is strictly reserved for the human maintainer.
4. **Pre-Filled Browser Review or Ticket Approval for PR Creation**: By default (`default_creation_mode: "web_url"`), `create_pull_request` returns a pre-filled GitHub `/compare/...` URL so the maintainer can inspect the diff and description in the browser before clicking "Create pull request". Submitting a PR directly via the GitHub REST API (`web_url=False` / `--api`) or editing an existing PR (`edit_pull_request`) always requires an interactive maintainer approval ticket.
5. **Read-Only `shared_repos/` Mount by Default**: Session containers mount `shared_repos/` read-only (`:ro` / `readonly` with `GIT_OPTIONAL_LOCKS=0`) by default so untrusted code inside one session cannot mutate shared parent `.git` directories or other sessions' worktree metadata. Mounting `shared_repos/` read-write requires explicit opt-in (`--writable-shared-repos` on `session up` / `session devcontainer` or `writable_shared_repos=True` on `start_session_container`).
6. **Per-Session PR Branch and Repository Push Scoping**: When a session is scaffolded from a pull request (`pr_ref`), pushing to any branch other than the PR's own `head_ref` (or a maintainer-prefixed branch `<github_username>/*`) or to an unrelated repository automatically requires an interactive maintainer approval ticket, even if the target matches workspace-wide `allowed_branch_patterns`.
7. **Session Host Subcommand Allowlist**: Inside session conversations, the `PreToolUse` hook restricts host passthrough of `ros-maintainer-harness` to a safe allowlist of status, CI, and ticket-request subcommands (`status`, `next`, `ci`, `push`, `create-pr`, `edit-pr`, `release`, `approval list`, `audit`, `policy`, `rules show`, `rules path`, `session status-log`, `session exec`). Privileged host administration subcommands (`approval approve`, `approval reject`, `token-setup`, `mcp-install`, `rules add`, `init`, `serve`) are blocked from session conversations.

---

## 3. Configurable Policies (`config/policy.yaml`)

Policies are configured workspace-wide in `config/policy.yaml`:

```yaml
version: "1.0"
maintainer:
  github_username: "wjwwood"

policies:
  git_push:
    # Regex patterns for allowable feature branches to push
    allowed_branch_patterns:
      - "^wjwwood/.*$"
      - "^fix/.*$"
      - "^feature/.*$"
      - "^backport/.*$"
      - "^pr-[0-9]+$"

    # Protected base branches (cannot be pushed via normal git_push)
    protected_base_branches:
      - "main"
      - "master"
      - "rolling"
      - "jazzy"
      - "iron"
      - "humble"
      - "kilted"
      - "lyrical"
      - "noetic"

    # Restrict git push operations to specific GitHub organizations/repos
    allowed_repositories:
      - "ros2/*"
      - "ros/*"
      - "ament/*"
      - "osrf/*"
      - "gazebosim/*"

    # Require git push to use --force-with-lease instead of --force
    require_force_with_lease: true
    allow_raw_force: false

    # Require interactive maintainer approval before pushing to 3rd-party contributor forks
    require_approval_for_external_forks: true

  jenkins_ci:
    ci_server: "https://ci.ros2.org"
    # Maximum concurrent Jenkins builds allowed per PR
    max_concurrent_runs_per_pr: 2
    # Cooldown period (in seconds) between triggering builds on the same PR
    cooldown_seconds: 300
    # Automatically cancel older running builds when a new build is launched for the same PR
    auto_cancel_superseded: true

  pull_request:
    # Default PR creation mode: "web_url" (pre-filled GitHub compare link) or "api"
    default_creation_mode: "web_url"
    # Require interactive maintainer approval when creating/editing PRs via the GitHub API
    require_maintainer_approval: true
    allow_auto_merge: false
    allow_freeform_comments: false
```

*(Note: Legacy top-level keys `git:` and `jenkins:`, as well as `require_fork_approval` and `enforce_force_with_lease`, are also accepted as aliases for backward compatibility).*

### Checking Policy Compliance
You can test whether a hypothetical action complies with the policy:

```bash
ros-maintainer-harness policy check --branch wjwwood/my-feature --repo ros2/rclcpp
```

---

## 4. Maintainer Approval Workflow

For sensitive operations (such as pushing to an external contributor's fork, pushing a release commit and tag, running `bloom-release`, or creating/editing a PR via the GitHub API), the gateway creates an approval ticket rather than immediately executing the request.

1. **Ticket Creation**: The gateway writes a pending ticket to `audit/approvals.json` and returns `PENDING_APPROVAL` with a unique request ID (e.g. `req-1234abcd`). Each ticket is strictly bound to its originating `session_id`, `action`, and `target`, expires after 24 hours by default, and is marked consumed (`consumed_at`) upon execution so it cannot be replayed or repurposed for another action.
2. **Reviewing Tickets**:
   ```bash
   ros-maintainer-harness approval list --status PENDING
   ```
3. **Approving or Rejecting**:
   ```bash
   # Approve with a review comment
   ros-maintainer-harness approval approve req-1234abcd --comment "Reviewed diff, approved to push"

   # Or reject
   ros-maintainer-harness approval reject req-1234abcd --comment "Commit message does not follow DCO"
   ```
4. **Execution**: Once approved, the agent retries the operation with `--ticket req-1234abcd`, and the gateway executes it using the maintainer's host credentials and marks the ticket consumed.

---

## 5. Action Audit Logging (`audit/audit.jsonl`)

Every attempt to interact with a remote system (whether allowed, denied, or rejected) is recorded in `audit/audit.jsonl` with a tamper-evident SHA-256 hash chain.

### Mandatory Reason Strings
Every guarded tool (`git_push`, `push_release`, `run_bloom_release`, `launch_jenkins_ci`, `create_pull_request`, `edit_pull_request`) requires the agent to supply a `reason` string explaining why it is taking that action. Requests without a valid reason are rejected.

### Inspecting the Audit Trail
```bash
ros-maintainer-harness audit show -n 20
```

Each log record is a JSON object containing:
- `timestamp`: UTC ISO timestamp
- `session_id`: Originating session identifier
- `action`: Operation name (e.g. `git_push`, `release_push`, `bloom_release`, `launch_jenkins_ci`)
- `status`: Outcome (`DENIED`, `REJECTED`, `PENDING_APPROVAL`, `WEB_URL_READY`, `APPROVED`, `FAILED`)
- `target`: Target repository, branch, or job URL
- `reason`: Explanation provided by the agent
- `details`: Additional details (e.g. `commit_sha`, `tag`, `ticket_id`, `output`)
- `prev_hash` & `record_hash`: SHA-256 hash chain linking each record to its predecessor

---

## 6. Maintainer Conventions (`config/maintainer_rules.md`)

In addition to programmatic policies, maintainers can document guidelines, conventions, and preferences in `config/maintainer_rules.md`.

This file is automatically mounted into every session container as `/workspace/MAINTAINER_RULES.md` (read-only) and can be queried by agents via the `get_maintainer_rules` MCP tool.

Common rules to include:
- **Local-First Verification**: Maximize local testing (building and executing affected unit and integration tests inside the container sandbox) before triggering remote Jenkins CI. Shared build farm infrastructure (`ci.ros2.org`) has limited capacity; running broken builds on CI wastes community resources.
- **AI Attribution**: Ensuring all AI assistance, models, and harnesses used are explicitly acknowledged in PR descriptions and commit metadata.
- **DCO Sign-off**: Requiring `Signed-off-by:` on all commits.
- **Pre-Build Diff Inspection & Security Scanning**: On initial review of any incoming PR or untrusted branch, the agent must inspect the diff *before* building changed code or running any tests. The inspection must verify:
  - No accidentally committed secrets, credentials, or API tokens.
  - No modified CI workflows (such as `.github/workflows/`) that could tamper with automation or attempt to dump repository/runner secrets.
  - No suspicious modifications to build scripts (`CMakeLists.txt`, `setup.py`, package manifests) or test code that could execute arbitrary commands, establish unauthorized network connections, or attempt to exfiltrate secrets during local `colcon build` or `colcon test` runs.
  If any suspicious or unexpected changes are found, the agent must halt immediately and notify the maintainer before executing any builds or tests.
- **Testing Requirements**: Requiring all bugfixes and new features to include regression unit tests.
- **Code Style**: Pointers to linters (`ament_flake8`, `ament_uncrustify`, `ament_cpplint`) and C++ standards.

---

## 7. GitHub Token Strategy (Host vs. Container)

A critical part of maintaining containment is isolating GitHub credentials:
- **The Host Gateway Token**: Holds write permissions (to push approved branches and create PRs). It stays strictly on the host.
- **The Container Token**: If provided to the agent, it should be a **strictly read-only, fine-grained Personal Access Token (PAT)** scoped only to the relevant repositories. Never pass your personal write token or `gh` CLI credentials into the container.

For detailed instructions on generating and configuring scoped tokens, see [GitHub Token Setup & Best Practices](github_tokens.md).
