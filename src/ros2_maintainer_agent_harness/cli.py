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

from .rules import MaintainerRules
from .workspace import WorkspaceLayout
from .worktree import SessionManager


def get_default_workspace_path() -> Path:
    env_ws = os.environ.get('ROS2_MAINTAINER_WS')
    if env_ws:
        return Path(env_ws).resolve()
    return (Path.home() / 'ros2_maintainer_ws').resolve()


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
    session = mgr.create_session(args.session_id, topic=args.topic)
    print(f"✅ Created session '{args.session_id}' at: {session.session_dir}")

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


def parse_args():
    parser = argparse.ArgumentParser(
        prog='ros2-maintainer-harness',
        description='ROS 2 Maintainer Agent Harness & Workspace Manager',
    )
    parser.add_argument(
        '-w', '--workspace',
        type=str,
        default=None,
        help='Maintainer workspace root directory (defaults to $ROS2_MAINTAINER_WS or ~/ros2_maintainer_ws)',
    )

    subparsers = parser.add_subparsers(dest='command')

    # init
    subparsers.add_parser('init', help='Initialize workspace layout and default configs')

    # session
    session_parser = subparsers.add_parser('session', help='Manage multi-task session environments')
    session_subparsers = session_parser.add_subparsers(dest='session_action')

    # session create
    s_create = session_subparsers.add_parser('create', help='Create a new session workspace')
    s_create.add_argument('session_id', type=str, help='Unique session identifier (e.g. session-pr-160)')
    s_create.add_argument('--topic', type=str, default=None, help='Short topic/PR description')
    s_create.add_argument('--repo', type=str, default=None, help='Path to source repository for worktree')
    s_create.add_argument('--branch', type=str, default=None, help='Branch name for session worktree')
    s_create.add_argument(
        '--base', type=str, default='HEAD', help='Base branch/commit to branch from (default: HEAD)'
    )

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

    return parser.parse_args()


def main():
    args = parse_args()

    if not args.command:
        print("Run `ros2-maintainer-harness --help` for usage instructions.")
        return 0

    if args.command == 'init':
        return handle_init(args)
    elif args.command == 'session':
        if args.session_action == 'create':
            return handle_session_create(args)
        elif args.session_action == 'list':
            return handle_session_list(args)
        elif args.session_action == 'prune':
            return handle_session_prune(args)
        else:
            print("Run `ros2-maintainer-harness session --help` for session commands.")
            return 0
    elif args.command == 'rules':
        return handle_rules(args)

    return 0


if __name__ == '__main__':
    sys.exit(main())
