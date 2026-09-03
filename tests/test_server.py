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

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ros_maintainer_agent_harness.audit import read_audit_records
from ros_maintainer_agent_harness.server import create_mcp_server
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestMCPServer(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name)
        self.workspace = WorkspaceLayout(self.ws_root)
        self.workspace.initialize()
        self.server = create_mcp_server(self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _call(self, name: str, args: dict):
        res = asyncio.run(self.server.call_tool(name, args))
        if not res.content:
            return []
        if len(res.content) == 1:
            text = res.content[0].text
            try:
                return json.loads(text)
            except Exception:
                return text
        items = []
        for c in res.content:
            try:
                items.append(json.loads(c.text))
            except Exception:
                items.append(c.text)
        return items

    def _call_list(self, name: str, args: dict):
        res = asyncio.run(self.server.call_tool(name, args))
        if not res.content:
            return []
        items = []
        for c in res.content:
            try:
                items.append(json.loads(c.text))
            except Exception:
                items.append(c.text)
        return items

    def test_log_status_tool(self):
        res = self._call('log_status', {
            'session_id': 'session-1',
            'message': 'Completed local colcon build',
            'milestone': 'Build Succeeded',
        })
        self.assertIn('Completed local colcon build', res)
        timeline_file = self.workspace.sessions_dir / 'session-1' / 'timeline.md'
        self.assertTrue(timeline_file.exists())
        content = timeline_file.read_text(encoding='utf-8')
        self.assertIn('Build Succeeded', content)
        self.assertIn('Completed local colcon build', content)

    def test_check_policy_tool(self):
        # 1. Blocked branch
        data = self._call('check_policy', {
            'action': 'git_push',
            'target': 'rolling',
        })
        self.assertFalse(data['allowed'])
        self.assertIn('forbidden by safety policy', data['reason'])

        # 2. Allowed branch
        data2 = self._call('check_policy', {
            'action': 'git_push',
            'target': 'wjwwood/fix_typo',
            'details': {'repo': 'ros2/rclcpp'},
        })
        self.assertTrue(data2['allowed'])
        self.assertFalse(data2['requires_approval'])

        # 3. External fork
        data3 = self._call('check_policy', {
            'action': 'git_push',
            'target': 'wjwwood/fix_typo',
            'details': {'repo': 'contributor_user/rclcpp'},
        })
        self.assertTrue(data3['allowed'])
        self.assertTrue(data3['requires_approval'])

    def test_git_push_tool_policy_and_audit(self):
        # Create a dummy local repo and dummy bare remote
        repo_dir = self.ws_root / 'test_repo'
        repo_dir.mkdir()
        subprocess.run(['git', 'init', '-b', 'wjwwood/fix'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=str(repo_dir), check=True)
        (repo_dir / 'file.txt').write_text('content')
        subprocess.run(['git', 'add', '.'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'commit', '-m', 'Commit 1'], cwd=str(repo_dir), check=True)

        remote_dir = self.ws_root / 'ros2' / 'test_repo.git'
        subprocess.run(['git', 'init', '--bare', str(remote_dir)], check=True)
        subprocess.run(['git', 'remote', 'add', 'origin', str(remote_dir)], cwd=str(repo_dir), check=True)

        # 1. Missing reason -> rejected
        res = self._call('git_push', {
            'session_id': 'session-1',
            'repo_path': str(repo_dir),
            'branch': 'wjwwood/fix',
            'reason': '',
        })
        self.assertEqual(res['status'], 'REJECTED')

        # 2. Direct push to base branch -> DENIED
        res_denied = self._call('git_push', {
            'session_id': 'session-1',
            'repo_path': str(repo_dir),
            'branch': 'rolling',
            'reason': 'Accidental push to rolling',
        })
        self.assertEqual(res_denied['status'], 'DENIED')

        # 3. Dry run allowed push
        res_ok = self._call('git_push', {
            'session_id': 'session-1',
            'repo_path': str(repo_dir),
            'branch': 'wjwwood/fix',
            'reason': 'Pushing fix for PR 160',
            'dry_run': True,
        })
        self.assertTrue(res_ok['success'])

        # Verify audit log
        audit_records = read_audit_records(self.workspace.audit_log_path)
        self.assertGreaterEqual(len(audit_records), 2)
        denied_rec = [r for r in audit_records if r['status'] == 'DENIED'][0]
        self.assertEqual(denied_rec['reason'], 'Accidental push to rolling')

    def test_launch_jenkins_ci_and_cooldown(self):
        # 1. Launch CI
        res = self._call('launch_jenkins_ci', {
            'session_id': 'session-pr-160',
            'pr_url': 'ros2/rclcpp#160',
            'target_distro': 'rolling',
            'reason': 'Verify build on Linux',
            'dry_run': False,
        })
        self.assertTrue(res['success'])
        self.assertEqual(res['status'], 'APPROVED')

        # 2. Launch CI again immediately -> RATE_LIMITED / PENDING_APPROVAL ticket created
        res2 = self._call('launch_jenkins_ci', {
            'session_id': 'session-pr-160',
            'pr_url': 'ros2/rclcpp#160',
            'target_distro': 'rolling',
            'reason': 'Immediate rerun',
            'dry_run': False,
        })
        self.assertEqual(res2['status'], 'RATE_LIMITED')
        self.assertIn('ticket_id', res2)

    def test_create_and_respond_approval_request(self):
        # 1. Create PR -> Pending approval
        res = self._call('create_pull_request', {
            'session_id': 'session-1',
            'repo': 'ros2/rclcpp',
            'title': 'Fix memory leak',
            'body': 'Fixes #123',
            'head': 'wjwwood:fix_leak',
            'base': 'rolling',
            'reason': 'Open PR for maintainer review',
        })
        self.assertEqual(res['status'], 'PENDING_APPROVAL')
        ticket_id = res['ticket_id']

        # 2. List approval requests
        tickets = self._call_list('list_approval_requests', {'status': 'PENDING'})
        self.assertTrue(any(t['ticket_id'] == ticket_id for t in tickets))

        # 3. Respond approval request
        resp_res = self._call('respond_approval_request', {
            'ticket_id': ticket_id,
            'approve': True,
            'maintainer': 'wjwwood',
            'comment': 'Approved opening PR',
        })
        self.assertEqual(resp_res['status'], 'APPROVED')

        # 4. Call create_pull_request with approved ticket
        res_approved = self._call('create_pull_request', {
            'session_id': 'session-1',
            'repo': 'ros2/rclcpp',
            'title': 'Fix memory leak',
            'body': 'Fixes #123',
            'head': 'wjwwood:fix_leak',
            'base': 'rolling',
            'reason': 'Open PR for maintainer review',
            'approval_ticket_id': ticket_id,
            'dry_run': True,
        })
        self.assertTrue(res_approved['success'])
        self.assertEqual(res_approved['status'], 'APPROVED')

    def test_session_lifecycle_tools(self):
        # Create session
        res = self._call('create_session', {
            'session_id': 'session-pr-42',
            'topic': 'Memory leak fix',
            'distro': 'jazzy',
        })
        self.assertEqual(res['session_id'], 'session-pr-42')
        self.assertEqual(res['distro'], 'jazzy')
        self.assertIsNotNone(res['devcontainer_path'])
        self.assertTrue((self.workspace.sessions_dir / 'session-pr-42').exists())
        devcontainer_json = (
            self.workspace.sessions_dir / 'session-pr-42' / '.devcontainer' / 'devcontainer.json'
        )
        self.assertTrue(devcontainer_json.exists())

        # List sessions
        sessions = self._call_list('list_sessions', {})
        self.assertTrue(any(s['session_id'] == 'session-pr-42' for s in sessions))

        # Prune session
        prune_res = self._call('prune_session', {
            'session_id': 'session-pr-42',
            'reason': 'Task completed',
        })
        self.assertTrue(prune_res['success'])
        self.assertFalse((self.workspace.sessions_dir / 'session-pr-42').exists())

    def test_rules_tools(self):
        # Add rule
        add_res = self._call('add_maintainer_rule', {
            'category': 'Testing',
            'rule': 'Run colcon test with --event-handlers console_direct+',
            'reason': 'Improve test output visibility',
        })
        self.assertIn('Successfully added rule', add_res)

        # Get rules
        get_res = self._call('get_maintainer_rules', {})
        self.assertIn('console_direct+', get_res)

    def test_ci_monitoring_tools(self):
        # 1. Launch a CI job
        launch_res = self._call('launch_jenkins_ci', {
            'session_id': 'session-ci-test',
            'pr_url': 'ros2/rclcpp#200',
            'target_distro': 'rolling',
            'reason': 'Test CI monitor tools',
            'dry_run': False,
        })
        self.assertTrue(launch_res['success'])
        job_url = launch_res['job_url']

        # 2. List CI runs
        runs = self._call_list('list_ci_runs', {'session_id': 'session-ci-test'})
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['pr_url'], 'ros2/rclcpp#200')

        # 3. Get CI status
        status_res = self._call('get_ci_status', {'job_url_or_id': job_url})
        self.assertIn('status', status_res)

        # 4. Get CI summary
        summary_res = self._call('get_ci_summary', {'job_url_or_id': job_url})
        self.assertTrue(summary_res['success'])
        self.assertIn('total_tests', summary_res)

        # 5. Cancel CI run
        cancel_res = self._call('cancel_ci_run', {
            'job_url_or_id': job_url,
            'reason': 'Aborting redundant test run',
            'session_id': 'session-ci-test',
        })
        self.assertIn('success', cancel_res)

    def test_mcp_config_and_launch_tools(self):
        # 1. Create a session
        self._call('create_session', {
            'session_id': 'session-agent-1',
            'topic': 'Agent launcher test',
            'distro': 'jazzy',
        })

        # 2. Test generate_mcp_config tool
        mcp_res = self._call('generate_mcp_config', {
            'session_id': 'session-agent-1',
            'transport': 'stdio',
        })
        self.assertTrue(mcp_res['success'])
        self.assertIn('written_configs', mcp_res)

        # 3. Test get_session_launch_info tool
        launch_res = self._call('get_session_launch_info', {
            'session_id': 'session-agent-1',
            'agent': 'claude',
        })
        self.assertTrue(launch_res['success'])
        self.assertEqual(launch_res['agent'], 'claude')
        self.assertEqual(launch_res['distro'], 'jazzy')
        self.assertIn('ROS_MAINTAINER_SESSION_ID', launch_res['environment'])


if __name__ == '__main__':
    unittest.main()
