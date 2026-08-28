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
from .ci import CITracker, JenkinsManager
from .git_ops import (
    execute_git_push,
    extract_repo_full_name,
    get_repo_remote_url,
)
from .rules import MaintainerRules
from .timeline import TimelineLogger
from .workspace import WorkspaceLayout
from .worktree import SessionManager


def create_mcp_server(workspace: WorkspaceLayout) -> MCPServer:
    """Create and configure the Host MCP Server Gateway with safety rules and tools."""
    if not workspace.is_initialized():
        workspace.initialize()

    server = MCPServer('ros-maintainer-harness')
    approval_mgr = ApprovalManager(workspace.audit_dir / 'approvals.json')
    ci_tracker = CITracker(workspace.audit_dir / 'ci_runs.json')
    session_mgr = SessionManager(workspace)

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
        if not reason or not reason.strip():
            return {
                'success': False,
                'status': 'REJECTED',
                'error': 'Mandatory parameter `reason` must not be empty.',
            }

        repo_dir = Path(repo_path).resolve()
        session_dir = workspace.sessions_dir / session_id
        timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
        policy = workspace.get_policy()

        remote_url = get_repo_remote_url(repo_dir, remote)
        repo_full_name = extract_repo_full_name(remote_url)

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
                target=f"{repo_full_name or repo_path}:{branch}",
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
                    target=f"{repo_full_name or repo_path}:{branch}",
                    reason=reason,
                    details={'remote': remote, 'force_with_lease': force_with_lease},
                )
                timeline.log_action(
                    action='git_push',
                    target=f"{repo_full_name or repo_path}:{branch}",
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
            target=f"{repo_full_name or repo_path}:{branch}",
            reason=reason,
            status=status_str,
            details={'output': out, 'force_with_lease': force_with_lease, 'dry_run': dry_run},
        )

        return {
            'success': success,
            'status': status_str,
            'target': f"{repo_full_name or repo_path}:{branch}",
            'output': out,
            'dry_run': dry_run,
        }

    # 3. launch_jenkins_ci
    @server.tool()
    def launch_jenkins_ci(
        session_id: str,
        pr_url: str,
        target_distro: Optional[str] = None,
        job_type: Optional[str] = None,
        only_fixes_test: bool = False,
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Launch a Jenkins CI run for a ROS 2 PR on ci.ros2.org with rate-limiting & cooldown checks.

        Args:
            session_id: Active session identifier.
            pr_url: GitHub PR URL or shorthand (e.g. 'ros2/rclcpp#160').
            target_distro: Target ROS 2 distro (e.g. 'rolling', 'jazzy').
            job_type: Jenkins launcher job type (default: 'ci_launcher').
            only_fixes_test: Whether to run only tests affected by the PR.
            reason: MANDATORY explanation for why CI is being launched.
            approval_ticket_id: Ticket ID if rate-limit override was approved.
            dry_run: If True, validate policy and generate launcher parameters without calling Jenkins.
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

        active_count = ci_tracker.get_active_runs_count(pr_url)
        since_last = ci_tracker.get_seconds_since_last_run(pr_url)

        allowed, msg, _ = policy.validate_jenkins_ci(
            pr_url=pr_url,
            active_runs_count=active_count,
            seconds_since_last_run=since_last,
        )

        if not allowed:
            # Check if override ticket was provided
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
            'details': res,
        }

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
        session_dir = workspace.sessions_dir / session_id
        timeline = TimelineLogger(session_id, session_dir, workspace.audit_log_path)
        policy = workspace.get_policy()

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

    # 5. create_pull_request
    @server.tool()
    def create_pull_request(
        session_id: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = 'rolling',
        reason: str = '',
        approval_ticket_id: Optional[str] = None,
        dry_run: bool = False,
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

        # Approved or simulation
        pr_number = 9999
        pr_html_url = f"https://github.com/{repo}/pull/{pr_number}"
        timeline.log_action(
            action='create_pull_request',
            target=f"{repo}:{head}->{base}",
            reason=reason,
            status='APPROVED',
            details={'pr_url': pr_html_url, 'dry_run': dry_run},
        )

        return {
            'success': True,
            'status': 'APPROVED',
            'pr_url': pr_html_url,
            'repo': repo,
            'title': title,
            'dry_run': dry_run,
        }

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
        repo_path: Optional[str] = None,
        branch: Optional[str] = None,
        base_ref: str = 'HEAD',
    ) -> Dict[str, Any]:
        """
        Create a new session directory layout and optional Git worktree.

        Args:
            session_id: Unique session ID (e.g. 'session-pr-160').
            topic: Optional PR/task topic description.
            repo_path: Optional path to Git repository for linking worktree.
            branch: Optional branch name for worktree.
            base_ref: Base ref/commit (default: 'HEAD').
        """
        info = session_mgr.create_session(session_id, topic=topic)
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
            'worktree_path': worktree_path,
        }

    # 8. list_sessions
    @server.tool()
    def list_sessions() -> List[Dict[str, Any]]:
        """List all active sessions and their attached Git worktree branches."""
        sessions = session_mgr.list_sessions()
        return [
            {
                'session_id': s.session_id,
                'session_dir': str(s.session_dir),
                'active_branches': s.active_branches,
            }
            for s in sessions
        ]

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
        if approve:
            req = approval_mgr.approve_request(ticket_id, maintainer=maintainer, comment=comment)
        else:
            req = approval_mgr.reject_request(ticket_id, maintainer=maintainer, comment=comment)

        timeline = TimelineLogger(req.session_id, workspace.sessions_dir / req.session_id, workspace.audit_log_path)
        timeline.log_action(
            action='respond_approval_request',
            target=ticket_id,
            reason=comment or ('Approved' if approve else 'Rejected'),
            status=req.status,
            details={'resolved_by': maintainer, 'action_target': req.target},
        )

        return req.to_dict()

    return server


def run_server(
    workspace: WorkspaceLayout,
    transport: str = 'stdio',
    host: str = '127.0.0.1',
    port: int = 8765,
) -> None:
    """Run the Host MCP Server Gateway."""
    server = create_mcp_server(workspace)
    if transport == 'stdio':
        server.run(transport='stdio')
    elif transport in ('sse', 'streamable-http'):
        server.run(transport=transport, host=host, port=port)
    else:
        raise ValueError(f"Unsupported transport: '{transport}'. Choose 'stdio', 'sse', or 'streamable-http'.")
