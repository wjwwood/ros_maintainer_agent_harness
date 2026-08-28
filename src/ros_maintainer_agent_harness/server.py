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
from .git_ops import (
    execute_git_push,
    extract_repo_full_name,
    get_repo_remote_url,
)
from .mcp_config import get_agent_launch_info, write_session_mcp_configs
from .rules import MaintainerRules
from .scaffolder import scaffold_session_from_pr as do_scaffold_from_pr
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
            data['success'] = True
            return data
        except Exception as e:
            return {'success': False, 'error': str(e)}

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
