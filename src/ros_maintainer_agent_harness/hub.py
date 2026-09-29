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

"""Maintainer Hub coordinator utilities for multi-session status, triage, and task conversations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .approval import ApprovalManager
from .ci import CITracker
from .devcontainer import (
    check_token_and_environment,
    get_container_status,
    start_session_container,
)
from .scaffolder import scaffold_session_from_pr
from .timeline import TimelineLogger
from .workspace import WorkspaceLayout
from .worktree import (
    get_session_metadata_path,
    read_session_metadata,
    SessionManager,
    write_session_metadata,
)

VALID_SESSION_STATUSES = (
    'active',
    'investigating',
    'local_tests_passing',
    'waiting_for_ci',
    'needs_review',
    'ready_to_merge',
    'blocked',
    'done',
)

UUID_PATTERN = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)

__all__ = [
    'VALID_SESSION_STATUSES',
    'build_task_conversation_prompt',
    'check_worktree_dirty',
    'find_agentapi_executable',
    'format_conversation_link',
    'format_session_dir_link',
    'get_next_actions',
    'get_session_metadata_path',
    'get_workspace_status',
    'parse_timeline_summary',
    'query_open_github_prs',
    'read_session_metadata',
    'start_session_conversation',
    'write_session_metadata',
]


def parse_timeline_summary(timeline_path: Path, max_entries: int = 5) -> Dict[str, Any]:
    """Extract the latest milestone and recent bullet entries from timeline.md."""
    if not timeline_path.exists():
        return {
            'latest_milestone': None,
            'recent_entries': [],
            'entry_count': 0,
        }

    entries: List[str] = []
    latest_milestone: Optional[str] = None
    try:
        lines = timeline_path.read_text(encoding='utf-8').splitlines()
        current_entry: List[str] = []
        for line in lines:
            if line.startswith('- **['):
                if current_entry:
                    entries.append(' '.join(current_entry))
                current_entry = [line[2:].strip()]
            elif current_entry and line.strip():
                current_entry.append(line.strip())
        if current_entry:
            entries.append(' '.join(current_entry))

        for entry in reversed(entries):
            if '**Milestone**:' in entry:
                idx = entry.find('**Milestone**:')
                latest_milestone = entry[idx + len('**Milestone**:'):].strip()
                break
    except Exception:
        pass

    return {
        'latest_milestone': latest_milestone,
        'recent_entries': entries[-max_entries:] if max_entries > 0 else [],
        'entry_count': len(entries),
    }


def check_worktree_dirty(src_dir: Path) -> Dict[str, bool]:
    """Return a map of repo subfolder name -> True if uncommitted changes exist."""
    dirty_map: Dict[str, bool] = {}
    if not src_dir.exists():
        return dirty_map
    for sub in sorted(src_dir.iterdir()):
        if sub.is_dir() and (sub / '.git').exists():
            res = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=str(sub),
                capture_output=True,
                text=True,
            )
            dirty_map[sub.name] = bool(res.returncode == 0 and res.stdout.strip())
    return dirty_map


def format_conversation_link(label: str, conversation_id: Optional[str]) -> Optional[str]:
    """Format a clickable conversation:// markdown link."""
    if not conversation_id:
        return None
    return f"[{label}](conversation://{conversation_id})"


def format_session_dir_link(label: str, session_dir: Path) -> str:
    """Format a clickable file:// markdown link to a session directory."""
    return f"[{label}](file://{session_dir.resolve()})"


def get_workspace_status(
    workspace: WorkspaceLayout,
    check_containers: bool = True,
) -> Dict[str, Any]:
    """
    Build a comprehensive dashboard of all sessions, containers, CI runs, and approvals
    in the maintainer workspace for the Hub agent or CLI.
    """
    env_report = check_token_and_environment(workspace.root)
    session_mgr = SessionManager(workspace)
    approval_mgr = ApprovalManager(workspace.audit_dir / 'approvals.json')
    ci_tracker = CITracker(workspace.audit_dir / 'ci_runs.json')

    pending_approvals = [r.to_dict() for r in approval_mgr.list_requests(status='PENDING')]
    all_ci_runs = [r.to_dict() for r in ci_tracker.list_runs(limit=50)]

    sessions_info = session_mgr.list_sessions()
    session_summaries: List[Dict[str, Any]] = []
    running_containers_count = 0

    for s in sessions_info:
        meta = read_session_metadata(s.session_dir)
        tl_summary = parse_timeline_summary(s.timeline_path, max_entries=3)
        git_dirty = check_worktree_dirty(s.src_dir)

        if check_containers:
            c_status = get_container_status(
                s.session_id,
                runtime=env_report.get('container_runtime'),
            )
        else:
            c_status = {
                'running': False,
                'status': 'unchecked',
                'container_name': f"ros-harness-{s.session_id}",
            }

        if c_status.get('running'):
            running_containers_count += 1

        latest_ci_obj = ci_tracker.get_latest_run_for_session(s.session_id)
        latest_ci = latest_ci_obj.to_dict() if latest_ci_obj else None
        sess_approvals = [a for a in pending_approvals if a.get('session_id') == s.session_id]

        conv_id = meta.get('conversation_id')
        conv_link = format_conversation_link(s.session_id, conv_id)
        dir_link = format_session_dir_link(s.session_id, s.session_dir)

        session_summaries.append({
            'session_id': s.session_id,
            'session_dir': str(s.session_dir),
            'session_dir_link': dir_link,
            'status': meta.get('status', 'active'),
            'topic': meta.get('topic') or meta.get('pr_title'),
            'distro': meta.get('distro') or s.distro or 'rolling',
            'pr_ref': meta.get('pr_ref'),
            'pr_url': meta.get('pr_url'),
            'pr_title': meta.get('pr_title'),
            'pr_author': meta.get('pr_author'),
            'conversation_id': conv_id,
            'conversation_link': conv_link,
            'hub_conversation_id': meta.get('hub_conversation_id'),
            'active_branches': s.active_branches,
            'git_dirty': git_dirty,
            'container_running': bool(c_status.get('running')),
            'container_name': c_status.get('container_name', f"ros-harness-{s.session_id}"),
            'latest_milestone': tl_summary['latest_milestone'],
            'recent_timeline': tl_summary['recent_entries'],
            'timeline_entry_count': tl_summary['entry_count'],
            'latest_ci_run': latest_ci,
            'pending_approvals': sess_approvals,
            'updated_at': meta.get('updated_at'),
        })

    active_ci_runs = [
        r for r in all_ci_runs if r.get('status') in ('PENDING', 'RUNNING')
    ]
    failed_ci_runs = [
        r for r in all_ci_runs if r.get('status') in ('FAILURE', 'UNSTABLE', 'ABORTED')
    ]

    return {
        'workspace_root': str(workspace.root),
        'environment': {
            'ready': env_report['ready'],
            'workspace_initialized': env_report['workspace_initialized'],
            'container_runtime': env_report['container_runtime'],
            'container_token_configured': env_report['container_token_configured'],
            'container_token_mode': env_report['container_token_mode'],
            'global_mcp_configured': env_report['global_mcp_configured'],
            'warnings': env_report['warnings'],
        },
        'summary': {
            'total_sessions': len(session_summaries),
            'running_containers': running_containers_count,
            'pending_approvals': len(pending_approvals),
            'active_ci_runs': len(active_ci_runs),
            'failed_ci_runs': len(failed_ci_runs),
        },
        'sessions': session_summaries,
        'pending_approvals': pending_approvals,
        'recent_ci_runs': all_ci_runs[:10],
    }


def query_open_github_prs(
    repos: Optional[List[str]] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Query GitHub via `gh` CLI for open Pull Requests requesting review or in target repositories.
    Fails gracefully and returns an empty list if `gh` is unavailable or unauthenticated.
    """
    if not shutil.which('gh'):
        return []

    candidates: List[Dict[str, Any]] = []
    seen_urls = set()

    # 1. Check PRs where review is requested from the authenticated maintainer
    try:
        res = subprocess.run(
            [
                'gh', 'search', 'prs',
                '--state=open',
                '--review-requested=@me',
                '--limit', str(limit),
                '--json', 'repository,number,title,url,updatedAt,author',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            items = json.loads(res.stdout)
            for item in items:
                url = item.get('url')
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                repo_info = item.get('repository') or {}
                repo_name = repo_info.get('nameWithOwner') or repo_info.get('name', '')
                num = item.get('number')
                author_info = item.get('author') or {}
                candidates.append({
                    'pr_ref': f"{repo_name}#{num}" if repo_name and num else url,
                    'pr_url': url,
                    'number': num,
                    'repo': repo_name,
                    'title': item.get('title', ''),
                    'author': author_info.get('login', ''),
                    'updated_at': item.get('updatedAt', ''),
                    'reason': 'Review requested from you',
                })
    except Exception:
        pass

    # 2. If specific repositories are provided, query their open PRs
    if repos:
        for repo in repos:
            if len(candidates) >= limit:
                break
            if '/' not in repo:
                continue
            try:
                res = subprocess.run(
                    [
                        'gh', 'pr', 'list',
                        '--repo', repo,
                        '--state', 'open',
                        '--limit', str(limit),
                        '--json', 'number,title,url,updatedAt,author,headRefName,baseRefName',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0 and res.stdout.strip():
                    items = json.loads(res.stdout)
                    for item in items:
                        url = item.get('url')
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        num = item.get('number')
                        author_info = item.get('author') or {}
                        candidates.append({
                            'pr_ref': f"{repo}#{num}",
                            'pr_url': url,
                            'number': num,
                            'repo': repo,
                            'title': item.get('title', ''),
                            'author': author_info.get('login', ''),
                            'base_ref': item.get('baseRefName', ''),
                            'head_ref': item.get('headRefName', ''),
                            'updated_at': item.get('updatedAt', ''),
                            'reason': f"Open PR in {repo}",
                        })
            except Exception:
                pass

    return candidates[:limit]


def get_next_actions(
    workspace: WorkspaceLayout,
    repos: Optional[List[str]] = None,
    include_github_prs: bool = True,
    limit_prs: int = 10,
) -> Dict[str, Any]:
    """
    Determine prioritized next actions for the maintainer across in-flight sessions,
    pending approvals, completed/failing CI runs, and open GitHub PRs.
    """
    ws_status = get_workspace_status(workspace, check_containers=True)
    actions: List[Dict[str, Any]] = []

    # Priority 0: Environment / Token Readiness
    env = ws_status['environment']
    if not env['ready'] or not env['container_token_configured']:
        actions.append({
            'priority': 'P0_ENVIRONMENT',
            'category': 'environment',
            'title': 'Complete workspace environment / GitHub token setup',
            'details': env['warnings'],
            'suggested_command': f"ros-maintainer-harness -w {workspace.root} doctor",
        })

    # Priority 1: Pending Approval Requests
    for req in ws_status['pending_approvals']:
        ticket_id = req.get('ticket_id')
        sess_id = req.get('session_id')
        actions.append({
            'priority': 'P1_APPROVAL_NEEDED',
            'category': 'approval',
            'session_id': sess_id,
            'ticket_id': ticket_id,
            'title': f"Approve or reject pending {req.get('action')} ticket {ticket_id} (session: {sess_id})",
            'details': f"Target: {req.get('target')} | Reason: {req.get('reason')}",
            'suggested_command': f"ros-maintainer-harness -w {workspace.root} approval approve {ticket_id}",
        })

    # Priority 2 & 3: Active Sessions & CI Outcomes
    active_pr_urls = set()
    active_pr_refs = set()

    for sess in ws_status['sessions']:
        sess_id = sess['session_id']
        status = sess.get('status', 'active')
        if sess.get('pr_url'):
            active_pr_urls.add(sess['pr_url'])
        if sess.get('pr_ref'):
            active_pr_refs.add(sess['pr_ref'])

        if status == 'done':
            continue

        ci_run = sess.get('latest_ci_run')
        conv_link = sess.get('conversation_link') or sess.get('session_dir_link') or sess_id
        milestone = sess.get('latest_milestone') or 'No milestone recorded yet'

        if ci_run:
            ci_status = ci_run.get('status')
            job_url = ci_run.get('job_url')
            if ci_status in ('FAILURE', 'UNSTABLE', 'ABORTED'):
                actions.append({
                    'priority': 'P2_CI_FAILURE',
                    'category': 'ci_failure',
                    'session_id': sess_id,
                    'session_dir_link': sess.get('session_dir_link'),
                    'conversation_link': sess.get('conversation_link'),
                    'title': f"Investigate {ci_status} CI run for {conv_link}",
                    'details': f"Job: {job_url} | Latest milestone: {milestone}",
                    'suggested_command': f"ros-maintainer-harness -w {workspace.root} ci summary {job_url}",
                })
                continue
            elif ci_status == 'SUCCESS':
                actions.append({
                    'priority': 'P2_CI_SUCCESS',
                    'category': 'ready_for_review',
                    'session_id': sess_id,
                    'session_dir_link': sess.get('session_dir_link'),
                    'conversation_link': sess.get('conversation_link'),
                    'title': f"CI passed for {conv_link}: ready for final review or merge",
                    'details': f"Job: {job_url} | Latest milestone: {milestone}",
                    'suggested_command': f"ros-maintainer-harness -w {workspace.root} session prune {sess_id}",
                })
                continue
            elif ci_status in ('PENDING', 'RUNNING'):
                actions.append({
                    'priority': 'P3_CI_RUNNING',
                    'category': 'ci_running',
                    'session_id': sess_id,
                    'session_dir_link': sess.get('session_dir_link'),
                    'conversation_link': sess.get('conversation_link'),
                    'title': f"Check running CI job for {conv_link} ({ci_status})",
                    'details': f"Job: {job_url}",
                    'suggested_command': f"ros-maintainer-harness -w {workspace.root} ci status {job_url}",
                })
                continue

        if status == 'blocked':
            actions.append({
                'priority': 'P1_SESSION_BLOCKED',
                'category': 'session_blocked',
                'session_id': sess_id,
                'session_dir_link': sess.get('session_dir_link'),
                'conversation_link': sess.get('conversation_link'),
                'title': f"Unblock session {conv_link}",
                'details': f"Topic: {sess.get('topic') or 'N/A'} | Latest milestone: {milestone}",
            })
        elif status in ('needs_review', 'ready_to_merge'):
            actions.append({
                'priority': 'P2_SESSION_REVIEW',
                'category': 'session_review',
                'session_id': sess_id,
                'session_dir_link': sess.get('session_dir_link'),
                'conversation_link': sess.get('conversation_link'),
                'title': f"Review findings in session {conv_link} (status: {status})",
                'details': f"Topic: {sess.get('topic') or 'N/A'} | Latest milestone: {milestone}",
            })
        else:
            actions.append({
                'priority': 'P3_SESSION_IN_PROGRESS',
                'category': 'session_active',
                'session_id': sess_id,
                'session_dir_link': sess.get('session_dir_link'),
                'conversation_link': sess.get('conversation_link'),
                'title': f"Continue investigation in session {conv_link} (status: {status})",
                'details': f"Topic: {sess.get('topic') or 'N/A'} | Latest milestone: {milestone}",
            })

    # Priority 4: Open GitHub PRs not yet in an active session
    candidate_prs: List[Dict[str, Any]] = []
    if include_github_prs:
        raw_prs = query_open_github_prs(repos=repos, limit=limit_prs)
        for pr in raw_prs:
            if pr.get('pr_url') in active_pr_urls or pr.get('pr_ref') in active_pr_refs:
                continue
            candidate_prs.append(pr)
            actions.append({
                'priority': 'P4_NEW_PR_TRIAGE',
                'category': 'github_pr',
                'pr_ref': pr.get('pr_ref'),
                'pr_url': pr.get('pr_url'),
                'title': f"Start review session for {pr.get('pr_ref')}: {pr.get('title')}",
                'details': f"Author: @{pr.get('author')} | {pr.get('reason')}",
                'suggested_command': (
                    f"ros-maintainer-harness -w {workspace.root} session from-pr {pr.get('pr_ref')}"
                ),
            })

    return {
        'workspace_root': str(workspace.root),
        'total_actions': len(actions),
        'actions': actions,
        'candidate_prs': candidate_prs,
        'workspace_summary': ws_status['summary'],
    }


def find_agentapi_executable() -> Optional[str]:
    """Locate the `agentapi` CLI executable if installed."""
    which_exe = shutil.which('agentapi')
    if which_exe:
        return which_exe
    try:
        for candidate in sorted(Path.home().glob('.gemini/*/bin/agentapi')):
            if candidate.is_file():
                return str(candidate)
    except Exception:
        pass
    return None


def build_task_conversation_prompt(
    session_id: str,
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    pr_ref: Optional[str] = None,
    pr_title: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    hub_conversation_id: Optional[str] = None,
) -> str:
    """Build the initial prompt for a dedicated session conversation or subagent."""
    ws_str = str(workspace_root.resolve())
    sess_str = str(session_dir.resolve())
    header = f"You are the dedicated ROS 2 Maintainer Task Agent for session `{session_id}`"
    if pr_ref:
        header += f" ({pr_ref}: {pr_title or ''})"
    header += "."

    hub_section = ""
    if hub_conversation_id:
        hub_section = f"""
5. **Report Back to the Maintainer Hub Conversation**:
   - This task was spawned from the Maintainer Hub conversation (`{hub_conversation_id}`).
   - When you finish your initial diff review, build, and test run (or if you encounter a blocker or need approval),
     send a concise status update back to the Hub conversation using `send_message`
     (`Recipient="{hub_conversation_id}"`) or `agentapi send-message "{hub_conversation_id}" "<summary>"`
     (if available in your agent environment).
"""

    extra_section = ""
    if extra_instructions and extra_instructions.strip():
        extra_section = f"\n## Additional Maintainer Instructions\n{extra_instructions.strip()}\n"

    return f"""{header}

## Session Environment
- **Session ID**: `{session_id}`
- **Session Directory (Use as `Cwd`)**: `{sess_str}`
- **Container Workspace Mount**: `/workspace` (inside `ros-harness-{session_id}`)
- **Workspace Root**: `{ws_str}`
- **Target ROS 2 Distro**: `{distro}`

## Mandatory Workflow
1. **Read Session Instructions & Task**:
   - View `{sess_str}/AGENTS.md` and `{sess_str}/TASK.md` first.
2. **Pre-Build Diff Security Check**:
   - Inspect the git diff inside `{sess_str}/src/` before compiling anything.
   - Check for committed secrets, modified `.github/workflows/`, or suspicious commands in `CMakeLists.txt`,
     `setup.py`, `package.xml`, or tests. If anything suspicious is found, halt immediately and alert the user.
3. **Containerized Command, Build & Test Execution**:
   - **Always set `Cwd` to `{sess_str}` (or a subdirectory inside `{sess_str}`).**
   - When the `PreToolUse` container-routing hook is active (or when running inside the Dev Container),
     all shell commands (`colcon build`, `colcon test`, `colcon test-result`, `git`, `gh`, `pytest`, `python3`)
     automatically execute inside `ros-harness-{session_id}` (`/workspace`) with ROS `{distro}` and the
     container's read-only `GITHUB_TOKEN` sourced. **Run commands directly without prefixing `session exec`:**
     ```bash
     colcon build --symlink-install --cmake-args -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
     colcon test --event-handlers console_direct+ --return-code-on-test-failure
     colcon test-result --all --verbose
     ```
   - *(Fallback if running in an agent without `PreToolUse` hooks)*: Use the MCP tool
     `exec_in_session(session_id="{session_id}", command="...")` or
     `ros-maintainer-harness -w {ws_str} session exec {session_id} -- "..."`.
4. **Log Milestones & Session Status**:
   - Record progress milestones to `timeline.md` using `log_status(session_id="{session_id}", ...)`
     (or `ros-session-status -m "Milestone" "Details"` inside the container).
   - Update the session state using `update_session_status(session_id="{session_id}", status="...")`
     (`investigating`, `local_tests_passing`, `waiting_for_ci`, `needs_review`, `ready_to_merge`, `blocked`, `done`).
{hub_section}{extra_section}"""


def start_session_conversation(
    workspace: WorkspaceLayout,
    pr_ref: Optional[str] = None,
    session_id: Optional[str] = None,
    distro: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    hub_conversation_id: Optional[str] = None,
    mode: str = 'auto',
    model: Optional[str] = None,
    auto_start_container: bool = True,
) -> Dict[str, Any]:
    """
    Scaffold (if needed) a session and start or prepare a dedicated task conversation for it.

    Args:
        workspace: Maintainer WorkspaceLayout.
        pr_ref: Optional PR reference (e.g. 'ros2/rclcpp#160') to scaffold if session doesn't exist.
        session_id: Optional session identifier.
        distro: Optional ROS distro override.
        extra_instructions: Optional specific instructions from the maintainer for this task.
        hub_conversation_id: Optional conversation ID of the calling Hub conversation.
        mode: 'auto' (uses `agentapi new-conversation` if available, else returns prompt for subagent),
              'agentapi' (launch top-level conversation via `agentapi`), or
              'prompt_only' (prepare session and return prompt for subagent delegation).
        model: Optional model tier for `agentapi new-conversation` ('flash_lite', 'flash', 'pro').
        auto_start_container: Pre-start the detached session container when environment is ready.
    """
    if not workspace.is_initialized():
        workspace.initialize()

    session_mgr = SessionManager(workspace)
    scaffolded = False

    if pr_ref:
        scaffold_res = scaffold_session_from_pr(
            workspace=workspace,
            pr_ref=pr_ref,
            session_id=session_id,
            distro=distro,
        )
        final_session_id = scaffold_res.session_id
        session_dir = scaffold_res.session_dir
        final_distro = scaffold_res.distro
        pr_title = scaffold_res.pr_metadata.title
        pr_shorthand = scaffold_res.pr_metadata.shorthand
        scaffolded = True
    elif session_id:
        if not session_mgr.session_exists(session_id):
            raise ValueError(f"Session '{session_id}' does not exist and no pr_ref was provided.")
        final_session_id = session_id
        session_dir = session_mgr.get_session_dir(session_id)
        meta = read_session_metadata(session_dir)
        final_distro = distro or meta.get('distro') or 'rolling'
        pr_title = meta.get('pr_title') or meta.get('topic')
        pr_shorthand = meta.get('pr_ref')
    else:
        raise ValueError("Either `pr_ref` or `session_id` must be provided.")

    effective_hub_id = hub_conversation_id or os.environ.get('ANTIGRAVITY_CONVERSATION_ID')
    if effective_hub_id:
        write_session_metadata(session_dir, {'hub_conversation_id': effective_hub_id})

    # Pre-start the session container if environment is ready so the task agent has an active container immediately
    container_info: Optional[Dict[str, Any]] = None
    if auto_start_container:
        env_check = check_token_and_environment(workspace.root)
        if env_check.get('ready'):
            container_info = start_session_container(
                session_id=final_session_id,
                session_dir=session_dir,
                workspace_root=workspace.root,
                distro=final_distro,
                runtime=env_check.get('container_runtime'),
            )

    task_prompt = build_task_conversation_prompt(
        session_id=final_session_id,
        session_dir=session_dir,
        workspace_root=workspace.root,
        distro=final_distro,
        pr_ref=pr_shorthand or pr_ref,
        pr_title=pr_title,
        extra_instructions=extra_instructions,
        hub_conversation_id=effective_hub_id,
    )

    title = f"[{final_session_id}] {pr_title}" if pr_title else f"Session: {final_session_id}"
    if len(title) > 80:
        title = title[:77] + '...'

    agentapi_bin = find_agentapi_executable()
    use_agentapi = (mode == 'agentapi') or (mode == 'auto' and agentapi_bin is not None)

    spawned_conv_id: Optional[str] = None
    launch_method = 'prompt_only'
    agentapi_output = None

    if use_agentapi and agentapi_bin:
        cmd = [agentapi_bin, 'new-conversation', f'--title={title}']
        if model in ('flash_lite', 'flash', 'pro'):
            cmd.append(f'--model={model}')
        cmd.append(task_prompt)

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        combined_out = (res.stdout or '') + '\n' + (res.stderr or '')
        agentapi_output = combined_out.strip()
        if res.returncode == 0:
            launch_method = 'agentapi'
            matches = UUID_PATTERN.findall(combined_out)
            for candidate_uuid in matches:
                if candidate_uuid != effective_hub_id:
                    spawned_conv_id = candidate_uuid
                    break

    updates: Dict[str, Any] = {'status': 'investigating'}
    if spawned_conv_id:
        updates['conversation_id'] = spawned_conv_id
    meta = write_session_metadata(session_dir, updates)

    timeline = TimelineLogger(final_session_id, session_dir, workspace.audit_log_path)
    if launch_method == 'agentapi':
        link_str = format_conversation_link(final_session_id, spawned_conv_id) or 'via agentapi'
        timeline.log_status(f"Started dedicated task conversation ({link_str}).")
    else:
        timeline.log_status("Prepared task prompt for dedicated session agent/subagent.")

    return {
        'success': True,
        'session_id': final_session_id,
        'session_dir': str(session_dir),
        'session_dir_link': format_session_dir_link(final_session_id, session_dir),
        'distro': final_distro,
        'scaffolded': scaffolded,
        'container': container_info,
        'launch_method': launch_method,
        'title': title,
        'conversation_id': spawned_conv_id,
        'conversation_link': format_conversation_link(final_session_id, spawned_conv_id),
        'hub_conversation_id': meta.get('hub_conversation_id'),
        'task_prompt': task_prompt,
        'agentapi_output': agentapi_output,
    }
