# Copyright 2026 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess
from typing import Optional

from .pr_harvester import PRMetadata, fetch_pr_metadata
from .timeline import TimelineLogger
from .workspace import WorkspaceLayout
from .worktree import SessionManager, validate_session_id


@dataclasses.dataclass
class ScaffoldResult:
    session_id: str
    session_dir: Path
    pr_metadata: PRMetadata
    worktree_path: Path
    distro: str
    task_file: Path
    timeline_path: Path

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'session_dir': str(self.session_dir),
            'pr_url': self.pr_metadata.url,
            'pr_title': self.pr_metadata.title,
            'pr_author': self.pr_metadata.author,
            'base_ref': self.pr_metadata.base_ref,
            'head_ref': self.pr_metadata.head_ref,
            'distro': self.distro,
            'worktree_path': str(self.worktree_path),
            'task_file': str(self.task_file),
            'timeline_path': str(self.timeline_path),
            'changed_files': self.pr_metadata.changed_files,
        }


def generate_task_prompt(meta: PRMetadata, distro: str) -> str:
    """Generate structured markdown task instructions for AI coding agent."""
    files_section = "\n".join([f"- `{f}`" for f in meta.changed_files]) if meta.changed_files else "- *(See git diff)*"
    body_excerpt = meta.body.strip() if meta.body else "*(No description provided)*"

    return f"""# Maintainer Task: Investigate & Review PR #{meta.number}

## Target Information
- **Repository**: `{meta.owner}/{meta.repo}`
- **Pull Request**: [{meta.shorthand}]({meta.url})
- **Title**: {meta.title}
- **Author**: `@{meta.author}`
- **Target Distribution**: `{distro}` (Base Branch: `{meta.base_ref}`)
- **PR Head**: `{meta.head_ref}` ({'Fork from ' + meta.head_repo_owner if meta.is_fork else 'Upstream branch'})

---

## Changed Files
{files_section}

---

## PR Description
{body_excerpt}

---

## Agent Objectives & Guidelines
1. **Workspace Inspection & Security Review**:
   - Inspect the checked-out PR branch in `src/{meta.repo}` and review the diff against `origin/{meta.base_ref}`.
   - **Important**: Inspect the diff *before* building or running tests. Check for accidentally committed secrets, modified CI workflows, or suspicious changes in build scripts and test code (e.g. unexpected network calls or command execution). If anything suspicious is found, halt and notify the maintainer.

2. **Build & Local Testing**:
   - Build packages with colcon:
     ```bash
     colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=RelWithDebInfo
     ```
   - Run relevant unit tests:
     ```bash
     colcon test --event-handlers console_direct+
     colcon test-result --verbose
     ```

3. **CI Status & Token Conservation**:
   - Check existing CI job runs with `ros-ci-status` or the MCP `get_ci_status` tool.
   - If CI failed, retrieve concise error diagnostics with `get_ci_summary` instead of raw logs.

4. **Progress Logging**:
   - Record key milestones in `timeline.md` using `ros-session-status` or the MCP `log_status` tool:
     ```bash
     ros-session-status -m "Reproduced failure in test_timer" "Details here..."
     ```
"""


def ensure_shared_repo(
    workspace: WorkspaceLayout,
    meta: PRMetadata,
    clone_if_missing: bool = True,
) -> Path:
    """Ensure shared repository clone exists in shared_repos directory."""
    repo_dir = workspace.shared_repos_dir / meta.repo
    if repo_dir.exists() and (repo_dir / '.git').exists():
        return repo_dir

    if not clone_if_missing:
        raise FileNotFoundError(
            f"Repository '{meta.repo}' not found in {workspace.shared_repos_dir}. "
            "Please clone it or set clone_if_missing=True."
        )

    workspace.shared_repos_dir.mkdir(parents=True, exist_ok=True)
    clone_url = f"https://github.com/{meta.owner}/{meta.repo}.git"

    git_env = {
        **os.environ,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes',
    }

    res = subprocess.run(
        ['git', '-c', 'url.git@github.com:.insteadof=', 'clone', '--', clone_url, str(repo_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        env=git_env,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to clone repository from {clone_url}: {res.stderr}")

    return repo_dir


def fetch_pr_ref_in_repo(repo_dir: Path, meta: PRMetadata) -> str:
    """
    Fetch PR commit/branch into the shared repository. Returns local branch name.
    """
    local_branch = f"pr-{meta.number}"
    git_env = {
        **os.environ,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes',
    }

    # Try fetching GitHub PR head ref: pull/{number}/head:pr-{number}
    fetch_res = subprocess.run(
        [
            'git', '-c', 'url.git@github.com:.insteadof=', 'fetch',
            'origin', f"pull/{meta.number}/head:{local_branch}", '--force'
        ],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=60,
        env=git_env,
    )

    if fetch_res.returncode == 0:
        return local_branch

    # If pull ref fails (e.g. mocked git repos or private forks), attempt fetching head_repo_url / head_ref
    if meta.head_repo_url:
        fork_res = subprocess.run(
            [
                'git', '-c', 'url.git@github.com:.insteadof=', 'fetch',
                meta.head_repo_url, f"{meta.head_ref}:{local_branch}", '--force'
            ],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=git_env,
        )
        if fork_res.returncode == 0:
            return local_branch

    # Fallback to checking if branch already exists locally
    check_res = subprocess.run(
        ['git', 'rev-parse', '--verify', f"refs/heads/{local_branch}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        env=git_env,
    )
    if check_res.returncode == 0:
        return local_branch

    # Create local PR branch based on base_ref
    subprocess.run(
        ['git', 'branch', local_branch, meta.base_ref],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        env=git_env,
    )
    return local_branch


def scaffold_session_from_pr(
    workspace: WorkspaceLayout,
    pr_ref: str,
    session_id: Optional[str] = None,
    distro: Optional[str] = None,
    target_subfolder: Optional[str] = None,
    clone_if_missing: bool = True,
) -> ScaffoldResult:
    """
    Automate full session setup directly from a Pull Request reference:
    1. Harvests PR metadata.
    2. Clones/fetches PR branch into shared repository.
    3. Creates isolated session workspace and linked worktree.
    4. Generates devcontainer and MCP client configurations.
    5. Injects rich PR summary into timeline.md and writes TASK.md.
    """
    if not workspace.is_initialized():
        workspace.initialize()

    # 1. Harvest PR metadata
    meta = fetch_pr_metadata(pr_ref)

    # 2. Resolve identifiers
    final_session_id = validate_session_id(session_id or meta.suggested_session_id)
    final_distro = distro or meta.detected_distro

    # 3. Ensure repo exists and fetch PR ref
    repo_dir = ensure_shared_repo(workspace, meta, clone_if_missing=clone_if_missing)
    branch_name = fetch_pr_ref_in_repo(repo_dir, meta)

    # 4. Create session
    mgr = SessionManager(workspace)
    if not mgr.session_exists(final_session_id):
        mgr.create_session(
            session_id=final_session_id,
            topic=f"PR #{meta.number}: {meta.title}",
            distro=final_distro,
        )

    session_dir = mgr.get_session_dir(final_session_id)

    # 5. Attach worktree
    worktree_path = mgr.attach_worktree(
        session_id=final_session_id,
        repo_dir=repo_dir,
        branch_name=branch_name,
        base_ref=meta.base_ref,
        target_subfolder=target_subfolder,
    )

    # 6. Initialize rich timeline with PR details
    timeline = TimelineLogger(final_session_id, session_dir, workspace.audit_log_path)
    timeline_path = session_dir / 'timeline.md'
    timeline.log_status(
        f"🎯 Scaffolded session from **[{meta.shorthand}]({meta.url})**:\n"
        f"- **Title**: {meta.title}\n"
        f"- **Author**: `@{meta.author}`\n"
        f"- **Target Distro**: `{final_distro}` (base: `{meta.base_ref}`)\n"
        f"- **Worktree**: `{worktree_path.relative_to(session_dir)}`"
    )

    # 7. Write TASK.md prompt
    task_file = session_dir / 'TASK.md'
    task_file.write_text(generate_task_prompt(meta, final_distro), encoding='utf-8')

    return ScaffoldResult(
        session_id=final_session_id,
        session_dir=session_dir,
        pr_metadata=meta,
        worktree_path=worktree_path,
        distro=final_distro,
        task_file=task_file,
        timeline_path=timeline_path,
    )
