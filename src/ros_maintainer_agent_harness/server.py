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

from datetime import datetime, timezone
import functools
import inspect
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as MCPServer
    except ImportError:
        # Fallback dummy class if mcp package is missing in minimal environments
        class MCPServer:
            def __init__(self, name: str):
                self.name = name
                self._tools = {}

            def tool(self):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

from .approval import ApprovalManager
from .audit import append_audit_record
from .auth import (
    authorize_tool_call,
    caller_context,
    CallerIdentity,
    get_active_caller,
    TokenStore,
)
from .ci import CIMonitorService, CITracker, JenkinsManager
from .devcontainer import (
    check_token_and_environment,
    exec_in_session_container as do_exec_in_session_container,
    start_session_container as do_start_session_container,
    stop_session_container as do_stop_session_container,
)
from .gateway import (
    _TCPBridgeForwarder,
    detect_linux_docker_bridge_ip,
    get_harness_version,
    validate_gateway_bind_host,
)
from .git_ops import (
    execute_git_push,
    execute_release_push,
    extract_repo_full_name,
    get_current_commit_sha,
    get_repo_remote_url,
    git_safe_cmd,
    verify_release_tag,
)
from .hub import (
    format_conversation_link,
    format_session_dir_link,
    get_next_actions as do_get_next_actions,
    get_workspace_status as do_get_workspace_status,
    parse_timeline_summary,
    read_session_metadata,
    start_session_conversation as do_start_session_conversation,
    write_session_metadata,
)
from .mcp_config import get_agent_launch_info, write_session_mcp_configs
from .rules import MaintainerRules
from .scaffolder import scaffold_session_from_pr as do_scaffold_from_pr
from .timeline import TimelineLogger
from .workspace import WorkspaceLayout
from .worktree import SessionManager


def resolve_session_repo_path(session_dir: Path, repo_path: Optional[str] = None) -> Path:
    """
    Resolve a repository path that may be a container path (`/workspace/...`),
    a path relative to `session_dir` (or `session_dir / 'src'`), an absolute host path,
    or omitted (auto-detecting a single repository worktree under `session_dir / 'src'`).
    """
    if not repo_path or not repo_path.strip():
        src_dir = session_dir / 'src'
        if src_dir.is_dir():
            candidates = [
                p for p in sorted(src_dir.iterdir())
                if p.is_dir() and (p / '.git').exists()
            ]
            if len(candidates) == 1:
                return candidates[0].resolve()
            if len(candidates) > 1:
                meta = read_session_metadata(session_dir)
                pr_ref = str(meta.get('pr_ref') or '')
                if '#' in pr_ref and '/' in pr_ref:
                    primary_repo = pr_ref.split('#', 1)[0].split('/')[-1]
                    for cand in candidates:
                        if cand.name == primary_repo:
                            return cand.resolve()
        return session_dir.resolve()

    cleaned = repo_path.strip()
    if cleaned == '/workspace':
        return session_dir.resolve()
    if cleaned.startswith('/workspace/'):
        rel_part = cleaned[len('/workspace/'):]
        return (session_dir / rel_part).resolve()

    cand = Path(cleaned).expanduser()
    if not cand.is_absolute():
        if (session_dir / cand).exists():
            return (session_dir / cand).resolve()
        if (session_dir / 'src' / cand).exists():
            return (session_dir / 'src' / cand).resolve()
    return cand.resolve()


def perform_git_push(
    workspace: WorkspaceLayout,
    session_id: str,
    repo_path: Optional[str],
    branch: str,
    remote: str = 'origin',
    force_with_lease: bool = False,
    force: bool = False,
    reason: str = '',
    approval_ticket_id: Optional[str] = None,
    dry_run: bool = False,
    approval_mgr: Optional[ApprovalManager] = None,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute policy-validated git push with approval ticket gating and timeline/audit logging."""
    if tag and tag.strip():
        return perform_push_release(
            workspace=workspace,
            session_id=session_id,
            repo_path=repo_path,
            target_branch=branch,
            tag=tag.strip(),
            remote=remote,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
        )

    if not reason or not reason.strip():
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Mandatory parameter `reason` must not be empty.',
        }

    session_dir = workspace.sessions_dir / session_id
    repo_dir = resolve_session_repo_path(session_dir, repo_path)
    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    policy = workspace.get_policy()
    approval_mgr = approval_mgr or ApprovalManager(workspace.audit_dir / 'approvals.json')

    meta = read_session_metadata(session_dir)
    if meta.get('is_fork') and meta.get('head_repo_url'):
        if remote == 'origin' and branch == meta.get('head_ref'):
            remote = 'fork'
        if get_repo_remote_url(repo_dir, remote) is None and remote in ('fork', meta.get('head_repo_owner')):
            import subprocess as _sp
            _sp.run(
                git_safe_cmd(repo_dir, 'remote', 'add', '--', remote, meta['head_repo_url']),
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )

    remote_url = get_repo_remote_url(repo_dir, remote)
    repo_full_name = extract_repo_full_name(remote_url)
    target_label = f"{repo_full_name or str(repo_dir)}:{branch}"
    commit_sha = get_current_commit_sha(repo_dir)

    # Policy validation
    allowed, msg, requires_approval = policy.validate_git_push(
        branch_name=branch,
        repo_full_name=repo_full_name,
        force_with_lease=force_with_lease,
        force=force,
    )

    if not allowed:
        if 'protected' in msg.lower():
            msg = (
                f"{msg} If you are pushing a 'catkin_prepare_release --no-push' release commit "
                f"and version tag, use 'ros-maintainer-harness release push -s {session_id} "
                f"-b {branch} -t <tag> -m \"...\"' (or MCP 'push_release')."
            )
        timeline.log_action(
            action='git_push',
            target=target_label,
            reason=reason,
            status='DENIED',
            details={'error': msg, 'force_with_lease': force_with_lease, 'commit_sha': commit_sha},
        )
        return {
            'success': False,
            'status': 'DENIED',
            'error': msg,
        }

    # Per-session PR branch & repository scoping:
    # Require maintainer approval if a PR-scaffolded session attempts to push to a repository or branch
    # other than its own PR head branch or a maintainer-prefixed branch (<github_username>/*).
    pr_ref_meta = str(meta.get('pr_ref') or '').strip()
    session_head_ref = str(meta.get('head_ref') or '').strip()
    if not requires_approval and (pr_ref_meta or session_head_ref):
        session_base_repo = str(meta.get('base_repo') or '').strip()
        if not session_base_repo and '#' in pr_ref_meta:
            session_base_repo = pr_ref_meta.split('#', 1)[0].strip()
        session_repo_name = session_base_repo.split('/')[-1] if '/' in session_base_repo else ''
        session_head_owner = str(meta.get('head_repo_owner') or '').strip()
        gh_user = (policy.github_username or '').strip()

        is_maintainer_branch = bool(gh_user and branch.lower().startswith(f"{gh_user.lower()}/"))
        branch_matches_session = bool(
            (session_head_ref and branch == session_head_ref)
            or is_maintainer_branch
        )

        repo_matches_session = True
        if repo_full_name and session_base_repo:
            allowed_session_repos = {session_base_repo.lower()}
            if session_repo_name:
                if session_head_owner:
                    allowed_session_repos.add(f"{session_head_owner.lower()}/{session_repo_name.lower()}")
                if gh_user:
                    allowed_session_repos.add(f"{gh_user.lower()}/{session_repo_name.lower()}")
            repo_matches_session = (repo_full_name.lower() in allowed_session_repos)

        if not branch_matches_session or not repo_matches_session:
            requires_approval = True
            scope_desc = pr_ref_meta or session_head_ref
            msg = (
                f"Session '{session_id}' is scoped to PR '{scope_desc}' "
                f"(head branch '{session_head_ref or 'unset'}'); "
                f"pushing to '{target_label}' requires maintainer approval."
            )

    # Check approval if required (e.g. external contributor fork or cross-branch/repo push from PR session)
    if requires_approval:
        ticket_valid = bool(
            approval_ticket_id
            and approval_mgr.is_approved(
                approval_ticket_id,
                action='git_push',
                target=target_label,
                session_id=session_id,
                allow_consumed=dry_run,
            )
        )
        if not ticket_valid:
            req = approval_mgr.create_request(
                session_id=session_id,
                action='git_push',
                target=target_label,
                reason=reason,
                details={
                    'remote': remote,
                    'force_with_lease': force_with_lease,
                    'force': force,
                    'commit_sha': commit_sha,
                },
            )
            timeline.log_action(
                action='git_push',
                target=target_label,
                reason=reason,
                status='PENDING_APPROVAL',
                details={'ticket_id': req.ticket_id, 'info': msg, 'commit_sha': commit_sha},
            )
            return {
                'success': False,
                'status': 'PENDING_APPROVAL',
                'ticket_id': req.ticket_id,
                'message': f"{msg} Created approval ticket '{req.ticket_id}'.",
            }

    # Repair any root-owned .git refs inside a running session container before host push
    _repair_session_repo_ownership_if_needed(session_id, repo_dir, remote)

    # Execute push
    success, out = execute_git_push(
        repo_dir=repo_dir,
        branch=branch,
        remote=remote,
        force_with_lease=force_with_lease,
        force=force,
        dry_run=dry_run,
    )
    if requires_approval and approval_ticket_id and success and not dry_run:
        approval_mgr.consume_ticket(approval_ticket_id)

    status_str = 'APPROVED' if success else 'FAILED'
    timeline.log_action(
        action='git_push',
        target=target_label,
        reason=reason,
        status=status_str,
        details={
            'output': out,
            'force_with_lease': force_with_lease,
            'force': force,
            'commit_sha': commit_sha,
            'dry_run': dry_run,
        },
    )

    return {
        'success': success,
        'status': status_str,
        'target': target_label,
        'commit_sha': commit_sha,
        'output': out,
        'dry_run': dry_run,
    }


def _repair_session_repo_ownership_if_needed(session_id: str, repo_dir: Path, remote: str = 'origin') -> None:
    """
    If `.git/refs/remotes/<remote>` in `repo_dir` is owned by root (from a container command)
    and the session container is running, restore host user ownership before pushing on the host.
    """
    import os
    from .devcontainer import exec_in_session_container, get_container_status
    from .git_ops import is_remote_tracking_writable

    if not session_id or is_remote_tracking_writable(repo_dir, remote):
        return
    if not (hasattr(os, 'getuid') and hasattr(os, 'getgid') and os.getuid() != 0):
        return
    try:
        st = get_container_status(session_id)
        if st.get('running'):
            uid = os.getuid()
            gid = os.getgid()
            exec_in_session_container(
                session_id=session_id,
                command=f"find /workspace/src -user 0 -exec chown -h {uid}:{gid} {{}} + 2>/dev/null || true",
                auto_start=False,
                timeout=30,
            )
    except Exception:
        pass


def perform_push_release(
    workspace: WorkspaceLayout,
    session_id: str,
    repo_path: Optional[str],
    target_branch: str,
    tag: str,
    remote: str = 'origin',
    reason: str = '',
    approval_ticket_id: Optional[str] = None,
    dry_run: bool = False,
    approval_mgr: Optional[ApprovalManager] = None,
) -> Dict[str, Any]:
    """
    Push a local release commit (created by `catkin_prepare_release --no-push`) and its version tag
    to the upstream distro branch (`target_branch`) and `refs/tags/<tag>`.
    Always requires an explicit maintainer approval ticket (`action='release_push'`).
    """
    if not reason or not reason.strip():
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Mandatory parameter `reason` must not be empty.',
        }

    cleaned_branch = (target_branch or '').strip()
    cleaned_tag = (tag or '').strip()
    if not cleaned_branch or not cleaned_tag:
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Both `target_branch` (e.g. "rolling") and `tag` (e.g. "3.10.2") must be provided.',
        }

    session_dir = workspace.sessions_dir / session_id
    repo_dir = resolve_session_repo_path(session_dir, repo_path)
    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    policy = workspace.get_policy()
    approval_mgr = approval_mgr or ApprovalManager(workspace.audit_dir / 'approvals.json')

    remote_url = get_repo_remote_url(repo_dir, remote)
    repo_full_name = extract_repo_full_name(remote_url)
    target_label = f"{repo_full_name or str(repo_dir)}:{cleaned_branch} (tag {cleaned_tag})"

    if policy.git_push.allowed_repositories:
        repo_allowed = bool(repo_full_name and (
            policy.is_repository_allowed(repo_full_name)
            or (policy.github_username and repo_full_name.startswith(f"{policy.github_username}/"))
        ))
        if not repo_allowed:
            msg = (
                f"Repository '{repo_full_name}' is not in policy allowed_repositories "
                f"{policy.git_push.allowed_repositories}."
            )
            timeline.log_action(
                action='release_push',
                target=target_label,
                reason=reason,
                status='DENIED',
                details={'error': msg},
            )
            return {'success': False, 'status': 'DENIED', 'error': msg}

    valid_tag, tag_msg, tag_commit_sha = verify_release_tag(repo_dir, cleaned_tag)
    if not valid_tag or not tag_commit_sha:
        timeline.log_action(
            action='release_push',
            target=target_label,
            reason=reason,
            status='REJECTED',
            details={'error': tag_msg},
        )
        return {'success': False, 'status': 'REJECTED', 'error': tag_msg}

    ticket_ok = bool(
        approval_ticket_id
        and approval_mgr.is_approved(
            approval_ticket_id,
            action='release_push',
            target=target_label,
            session_id=session_id,
            allow_consumed=dry_run,
        )
    )

    if not ticket_ok:
        req = approval_mgr.create_request(
            session_id=session_id,
            action='release_push',
            target=target_label,
            reason=reason,
            details={
                'repo': repo_full_name,
                'remote': remote,
                'target_branch': cleaned_branch,
                'tag': cleaned_tag,
                'commit_sha': tag_commit_sha,
            },
        )
        timeline.log_action(
            action='release_push',
            target=target_label,
            reason=reason,
            status='PENDING_APPROVAL',
            details={
                'ticket_id': req.ticket_id,
                'commit_sha': tag_commit_sha,
                'tag': cleaned_tag,
            },
        )
        return {
            'success': False,
            'status': 'PENDING_APPROVAL',
            'ticket_id': req.ticket_id,
            'target': target_label,
            'commit_sha': tag_commit_sha,
            'message': (
                f"Pushing release commit ({tag_commit_sha[:8]}) and tag '{cleaned_tag}' to "
                f"'{repo_full_name}:{cleaned_branch}' requires maintainer approval. "
                f"Created approval ticket '{req.ticket_id}'."
            ),
        }

    _repair_session_repo_ownership_if_needed(session_id, repo_dir, remote)

    success, out = execute_release_push(
        repo_dir=repo_dir,
        target_branch=cleaned_branch,
        tag=cleaned_tag,
        remote=remote,
        dry_run=dry_run,
    )
    if approval_ticket_id and success and not dry_run:
        approval_mgr.consume_ticket(approval_ticket_id)

    status_str = 'APPROVED' if success else 'FAILED'
    timeline.log_action(
        action='release_push',
        target=target_label,
        reason=reason,
        status=status_str,
        details={
            'output': out,
            'tag': cleaned_tag,
            'commit_sha': tag_commit_sha,
            'dry_run': dry_run,
        },
    )

    return {
        'success': success,
        'status': status_str,
        'target': target_label,
        'repo': repo_full_name,
        'target_branch': cleaned_branch,
        'tag': cleaned_tag,
        'commit_sha': tag_commit_sha,
        'output': out,
        'dry_run': dry_run,
    }


def perform_launch_jenkins_ci(
    workspace: WorkspaceLayout,
    session_id: str,
    pr_url: str = '',
    target_distro: Optional[str] = None,
    job_type: Optional[str] = None,
    only_fixes_test: bool = False,
    packages: Optional[List[str]] = None,
    colcon_build_args: Optional[str] = None,
    colcon_test_args: Optional[str] = None,
    cmake_args: Optional[str] = None,
    extra_repos: Optional[List[str]] = None,
    comment: bool = False,
    reason: str = '',
    approval_ticket_id: Optional[str] = None,
    dry_run: bool = False,
    approval_mgr: Optional[ApprovalManager] = None,
    ci_tracker: Optional[CITracker] = None,
) -> Dict[str, Any]:
    """Launch a Jenkins CI run on ci.ros2.org with rate-limiting, cooldown, and audit logging."""
    if not reason or not reason.strip():
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Mandatory parameter `reason` must not be empty.',
        }

    session_dir = workspace.sessions_dir / session_id
    meta = read_session_metadata(session_dir) if session_dir.is_dir() else {}
    if not pr_url or not pr_url.strip():
        pr_url = str(meta.get('pr_url') or meta.get('pr_ref') or '').strip()
    if not pr_url and session_dir.is_dir():
        discovered_pr = _discover_open_pr_for_session(session_dir, meta)
        if discovered_pr:
            pr_url = discovered_pr
            meta = read_session_metadata(session_dir)
    if not pr_url:
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'PR URL or shorthand could not be resolved from session metadata; please pass `pr_url`.',
        }
    if not target_distro and meta.get('distro'):
        target_distro = str(meta['distro'])
    if not packages and session_dir.is_dir():
        from .ci import detect_session_packages
        detected_pkgs = detect_session_packages(session_dir)
        if detected_pkgs:
            packages = detected_pkgs

    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    policy = workspace.get_policy()
    approval_mgr = approval_mgr or ApprovalManager(workspace.audit_dir / 'approvals.json')
    ci_tracker = ci_tracker or CITracker(workspace.audit_dir / 'ci_runs.json')

    active_count = ci_tracker.get_active_runs_count(pr_url)
    since_last = ci_tracker.get_seconds_since_last_run(pr_url)

    allowed, msg, _ = policy.validate_jenkins_ci(
        pr_url=pr_url,
        active_runs_count=active_count,
        seconds_since_last_run=since_last,
    )

    if not allowed:
        ticket_valid = bool(
            approval_ticket_id
            and approval_mgr.is_approved(
                approval_ticket_id,
                action='launch_jenkins_ci',
                target=pr_url,
                session_id=session_id,
                allow_consumed=dry_run,
            )
        )
        if not ticket_valid:
            req = approval_mgr.create_request(
                session_id=session_id,
                action='launch_jenkins_ci',
                target=pr_url,
                reason=reason,
                details={'active_count': active_count, 'cooldown_info': msg},
            )
            timeline.log_action(
                action='launch_jenkins_ci',
                target=pr_url,
                reason=reason,
                status='RATE_LIMITED',
                details={'ticket_id': req.ticket_id, 'error': msg},
            )
            return {
                'success': False,
                'status': 'RATE_LIMITED',
                'error': msg,
                'ticket_id': req.ticket_id,
            }

    jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)
    res = jenkins_mgr.launch_ci(
        session_id=session_id,
        pr_url=pr_url,
        target_distro=target_distro,
        job_type=job_type,
        only_fixes_test=only_fixes_test,
        packages=packages,
        colcon_build_args=colcon_build_args,
        colcon_test_args=colcon_test_args,
        cmake_args=cmake_args,
        extra_repos=extra_repos,
        comment=comment,
        dry_run=dry_run,
    )
    if not allowed and approval_ticket_id and not dry_run:
        approval_mgr.consume_ticket(approval_ticket_id)

    timeline.log_action(
        action='launch_jenkins_ci',
        target=pr_url,
        reason=reason,
        status='APPROVED',
        details=res,
    )

    return {
        'success': True,
        'status': 'APPROVED',
        'job_url': res.get('job_url'),
        'build_num': res.get('build_num'),
        'gist_url': res.get('gist_url'),
        'child_jobs': res.get('child_jobs'),
        'comment_markdown': res.get('comment_markdown'),
        'comment_url': res.get('comment_url'),
        'packages': res.get('packages'),
        'details': res,
    }


def perform_find_restarted_ci(
    workspace: WorkspaceLayout,
    session_id: str,
    pr_or_comment_url: str,
    update_comment: bool = False,
    reason: Optional[str] = None,
    dry_run: bool = False,
    ci_tracker: Optional[CITracker] = None,
) -> Dict[str, Any]:
    """Check for rescheduled or restarted Jenkins builds and optionally update PR comment markdown."""
    session_dir = workspace.sessions_dir / session_id
    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    policy = workspace.get_policy()
    ci_tracker = ci_tracker or CITracker(workspace.audit_dir / 'ci_runs.json')

    jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)
    res = jenkins_mgr.find_restarted_ci(
        pr_or_comment_url=pr_or_comment_url,
        update_comment=update_comment,
        dry_run=dry_run,
    )

    if update_comment:
        timeline.log_action(
            action='find_restarted_ci',
            target=pr_or_comment_url,
            reason=reason or 'Discovered rescheduled Jenkins build; updated PR comment.',
            status='APPROVED',
            details=res,
        )
    else:
        timeline.log_status(f"Inspected restarted CI for `{pr_or_comment_url}`.")

    return res


def resolve_session_file_path(session_dir: Path, file_path: str) -> Path:
    """
    Resolve a file path that may be a container path (`/workspace/...`),
    a path relative to `session_dir` (or the single worktree in `session_dir/src`),
    or an absolute host path.
    """
    cleaned = file_path.strip()
    if cleaned == '/workspace':
        return session_dir.resolve()
    if cleaned.startswith('/workspace/'):
        rel_part = cleaned[len('/workspace/'):]
        return (session_dir / rel_part).resolve()

    cand = Path(cleaned).expanduser()
    if not cand.is_absolute():
        if (session_dir / cand).exists():
            return (session_dir / cand).resolve()
        repo_dir = resolve_session_repo_path(session_dir)
        if (repo_dir / cand).exists():
            return (repo_dir / cand).resolve()
        return (session_dir / cand).resolve()
    return cand.resolve()


def check_pr_body_template(repo: str, body: str) -> List[str]:
    """
    Check whether a PR description body includes the expected PR template sections
    and a concise Generative AI attribution.
    """
    warnings: List[str] = []
    text = (body or '').strip()
    if not text:
        warnings.append(
            "PR body is empty. Always fill out the target repo/org PR template and include "
            "'### Did you use Generative AI?' with a concise attribution (e.g. 'Yes, Claude Opus 5.5')."
        )
        return warnings

    lower_text = text.lower()
    if 'did you use generative ai' not in lower_text and 'generative ai' not in lower_text:
        warnings.append(
            "Missing '### Did you use Generative AI?' section in PR body. "
            "Always include a concise attribution (e.g. 'Yes, Claude Opus 5.5' or 'Yes, Gemini')."
        )

    if repo and repo.lower().startswith('ros2/'):
        required_headings = (
            '## Description',
            '### Is this user-facing behavior change?',
            '### Did you use Generative AI?',
        )
        missing = [h for h in required_headings if h.lower() not in lower_text]
        if missing:
            warnings.append(
                f"PR body for '{repo}' is missing ros2/.github PR template section(s): {', '.join(missing)}."
            )
    return warnings


def _resolve_host_github_token() -> str:
    """Resolve a host GitHub token for creating or editing Pull Requests."""
    import os
    import subprocess

    token = (
        os.environ.get('ROS_HOST_GITHUB_TOKEN')
        or os.environ.get('ROS_CI_GITHUB_TOKEN')
        or os.environ.get('GITHUB_ACCESS_TOKEN')
        or os.environ.get('GITHUB_TOKEN')
        or os.environ.get('GH_TOKEN')
        or ''
    ).strip()

    if not token:
        try:
            res = subprocess.run(
                ['gh', 'auth', 'token'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0 and res.stdout.strip():
                token = res.stdout.strip()
        except Exception:
            pass
    return token


def _execute_github_create_pr(
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> Dict[str, Any]:
    """Create a Pull Request on GitHub via the REST API on the host."""
    import requests

    token = _resolve_host_github_token()
    if not token:
        return {
            'success': False,
            'error': 'No host GitHub authentication token available to create Pull Request.',
        }

    url = f'https://api.github.com/repos/{repo}/pulls'
    try:
        resp = requests.post(
            url,
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github+json',
            },
            json={
                'title': title,
                'body': body or '',
                'head': head,
                'base': base,
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                'success': True,
                'pr_url': data.get('html_url', f'https://github.com/{repo}/pull/{data.get("number")}'),
                'pr_number': data.get('number'),
            }
        try:
            err_json = resp.json()
            msg = err_json.get('message', f'HTTP {resp.status_code}')
            errors = err_json.get('errors')
            if errors:
                msg = f"{msg} ({errors})"
        except Exception:
            msg = resp.text[:300] or f'HTTP {resp.status_code}'
        return {
            'success': False,
            'error': f'GitHub API HTTP {resp.status_code}: {msg}',
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'GitHub API request failed: {e}',
        }


def _execute_github_edit_pr(
    repo: str,
    pr_number: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing Pull Request title and/or body on GitHub via the REST API on the host."""
    import requests

    token = _resolve_host_github_token()
    if not token:
        return {
            'success': False,
            'error': 'No host GitHub authentication token available to edit Pull Request.',
        }

    payload: Dict[str, Any] = {}
    if title is not None:
        payload['title'] = title
    if body is not None:
        payload['body'] = body

    url = f'https://api.github.com/repos/{repo}/pulls/{pr_number}'
    try:
        resp = requests.patch(
            url,
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github+json',
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                'success': True,
                'pr_url': data.get('html_url', f'https://github.com/{repo}/pull/{pr_number}'),
                'pr_number': data.get('number', pr_number),
                'title': data.get('title', title),
            }
        try:
            err_json = resp.json()
            msg = err_json.get('message', f'HTTP {resp.status_code}')
            errors = err_json.get('errors')
            if errors:
                msg = f"{msg} ({errors})"
        except Exception:
            msg = resp.text[:300] or f'HTTP {resp.status_code}'
        return {
            'success': False,
            'error': f'GitHub API HTTP {resp.status_code}: {msg}',
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'GitHub API request failed: {e}',
        }


def build_github_compare_pr_url(
    repo: str,
    base: str,
    head: str,
    title: str,
    body: str = '',
    max_url_length: int = 6000,
) -> Dict[str, Any]:
    """
    Build a pre-filled GitHub '/compare/<base>...<head>?quick_pull=1&title=...&body=...' URL
    so the maintainer can review the diff, title, and description in the browser before clicking
    'Create pull request'.
    """
    from urllib.parse import quote, urlencode

    repo_clean = repo.strip().strip('/')
    repo_parts = repo_clean.split('/')
    repo_owner = repo_parts[0] if len(repo_parts) >= 2 else ''
    repo_name = repo_parts[1] if len(repo_parts) >= 2 else repo_clean

    base_clean = (base or 'rolling').strip()
    head_clean = (head or '').strip()

    if ':' in head_clean:
        h_parts = head_clean.split(':')
        if len(h_parts) == 2:
            head_owner, head_branch = h_parts[0].strip(), h_parts[1].strip()
            if repo_owner and head_owner.lower() == repo_owner.lower():
                head_spec = quote(head_branch, safe='/')
            else:
                head_spec = f"{quote(head_owner, safe='')}:{quote(repo_name, safe='')}:{quote(head_branch, safe='/')}"
        else:
            head_owner = h_parts[0].strip()
            head_repo = h_parts[1].strip()
            head_branch = ':'.join(h_parts[2:]).strip()
            head_spec = f"{quote(head_owner, safe='')}:{quote(head_repo, safe='')}:{quote(head_branch, safe='/')}"
    else:
        head_spec = quote(head_clean, safe='/')

    base_spec = quote(base_clean, safe='/')
    compare_base_url = f"https://github.com/{repo_clean}/compare/{base_spec}...{head_spec}"

    params_full = {'quick_pull': '1', 'title': title}
    if body:
        params_full['body'] = body
    full_url = f"{compare_base_url}?{urlencode(params_full, quote_via=quote)}"

    if body and len(full_url) > max_url_length:
        params_short = {'quick_pull': '1', 'title': title}
        short_url = f"{compare_base_url}?{urlencode(params_short, quote_via=quote)}"
        return {
            'compare_url': short_url,
            'body_truncated_from_url': True,
        }

    return {
        'compare_url': full_url,
        'body_truncated_from_url': False,
    }


def _discover_open_pr_for_session(session_dir: Path, meta: Dict[str, Any]) -> Optional[str]:
    """
    Attempt to discover an open GitHub PR for a session whose PR was created by the maintainer
    in the browser via a pre-filled compare URL.
    """
    import requests
    from .git_ops import get_current_branch

    repo = str(meta.get('pending_pr_repo') or '').strip()
    head = str(meta.get('pending_pr_head') or '').strip()

    if not repo or not head:
        repo_dir = resolve_session_repo_path(session_dir)
        if (repo_dir / '.git').exists():
            if not repo:
                repo = extract_repo_full_name(get_repo_remote_url(repo_dir, 'origin')) or ''
            if not head:
                head = get_current_branch(repo_dir) or ''

    if not repo or '/' not in repo or not head:
        return None

    repo_owner = repo.split('/', 1)[0]
    if ':' in head:
        h_parts = head.split(':')
        head_query = f"{h_parts[0]}:{h_parts[-1]}"
    else:
        head_query = f"{repo_owner}:{head}"

    token = _resolve_host_github_token()
    headers = {'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'token {token}'

    try:
        resp = requests.get(
            f'https://api.github.com/repos/{repo}/pulls',
            headers=headers,
            params={'state': 'open', 'head': head_query},
            timeout=15,
        )
        if resp.status_code == 200:
            pulls = resp.json()
            if isinstance(pulls, list) and pulls:
                pr_data = pulls[0]
                pr_num = pr_data.get('number')
                pr_html_url = pr_data.get('html_url') or (
                    f"https://github.com/{repo}/pull/{pr_num}" if pr_num else None
                )
                if pr_html_url and pr_num:
                    write_session_metadata(
                        session_dir,
                        {
                            'pr_url': pr_html_url,
                            'pr_ref': f"{repo}#{pr_num}",
                            'pr_title': pr_data.get('title') or meta.get('pending_pr_title'),
                        },
                    )
                    return pr_html_url
    except Exception:
        pass
    return None


def perform_create_pull_request(
    workspace: WorkspaceLayout,
    session_id: str,
    repo: str,
    title: str,
    body: str = '',
    head: str = '',
    base: str = 'rolling',
    reason: str = '',
    approval_ticket_id: Optional[str] = None,
    dry_run: bool = False,
    approval_mgr: Optional[ApprovalManager] = None,
    body_file: Optional[str] = None,
    web_url: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Prepare a pre-filled GitHub compare URL for opening a Pull Request (default, `web_url=True`),
    or create the Pull Request directly via the GitHub REST API (`web_url=False`, requires
    maintainer approval ticket).
    """
    if not reason or not reason.strip():
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Mandatory parameter `reason` must not be empty.',
        }

    session_dir = workspace.sessions_dir / session_id
    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    policy = workspace.get_policy()
    approval_mgr = approval_mgr or ApprovalManager(workspace.audit_dir / 'approvals.json')

    if body_file and body_file.strip():
        resolved_file = resolve_session_file_path(session_dir, body_file)
        if not resolved_file.is_file():
            return {
                'success': False,
                'status': 'REJECTED',
                'error': f"PR body file not found: '{body_file}' (resolved to '{resolved_file}').",
            }
        body = resolved_file.read_text(encoding='utf-8')
    elif not body and approval_ticket_id:
        try:
            existing_req = approval_mgr.get_request(approval_ticket_id)
            if existing_req and isinstance(existing_req.details, dict) and existing_req.details.get('body'):
                body = existing_req.details['body']
        except Exception:
            pass

    template_warnings = check_pr_body_template(repo, body)

    allowed, msg, requires_approval = policy.validate_pull_request_creation(
        repo_full_name=repo,
        base_branch=base,
    )

    if not allowed:
        timeline.log_action(
            action='create_pull_request',
            target=f"{repo}:{head}->{base}",
            reason=reason,
            status='DENIED',
            details={'error': msg},
        )
        return {'success': False, 'status': 'DENIED', 'error': msg}

    if web_url is not None:
        use_web_url = bool(web_url)
    elif approval_ticket_id:
        use_web_url = False
    else:
        use_web_url = (policy.pull_request.default_creation_mode != 'api')

    if use_web_url:
        url_info = build_github_compare_pr_url(
            repo=repo,
            base=base,
            head=head,
            title=title,
            body=body,
        )
        compare_url = url_info['compare_url']
        body_truncated = url_info['body_truncated_from_url']
        if body_truncated:
            template_warnings = list(template_warnings) + [
                "PR description exceeded maximum safe URL query length and was omitted from the URL; "
                "copy-paste the markdown description block directly into GitHub's PR form."
            ]

        if session_dir.is_dir():
            write_session_metadata(
                session_dir,
                {
                    'pending_pr_repo': repo,
                    'pending_pr_head': head,
                    'pending_pr_base': base,
                    'pending_pr_title': title,
                    'pr_compare_url': compare_url,
                },
            )

        timeline.log_action(
            action='create_pull_request',
            target=f"{repo}:{head}->{base}",
            reason=reason,
            status='WEB_URL_READY',
            details={
                'mode': 'web_url',
                'compare_url': compare_url,
                'title': title,
                'body_truncated_from_url': body_truncated,
            },
        )

        out_web: Dict[str, Any] = {
            'success': True,
            'status': 'WEB_URL_READY',
            'mode': 'web_url',
            'pr_url': compare_url,
            'compare_url': compare_url,
            'repo': repo,
            'head': head,
            'base': base,
            'title': title,
            'body': body,
            'body_truncated_from_url': body_truncated,
            'message': (
                "Generated pre-filled GitHub PR creation URL. Open the link in your browser to review "
                "the diff, title, and description before clicking 'Create pull request'."
            ),
        }
        if template_warnings:
            out_web['template_warnings'] = template_warnings
        return out_web

    target_label = f"{repo}:{head}->{base}"
    if requires_approval:
        ticket_valid = bool(
            approval_ticket_id
            and approval_mgr.is_approved(
                approval_ticket_id,
                action='create_pull_request',
                target=target_label,
                session_id=session_id,
                allow_consumed=dry_run,
            )
        )
        if not ticket_valid:
            req = approval_mgr.create_request(
                session_id=session_id,
                action='create_pull_request',
                target=target_label,
                reason=reason,
                details={'title': title, 'body': body, 'head': head, 'base': base},
            )
            timeline.log_action(
                action='create_pull_request',
                target=target_label,
                reason=reason,
                status='PENDING_APPROVAL',
                details={'ticket_id': req.ticket_id},
            )
            res_pending: Dict[str, Any] = {
                'success': False,
                'status': 'PENDING_APPROVAL',
                'ticket_id': req.ticket_id,
                'message': f"{msg} Created approval ticket '{req.ticket_id}'.",
            }
            if template_warnings:
                res_pending['template_warnings'] = template_warnings
            return res_pending

    if dry_run:
        pr_number = 9999
        pr_html_url = f"https://github.com/{repo}/pull/{pr_number}"
    else:
        gh_res = _execute_github_create_pr(
            repo=repo,
            title=title,
            body=body,
            head=head,
            base=base,
        )
        if not gh_res.get('success'):
            err = gh_res.get('error', 'Failed to create Pull Request on GitHub')
            timeline.log_action(
                action='create_pull_request',
                target=target_label,
                reason=reason,
                status='FAILED',
                details={'error': err},
            )
            return {
                'success': False,
                'status': 'FAILED',
                'error': err,
            }
        pr_html_url = gh_res['pr_url']
        pr_number = gh_res.get('pr_number')
        if requires_approval and approval_ticket_id:
            approval_mgr.consume_ticket(approval_ticket_id)

    if session_dir.is_dir() and pr_html_url and pr_number and not dry_run:
        write_session_metadata(
            session_dir,
            {
                'pr_url': pr_html_url,
                'pr_ref': f"{repo}#{pr_number}",
                'pr_title': title,
            },
        )

    timeline.log_action(
        action='create_pull_request',
        target=target_label,
        reason=reason,
        status='APPROVED',
        details={'mode': 'api', 'pr_url': pr_html_url, 'pr_number': pr_number, 'dry_run': dry_run},
    )

    out: Dict[str, Any] = {
        'success': True,
        'status': 'APPROVED',
        'mode': 'api',
        'pr_url': pr_html_url,
        'pr_number': pr_number,
        'repo': repo,
        'title': title,
        'dry_run': dry_run,
    }
    if template_warnings:
        out['template_warnings'] = template_warnings
    return out


def perform_edit_pull_request(
    workspace: WorkspaceLayout,
    session_id: str,
    repo: Optional[str] = None,
    pr_number: Optional[int] = None,
    pr_url: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    body_file: Optional[str] = None,
    reason: str = '',
    approval_ticket_id: Optional[str] = None,
    dry_run: bool = False,
    approval_mgr: Optional[ApprovalManager] = None,
) -> Dict[str, Any]:
    """Edit an existing GitHub Pull Request title and/or body with policy validation and approval gating."""
    from .ci import parse_pr_url

    if not reason or not reason.strip():
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Mandatory parameter `reason` must not be empty.',
        }

    session_dir = workspace.sessions_dir / session_id
    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    policy = workspace.get_policy()
    approval_mgr = approval_mgr or ApprovalManager(workspace.audit_dir / 'approvals.json')

    if pr_url and (not repo or not pr_number):
        parsed_repo, parsed_num = parse_pr_url(pr_url)
        repo = repo or parsed_repo
        pr_number = pr_number or parsed_num

    if not repo or not pr_number:
        meta = read_session_metadata(session_dir)
        fallback_ref = meta.get('pr_url') or meta.get('pr_ref') or ''
        if fallback_ref:
            parsed_repo, parsed_num = parse_pr_url(fallback_ref)
            repo = repo or parsed_repo
            pr_number = pr_number or parsed_num

    if not repo or not pr_number:
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Both `repo` (owner/repo) and `pr_number` (or `pr_url`) must be provided.',
        }

    if body_file and body_file.strip():
        resolved_file = resolve_session_file_path(session_dir, body_file)
        if not resolved_file.is_file():
            return {
                'success': False,
                'status': 'REJECTED',
                'error': f"PR body file not found: '{body_file}' (resolved to '{resolved_file}').",
            }
        body = resolved_file.read_text(encoding='utf-8')

    if approval_ticket_id and title is None and body is None:
        try:
            existing_req = approval_mgr.get_request(approval_ticket_id)
            if existing_req and isinstance(existing_req.details, dict):
                if existing_req.details.get('title') is not None:
                    title = existing_req.details['title']
                if existing_req.details.get('body') is not None:
                    body = existing_req.details['body']
        except Exception:
            pass

    if title is None and body is None:
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Provide at least one of `title`, `body`, or `body_file` to update the Pull Request.',
        }

    template_warnings = check_pr_body_template(repo, body) if body is not None else []
    target_label = f"{repo}#{pr_number}"

    allowed, msg, requires_approval = policy.validate_pull_request_creation(
        repo_full_name=repo,
    )
    if not allowed:
        timeline.log_action(
            action='edit_pull_request',
            target=target_label,
            reason=reason,
            status='DENIED',
            details={'error': msg},
        )
        return {'success': False, 'status': 'DENIED', 'error': msg}

    if requires_approval:
        ticket_valid = bool(
            approval_ticket_id
            and approval_mgr.is_approved(
                approval_ticket_id,
                action='edit_pull_request',
                target=target_label,
                session_id=session_id,
                allow_consumed=dry_run,
            )
        )
        if not ticket_valid:
            req = approval_mgr.create_request(
                session_id=session_id,
                action='edit_pull_request',
                target=target_label,
                reason=reason,
                details={'repo': repo, 'pr_number': pr_number, 'title': title, 'body': body},
            )
            timeline.log_action(
                action='edit_pull_request',
                target=target_label,
                reason=reason,
                status='PENDING_APPROVAL',
                details={'ticket_id': req.ticket_id},
            )
            res_pending: Dict[str, Any] = {
                'success': False,
                'status': 'PENDING_APPROVAL',
                'ticket_id': req.ticket_id,
                'message': f"Editing Pull Request '{target_label}' requires maintainer approval. "
                           f"Created approval ticket '{req.ticket_id}'.",
            }
            if template_warnings:
                res_pending['template_warnings'] = template_warnings
            return res_pending

    pr_html_url = f"https://github.com/{repo}/pull/{pr_number}"
    if not dry_run:
        gh_res = _execute_github_edit_pr(
            repo=repo,
            pr_number=pr_number,
            title=title,
            body=body,
        )
        if not gh_res.get('success'):
            err = gh_res.get('error', 'Failed to edit Pull Request on GitHub')
            timeline.log_action(
                action='edit_pull_request',
                target=target_label,
                reason=reason,
                status='FAILED',
                details={'error': err},
            )
            return {
                'success': False,
                'status': 'FAILED',
                'error': err,
            }
        pr_html_url = gh_res.get('pr_url', pr_html_url)
        if requires_approval and approval_ticket_id:
            approval_mgr.consume_ticket(approval_ticket_id)

    timeline.log_action(
        action='edit_pull_request',
        target=target_label,
        reason=reason,
        status='APPROVED',
        details={'pr_url': pr_html_url, 'pr_number': pr_number, 'dry_run': dry_run},
    )

    out: Dict[str, Any] = {
        'success': True,
        'status': 'APPROVED',
        'pr_url': pr_html_url,
        'pr_number': pr_number,
        'repo': repo,
        'title': title,
        'dry_run': dry_run,
    }
    if template_warnings:
        out['template_warnings'] = template_warnings
    return out


def _find_bloom_release_executable() -> Optional[str]:
    """Locate `bloom-release` on the host."""
    import shutil

    found = shutil.which('bloom-release')
    if found:
        return found
    local_bin = Path.home() / '.local' / 'bin' / 'bloom-release'
    if local_bin.exists():
        return str(local_bin)
    usr_bin = Path('/usr/bin/bloom-release')
    if usr_bin.exists():
        return str(usr_bin)
    return None


def perform_run_bloom_release(
    workspace: WorkspaceLayout,
    session_id: str,
    repository: str,
    rosdistro: str = 'rolling',
    track: Optional[str] = None,
    non_interactive: bool = True,
    pretend: bool = False,
    no_web: bool = True,
    no_pull_request: bool = False,
    pull_request_only: bool = False,
    reason: str = '',
    approval_ticket_id: Optional[str] = None,
    dry_run: bool = False,
    approval_mgr: Optional[ApprovalManager] = None,
) -> Dict[str, Any]:
    """
    Execute `bloom-release` on the host for a ROS repository after maintainer approval.
    Always requires an explicit maintainer approval ticket (`action='bloom_release'`).
    """
    import os
    import re
    import subprocess

    if not reason or not reason.strip():
        return {
            'success': False,
            'status': 'REJECTED',
            'error': 'Mandatory parameter `reason` must not be empty.',
        }

    raw_repo = (repository or '').strip().strip('/')
    repo_name = raw_repo.split('/')[-1] if '/' in raw_repo else raw_repo
    cleaned_distro = (rosdistro or 'rolling').strip()
    cleaned_track = (track or cleaned_distro).strip()

    safe_ident = re.compile(r'^[A-Za-z0-9_.-]+$')
    if not repo_name or not safe_ident.match(repo_name) or repo_name.startswith('-'):
        return {
            'success': False,
            'status': 'REJECTED',
            'error': f"Invalid repository name for bloom-release: '{repository}'.",
        }
    if not cleaned_distro or not safe_ident.match(cleaned_distro) or cleaned_distro.startswith('-'):
        return {
            'success': False,
            'status': 'REJECTED',
            'error': f"Invalid rosdistro name for bloom-release: '{rosdistro}'.",
        }
    if not cleaned_track or not safe_ident.match(cleaned_track) or cleaned_track.startswith('-'):
        return {
            'success': False,
            'status': 'REJECTED',
            'error': f"Invalid track name for bloom-release: '{track}'.",
        }

    session_dir = workspace.sessions_dir / session_id
    timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
    approval_mgr = approval_mgr or ApprovalManager(workspace.audit_dir / 'approvals.json')

    target_label = f"{repo_name} ({cleaned_distro}/{cleaned_track})"
    use_pretend = bool(pretend or dry_run)

    ticket_ok = bool(
        approval_ticket_id
        and approval_mgr.is_approved(
            approval_ticket_id,
            action='bloom_release',
            target=target_label,
            session_id=session_id,
            allow_consumed=use_pretend,
        )
    )

    if not ticket_ok:
        req = approval_mgr.create_request(
            session_id=session_id,
            action='bloom_release',
            target=target_label,
            reason=reason,
            details={
                'repository': repo_name,
                'rosdistro': cleaned_distro,
                'track': cleaned_track,
                'non_interactive': non_interactive,
                'pretend': use_pretend,
                'pull_request_only': pull_request_only,
                'no_pull_request': no_pull_request,
            },
        )
        timeline.log_action(
            action='bloom_release',
            target=target_label,
            reason=reason,
            status='PENDING_APPROVAL',
            details={
                'ticket_id': req.ticket_id,
                'repository': repo_name,
                'rosdistro': cleaned_distro,
                'track': cleaned_track,
                'pretend': use_pretend,
            },
        )
        return {
            'success': False,
            'status': 'PENDING_APPROVAL',
            'ticket_id': req.ticket_id,
            'target': target_label,
            'message': (
                f"Running 'bloom-release' for '{target_label}' requires maintainer approval. "
                f"Created approval ticket '{req.ticket_id}'."
            ),
        }

    bloom_bin = _find_bloom_release_executable()
    if not bloom_bin:
        err = (
            "Executable 'bloom-release' was not found on the host. "
            "Install bloom via 'pip install --user --break-system-packages bloom'."
        )
        timeline.log_action(
            action='bloom_release',
            target=target_label,
            reason=reason,
            status='FAILED',
            details={'error': err},
        )
        return {'success': False, 'status': 'FAILED', 'error': err}

    cmd: List[str] = [
        bloom_bin,
        '--rosdistro', cleaned_distro,
        '--track', cleaned_track,
    ]
    if non_interactive:
        cmd.append('--non-interactive')
    if no_web:
        cmd.append('--no-web')
    if use_pretend:
        cmd.append('--pretend')
    if no_pull_request:
        cmd.append('--no-pull-request')
    if pull_request_only:
        cmd.append('--pull-request-only')
    cmd.append(repo_name)

    bloom_env = {
        **os.environ,
        'GIT_TERMINAL_PROMPT': '0',
        'BLOOM_NO_WEBBROWSER': '1',
    }
    if non_interactive:
        bloom_env['BLOOM_DONT_ASK_FOR_DOCS'] = '1'
        bloom_env['BLOOM_DONT_ASK_FOR_SOURCE'] = '1'
        bloom_env['BLOOM_DONT_ASK_FOR_MAINTENANCE_STATUS'] = '1'

    local_bin_str = str(Path.home() / '.local' / 'bin')
    curr_path = bloom_env.get('PATH', '')
    if local_bin_str not in curr_path.split(os.pathsep):
        bloom_env['PATH'] = f"{local_bin_str}{os.pathsep}{curr_path}" if curr_path else local_bin_str

    default_sock = Path.home() / '.ssh' / 'ssh_auth_sock'
    if (
        (not bloom_env.get('SSH_AUTH_SOCK') or not Path(bloom_env['SSH_AUTH_SOCK']).exists())
        and default_sock.exists()
    ):
        bloom_env['SSH_AUTH_SOCK'] = str(default_sock)

    from .audit import redact_credentials

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
            env=bloom_env,
        )
    except subprocess.TimeoutExpired:
        err = 'bloom-release timed out after 600 seconds.'
        timeline.log_action(
            action='bloom_release',
            target=target_label,
            reason=reason,
            status='FAILED',
            details={'error': err},
        )
        return {'success': False, 'status': 'FAILED', 'error': err}
    except Exception as e:
        err = redact_credentials(f'bloom-release execution failed: {e}')
        timeline.log_action(
            action='bloom_release',
            target=target_label,
            reason=reason,
            status='FAILED',
            details={'error': err},
        )
        return {'success': False, 'status': 'FAILED', 'error': err}

    combined_output = ((res.stdout or '') + '\n' + (res.stderr or '')).strip()
    ansi_stripped = redact_credentials(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', combined_output))
    pr_matches = re.findall(r'https://github\.com/[^\s"\'\)]+/pull/\d+', ansi_stripped)
    rosdistro_pr_url = pr_matches[-1] if pr_matches else None

    ok = (res.returncode == 0)
    if approval_ticket_id and ok and not use_pretend:
        approval_mgr.consume_ticket(approval_ticket_id)
    status_str = 'APPROVED' if ok else 'FAILED'

    if ok and rosdistro_pr_url and session_dir.is_dir() and not use_pretend:
        meta = read_session_metadata(session_dir)
        existing_urls = list(meta.get('rosdistro_pr_urls') or [])
        if rosdistro_pr_url not in existing_urls:
            existing_urls.append(rosdistro_pr_url)
        write_session_metadata(
            session_dir,
            {
                'last_rosdistro_pr_url': rosdistro_pr_url,
                'rosdistro_pr_urls': existing_urls,
            },
        )

    timeline.log_action(
        action='bloom_release',
        target=target_label,
        reason=reason,
        status=status_str,
        details={
            'repository': repo_name,
            'rosdistro': cleaned_distro,
            'track': cleaned_track,
            'pretend': use_pretend,
            'rosdistro_pr_url': rosdistro_pr_url,
            'returncode': res.returncode,
        },
    )

    return {
        'success': ok,
        'status': status_str,
        'target': target_label,
        'repository': repo_name,
        'rosdistro': cleaned_distro,
        'track': cleaned_track,
        'pretend': use_pretend,
        'rosdistro_pr_url': rosdistro_pr_url,
        'returncode': res.returncode,
        'output': ansi_stripped,
    }


def create_mcp_server(workspace: WorkspaceLayout, require_auth: bool = False) -> MCPServer:
    """Create and configure the Host MCP Server Gateway with safety rules and tools."""
    if not workspace.is_initialized():
        workspace.initialize()

    server = MCPServer('ros-maintainer-harness')
    approval_mgr = ApprovalManager(workspace.audit_dir / 'approvals.json')
    ci_tracker = CITracker(workspace.audit_dir / 'ci_runs.json')
    session_mgr = SessionManager(workspace)
    policy = workspace.get_policy()
    jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)

    orig_server_tool = server.tool

    def _authorized_tool(*t_args: Any, **t_kwargs: Any) -> Any:
        base_decorator = orig_server_tool(*t_args, **t_kwargs)

        def decorator(fn: Any) -> Any:
            sig = inspect.signature(fn)
            ret_ann = sig.return_annotation

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                active_caller = get_active_caller()
                if active_caller is None:
                    effective_caller = (
                        None
                        if require_auth
                        else CallerIdentity(role='admin', token_id='local-stdio')
                    )
                else:
                    effective_caller = active_caller

                curr_policy = workspace.get_policy()
                allowed, err_msg, overrides = authorize_tool_call(
                    caller=effective_caller,
                    tool_name=fn.__name__,
                    bound_args=dict(bound.arguments),
                    workspace_root=workspace.root,
                    allow_hub_exec_in_session=curr_policy.server.allow_hub_exec_in_session,
                    allow_session_release_tools=curr_policy.server.allow_session_release_tools,
                )
                if not allowed:
                    caller_role = effective_caller.role if effective_caller else 'unauthenticated'
                    target_sid = str(
                        (
                            effective_caller.session_id
                            if effective_caller and effective_caller.session_id
                            else None
                        )
                        or bound.arguments.get('session_id')
                        or 'unknown'
                    )
                    append_audit_record(
                        workspace.audit_log_path,
                        {
                            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'session_id': target_sid,
                            'action': fn.__name__,
                            'target': str(
                                bound.arguments.get('session_id')
                                or bound.arguments.get('repo')
                                or fn.__name__
                            ),
                            'reason': f"Unauthorized tool call by role '{caller_role}'",
                            'status': 'DENIED',
                            'caller_role': caller_role,
                            'token_id': effective_caller.token_id if effective_caller else None,
                            'details': {'error': err_msg},
                        },
                    )
                    err_payload = {
                        'success': False,
                        'authorized': False,
                        'status': 'UNAUTHORIZED',
                        'error': err_msg,
                    }
                    if ret_ann is str or ret_ann == 'str':
                        return json.dumps(err_payload)
                    if getattr(ret_ann, '__origin__', None) in (list, List):
                        return [err_payload]
                    return err_payload

                for k, v in overrides.items():
                    if k in bound.arguments:
                        bound.arguments[k] = v

                with caller_context(effective_caller):
                    return fn(*bound.args, **bound.kwargs)

            return base_decorator(wrapper)

        return decorator

    server.tool = _authorized_tool  # type: ignore[method-assign]

    # 1. log_status
    @server.tool()
    def log_status(session_id: str, message: str, milestone: Optional[str] = None) -> str:
        """
        Record a status update or milestone progress note to the session's timeline.md.

        Args:
            session_id: Session identifier (e.g. 'session-pr-160').
            message: Description of the progress or finding.
            milestone: Optional milestone label (e.g. 'Build Succeeded', 'CI Clean').
        """
        session_dir = workspace.sessions_dir / session_id
        timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
        return timeline.log_status(message=message, milestone=milestone)

    # 2. git_push
    @server.tool()
    def git_push(
        session_id: str,
        repo_path: str,
        branch: str,
        remote: str = 'origin',
        force_with_lease: bool = False,
        force: bool = False,
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Push a branch (or a release commit + version tag when `tag` is provided) to a Git remote
        with safety policy validation and audit logging.

        Args:
            session_id: Active session identifier.
            repo_path: Path to the local git repository or worktree (e.g. 'launch' or 'src/launch').
            branch: Branch name to push (or target distro branch when `tag` is set).
            remote: Remote name (default: 'origin').
            force_with_lease: Whether to use --force-with-lease (required for force pushes).
            force: Whether force push is requested.
            reason: MANDATORY explanation for why this push is being executed.
            approval_ticket_id: Ticket ID if this action required prior maintainer approval.
            dry_run: If True, validate policy without executing network git push.
            tag: Optional release version tag (e.g. '3.10.2') created by `catkin_prepare_release --no-push`.
                When set, delegates to `push_release` (requires maintainer approval ticket).
        """
        return perform_git_push(
            workspace=workspace,
            session_id=session_id,
            repo_path=repo_path,
            branch=branch,
            remote=remote,
            force_with_lease=force_with_lease,
            force=force,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
            tag=tag,
        )

    # 3. launch_jenkins_ci
    @server.tool()
    def launch_jenkins_ci(
        session_id: str,
        pr_url: str = '',
        target_distro: Optional[str] = None,
        job_type: Optional[str] = None,
        only_fixes_test: bool = False,
        packages: Optional[List[str]] = None,
        colcon_build_args: Optional[str] = None,
        colcon_test_args: Optional[str] = None,
        cmake_args: Optional[str] = None,
        extra_repos: Optional[List[str]] = None,
        comment: bool = False,
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Launch a Jenkins CI run for a ROS 2 PR on ci.ros2.org with rate-limiting & cooldown checks.

        Args:
            session_id: Active session identifier.
            pr_url: Optional GitHub PR URL or shorthand (auto-detected from session metadata if omitted).
            target_distro: Optional target ROS 2 distro (auto-detected from session metadata if omitted).
            job_type: Jenkins launcher job type (default: 'ci_launcher').
            only_fixes_test: Whether to run only tests affected by the PR.
            packages: Optional list of ROS package names to build/test (auto-detected from session worktree if omitted).
            colcon_build_args: Additional colcon build arguments.
            colcon_test_args: Additional colcon test arguments.
            cmake_args: Additional CMake arguments passed to colcon build.
            extra_repos: Optional list of 'owner/repo:branch' entries to include in ros2.repos Gist.
            comment: If True, post the CI build badge summary comment on the GitHub PR.
            reason: MANDATORY explanation for why CI is being launched.
            approval_ticket_id: Ticket ID if rate-limit override was approved.
            dry_run: If True, validate policy and generate launcher parameters without calling Jenkins.
        """
        return perform_launch_jenkins_ci(
            workspace=workspace,
            session_id=session_id,
            pr_url=pr_url,
            target_distro=target_distro,
            job_type=job_type,
            only_fixes_test=only_fixes_test,
            packages=packages,
            colcon_build_args=colcon_build_args,
            colcon_test_args=colcon_test_args,
            cmake_args=cmake_args,
            extra_repos=extra_repos,
            comment=comment,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
            ci_tracker=ci_tracker,
        )

    # 4. find_restarted_ci
    @server.tool()
    def find_restarted_ci(
        session_id: str,
        pr_or_comment_url: str,
        update_comment: bool = False,
        reason: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Check for rescheduled or restarted Jenkins builds and optionally update PR comment markdown.

        Args:
            session_id: Active session identifier.
            pr_or_comment_url: PR or comment URL to inspect.
            update_comment: If True, update the GitHub PR comment in-place.
            reason: Optional explanation if updating comment.
            dry_run: If True, perform dry-run discovery without mutating comments.
        """
        return perform_find_restarted_ci(
            workspace=workspace,
            session_id=session_id,
            pr_or_comment_url=pr_or_comment_url,
            update_comment=update_comment,
            reason=reason,
            dry_run=dry_run,
            ci_tracker=ci_tracker,
        )

    # 5. create_pull_request
    @server.tool()
    def create_pull_request(
        session_id: str,
        repo: str,
        title: str,
        body: str = '',
        head: str = '',
        base: str = 'rolling',
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
        body_file: Optional[str] = None,
        web_url: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Prepare a pre-filled GitHub compare URL for opening a Pull Request (default, `web_url=True`),
        or create the Pull Request directly via the GitHub REST API (`web_url=False`, requires
        maintainer approval ticket).

        Args:
            session_id: Active session identifier.
            repo: Target repository full name (e.g. 'ros2/rclcpp').
            title: Pull request title.
            body: Pull request description.
            head: Head branch (e.g. 'wjwwood/feature_branch' or 'wjwwood:feature_branch').
            base: Base branch (e.g. 'rolling').
            reason: MANDATORY explanation for opening this PR.
            approval_ticket_id: Ticket ID approved by maintainer (when `web_url=False`).
            dry_run: If True, simulate creation without GitHub API call.
            body_file: Optional path to a markdown file containing the PR description.
            web_url: If True (default), return a pre-filled GitHub compare URL so the maintainer can
                review the diff and description in the browser before clicking 'Create pull request'.
                If False, create the PR directly via the GitHub REST API (requires approval ticket).
        """
        return perform_create_pull_request(
            workspace=workspace,
            session_id=session_id,
            repo=repo,
            title=title,
            body=body,
            head=head,
            base=base,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
            body_file=body_file,
            web_url=web_url,
        )

    # 5b. edit_pull_request
    @server.tool()
    def edit_pull_request(
        session_id: str,
        repo: Optional[str] = None,
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        body_file: Optional[str] = None,
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Edit an existing GitHub Pull Request title and/or body (requires maintainer approval).

        Args:
            session_id: Active session identifier.
            repo: Target repository full name (e.g. 'ros2/launch').
            pr_number: Pull request number (e.g. 1025).
            pr_url: Optional PR URL or shorthand (e.g. 'ros2/launch#1025').
            title: Optional updated pull request title.
            body: Optional updated pull request description.
            body_file: Optional path to a markdown file containing the updated PR description.
            reason: MANDATORY explanation for editing this PR.
            approval_ticket_id: Ticket ID approved by maintainer.
            dry_run: If True, simulate edit without GitHub API call.
        """
        return perform_edit_pull_request(
            workspace=workspace,
            session_id=session_id,
            repo=repo,
            pr_number=pr_number,
            pr_url=pr_url,
            title=title,
            body=body,
            body_file=body_file,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
        )

    # 6. check_policy
    @server.tool()
    def check_policy(
        action: str,
        target: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Pre-flight check whether a planned action complies with maintainer policy.

        Args:
            action: Action type ('git_push', 'launch_jenkins_ci', 'create_pull_request').
            target: Target branch, repository full name, or PR URL.
            details: Optional dictionary of parameters (e.g. force_with_lease, base_branch).
        """
        policy = workspace.get_policy()
        details = details or {}

        if action == 'git_push':
            branch = target
            repo = details.get('repo')
            force = details.get('force', False)
            force_with_lease = details.get('force_with_lease', False)
            allowed, msg, requires_approval = policy.validate_git_push(
                branch_name=branch,
                repo_full_name=repo,
                force_with_lease=force_with_lease,
                force=force,
            )
            return {
                'action': action,
                'allowed': allowed,
                'requires_approval': requires_approval,
                'reason': msg,
            }
        elif action == 'launch_jenkins_ci':
            active_count = ci_tracker.get_active_runs_count(target)
            since_last = ci_tracker.get_seconds_since_last_run(target)
            allowed, msg, req_appr = policy.validate_jenkins_ci(
                pr_url=target,
                active_runs_count=active_count,
                seconds_since_last_run=since_last,
            )
            return {
                'action': action,
                'allowed': allowed,
                'requires_approval': req_appr,
                'reason': msg,
            }
        elif action == 'create_pull_request':
            base = details.get('base', 'rolling')
            allowed, msg, req_appr = policy.validate_pull_request_creation(
                repo_full_name=target,
                base_branch=base,
            )
            return {
                'action': action,
                'allowed': allowed,
                'requires_approval': req_appr,
                'reason': msg,
            }
        else:
            return {
                'action': action,
                'allowed': False,
                'requires_approval': False,
                'reason': f"Unknown action '{action}'.",
            }

    # 7. create_session
    @server.tool()
    def create_session(
        session_id: str,
        topic: Optional[str] = None,
        distro: str = 'rolling',
        custom_image: Optional[str] = None,
        repo_path: Optional[str] = None,
        branch: Optional[str] = None,
        base_ref: str = 'HEAD',
    ) -> Dict[str, Any]:
        """
        Create a new session directory layout, .devcontainer config, and optional Git worktree.

        Args:
            session_id: Unique session ID (e.g. 'session-pr-160').
            topic: Optional PR/task topic description.
            distro: Target ROS 2 distribution (e.g. 'rolling', 'jazzy', 'humble').
            custom_image: Optional custom Docker container image override.
            repo_path: Optional path to Git repository for linking worktree.
            branch: Optional branch name for worktree.
            base_ref: Base ref/commit (default: 'HEAD').
        """
        info = session_mgr.create_session(
            session_id,
            topic=topic,
            distro=distro,
            custom_image=custom_image,
        )
        caller = get_active_caller()
        write_session_metadata(
            info.session_dir,
            {'started_by_hub': bool(caller and caller.is_hub)},
        )
        worktree_path = None
        if repo_path and branch:
            worktree_path = str(session_mgr.attach_worktree(
                session_id=session_id,
                repo_dir=Path(repo_path),
                branch_name=branch,
                base_ref=base_ref,
            ))

        return {
            'session_id': info.session_id,
            'session_dir': str(info.session_dir),
            'src_dir': str(info.src_dir),
            'build_dir': str(info.build_dir),
            'install_dir': str(info.install_dir),
            'log_dir': str(info.log_dir),
            'scratch_dir': str(info.scratch_dir),
            'timeline_path': str(info.timeline_path),
            'devcontainer_path': str(info.devcontainer_path) if info.devcontainer_path else None,
            'distro': info.distro,
            'worktree_path': worktree_path,
        }

    # 8. list_sessions
    @server.tool()
    def list_sessions() -> List[Dict[str, Any]]:
        """List all active sessions, their metadata, linked conversation IDs, and Git worktree branches."""
        sessions = session_mgr.list_sessions()
        results = []
        for s in sessions:
            meta = read_session_metadata(s.session_dir)
            tl = parse_timeline_summary(s.timeline_path, max_entries=1)
            conv_id = meta.get('conversation_id')
            results.append({
                'session_id': s.session_id,
                'session_dir': str(s.session_dir),
                'status': meta.get('status', 'active'),
                'topic': meta.get('topic') or meta.get('pr_title'),
                'distro': meta.get('distro') or s.distro or 'rolling',
                'pr_ref': meta.get('pr_ref'),
                'pr_url': meta.get('pr_url'),
                'conversation_id': conv_id,
                'conversation_link': format_conversation_link(s.session_id, conv_id),
                'latest_milestone': tl['latest_milestone'],
                'active_branches': s.active_branches,
            })
        return results

    # 9. prune_session
    @server.tool()
    def prune_session(
        session_id: str,
        force: bool = False,
        reason: str = '',
    ) -> Dict[str, Any]:
        """
        Prune and clean up a completed session workspace.

        Args:
            session_id: Session ID to remove.
            force: Whether to force remove dirty worktrees.
            reason: MANDATORY explanation for pruning the session.
        """
        if not reason or not reason.strip():
            return {
                'success': False,
                'error': 'Mandatory parameter `reason` must not be empty.',
            }

        session_dir = workspace.sessions_dir / session_id
        timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
        timeline.log_action(
            action='prune_session',
            target=session_id,
            reason=reason,
            status='APPROVED',
            details={'force': force},
        )
        success = session_mgr.prune_session(session_id, force=force)
        return {'success': success, 'session_id': session_id}

    # 10. get_maintainer_rules
    @server.tool()
    def get_maintainer_rules() -> str:
        """Read and return the maintainer conventions and style preferences."""
        rules = MaintainerRules(workspace.rules_path)
        return rules.load_content()

    # 11. add_maintainer_rule
    @server.tool()
    def add_maintainer_rule(category: str, rule: str, reason: str = '') -> str:
        """
        Record a new preference/rule into maintainer_rules.md.

        Args:
            category: Section category (e.g. 'Git & Commits', 'CI Conventions').
            rule: Rule text description.
            reason: Reason or context for recording this rule.
        """
        rules = MaintainerRules(workspace.rules_path)
        rules.record_preference(category, rule)
        if workspace.audit_log_path:
            timeline = TimelineLogger('global', workspace.root, workspace.audit_log_path)
            timeline.log_action(
                action='add_maintainer_rule',
                target=category,
                reason=reason or f"Added rule to {category}",
                status='APPROVED',
                details={'rule': rule},
            )
        return f"Successfully added rule to category '{category}': {rule}"

    # 12. list_approval_requests
    @server.tool()
    def list_approval_requests(
        status: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List maintainer approval requests."""
        requests = approval_mgr.list_requests(status=status, session_id=session_id)
        return [r.to_dict() for r in requests]

    # 13. respond_approval_request
    @server.tool()
    def respond_approval_request(
        ticket_id: str,
        approve: bool,
        maintainer: str = 'maintainer',
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Approve or reject a pending approval ticket.

        Args:
            ticket_id: Approval ticket identifier (e.g. 'req-abcd1234').
            approve: True to approve, False to reject.
            maintainer: Name/handle of approving maintainer.
            comment: Optional maintainer explanation/comment.
        """
        try:
            if approve:
                req = approval_mgr.approve_request(ticket_id, maintainer=maintainer, comment=comment)
            else:
                req = approval_mgr.reject_request(ticket_id, maintainer=maintainer, comment=comment)
        except KeyError as e:
            return {'success': False, 'status': 'NOT_FOUND', 'error': str(e)}

        timeline = TimelineLogger(req.session_id, workspace.sessions_dir / req.session_id, workspace.audit_log_path)
        timeline.log_action(
            action='respond_approval_request',
            target=ticket_id,
            reason=comment or ('Approved' if approve else 'Rejected'),
            status=req.status,
            details={'resolved_by': maintainer, 'action_target': req.target},
        )

        return req.to_dict()

    # 14. get_ci_status
    @server.tool()
    def get_ci_status(
        job_url_or_id: Optional[str] = None,
        session_id: Optional[str] = None,
        pr_url: Optional[str] = None,
        wait_for_completion: bool = False,
        timeout_seconds: int = 60,
        poll_interval_seconds: float = 5.0,
    ) -> Dict[str, Any]:
        """
        Query the current status of a Jenkins CI run or wait for completion on the host.

        Enables agents to monitor CI progress and retrieve failure summaries with minimal token overhead.

        Args:
            job_url_or_id: Jenkins job URL, build number, or PR shorthand (e.g. 'ros2/rclcpp#160').
            session_id: Session ID to query latest CI run for (if job_url_or_id not specified).
            pr_url: PR URL to query latest CI run for.
            wait_for_completion: If True, blocks on the host until build completes or timeout occurs.
            timeout_seconds: Maximum time to wait in seconds (default: 60).
            poll_interval_seconds: Polling frequency in seconds (default: 5.0).
        """
        caller = get_active_caller()
        run = None
        if job_url_or_id:
            run = ci_tracker.get_run(job_url_or_id)
        elif pr_url:
            runs = ci_tracker.list_runs(
                session_id=(caller.session_id if caller and caller.is_session else session_id),
                pr_url=pr_url,
                limit=1,
            )
            if runs:
                run = runs[0]
        elif session_id:
            run = ci_tracker.get_latest_run_for_session(session_id)

        if (
            caller is not None
            and caller.is_session
            and run is not None
            and run.session_id
            and run.session_id != caller.session_id
        ):
            return {
                'success': False,
                'status': 'UNAUTHORIZED',
                'error': (
                    f"Role '{caller.role}' cannot access CI run belonging to "
                    f"another session ('{run.session_id}')."
                ),
            }

        target_url = run.job_url if run else job_url_or_id
        if not target_url or not target_url.startswith(('http://', 'https://')):
            return {
                'success': False,
                'status': 'NOT_FOUND',
                'error': (
                    f"No tracked Jenkins CI run found for '{target_url or pr_url or session_id}'. "
                    "Pass a full Jenkins build URL or launch CI first."
                ),
            }

        if wait_for_completion:
            status_res = jenkins_mgr.poll_job_until_complete(
                target_url,
                timeout_seconds=float(timeout_seconds),
                poll_interval_seconds=float(poll_interval_seconds),
            )
            if run and status_res.get('success'):
                ci_tracker.update_run(
                    job_url=target_url,
                    status=status_res.get('status'),
                    duration_seconds=status_res.get('duration_seconds'),
                    test_summary=status_res.get('test_summary'),
                    failure_reason=status_res.get('failure_reason'),
                    log_excerpt=status_res.get('log_excerpt'),
                    artifacts=status_res.get('artifacts', []),
                )
            return status_res

        status_res = jenkins_mgr.fetch_build_status(target_url)
        if run:
            status_res['tracked_record'] = run.to_dict()
            if status_res.get('success'):
                ci_tracker.update_run_status(target_url, status_res.get('status', run.status))
        return status_res

    # 15. list_ci_runs
    @server.tool()
    def list_ci_runs(
        session_id: Optional[str] = None,
        pr_url: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        List historical and active Jenkins CI runs recorded by the gateway.

        Args:
            session_id: Filter by session ID.
            pr_url: Filter by PR URL or shorthand.
            status: Filter by status ('PENDING', 'RUNNING', 'SUCCESS', 'UNSTABLE', 'FAILURE', 'CANCELLED').
            limit: Maximum records to return.
        """
        runs = ci_tracker.list_runs(session_id=session_id, pr_url=pr_url, status=status, limit=limit)
        return [r.to_dict() for r in runs]

    # 16. get_ci_summary
    @server.tool()
    def get_ci_summary(
        job_url_or_id: str,
        max_log_lines: int = 50,
    ) -> Dict[str, Any]:
        """
        Retrieve a token-efficient failure summary and log excerpt for a Jenkins CI run.

        Args:
            job_url_or_id: Jenkins job URL or build number or PR shorthand.
            max_log_lines: Maximum number of relevant error log lines to include.
        """
        caller = get_active_caller()
        run = ci_tracker.get_run(job_url_or_id)
        if (
            caller is not None
            and caller.is_session
            and run is not None
            and run.session_id
            and run.session_id != caller.session_id
        ):
            return {
                'success': False,
                'status': 'UNAUTHORIZED',
                'error': (
                    f"Role '{caller.role}' cannot access CI summary belonging to "
                    f"another session ('{run.session_id}')."
                ),
            }

        target_url = run.job_url if run else job_url_or_id
        if not target_url or not target_url.startswith(('http://', 'https://')):
            return {
                'success': False,
                'status': 'NOT_FOUND',
                'error': (
                    f"No tracked Jenkins CI run found for '{job_url_or_id}'. "
                    "Pass a full Jenkins build URL or launch CI first."
                ),
            }

        build_info = jenkins_mgr.fetch_build_status(target_url)
        test_report = jenkins_mgr.fetch_test_report(target_url)
        status = build_info.get('status', 'UNKNOWN')

        log_excerpt = None
        if status in ('FAILURE', 'UNSTABLE', 'ABORTED'):
            log_excerpt = jenkins_mgr.fetch_console_excerpt(target_url, max_lines=max_log_lines)

        failures = test_report.get('failures', []) if test_report.get('success') else []

        return {
            'success': True,
            'job_url': target_url,
            'status': status,
            'duration_seconds': build_info.get('duration_seconds'),
            'total_tests': test_report.get('total', 0),
            'passed_tests': test_report.get('passed', 0),
            'failed_tests': test_report.get('failed', 0),
            'skipped_tests': test_report.get('skipped', 0),
            'failures': failures[:20],
            'log_excerpt': log_excerpt,
            'artifacts': build_info.get('artifacts', []),
        }

    # 17. cancel_ci_run
    @server.tool()
    def cancel_ci_run(
        job_url_or_id: str,
        reason: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Abort / cancel a running Jenkins CI job.

        Args:
            job_url_or_id: Jenkins job URL or build number or PR shorthand.
            reason: Mandatory explanation for why the CI job is being cancelled.
            session_id: Optional session identifier for timeline logging.
        """
        if not reason or not reason.strip():
            return {
                'success': False,
                'status': 'REJECTED',
                'error': 'Mandatory parameter `reason` must not be empty.',
            }

        caller = get_active_caller()
        run = ci_tracker.get_run(job_url_or_id)
        if (
            caller is not None
            and caller.is_session
            and run is not None
            and run.session_id
            and run.session_id != caller.session_id
        ):
            return {
                'success': False,
                'status': 'UNAUTHORIZED',
                'error': (
                    f"Role '{caller.role}' cannot cancel CI run belonging to "
                    f"another session ('{run.session_id}')."
                ),
            }

        target_url = run.job_url if run else job_url_or_id
        if not target_url or not target_url.startswith(('http://', 'https://')):
            return {
                'success': False,
                'status': 'NOT_FOUND',
                'error': (
                    f"No tracked Jenkins CI run found for '{job_url_or_id}'. "
                    "Pass a full Jenkins build URL."
                ),
            }
        sess_id = session_id or (run.session_id if run else None)

        res = jenkins_mgr.cancel_job(target_url)
        if res.get('success') and sess_id:
            timeline = TimelineLogger(sess_id, workspace.sessions_dir / sess_id, workspace.audit_log_path)
            timeline.log_action(
                action='cancel_ci_run',
                target=target_url,
                reason=reason,
                status='CANCELLED',
                details={'job_url': target_url},
            )
        return res

    # 18. generate_mcp_config
    @server.tool()
    def generate_mcp_config(
        session_id: str,
        transport: str = 'stdio',
        host: str = '127.0.0.1',
        port: int = 8765,
    ) -> Dict[str, Any]:
        """
        Generate and write MCP client configuration files (.mcp.json, .cursor/mcp.json, .vscode/mcp.json)
        for a session directory so AI agents and editors can connect to the Host MCP Gateway.

        Args:
            session_id: Session identifier.
            transport: Transport protocol ('stdio', 'sse', or 'streamable-http').
            host: Host IP for network transports.
            port: Port for network transports.
        """
        if not session_mgr.session_exists(session_id):
            return {'success': False, 'error': f"Session '{session_id}' not found."}

        session_dir = session_mgr.get_session_dir(session_id)
        written = write_session_mcp_configs(
            session_dir=session_dir,
            workspace_path=workspace.root,
            transport=transport,
            host=host,
            port=port,
        )
        return {
            'success': True,
            'session_id': session_id,
            'transport': transport,
            'written_configs': {k: str(v) for k, v in written.items()},
        }

    # 19. get_session_launch_info
    @server.tool()
    def get_session_launch_info(
        session_id: str,
        agent: str = 'claude',
    ) -> Dict[str, Any]:
        """
        Retrieve launch environment variables and CLI execution command for an AI agent in a session.

        Args:
            session_id: Session identifier.
            agent: Agent or editor name ('claude', 'cursor', 'code', 'shell').
        """
        if not session_mgr.session_exists(session_id):
            return {'success': False, 'error': f"Session '{session_id}' not found."}

        session_dir = session_mgr.get_session_dir(session_id)
        info = session_mgr.get_session_info(session_id)
        distro = info.distro if info else 'rolling'

        launch_data = get_agent_launch_info(
            session_id=session_id,
            session_dir=session_dir,
            workspace_path=workspace.root,
            distro=distro,
            agent=agent,
        )
        launch_data['success'] = True
        return launch_data

    # 20. scaffold_session_from_pr
    @server.tool()
    def scaffold_session_from_pr(
        pr_ref: str,
        session_id: Optional[str] = None,
        distro: Optional[str] = None,
        clone_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Automatically scaffold a complete session directly from a GitHub Pull Request URL or shorthand:
        - Fetches PR metadata (branch, diff, author, description).
        - Clones/fetches PR branch into shared repositories.
        - Creates isolated session workspace and linked worktree.
        - Generates devcontainer and editor/agent MCP configs.
        - Pre-populates timeline.md and writes TASK.md prompt.

        Args:
            pr_ref: PR URL or shorthand (e.g. 'https://github.com/ros2/rclcpp/pull/160' or 'ros2/rclcpp#160').
            session_id: Optional custom session ID override (default: 'pr-<repo>-<number>').
            distro: Optional ROS distro override (default: auto-detected from PR base branch).
            clone_if_missing: Automatically clone base repository if not present in shared_repos.
        """
        try:
            result = do_scaffold_from_pr(
                workspace=workspace,
                pr_ref=pr_ref,
                session_id=session_id,
                distro=distro,
                clone_if_missing=clone_if_missing,
            )
            caller = get_active_caller()
            write_session_metadata(
                result.session_dir,
                {'started_by_hub': bool(caller and caller.is_hub)},
            )
            data = result.to_dict()
            env_check = check_token_and_environment(workspace.root)
            data['environment_ready'] = env_check['ready']
            data['container_token_configured'] = env_check['container_token_configured']
            if env_check['warnings']:
                data['environment_warnings'] = env_check['warnings']
            data['success'] = True
            return data
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # 21. check_environment
    @server.tool()
    def check_environment() -> Dict[str, Any]:
        """
        Check workspace readiness: initialization, container runtime (docker/podman),
        GitHub token setup (ROS_CONTAINER_GITHUB_TOKEN), and global MCP registration.

        Call this tool FIRST before scaffolding sessions or starting containers.
        If `container_token_configured` is False, prompt the user to configure their token
        with `ros-maintainer-harness token-setup` before proceeding.
        """
        return check_token_and_environment(workspace.root)

    # 22. start_session_container
    @server.tool()
    def start_session_container(
        session_id: str,
        distro: Optional[str] = None,
        custom_image: Optional[str] = None,
        writable_shared_repos: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Start a detached sandbox container (`ros-harness-<session_id>`) for the given session
        so builds and tests can run in isolation rather than on the host OS.

        Args:
            session_id: Session identifier.
            distro: Optional ROS distro override (defaults to session's configured distro).
            custom_image: Optional custom container image override.
            writable_shared_repos: Optional boolean override to mount `shared_repos/` read-write (`True`)
                instead of the default read-only (`False`), e.g. after pre-build security review.
        """
        if not session_mgr.session_exists(session_id):
            return {'success': False, 'error': f"Session '{session_id}' not found."}

        session_dir = session_mgr.get_session_dir(session_id)
        info = session_mgr.get_session_info(session_id)
        target_distro = distro or (info.distro if info and info.distro else 'rolling')

        res = do_start_session_container(
            session_id=session_id,
            session_dir=session_dir,
            workspace_root=workspace.root,
            distro=target_distro,
            custom_image=custom_image,
            writable_shared_repos=writable_shared_repos,
        )
        if res.get('success'):
            caller = get_active_caller()
            if caller and caller.is_hub:
                write_session_metadata(session_dir, {'started_by_hub': True})
            timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
            timeline.log_status(
                f"Started session sandbox container `{res.get('container_name')}` (distro: `{target_distro}`)."
            )
        return res

    # 23. exec_in_session
    @server.tool()
    def exec_in_session(
        session_id: str,
        command: str,
        workdir: str = '/workspace',
        timeout_seconds: int = 600,
        auto_start: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a shell command (such as `colcon build` or `colcon test`) inside the session's
        isolated sandbox container (`ros-harness-<session_id>`), with the ROS 2 environment
        and `/workspace/install/setup.bash` automatically sourced.

        Host-based agents and subagents MUST use this tool (or `ros-maintainer-harness session exec`)
        for all compilation and test execution instead of running commands directly on the host OS.

        Args:
            session_id: Session identifier.
            command: Shell command string to execute inside the container.
            workdir: Working directory inside the container (default: '/workspace').
            timeout_seconds: Command timeout in seconds (default: 600).
            auto_start: Automatically start the session container if it is not already running.
        """
        if not session_mgr.session_exists(session_id):
            return {'success': False, 'error': f"Session '{session_id}' not found."}

        session_dir = session_mgr.get_session_dir(session_id)
        info = session_mgr.get_session_info(session_id)
        target_distro = info.distro if info and info.distro else 'rolling'

        return do_exec_in_session_container(
            session_id=session_id,
            command=command,
            workdir=workdir,
            timeout=timeout_seconds,
            auto_start=auto_start,
            session_dir=session_dir,
            workspace_root=workspace.root,
            distro=target_distro,
        )

    # 24. stop_session_container
    @server.tool()
    def stop_session_container(session_id: str) -> Dict[str, Any]:
        """
        Stop and remove the sandbox container (`ros-harness-<session_id>`) for a session.

        Args:
            session_id: Session identifier.
        """
        return do_stop_session_container(session_id=session_id)

    # 25. get_workspace_status
    @server.tool()
    def get_workspace_status(check_containers: bool = True) -> Dict[str, Any]:
        """
        Return a unified Maintainer Hub dashboard of all active sessions, their status,
        linked task conversation links (`conversation://<id>`), container state, latest
        timeline milestones, active/failed Jenkins CI runs, and pending approval requests.

        Use this tool in the Hub conversation when the user asks "what is the status of
        things we're working on?".
        """
        return do_get_workspace_status(workspace, check_containers=check_containers)

    # 26. get_next_actions
    @server.tool()
    def get_next_actions(
        repos: Optional[List[str]] = None,
        include_github_prs: bool = True,
        limit_prs: int = 10,
    ) -> Dict[str, Any]:
        """
        Return a prioritized list of recommended next maintainer actions across:
        - Pending approval tickets waiting on the maintainer (`P1_APPROVAL_NEEDED`)
        - Blocked sessions (`P1_SESSION_BLOCKED`)
        - Failing or completed Jenkins CI runs (`P2_CI_FAILURE`, `P2_CI_SUCCESS`)
        - Sessions ready for review or in progress (`P2_SESSION_REVIEW`, `P3_SESSION_IN_PROGRESS`)
        - Open GitHub PRs requesting review or in target repositories (`P4_NEW_PR_TRIAGE`)

        Use this tool in the Hub conversation when the user asks "what should I work on next?".

        Args:
            repos: Optional list of GitHub repositories (e.g. ['ros2/rclcpp', 'ros2/rcutils']) to query for open PRs.
            include_github_prs: Whether to query GitHub via `gh` for open PRs (default: True).
            limit_prs: Maximum number of open GitHub PRs to return (default: 10).
        """
        return do_get_next_actions(
            workspace=workspace,
            repos=repos,
            include_github_prs=include_github_prs,
            limit_prs=limit_prs,
        )

    # 27. start_session_conversation
    @server.tool()
    def start_session_conversation(
        pr_ref: Optional[str] = None,
        session_id: Optional[str] = None,
        distro: Optional[str] = None,
        extra_instructions: Optional[str] = None,
        hub_conversation_id: Optional[str] = None,
        mode: str = 'auto',
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Scaffold a session (if `pr_ref` is given and not yet scaffolded) and launch or prepare
        a dedicated task conversation for that session.

        When `mode='auto'` or `mode='agentapi'` and the `agentapi` CLI is available,
        this spawns a brand-new top-level conversation titled `[<session_id>] <PR Title>`
        and records its `conversation_id` in `session.json`. It also returns `task_prompt` so
        the Hub agent can alternatively spawn a subagent via `invoke_subagent`.

        Args:
            pr_ref: Optional PR reference (e.g. 'ros2/rclcpp#160') to scaffold.
            session_id: Optional session ID (required if `pr_ref` is not provided).
            distro: Optional ROS distro override.
            extra_instructions: Optional additional instructions from the maintainer.
            hub_conversation_id: Optional conversation ID of the calling Hub conversation.
            mode: 'auto', 'agentapi', or 'prompt_only'.
            model: Optional model tier ('flash_lite', 'flash', 'pro').
        """
        try:
            res = do_start_session_conversation(
                workspace=workspace,
                pr_ref=pr_ref,
                session_id=session_id,
                distro=distro,
                extra_instructions=extra_instructions,
                hub_conversation_id=hub_conversation_id,
                mode=mode,
                model=model,
            )
            caller = get_active_caller()
            sid = res.get('session_id') or session_id
            if caller and caller.is_hub and sid:
                sdir = workspace.sessions_dir / str(sid)
                if sdir.is_dir():
                    write_session_metadata(sdir, {'started_by_hub': True})
            return res
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # 28. update_session_status
    @server.tool()
    def update_session_status(
        session_id: str,
        status: Optional[str] = None,
        conversation_id: Optional[str] = None,
        hub_conversation_id: Optional[str] = None,
        topic: Optional[str] = None,
        milestone: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update a session's lifecycle status, linked task `conversation_id`, or `hub_conversation_id`
        in `<session_dir>/session.json`, and optionally record a timeline entry.

        Valid statuses: 'active', 'investigating', 'local_tests_passing', 'waiting_for_ci',
        'needs_review', 'ready_to_merge', 'blocked', 'done'.
        """
        if not session_mgr.session_exists(session_id):
            return {'success': False, 'error': f"Session '{session_id}' not found."}

        session_dir = session_mgr.get_session_dir(session_id)
        updates: Dict[str, Any] = {}
        if status:
            updates['status'] = status
        if conversation_id:
            updates['conversation_id'] = conversation_id
        if hub_conversation_id:
            updates['hub_conversation_id'] = hub_conversation_id
        if topic:
            updates['topic'] = topic

        meta = write_session_metadata(session_dir, updates)
        if message or milestone:
            timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
            timeline.log_status(message=message or f"Updated status to '{status}'.", milestone=milestone)

        conv_id = meta.get('conversation_id')
        return {
            'success': True,
            'session_id': session_id,
            'metadata': meta,
            'conversation_link': format_conversation_link(session_id, conv_id),
            'session_dir_link': format_session_dir_link(session_id, session_dir),
        }

    # 29. push_release
    @server.tool()
    def push_release(
        session_id: str,
        target_branch: str,
        tag: str,
        reason: str = '',
        repo_path: str = '',
        remote: str = 'origin',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Push a local release commit (created by `catkin_prepare_release --no-push`) and its version tag
        (`refs/tags/<tag>`) to the upstream distro branch (`target_branch`).
        Always requires an explicit maintainer approval ticket (`action='release_push'`).

        Args:
            session_id: Active session identifier.
            target_branch: Target upstream release/distro branch (e.g. 'rolling').
            tag: Version tag created by `catkin_prepare_release --no-push` (e.g. '3.10.2').
            reason: MANDATORY explanation for pushing this release commit and tag.
            repo_path: Optional repository path or name (e.g. 'launch' or 'src/launch').
            remote: Remote name (default: 'origin').
            approval_ticket_id: Approval ticket ID approved by the maintainer.
            dry_run: If True, run `git push --dry-run` instead of pushing to the remote.
        """
        return perform_push_release(
            workspace=workspace,
            session_id=session_id,
            repo_path=repo_path,
            target_branch=target_branch,
            tag=tag,
            remote=remote,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
        )

    # 30. run_bloom_release
    @server.tool()
    def run_bloom_release(
        session_id: str,
        repository: str,
        rosdistro: str = 'rolling',
        track: Optional[str] = None,
        non_interactive: bool = True,
        pretend: bool = False,
        no_pull_request: bool = False,
        pull_request_only: bool = False,
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute `bloom-release` on the host for a ROS repository after the release commit and tag
        have been pushed upstream. Always requires an explicit maintainer approval ticket
        (`action='bloom_release'`).

        Args:
            session_id: Active session identifier.
            repository: ROS distro repository name (e.g. 'launch' or 'launch_ros').
            rosdistro: Target ROS distribution (default: 'rolling').
            track: Optional release track name (defaults to `rosdistro`).
            non_interactive: Pass `--non-interactive` (`-y`) to `bloom-release` (default: True).
            pretend: Pass `--pretend` (`-s`) to `bloom-release` (default: False).
            no_pull_request: Pass `--no-pull-request` to `bloom-release` (default: False).
            pull_request_only: Pass `--pull-request-only` (`-p`) to `bloom-release` (default: False).
            reason: MANDATORY explanation for running `bloom-release`.
            approval_ticket_id: Approval ticket ID approved by the maintainer.
            dry_run: Alias for `pretend=True`.
        """
        return perform_run_bloom_release(
            workspace=workspace,
            session_id=session_id,
            repository=repository,
            rosdistro=rosdistro,
            track=track,
            non_interactive=non_interactive,
            pretend=pretend,
            no_web=True,
            no_pull_request=no_pull_request,
            pull_request_only=pull_request_only,
            reason=reason,
            approval_ticket_id=approval_ticket_id,
            dry_run=dry_run,
            approval_mgr=approval_mgr,
        )

    return server


class _GatewayASGIMiddleware:
    """
    ASGI middleware for the HTTP MCP launch service:
    - Serves `GET /health` without requiring auth so containers and host CLI can verify connectivity.
    - Verifies `Authorization: Bearer <token>` (or `X-Ros-Maintainer-Token`) against `TokenStore`
      and binds the resulting `CallerIdentity` to the request context.
    """

    def __init__(
        self,
        app: Any,
        require_auth: bool = False,
        state_dir: Optional[Path] = None,
    ):
        self.app = app
        self.require_auth = require_auth
        self.token_store = TokenStore(state_dir)

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        path = scope.get('path', '')
        if path == '/health':
            body = json.dumps({
                'status': 'ok',
                'version': get_harness_version(),
                'pid': os.getpid(),
            }).encode('utf-8')
            await send({
                'type': 'http.response.start',
                'status': 200,
                'headers': [
                    (b'content-type', b'application/json'),
                    (b'content-length', str(len(body)).encode('ascii')),
                ],
            })
            await send({
                'type': 'http.response.body',
                'body': body,
            })
            return

        raw_token: Optional[str] = None
        for k_bytes, v_bytes in scope.get('headers') or []:
            k = k_bytes.decode('latin-1').lower()
            if k == 'authorization':
                raw_token = v_bytes.decode('latin-1').strip()
                break
            if k == 'x-ros-maintainer-token' and not raw_token:
                raw_token = v_bytes.decode('latin-1').strip()

        caller = self.token_store.verify_token(raw_token)
        if self.require_auth and caller is None:
            body = json.dumps({
                'error': 'unauthorized',
                'message': 'Missing, invalid, or revoked bearer token.',
            }).encode('utf-8')
            await send({
                'type': 'http.response.start',
                'status': 401,
                'headers': [
                    (b'content-type', b'application/json'),
                    (b'content-length', str(len(body)).encode('ascii')),
                    (b'www-authenticate', b'Bearer'),
                ],
            })
            await send({
                'type': 'http.response.body',
                'body': body,
            })
            return

        with caller_context(caller):
            await self.app(scope, receive, send)


def create_gateway_http_app(
    server: MCPServer,
    transport: str = 'streamable-http',
    host: str = '127.0.0.1',
    require_auth: bool = False,
    state_dir: Optional[Path] = None,
) -> Any:
    """Create the Starlette ASGI app wrapped with `/health` and bearer-token auth middleware."""
    transport_security = None
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        allowed_hosts = [
            '127.0.0.1:*',
            'localhost:*',
            '[::1]:*',
            'host.docker.internal:*',
        ]
        if host not in ('127.0.0.1', 'localhost', '::1', '0.0.0.0', '::'):
            allowed_hosts.append(f'{host}:*')
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[
                'http://127.0.0.1:*',
                'http://localhost:*',
                'http://[::1]:*',
                'http://host.docker.internal:*',
            ],
        )
    except Exception:
        transport_security = None

    if transport == 'streamable-http':
        inner_app = server.streamable_http_app(
            transport_security=transport_security,
            host=host,
        )
    elif transport == 'sse':
        inner_app = server.sse_app(
            transport_security=transport_security,
            host=host,
        )
    else:
        raise ValueError(f"Unsupported HTTP transport: '{transport}'.")

    return _GatewayASGIMiddleware(
        inner_app,
        require_auth=require_auth,
        state_dir=state_dir,
    )


def run_server(
    workspace: WorkspaceLayout,
    transport: str = 'stdio',
    host: str = '127.0.0.1',
    port: int = 8765,
    enable_ci_monitor: bool = True,
    require_auth: bool = False,
    allow_wide_bind: bool = False,
) -> None:
    """Run the Host MCP Server Gateway with background CI monitoring."""
    if transport in ('sse', 'streamable-http'):
        valid_host, host_err = validate_gateway_bind_host(host, allow_wide_bind=allow_wide_bind)
        if not valid_host:
            raise ValueError(host_err)
    elif transport != 'stdio':
        raise ValueError(f"Unsupported transport: '{transport}'. Choose 'stdio', 'sse', or 'streamable-http'.")

    server = create_mcp_server(workspace, require_auth=require_auth)
    monitor_service = None
    bridge_forwarder: Optional[_TCPBridgeForwarder] = None
    if enable_ci_monitor:
        ci_tracker = CITracker(workspace.audit_dir / 'ci_runs.json')
        policy = workspace.get_policy()
        jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)
        monitor_service = CIMonitorService(
            tracker=ci_tracker,
            jenkins_mgr=jenkins_mgr,
            sessions_dir=workspace.sessions_dir,
            audit_log_path=workspace.audit_log_path,
            poll_interval_seconds=60.0,
        )
        monitor_service.start()

    try:
        if transport == 'stdio':
            server.run(transport='stdio')
        elif transport in ('sse', 'streamable-http'):
            import anyio
            import uvicorn

            if host == '127.0.0.1':
                bridge_ip = detect_linux_docker_bridge_ip()
                if bridge_ip:
                    forwarder = _TCPBridgeForwarder(listen_host=bridge_ip, port=port, target_host=host)
                    if forwarder.start():
                        bridge_forwarder = forwarder

            asgi_app = create_gateway_http_app(
                server=server,
                transport=transport,
                host=host,
                require_auth=require_auth,
            )
            config = uvicorn.Config(
                asgi_app,
                host=host,
                port=port,
                log_level='info',
            )
            uv_server = uvicorn.Server(config)
            anyio.run(uv_server.serve)
    finally:
        if bridge_forwarder:
            bridge_forwarder.stop()
        if monitor_service:
            monitor_service.stop()
