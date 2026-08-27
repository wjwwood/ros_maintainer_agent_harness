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
import sys

from .approval import ApprovalManager
from .audit import format_audit_record, read_audit_records
from .devcontainer import write_devcontainer_config
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

    return 0


if __name__ == '__main__':
    sys.exit(main())
