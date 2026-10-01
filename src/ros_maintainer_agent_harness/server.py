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
from .ci import CIMonitorService, CITracker, JenkinsManager
from .devcontainer import (
    check_token_and_environment,
    exec_in_session_container as do_exec_in_session_container,
    start_session_container as do_start_session_container,
    stop_session_container as do_stop_session_container,
)
from .git_ops import (
    execute_git_push,
    extract_repo_full_name,
    get_repo_remote_url,
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
    a path relative to `session_dir`, an absolute host path, or omitted (auto-detecting
    a single repository worktree under `session_dir / 'src'`).
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
        return session_dir.resolve()

    cleaned = repo_path.strip()
    if cleaned == '/workspace':
        return session_dir.resolve()
    if cleaned.startswith('/workspace/'):
        rel_part = cleaned[len('/workspace/'):]
        return (session_dir / rel_part).resolve()

    cand = Path(cleaned).expanduser()
    if not cand.is_absolute() and (session_dir / cand).exists():
        return (session_dir / cand).resolve()
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
) -> Dict[str, Any]:
    """Execute policy-validated git push with approval ticket gating and timeline/audit logging."""
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
                ['git', 'remote', 'add', '--', remote, meta['head_repo_url']],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )

    remote_url = get_repo_remote_url(repo_dir, remote)
    repo_full_name = extract_repo_full_name(remote_url)
    target_label = f"{repo_full_name or str(repo_dir)}:{branch}"

    # Policy validation
    allowed, msg, requires_approval = policy.validate_git_push(
        branch_name=branch,
        repo_full_name=repo_full_name,
        force_with_lease=force_with_lease,
        force=force,
    )

    if not allowed:
        timeline.log_action(
            action='git_push',
            target=target_label,
            reason=reason,
            status='DENIED',
            details={'error': msg, 'force_with_lease': force_with_lease},
        )
        return {
            'success': False,
            'status': 'DENIED',
            'error': msg,
        }

    # Check approval if required (e.g. external contributor fork)
    if requires_approval:
        if not approval_ticket_id or not approval_mgr.is_approved(approval_ticket_id):
            req = approval_mgr.create_request(
                session_id=session_id,
                action='git_push',
                target=target_label,
                reason=reason,
                details={'remote': remote, 'force_with_lease': force_with_lease},
            )
            timeline.log_action(
                action='git_push',
                target=target_label,
                reason=reason,
                status='PENDING_APPROVAL',
                details={'ticket_id': req.ticket_id, 'info': msg},
            )
            return {
                'success': False,
                'status': 'PENDING_APPROVAL',
                'ticket_id': req.ticket_id,
                'message': f"{msg} Created approval ticket '{req.ticket_id}'.",
            }

    # Execute push
    success, out = execute_git_push(
        repo_dir=repo_dir,
        branch=branch,
        remote=remote,
        force_with_lease=force_with_lease,
        dry_run=dry_run,
    )

    status_str = 'APPROVED' if success else 'FAILED'
    timeline.log_action(
        action='git_push',
        target=target_label,
        reason=reason,
        status=status_str,
        details={'output': out, 'force_with_lease': force_with_lease, 'dry_run': dry_run},
    )

    return {
        'success': success,
        'status': status_str,
        'target': target_label,
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
        if not approval_ticket_id or not approval_mgr.is_approved(approval_ticket_id):
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


def _execute_github_create_pr(
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> Dict[str, Any]:
    """Create a Pull Request on GitHub via the REST API (or `gh api` fallback) on the host."""
    import os
    import subprocess
    import requests

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
) -> Dict[str, Any]:
    """Create a GitHub Pull Request with policy validation and maintainer approval ticket gating."""
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

    if requires_approval:
        if not approval_ticket_id or not approval_mgr.is_approved(approval_ticket_id):
            req = approval_mgr.create_request(
                session_id=session_id,
                action='create_pull_request',
                target=f"{repo}:{head}->{base}",
                reason=reason,
                details={'title': title, 'body': body, 'head': head, 'base': base},
            )
            timeline.log_action(
                action='create_pull_request',
                target=f"{repo}:{head}->{base}",
                reason=reason,
                status='PENDING_APPROVAL',
                details={'ticket_id': req.ticket_id},
            )
            return {
                'success': False,
                'status': 'PENDING_APPROVAL',
                'ticket_id': req.ticket_id,
                'message': f"{msg} Created approval ticket '{req.ticket_id}'.",
            }

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
                target=f"{repo}:{head}->{base}",
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

    timeline.log_action(
        action='create_pull_request',
        target=f"{repo}:{head}->{base}",
        reason=reason,
        status='APPROVED',
        details={'pr_url': pr_html_url, 'pr_number': pr_number, 'dry_run': dry_run},
    )

    return {
        'success': True,
        'status': 'APPROVED',
        'pr_url': pr_html_url,
        'pr_number': pr_number,
        'repo': repo,
        'title': title,
        'dry_run': dry_run,
    }


def create_mcp_server(workspace: WorkspaceLayout) -> MCPServer:
    """Create and configure the Host MCP Server Gateway with safety rules and tools."""
    if not workspace.is_initialized():
        workspace.initialize()

    server = MCPServer('ros-maintainer-harness')
    approval_mgr = ApprovalManager(workspace.audit_dir / 'approvals.json')
    ci_tracker = CITracker(workspace.audit_dir / 'ci_runs.json')
    session_mgr = SessionManager(workspace)
    policy = workspace.get_policy()
    jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)

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
    ) -> Dict[str, Any]:
        """
        Push a branch to a Git remote with safety policy validation and audit logging.

        Args:
            session_id: Active session identifier.
            repo_path: Path to the local git repository or worktree.
            branch: Branch name to push.
            remote: Remote name (default: 'origin').
            force_with_lease: Whether to use --force-with-lease (required for force pushes).
            force: Whether force push is requested.
            reason: MANDATORY explanation for why this push is being executed.
            approval_ticket_id: Ticket ID if this action required prior maintainer approval.
            dry_run: If True, validate policy without executing network git push.
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
    ) -> Dict[str, Any]:
        """
        Create a GitHub Pull Request (requires maintainer approval).

        Args:
            session_id: Active session identifier.
            repo: Target repository full name (e.g. 'ros2/rclcpp').
            title: Pull request title.
            body: Pull request description.
            head: Head branch (e.g. 'wjwwood:feature_branch').
            base: Base branch (e.g. 'rolling').
            reason: MANDATORY explanation for opening this PR.
            approval_ticket_id: Ticket ID approved by maintainer.
            dry_run: If True, simulate creation without GitHub API call.
            body_file: Optional path to a markdown file containing the PR description.
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
        run = None
        if job_url_or_id:
            run = ci_tracker.get_run(job_url_or_id)
        elif session_id:
            run = ci_tracker.get_latest_run_for_session(session_id)
        elif pr_url:
            runs = ci_tracker.list_runs(pr_url=pr_url, limit=1)
            if runs:
                run = runs[0]

        target_url = run.job_url if run else job_url_or_id
        if not target_url:
            return {
                'success': False,
                'status': 'NOT_FOUND',
                'error': 'No CI run found matching the provided parameters.',
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
        run = ci_tracker.get_run(job_url_or_id)
        target_url = run.job_url if run else job_url_or_id

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

        run = ci_tracker.get_run(job_url_or_id)
        target_url = run.job_url if run else job_url_or_id
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
    ) -> Dict[str, Any]:
        """
        Start a detached sandbox container (`ros-harness-<session_id>`) for the given session
        so builds and tests can run in isolation rather than on the host OS.

        Args:
            session_id: Session identifier.
            distro: Optional ROS distro override (defaults to session's configured distro).
            custom_image: Optional custom container image override.
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
        )
        if res.get('success'):
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
            return do_start_session_conversation(
                workspace=workspace,
                pr_ref=pr_ref,
                session_id=session_id,
                distro=distro,
                extra_instructions=extra_instructions,
                hub_conversation_id=hub_conversation_id,
                mode=mode,
                model=model,
            )
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

    return server


def run_server(
    workspace: WorkspaceLayout,
    transport: str = 'stdio',
    host: str = '127.0.0.1',
    port: int = 8765,
    enable_ci_monitor: bool = True,
) -> None:
    """Run the Host MCP Server Gateway with background CI monitoring."""
    server = create_mcp_server(workspace)
    monitor_service = None
    if enable_ci_monitor:
        ci_tracker = CITracker(workspace.audit_dir / 'ci_runs.json')
        policy = workspace.get_policy()
        jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)
        monitor_service = CIMonitorService(
            tracker=ci_tracker,
            jenkins_mgr=jenkins_mgr,
            sessions_dir=workspace.sessions_dir,
            audit_log_path=workspace.audit_log_path,
            poll_interval_seconds=10.0,
        )
        monitor_service.start()

    try:
        if transport == 'stdio':
            server.run(transport='stdio')
        elif transport in ('sse', 'streamable-http'):
            server.run(transport=transport, host=host, port=port)
        else:
            raise ValueError(f"Unsupported transport: '{transport}'. Choose 'stdio', 'sse', or 'streamable-http'.")
    finally:
        if monitor_service:
            monitor_service.stop()
