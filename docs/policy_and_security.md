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

### Why Containment Matters
- **Credential Theft / Leakage**: If an agent is tricked by a malicious pull request or repository (e.g., via prompt injection in an issue description or source code), it cannot exfiltrate host SSH keys or write tokens because they are not mounted into the container.
- **Accidental Side Effects**: The agent cannot run `git push origin main` or trigger endless Jenkins rebuilds because the container has no network credentials to do so. All remote requests must pass through the Host Gateway.

---

## 2. Invariant Rules (Hardcoded Protections)

Certain rules are non-configurable invariants enforced directly in code:

1. **No Direct Base Branch Pushes**: Pushes to base branches (`main`, `master`, `rolling`, `jazzy`, `iron`, `humble`, etc.) are unconditionally rejected.
2. **No Conversational Commenting**: The gateway does not expose a tool for posting freeform comments on GitHub issues or PRs. This prevents the agent from hallucinating or impersonating the maintainer in public discussions.
3. **No Auto-Merging**: Merging pull requests is strictly reserved for the human maintainer.
4. **Approval Required for PR Creation**: Opening a pull request always requires an interactive maintainer approval ticket.

---

## 3. Configurable Policies (`config/policy.yaml`)

Policies can be tuned per workspace in `config/policy.yaml`:

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

For sensitive operations—such as pushing to an external contributor's fork or creating a PR—the gateway creates an approval ticket rather than immediately executing the request.

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
- **DCO Sign-off**: Requiring `Signed-off-by:` on all commits.
- **AI Attribution**: Stating whether AI assistance should be acknowledged in PR descriptions.
- **Testing Requirements**: Requiring all new features to include unit tests.
- **Code Style**: Pointers to linters, uncrustify configs, and C++ standards.
