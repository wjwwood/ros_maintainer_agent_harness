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

import argparse
import os
from pathlib import Path
import subprocess
import sys

from .approval import ApprovalManager
from .audit import format_audit_record, read_audit_records
from .ci import CITracker, JenkinsManager
from .devcontainer import write_devcontainer_config
from .mcp_config import get_agent_launch_info, write_session_mcp_configs
from .rules import MaintainerRules
from .server import run_server
from .workspace import WorkspaceLayout
from .worktree import SessionManager


def get_default_workspace_path() -> Path:
    env_ws = os.environ.get('ROS_MAINTAINER_WS') or os.environ.get('ROS2_MAINTAINER_WS')
    if env_ws:
        return Path(env_ws).resolve()
    return (Path.home() / 'ros_maintainer_ws').resolve()


def handle_init(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)

    if layout.is_initialized():
        print(f"Workspace already initialized at: {ws_path}")
        return 0

    layout.initialize()
    print(f"✅ Successfully initialized maintainer workspace at: {ws_path}")
    print(f"  - Config:    {layout.config_dir}")
    print(f"  - Tools:     {layout.tools_dir}")
    print(f"  - Repos:     {layout.shared_repos_dir}")
    print(f"  - Sessions:  {layout.sessions_dir}")
    print(f"  - Audit log: {layout.audit_dir}")
    return 0


def handle_session_create(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    if not layout.is_initialized():
        layout.initialize()

    mgr = SessionManager(layout)
    session = mgr.create_session(
        session_id=args.session_id,
        topic=args.topic,
        distro=args.distro or 'rolling',
        custom_image=args.image,
    )
    print(f"✅ Created session '{args.session_id}' (distro: {session.distro}) at: {session.session_dir}")
    if session.devcontainer_path:
        print(f"  - Devcontainer: {session.devcontainer_path}")

    if args.repo and args.branch:
        repo_path = Path(args.repo).resolve()
        if not repo_path.exists():
            print(f"Error: Repository path '{repo_path}' does not exist.", file=sys.stderr)
            return 1

        wt_path = mgr.attach_worktree(
            session_id=args.session_id,
            repo_dir=repo_path,
            branch_name=args.branch,
            base_ref=args.base or 'HEAD',
        )
        print(f"  - Attached worktree: {wt_path} (branch: {args.branch})")

    return 0


def handle_session_devcontainer(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    mgr = SessionManager(layout)

    if not mgr.session_exists(args.session_id):
        print(f"Error: Session '{args.session_id}' does not exist.", file=sys.stderr)
        return 1

    session_dir = mgr.get_session_dir(args.session_id)
    config_file = write_devcontainer_config(
        session_dir=session_dir,
        workspace_root=layout.root,
        distro=args.distro or 'rolling',
        custom_image=args.image,
    )
    print(f"✅ Generated .devcontainer configuration at: {config_file}")
    return 0


def handle_session_list(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    mgr = SessionManager(layout)
    sessions = mgr.list_sessions()

    if not sessions:
        print(f"No active sessions found in workspace: {ws_path}")
        return 0

    print(f"\nActive Sessions in {ws_path}:")
    print("=" * 70)
    for s in sessions:
        print(f"📁 Session: {s.session_id}")
        print(f"   Directory: {s.session_dir}")
        if s.active_branches:
            for repo, branch in s.active_branches.items():
                print(f"   * {repo} -> branch: {branch}")
        else:
            print("   * No worktrees attached yet")
        print("-" * 70)
    return 0


def handle_session_prune(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    mgr = SessionManager(layout)

    if not mgr.session_exists(args.session_id):
        print(f"Error: Session '{args.session_id}' does not exist.", file=sys.stderr)
        return 1

    success = mgr.prune_session(args.session_id, force=args.force)
    if success:
        print(f"✅ Successfully pruned session '{args.session_id}'.")
        return 0
    else:
        print(f"Failed to prune session '{args.session_id}'.", file=sys.stderr)
        return 1


def handle_session_launch(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    mgr = SessionManager(layout)

    if not mgr.session_exists(args.session_id):
        print(f"Error: Session '{args.session_id}' does not exist.", file=sys.stderr)
        return 1

    session_dir = mgr.get_session_dir(args.session_id)
    info = mgr.get_session_info(args.session_id)
    distro = info.distro if info else 'rolling'

    # Ensure MCP configs exist
    write_session_mcp_configs(
        session_dir=session_dir,
        workspace_path=layout.root,
        transport=args.transport or 'stdio',
    )

    agent_name = args.agent or 'claude'
    launch_info = get_agent_launch_info(
        session_id=args.session_id,
        session_dir=session_dir,
        workspace_path=layout.root,
        distro=distro,
        agent=agent_name,
    )

    if args.dry_run or args.print_env:
        print(f"🚀 Launch configuration for session '{args.session_id}':")
        print(f"  - Agent:     {launch_info['agent']}")
        print(f"  - Directory: {launch_info['session_dir']}")
        print(f"  - Command:   {' '.join(launch_info['command'])}")
        print("  - Environment:")
        for k, v in launch_info['environment'].items():
            print(f"      {k}={v}")
        return 0

    print(f"🚀 Launching agent '{agent_name}' in session '{args.session_id}'...")
    env = os.environ.copy()
    env.update(launch_info['environment'])

    try:
        res = subprocess.run(launch_info['command'], cwd=str(session_dir), env=env)
        return res.returncode
    except FileNotFoundError:
        print(f"Error: Executable for agent '{agent_name}' not found on PATH.", file=sys.stderr)
        print("Run with --dry-run to inspect the command line and environment variables.", file=sys.stderr)
        return 1


def handle_session_mcp_config(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    mgr = SessionManager(layout)

    if not mgr.session_exists(args.session_id):
        print(f"Error: Session '{args.session_id}' does not exist.", file=sys.stderr)
        return 1

    session_dir = mgr.get_session_dir(args.session_id)
    written = write_session_mcp_configs(
        session_dir=session_dir,
        workspace_path=layout.root,
        transport=args.transport or 'stdio',
        host=args.host or '127.0.0.1',
        port=args.port or 8765,
    )

    print(f"✅ Generated MCP client configuration files in session '{args.session_id}':")
    for fmt, p in written.items():
        print(f"  - {fmt:<8} -> {p}")
    return 0


def handle_rules(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    rules = MaintainerRules(layout.rules_path)

    if args.rules_action == 'show':
        print(rules.load_content())
        return 0
    elif args.rules_action == 'add':
        if not args.category or not args.rule:
            print("Error: Must provide category and rule text.", file=sys.stderr)
            return 1
        rules.record_preference(args.category, args.rule)
        print(f"✅ Added rule to category '{args.category}': {args.rule}")
        return 0

    return 0


def handle_serve(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    if not layout.is_initialized():
        layout.initialize()

    policy = layout.get_policy()
    transport = args.transport or policy.server.transport or 'stdio'
    host = args.host or policy.server.host or '127.0.0.1'
    port = args.port or policy.server.port or 8765

    print(f"🚀 Starting ROS Maintainer MCP Server Gateway on {host}:{port} (transport: {transport})...")
    run_server(workspace=layout, transport=transport, host=host, port=port)
    return 0


def handle_policy(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    policy = layout.get_policy()

    if args.policy_action == 'show':
        if layout.policy_path.exists():
            with open(layout.policy_path, 'r', encoding='utf-8') as f:
                print(f.read())
        else:
            print("Policy file does not exist. Run `ros-maintainer-harness init` first.")
        return 0
    elif args.policy_action == 'check':
        if not args.branch:
            print("Error: --branch is required for policy check.", file=sys.stderr)
            return 1
        allowed, msg, requires_approval = policy.validate_git_push(
            branch_name=args.branch,
            repo_full_name=args.repo,
            force_with_lease=args.force_with_lease,
            force=args.force,
        )
        if allowed:
            if requires_approval:
                print(f"⚠️  POLICY PENDING: {msg}")
            else:
                print(f"✅ POLICY ALLOWED: Branch '{args.branch}' is permitted.")
        else:
            print(f"❌ POLICY DENIED: {msg}", file=sys.stderr)
            return 1
        return 0

    return 0


def handle_audit(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)

    if not layout.audit_log_path.exists():
        print(f"No audit log records found at: {layout.audit_log_path}")
        return 0

    records = read_audit_records(
        audit_log_path=layout.audit_log_path,
        limit=args.limit,
        session_id=args.session,
        action=args.action_type,
        status=args.status,
    )

    if not records:
        print("No matching audit log entries found.")
        return 0

    print(f"\nAudit Log Records ({len(records)} entries):")
    print("=" * 90)
    for rec in records:
        print(format_audit_record(rec))
    print("=" * 90)
    return 0


def handle_approval(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    approval_mgr = ApprovalManager(layout.audit_dir / 'approvals.json')

    if args.approval_action == 'list':
        requests = approval_mgr.list_requests(status=args.status)
        if not requests:
            print("No approval requests found.")
            return 0
        print(f"\nApproval Requests ({len(requests)}):")
        print("=" * 80)
        for r in requests:
            print(f"🎟️  Ticket: {r.ticket_id} [{r.status}]")
            print(f"   Action: {r.action} -> {r.target}")
            print(f"   Reason: {r.reason}")
            if r.resolved_by:
                print(f"   Resolved by: {r.resolved_by} (comment: {r.resolution_comment})")
            print("-" * 80)
        return 0
    elif args.approval_action == 'approve':
        try:
            req = approval_mgr.approve_request(
                ticket_id=args.ticket_id,
                maintainer=args.maintainer or 'maintainer',
                comment=args.comment,
            )
            print(f"✅ Approved ticket '{req.ticket_id}' for action '{req.action}'.")
            return 0
        except KeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    elif args.approval_action == 'reject':
        try:
            req = approval_mgr.reject_request(
                ticket_id=args.ticket_id,
                maintainer=args.maintainer or 'maintainer',
                comment=args.comment,
            )
            print(f"❌ Rejected ticket '{req.ticket_id}' for action '{req.action}'.")
            return 0
        except KeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    return 0


def handle_ci(args: argparse.Namespace) -> int:
    ws_path = Path(args.workspace).resolve() if args.workspace else get_default_workspace_path()
    layout = WorkspaceLayout(ws_path)
    ci_tracker = CITracker(layout.ci_runs_path)
    policy = layout.get_policy()
    jenkins_mgr = JenkinsManager(ci_server=policy.jenkins_ci.ci_server, tracker=ci_tracker)

    if args.ci_action == 'list':
        runs = ci_tracker.list_runs(session_id=args.session, pr_url=args.pr, status=args.status, limit=args.limit)
        if not runs:
            print("No CI runs found.")
            return 0
        print(f"{'STATUS':<12} {'BUILD #':<10} {'PR':<30} {'SESSION':<20} {'JOB URL'}")
        print("-" * 100)
        for r in runs:
            print(f"{r.status:<12} {r.build_num:<10} {r.pr_url:<30} {r.session_id:<20} {r.job_url}")
        return 0

    elif args.ci_action == 'status':
        run = ci_tracker.get_run(args.target)
        target_url = run.job_url if run else args.target

        if args.wait:
            print(f"Polling CI build at '{target_url}' (timeout: {args.timeout}s)...")
            res = jenkins_mgr.poll_job_until_complete(
                target_url,
                timeout_seconds=float(args.timeout),
                poll_interval_seconds=float(args.interval),
            )
            if run and res.get('success'):
                ci_tracker.update_run(
                    job_url=target_url,
                    status=res.get('status'),
                    duration_seconds=res.get('duration_seconds'),
                    test_summary=res.get('test_summary'),
                    failure_reason=res.get('failure_reason'),
                    log_excerpt=res.get('log_excerpt'),
                    artifacts=res.get('artifacts', []),
                )
        else:
            res = jenkins_mgr.fetch_build_status(target_url)

        if not res.get('success'):
            print(f"Error: {res.get('error', 'Failed to fetch build status')}", file=sys.stderr)
            return 1

        print(f"Build Status: {res.get('status')}")
        if res.get('duration_seconds'):
            print(f"Duration:     {res['duration_seconds']:.1f}s")
        if res.get('full_display_name'):
            print(f"Display Name: {res['full_display_name']}")
        print(f"Job URL:      {target_url}")

        test_summary = res.get('test_summary') or jenkins_mgr.fetch_test_report(target_url)
        if test_summary.get('success'):
            tot = test_summary.get('total', 0)
            pas = test_summary.get('passed', 0)
            fai = test_summary.get('failed', 0)
            skp = test_summary.get('skipped', 0)
            print(f"Tests:        Total: {tot}, Passed: {pas}, Failed: {fai}, Skipped: {skp}")
            failures = test_summary.get('failures', [])
            if failures:
                print("\nFailed Tests:")
                for f in failures[:10]:
                    print(f"  ❌ {f.get('class_name')}.{f.get('name')}")
                    if f.get('error_details'):
                        print(f"     Details: {f['error_details'][:150]}")
        return 0

    elif args.ci_action == 'summary':
        run = ci_tracker.get_run(args.target)
        target_url = run.job_url if run else args.target

        build_info = jenkins_mgr.fetch_build_status(target_url)
        test_report = jenkins_mgr.fetch_test_report(target_url)
        status = build_info.get('status', 'UNKNOWN')

        print(f"=== CI Summary for {target_url} ===")
        print(f"Status: {status}")
        if build_info.get('duration_seconds'):
            print(f"Duration: {build_info['duration_seconds']:.1f}s")
        if test_report.get('success'):
            pas = test_report.get('passed', 0)
            tot = test_report.get('total', 0)
            fai = test_report.get('failed', 0)
            print(f"Tests: Passed: {pas} / Total: {tot} (Failed: {fai})")
            for f in test_report.get('failures', [])[:10]:
                print(f"  ❌ {f.get('name')}: {f.get('error_details', '')[:100]}")

        if status in ('FAILURE', 'UNSTABLE'):
            excerpt = jenkins_mgr.fetch_console_excerpt(target_url, max_lines=args.max_lines)
            print("\n--- Console Log Excerpt ---")
            print(excerpt)
        return 0

    elif args.ci_action == 'cancel':
        run = ci_tracker.get_run(args.target)
        target_url = run.job_url if run else args.target
        res = jenkins_mgr.cancel_job(target_url)
        if res.get('success'):
            print(f"✅ Successfully cancelled CI build at '{target_url}'.")
            return 0
        else:
            print(f"Error cancelling CI build: {res.get('error')}", file=sys.stderr)
            return 1

    return 0


def parse_args():
    parser = argparse.ArgumentParser(
        prog='ros-maintainer-harness',
        description='ROS Maintainer Agent Harness & Workspace Manager',
    )
    parser.add_argument(
        '-w', '--workspace',
        type=str,
        default=None,
        help='Maintainer workspace root directory (defaults to $ROS_MAINTAINER_WS or ~/ros_maintainer_ws)',
    )

    subparsers = parser.add_subparsers(dest='command')

    # instructions
    subparsers.add_parser('instructions', help='Show standard instructions and prompt for AI coding agents')

    # init
    subparsers.add_parser('init', help='Initialize workspace layout and default configs')

    # serve
    serve_parser = subparsers.add_parser('serve', help='Run the Host MCP Server Gateway')
    serve_parser.add_argument(
        '--transport', choices=['stdio', 'sse', 'streamable-http'], default=None,
        help='MCP transport protocol (stdio, sse, or streamable-http)',
    )
    serve_parser.add_argument('--host', type=str, default=None, help='Server host (default: 127.0.0.1)')
    serve_parser.add_argument('--port', type=int, default=None, help='Server port (default: 8765)')

    # session
    session_parser = subparsers.add_parser('session', help='Manage multi-task session environments')
    session_subparsers = session_parser.add_subparsers(dest='session_action')

    # session create
    s_create = session_subparsers.add_parser('create', help='Create a new session workspace')
    s_create.add_argument('session_id', type=str, help='Unique session identifier (e.g. session-pr-160)')
    s_create.add_argument('--topic', type=str, default=None, help='Short topic/PR description')
    s_create.add_argument('--distro', type=str, default='rolling', help='Target ROS distro (default: rolling)')
    s_create.add_argument('--image', type=str, default=None, help='Custom Docker image override')
    s_create.add_argument('--repo', type=str, default=None, help='Path to source repository for worktree')
    s_create.add_argument('--branch', type=str, default=None, help='Branch name for session worktree')
    s_create.add_argument(
        '--base', type=str, default='HEAD', help='Base branch/commit to branch from (default: HEAD)'
    )

    # session devcontainer
    s_devcontainer = session_subparsers.add_parser(
        'devcontainer', help='Generate .devcontainer configuration for session'
    )
    s_devcontainer.add_argument('session_id', type=str, help='Session ID')
    s_devcontainer.add_argument('--distro', type=str, default='rolling', help='Target ROS distro (default: rolling)')
    s_devcontainer.add_argument('--image', type=str, default=None, help='Custom Docker image override')

    # session list
    session_subparsers.add_parser('list', help='List active sessions')

    # session prune
    s_prune = session_subparsers.add_parser('prune', help='Prune session worktrees and directory')
    s_prune.add_argument('session_id', type=str, help='Session ID to prune')
    s_prune.add_argument(
        '-f', '--force', action='store_true', help='Force remove worktree even if untracked changes exist'
    )

    # session launch
    s_launch = session_subparsers.add_parser('launch', help='Launch AI coding agent or IDE in session')
    s_launch.add_argument('session_id', type=str, help='Session ID')
    s_launch.add_argument(
        '--agent', choices=['claude', 'cursor', 'code', 'vscode', 'shell'], default='claude',
        help='Agent or editor to launch (default: claude)',
    )
    s_launch.add_argument(
        '--transport', choices=['stdio', 'sse', 'streamable-http'], default=None,
        help='MCP transport protocol for client configs',
    )
    s_launch.add_argument(
        '--dry-run', action='store_true', help='Print command and environment variables without executing',
    )
    s_launch.add_argument('--print-env', action='store_true', help='Print launch environment variables')

    # session mcp-config
    s_mcp = session_subparsers.add_parser('mcp-config', help='Generate MCP client configuration files in session')
    s_mcp.add_argument('session_id', type=str, help='Session ID')
    s_mcp.add_argument(
        '--transport', choices=['stdio', 'sse', 'streamable-http'], default='stdio',
        help='MCP transport protocol (default: stdio)',
    )
    s_mcp.add_argument('--host', type=str, default='127.0.0.1', help='Host for network transport')
    s_mcp.add_argument('--port', type=int, default=8765, help='Port for network transport')

    # rules
    rules_parser = subparsers.add_parser('rules', help='View or update maintainer style & preferences')
    rules_subparsers = rules_parser.add_subparsers(dest='rules_action')
    rules_subparsers.add_parser('show', help='Display maintainer_rules.md')
    r_add = rules_subparsers.add_parser('add', help='Record a new preference rule')
    r_add.add_argument('category', type=str, help='Category name (e.g. Git, CI, Testing)')
    r_add.add_argument('rule', type=str, help='Rule text description')

    # policy
    policy_parser = subparsers.add_parser('policy', help='Inspect or test safety policies')
    policy_subparsers = policy_parser.add_subparsers(dest='policy_action')
    policy_subparsers.add_parser('show', help='Show policy.yaml configuration')
    p_check = policy_subparsers.add_parser('check', help='Check whether a git push or action is allowed')
    p_check.add_argument('--branch', type=str, required=True, help='Branch name to check')
    p_check.add_argument('--repo', type=str, default=None, help='Repository full name (e.g. ros2/rclcpp)')
    p_check.add_argument('--force', action='store_true', help='Check force push')
    p_check.add_argument('--force-with-lease', action='store_true', help='Check force-with-lease push')

    # audit
    audit_parser = subparsers.add_parser('audit', help='Inspect audit logs')
    audit_subparsers = audit_parser.add_subparsers(dest='audit_action')
    a_show = audit_subparsers.add_parser('show', help='Show recent audit log records')
    a_show.add_argument('-n', '--limit', type=int, default=20, help='Maximum number of records to show')
    a_show.add_argument('--session', type=str, default=None, help='Filter by session ID')
    a_show.add_argument('--action-type', type=str, default=None, help='Filter by action type')
    a_show.add_argument('--status', type=str, default=None, help='Filter by status (APPROVED, DENIED, etc.)')

    # approval
    appr_parser = subparsers.add_parser('approval', help='Manage maintainer approval tickets')
    appr_subparsers = appr_parser.add_subparsers(dest='approval_action')
    appr_list = appr_subparsers.add_parser('list', help='List approval requests')
    appr_list.add_argument('--status', choices=['PENDING', 'APPROVED', 'REJECTED'], default=None)

    appr_ok = appr_subparsers.add_parser('approve', help='Approve an approval request')
    appr_ok.add_argument('ticket_id', type=str, help='Ticket ID to approve')
    appr_ok.add_argument('--maintainer', type=str, default='maintainer', help='Approver name')
    appr_ok.add_argument('--comment', type=str, default=None, help='Approval comment')

    appr_no = appr_subparsers.add_parser('reject', help='Reject an approval request')
    appr_no.add_argument('ticket_id', type=str, help='Ticket ID to reject')
    appr_no.add_argument('--maintainer', type=str, default='maintainer', help='Rejecter name')
    appr_no.add_argument('--comment', type=str, default=None, help='Rejection comment')

    # ci
    ci_parser = subparsers.add_parser('ci', help='Query and monitor Jenkins CI runs')
    ci_subparsers = ci_parser.add_subparsers(dest='ci_action')

    ci_list = ci_subparsers.add_parser('list', help='List tracked CI runs')
    ci_list.add_argument('--session', type=str, default=None, help='Filter by session ID')
    ci_list.add_argument('--pr', type=str, default=None, help='Filter by PR URL or shorthand')
    ci_list.add_argument('--status', type=str, default=None, help='Filter by status (RUNNING, SUCCESS, etc.)')
    ci_list.add_argument('-n', '--limit', type=int, default=20, help='Max records to display')

    ci_status = ci_subparsers.add_parser('status', help='Get build status for a CI job or PR')
    ci_status.add_argument('target', type=str, help='Jenkins job URL, build number, or PR shorthand')
    ci_status.add_argument('--wait', action='store_true', help='Block and poll until build finishes')
    ci_status.add_argument('--timeout', type=int, default=120, help='Polling timeout in seconds (default: 120)')
    ci_status.add_argument('--interval', type=float, default=5.0, help='Polling interval in seconds (default: 5.0)')

    ci_summary = ci_subparsers.add_parser('summary', help='Get concise failure diagnosis and log excerpt')
    ci_summary.add_argument('target', type=str, help='Jenkins job URL, build number, or PR shorthand')
    ci_summary.add_argument('--max-lines', type=int, default=50, help='Max lines of error log excerpt')

    ci_cancel = ci_subparsers.add_parser('cancel', help='Abort a running Jenkins CI job')
    ci_cancel.add_argument('target', type=str, help='Jenkins job URL or build number to cancel')
    ci_cancel.add_argument('--reason', type=str, required=True, help='Reason for aborting the build')

    return parser.parse_args()


def handle_instructions(args: argparse.Namespace) -> int:
    from .instructions import get_agent_system_prompt
    print(get_agent_system_prompt())
    return 0


def main():
    args = parse_args()

    if not args.command:
        print("Run `ros-maintainer-harness --help` for usage instructions.")
        return 0

    if args.command == 'instructions':
        return handle_instructions(args)
    elif args.command == 'init':
        return handle_init(args)
    elif args.command == 'serve':
        return handle_serve(args)
    elif args.command == 'session':
        if args.session_action == 'create':
            return handle_session_create(args)
        elif args.session_action == 'devcontainer':
            return handle_session_devcontainer(args)
        elif args.session_action == 'list':
            return handle_session_list(args)
        elif args.session_action == 'prune':
            return handle_session_prune(args)
        elif args.session_action == 'launch':
            return handle_session_launch(args)
        elif args.session_action == 'mcp-config':
            return handle_session_mcp_config(args)
        else:
            print("Run `ros-maintainer-harness session --help` for session commands.")
            return 0
    elif args.command == 'rules':
        return handle_rules(args)
    elif args.command == 'policy':
        return handle_policy(args)
    elif args.command == 'audit':
        return handle_audit(args)
    elif args.command == 'approval':
        return handle_approval(args)
    elif args.command == 'ci':
        return handle_ci(args)

    return 0


if __name__ == '__main__':
    sys.exit(main())
