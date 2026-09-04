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

---

## 2. Invariant Rules (Hardcoded Protections)

Certain rules are non-configurable invariants enforced directly in code:

1. **No Direct Base Branch Pushes**: Pushes to base branches (`main`, `master`, `rolling`, `jazzy`, `iron`, `humble`, etc.) are unconditionally rejected.
2. **No Conversational Commenting**: The gateway does not expose a tool for posting freeform comments on GitHub issues or PRs. This prevents the agent from hallucinating or impersonating the maintainer in public discussions.
3. **No Auto-Merging**: Merging pull requests is strictly reserved for the human maintainer.
4. **Approval Required for PR Creation**: Opening a pull request always requires an interactive maintainer approval ticket.

---

## 3. Configurable Policies (`config/policy.yaml`)

Policies are currently configured workspace-wide in `config/policy.yaml`. 

*(Note: In the current implementation, policies apply across all sessions in a workspace. A planned future enhancement is **per-session policy scoping**, which will allow constraining a specific session to only push to its target PR branch, preventing a session working on PR A from accidentally modifying branches for PR B).*

```yaml
version: 1

git:
  # Regex patterns for allowable feature branches to push
  allowed_branch_patterns:
    - "^wjwwood/.*$"
    - "^fix/.*$"
    - "^feature/.*$"
    - "^pr-[0-9]+$"

  # Restrict git push operations to specific GitHub organizations/repos
  allowed_repositories:
    - "ros2/*"
    - "ros-tooling/*"
    - "wjwwood/*"

  # Require interactive maintainer approval before pushing to 3rd-party contributor forks
  require_fork_approval: true

  # Require git push to use --force-with-lease instead of --force
  enforce_force_with_lease: true

jenkins:
  # Maximum concurrent Jenkins builds allowed per PR
  max_concurrent_runs_per_pr: 2

  # Cooldown period (in seconds) between triggering builds on the same PR
  cooldown_seconds: 300

  # Automatically cancel older running builds when a new build is launched for the same PR
  auto_cancel_superseded: true
```

### Checking Policy Compliance
You can test whether a hypothetical action complies with the policy:

```bash
ros-maintainer-harness policy check --branch wjwwood/my-feature --repo ros2/rclcpp
```

---

## 4. Maintainer Approval Workflow

For sensitive operations (such as pushing to an external contributor's fork or creating a PR), the gateway creates an approval ticket rather than immediately executing the request.

1. **Ticket Creation**: The gateway writes a pending ticket to `audit/approvals.json` and returns `PENDING_APPROVAL` with a unique request ID (e.g. `req-1234abcd`).
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
4. **Execution**: Once approved, the agent retries the operation, and the gateway executes it using the maintainer's host credentials.

---

## 5. Action Audit Logging (`audit/audit.jsonl`)

Every attempt to interact with a remote system (whether allowed, denied, or rejected) is recorded in `audit/audit.jsonl`.

### Mandatory Reason Strings
Every guarded tool (`git_push`, `launch_jenkins_ci`, `create_pull_request`) requires the agent to supply a `reason` string explaining why it is taking that action. Requests without a valid reason are rejected.

### Inspecting the Audit Trail
```bash
ros-maintainer-harness audit show -n 20
```

Each log record is a JSON object containing:
- `timestamp`: UTC ISO timestamp
- `session_id`: Originating session identifier
- `action`: Operation name (e.g. `git_push`, `launch_jenkins_ci`)
- `status`: Outcome (`ALLOWED`, `DENIED`, `REJECTED`, `PENDING_APPROVAL`, `APPROVED`)
- `target`: Target repository, branch, or job URL
- `reason`: Explanation provided by the agent
- `metadata`: Additional details (e.g. commit SHAs, author, approval ticket ID)

---

## 6. Maintainer Conventions (`config/maintainer_rules.md`)

In addition to programmatic policies, maintainers can document guidelines, conventions, and preferences in `config/maintainer_rules.md`.

This file is automatically mounted into every session container as `/workspace/MAINTAINER_RULES.md` (read-only) and can be queried by agents via the `get_maintainer_rules` MCP tool.

Common rules to include:
- **Local-First Verification**: Maximize local testing (building and executing affected unit and integration tests inside the container sandbox) before triggering remote Jenkins CI. Shared build farm infrastructure (`ci.ros2.org`) has limited capacity; running broken builds on CI wastes community resources.
- **AI Attribution**: Ensuring all AI assistance, models, and harnesses used are explicitly acknowledged in PR descriptions and commit metadata.
- **DCO Sign-off**: Requiring `Signed-off-by:` on all commits.
- **Diff Secret & Credential Scanning**: Directing the agent to inspect incoming PR diffs on first review for accidentally committed secrets, credentials, API tokens, or modified CI workflows, with the intent to notify the maintainer if anything suspicious is found.
- **Testing Requirements**: Requiring all bugfixes and new features to include regression unit tests.
- **Code Style**: Pointers to linters (`ament_flake8`, `ament_uncrustify`, `ament_cpplint`) and C++ standards.

---

## 7. GitHub Token Strategy (Host vs. Container)

A critical part of maintaining containment is isolating GitHub credentials:
- **The Host Gateway Token**: Holds write permissions (to push approved branches and create PRs). It stays strictly on the host.
- **The Container Token**: If provided to the agent, it should be a **strictly read-only, fine-grained Personal Access Token (PAT)** scoped only to the relevant repositories. Never pass your personal write token or `gh` CLI credentials into the container.

For detailed instructions on generating and configuring scoped tokens, see [GitHub Token Setup & Best Practices](github_tokens.md).
