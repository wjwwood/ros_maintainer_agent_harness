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

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ros_maintainer_agent_harness.approval import ApprovalManager
from ros_maintainer_agent_harness.ci import CITracker
from ros_maintainer_agent_harness.cli import main
from ros_maintainer_agent_harness.devcontainer import save_workspace_env_var
from ros_maintainer_agent_harness.hub import (
    get_next_actions,
    get_workspace_status,
    parse_timeline_summary,
    read_session_metadata,
    start_session_conversation,
    write_session_metadata,
)
from ros_maintainer_agent_harness.server import create_mcp_server
from ros_maintainer_agent_harness.timeline import TimelineLogger
from ros_maintainer_agent_harness.workspace import WorkspaceLayout
from ros_maintainer_agent_harness.worktree import SessionManager


class TestHubCoordinator(unittest.TestCase):

    def test_session_metadata_and_timeline_parsing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = WorkspaceLayout(Path(temp_dir) / 'ws')
            ws.initialize()
            mgr = SessionManager(ws)
            info = mgr.create_session('pr-rclcpp-160', topic='Fix timer drift', distro='jazzy')

            meta = read_session_metadata(info.session_dir)
            self.assertEqual(meta['session_id'], 'pr-rclcpp-160')
            self.assertEqual(meta['distro'], 'jazzy')
            self.assertEqual(meta['status'], 'active')

            # Log status and milestone to timeline
            tl = TimelineLogger('pr-rclcpp-160', info.session_dir, ws.audit_log_path)
            tl.log_status('Checked out PR branch')
            tl.log_milestone('Local Tests Green', 'All 42 unit tests passed')

            tl_sum = parse_timeline_summary(info.timeline_path, max_entries=3)
            self.assertIn('Local Tests Green', tl_sum['latest_milestone'])
            self.assertGreaterEqual(tl_sum['entry_count'], 3)

            # Update metadata with conversation_id
            conv_uuid = '11111111-2222-3333-4444-555555555555'
            updated = write_session_metadata(
                info.session_dir,
                {'status': 'local_tests_passing', 'conversation_id': conv_uuid},
            )
            self.assertEqual(updated['status'], 'local_tests_passing')
            self.assertEqual(updated['conversation_id'], conv_uuid)

    def test_workspace_status_and_next_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = WorkspaceLayout(Path(temp_dir) / 'ws')
            ws.initialize()
            save_workspace_env_var(ws.root, 'ROS_CONTAINER_GITHUB_TOKEN', 'none')

            mgr = SessionManager(ws)
            s1 = mgr.create_session('pr-rclcpp-160', topic='Fix timer', distro='rolling')
            s2 = mgr.create_session('pr-rcutils-99', topic='Fix allocator', distro='jazzy')

            conv_1 = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
            write_session_metadata(
                s1.session_dir,
                {
                    'pr_ref': 'ros2/rclcpp#160',
                    'pr_url': 'https://github.com/ros2/rclcpp/pull/160',
                    'status': 'waiting_for_ci',
                    'conversation_id': conv_1,
                },
            )
            write_session_metadata(s2.session_dir, {'status': 'blocked'})

            # Add a pending approval and a failed CI run
            appr_mgr = ApprovalManager(ws.audit_dir / 'approvals.json')
            req = appr_mgr.create_request(
                session_id='pr-rclcpp-160',
                action='git_push',
                target='fork:fix-branch',
                reason='Push fix to contributor fork',
            )

            ci_tracker = CITracker(ws.audit_dir / 'ci_runs.json')
            ci_tracker.record_run(
                pr_url='ros2/rclcpp#160',
                session_id='pr-rclcpp-160',
                job_name='ci_launcher',
                build_num=101,
                job_url='https://ci.ros2.org/job/ci_launcher/101/',
                status='FAILURE',
            )

            with patch('ros_maintainer_agent_harness.hub.get_container_status') as mock_cstatus:
                mock_cstatus.return_value = {
                    'running': True,
                    'status': 'running',
                    'container_name': 'ros-harness-pr-rclcpp-160',
                }
                status = get_workspace_status(ws, check_containers=True)

            self.assertEqual(status['summary']['total_sessions'], 2)
            self.assertEqual(status['summary']['pending_approvals'], 1)
            self.assertEqual(status['summary']['failed_ci_runs'], 1)

            s1_stat = next(s for s in status['sessions'] if s['session_id'] == 'pr-rclcpp-160')
            self.assertEqual(
                s1_stat['conversation_link'],
                f"[pr-rclcpp-160](conversation://{conv_1})",
            )

            mock_prs = [
                {
                    'pr_ref': 'ros2/rclcpp#160',
                    'pr_url': 'https://github.com/ros2/rclcpp/pull/160',
                    'number': 160,
                    'repo': 'ros2/rclcpp',
                    'title': 'Already in session',
                    'author': 'alice',
                    'reason': 'Review requested',
                },
                {
                    'pr_ref': 'ros2/rclcpp#200',
                    'pr_url': 'https://github.com/ros2/rclcpp/pull/200',
                    'number': 200,
                    'repo': 'ros2/rclcpp',
                    'title': 'New candidate PR',
                    'author': 'bob',
                    'reason': 'Review requested',
                },
            ]
            with patch('ros_maintainer_agent_harness.hub.query_open_github_prs', return_value=mock_prs):
                with patch('ros_maintainer_agent_harness.hub.get_container_status') as mock_cs:
                    mock_cs.return_value = {'running': False, 'status': 'stopped'}
                    next_res = get_next_actions(ws, include_github_prs=True)

            priorities = [a['priority'] for a in next_res['actions']]
            self.assertIn('P1_APPROVAL_NEEDED', priorities)
            self.assertIn('P2_CI_FAILURE', priorities)
            self.assertIn('P1_SESSION_BLOCKED', priorities)
            self.assertIn('P4_NEW_PR_TRIAGE', priorities)
            # PR #160 is already in a session, so only PR #200 should be in candidate_prs
            self.assertEqual(len(next_res['candidate_prs']), 1)
            self.assertEqual(next_res['candidate_prs'][0]['pr_ref'], 'ros2/rclcpp#200')
            self.assertEqual(req.ticket_id, next_res['actions'][1]['ticket_id'] if priorities[0] == 'P0_ENVIRONMENT'
                             else next_res['actions'][0]['ticket_id'])

    def test_start_session_conversation_and_mcp_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = WorkspaceLayout(Path(temp_dir) / 'ws')
            ws.initialize()
            mgr = SessionManager(ws)
            mgr.create_session('sess-50', topic='Investigate QoS', distro='rolling')

            hub_uuid = '00000000-1111-2222-3333-444444444444'
            spawned_uuid = '99999999-8888-7777-6666-555555555555'

            with patch('ros_maintainer_agent_harness.hub.find_agentapi_executable', return_value='/usr/bin/agentapi'):
                with patch('ros_maintainer_agent_harness.hub.subprocess.run') as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=0,
                        stdout=f'Started conversation {spawned_uuid}\n',
                        stderr='',
                    )
                    res = start_session_conversation(
                        workspace=ws,
                        session_id='sess-50',
                        hub_conversation_id=hub_uuid,
                        mode='agentapi',
                    )

            self.assertTrue(res['success'])
            self.assertEqual(res['launch_method'], 'agentapi')
            self.assertEqual(res['conversation_id'], spawned_uuid)
            self.assertEqual(res['conversation_link'], f"[sess-50](conversation://{spawned_uuid})")
            self.assertIn(hub_uuid, res['task_prompt'])

            # Test MCP tools on create_mcp_server
            server = create_mcp_server(ws)
            tools = getattr(server, '_tools', None)
            if tools is None and hasattr(server, '_tool_manager'):
                tools = {t.name: t.fn for t in server._tool_manager.list_tools()}

            if tools:
                upd_res = tools['update_session_status'](
                    session_id='sess-50',
                    status='needs_review',
                    milestone='Ready for Review',
                    message='All checks complete',
                )
                self.assertTrue(upd_res['success'])
                self.assertEqual(upd_res['metadata']['status'], 'needs_review')

                ws_stat = tools['get_workspace_status'](check_containers=False)
                self.assertEqual(ws_stat['summary']['total_sessions'], 1)

                nx_stat = tools['get_next_actions'](include_github_prs=False)
                self.assertGreaterEqual(nx_stat['total_actions'], 1)

            # Test CLI status, next, and session status
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws.root), 'status', '--no-containers']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('sess-50', fake_out.getvalue())

            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws.root), 'next', '--no-github']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('sess-50', fake_out.getvalue())

            s_stat_args = [
                'ros-maintainer-harness', '-w', str(ws.root),
                'session', 'status', 'sess-50', '--set-status', 'ready_to_merge',
            ]
            with patch.object(sys, 'argv', s_stat_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('ready_to_merge', fake_out.getvalue())


if __name__ == '__main__':
    unittest.main()
