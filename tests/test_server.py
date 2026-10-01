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
from unittest.mock import patch

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

    def test_git_push_external_fork_from_pr_worktree(self):
        from ros_maintainer_agent_harness.worktree import write_session_metadata

        fork_remote_dir = self.ws_root / 'sachingp19' / 'launch.git'
        subprocess.run(['git', 'init', '--bare', str(fork_remote_dir)], check=True)

        repo_dir = self.ws_root / 'pr_worktree'
        repo_dir.mkdir()
        subprocess.run(['git', 'init', '-b', 'pr-1002'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=str(repo_dir), check=True)
        (repo_dir / 'file.txt').write_text('fork fix')
        subprocess.run(['git', 'add', '.'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'commit', '-m', 'Fix JUnit test names'], cwd=str(repo_dir), check=True)

        session_dir = self.workspace.sessions_dir / 'pr-launch-1002'
        session_dir.mkdir(parents=True, exist_ok=True)
        write_session_metadata(
            session_dir,
            {
                'session_id': 'pr-launch-1002',
                'is_fork': True,
                'head_ref': 'fix-unique-junit-test-names',
                'head_repo_owner': 'sachingp19',
                'head_repo_url': str(fork_remote_dir),
            },
        )

        # 1. Push without ticket -> PENDING_APPROVAL for sachingp19/launch:fix-unique-junit-test-names
        res_pending = self._call('git_push', {
            'session_id': 'pr-launch-1002',
            'repo_path': str(repo_dir),
            'branch': 'fix-unique-junit-test-names',
            'reason': 'Push review fix to contributor fork branch',
        })
        self.assertEqual(res_pending['status'], 'PENDING_APPROVAL')
        ticket_id = res_pending['ticket_id']

        # 2. Approve ticket and push HEAD:refs/heads/fix-unique-junit-test-names to fork remote
        self._call('respond_approval_request', {
            'ticket_id': ticket_id,
            'approve': True,
            'maintainer': 'wjwwood',
        })
        res_pushed = self._call('git_push', {
            'session_id': 'pr-launch-1002',
            'repo_path': str(repo_dir),
            'branch': 'fix-unique-junit-test-names',
            'reason': 'Push review fix to contributor fork branch',
            'approval_ticket_id': ticket_id,
        })
        self.assertTrue(res_pushed['success'])
        self.assertEqual(res_pushed['status'], 'APPROVED')
        self.assertEqual(res_pushed['target'], 'sachingp19/launch:fix-unique-junit-test-names')

        # 3. Also test contributor fork whose PR head branch is named 'rolling' (e.g. ciandonovan/launch:rolling)
        #    and verify CLI handle_git_push + container path resolution (/workspace/src/launch)
        import argparse
        from ros_maintainer_agent_harness.cli import handle_ci, handle_git_push

        fork2_remote_dir = self.ws_root / 'ciandonovan' / 'launch.git'
        subprocess.run(['git', 'init', '--bare', str(fork2_remote_dir)], check=True)

        session_dir_712 = self.workspace.sessions_dir / 'pr-launch-712'
        wt_712 = session_dir_712 / 'src' / 'launch'
        wt_712.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-b', 'pr-712'], cwd=str(wt_712), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(wt_712), check=True)
        subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=str(wt_712), check=True)
        (wt_712 / 'file.txt').write_text('resolve merge conflict')
        subprocess.run(['git', 'add', '.'], cwd=str(wt_712), check=True)
        subprocess.run(['git', 'commit', '-m', 'Merge rolling into PR 712'], cwd=str(wt_712), check=True)

        write_session_metadata(
            session_dir_712,
            {
                'session_id': 'pr-launch-712',
                'pr_ref': 'ros2/launch#712',
                'is_fork': True,
                'head_ref': 'rolling',
                'head_repo_owner': 'ciandonovan',
                'head_repo_url': str(fork2_remote_dir),
            },
        )

        # First call via CLI handle_git_push (using container path /workspace/src/launch) -> PENDING_APPROVAL (exit 1)
        gp_args_1 = argparse.Namespace(
            workspace=str(self.ws_root),
            session='pr-launch-712',
            repo_path='/workspace/src/launch',
            branch='rolling',
            remote='fork',
            force_with_lease=False,
            force=False,
            reason='Push conflict resolution to contributor fork rolling branch',
            approval_ticket_id=None,
            dry_run=False,
            json=True,
        )
        rc1 = handle_git_push(gp_args_1)
        self.assertEqual(rc1, 1)

        tickets = self._call_list('list_approval_requests', {'status': 'PENDING'})
        ticket_712 = [t['ticket_id'] for t in tickets if t['session_id'] == 'pr-launch-712'][0]
        self._call('respond_approval_request', {
            'ticket_id': ticket_712,
            'approve': True,
            'maintainer': 'wjwwood',
        })

        # Second call via CLI handle_git_push with approved ticket (and auto-detected repo_path=None) -> exit 0
        gp_args_2 = argparse.Namespace(
            workspace=str(self.ws_root),
            session='pr-launch-712',
            repo_path=None,
            branch='rolling',
            remote='fork',
            force_with_lease=False,
            force=False,
            reason='Push conflict resolution to contributor fork rolling branch',
            approval_ticket_id=ticket_712,
            dry_run=False,
            json=True,
        )
        rc2 = handle_git_push(gp_args_2)
        self.assertEqual(rc2, 0)

        # Also test CLI ci launch --dry-run
        ci_args = argparse.Namespace(
            workspace=str(self.ws_root),
            ci_action='launch',
            pr_target=None,
            pr_opt=None,
            session='pr-launch-712',
            distro='rolling',
            job_type=None,
            only_fixes_test=False,
            packages=['launch,launch_testing'],
            colcon_build_args=None,
            colcon_test_args=None,
            cmake_args=None,
            extra_repos=None,
            comment=False,
            reason='Run CI for ros2/launch#712 via CLI',
            approval_ticket_id=None,
            dry_run=True,
            json=True,
        )
        self.assertEqual(handle_ci(ci_args), 0)

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
        # 0. Default create_pull_request (web_url=True by default) -> returns pre-filled GitHub compare URL
        #    immediately without requiring an approval ticket
        res_web = self._call('create_pull_request', {
            'session_id': 'session-1',
            'repo': 'ros2/rclcpp',
            'title': 'Fix memory leak',
            'body': (
                '## Description\nFix leak.\n\n'
                '### Is this user-facing behavior change?\nNo.\n\n'
                '### Did you use Generative AI?\nYes, Gemini\n'
            ),
            'head': 'wjwwood:fix_leak',
            'base': 'rolling',
            'reason': 'Generate pre-filled PR link for maintainer review',
        })
        self.assertTrue(res_web['success'])
        self.assertEqual(res_web['status'], 'WEB_URL_READY')
        self.assertEqual(res_web['mode'], 'web_url')
        self.assertIn(
            'https://github.com/ros2/rclcpp/compare/rolling...wjwwood:rclcpp:fix_leak?quick_pull=1',
            res_web['compare_url'],
        )
        self.assertIn('title=Fix%20memory%20leak', res_web['compare_url'])
        self.assertNotIn('template_warnings', res_web)

        # Same-repo branch compare URL (head has no fork prefix or same owner)
        res_web_same = self._call('create_pull_request', {
            'session_id': 'session-1',
            'repo': 'ros2/rclcpp',
            'title': 'Fix memory leak',
            'body': 'Fixes #123',
            'head': 'wjwwood/fix_leak',
            'base': 'rolling',
            'reason': 'Generate pre-filled PR link for same-repo branch',
        })
        self.assertTrue(res_web_same['success'])
        self.assertIn(
            'https://github.com/ros2/rclcpp/compare/rolling...wjwwood/fix_leak?quick_pull=1',
            res_web_same['compare_url'],
        )

        # 1. Create PR via direct API (web_url=False) -> Pending approval
        res = self._call('create_pull_request', {
            'session_id': 'session-1',
            'repo': 'ros2/rclcpp',
            'title': 'Fix memory leak',
            'body': 'Fixes #123',
            'head': 'wjwwood:fix_leak',
            'base': 'rolling',
            'reason': 'Open PR for maintainer review',
            'web_url': False,
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

        # 4. Call create_pull_request with approved ticket (dry_run=True)
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
        self.assertEqual(res_approved['mode'], 'api')

        # 5. Call create_pull_request with body_file and dry_run=False (mocked GitHub API)
        session_dir = self.workspace.sessions_dir / 'session-1'
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / 'pr_body.md').write_text('Fixes #123 with `SIGINT` handling\n', encoding='utf-8')
        with patch('ros_maintainer_agent_harness.server._execute_github_create_pr') as mock_gh_pr:
            mock_gh_pr.return_value = {
                'success': True,
                'pr_url': 'https://github.com/ros2/rclcpp/pull/1003',
                'pr_number': 1003,
            }
            res_live = self._call('create_pull_request', {
                'session_id': 'session-1',
                'repo': 'ros2/rclcpp',
                'title': 'Fix memory leak',
                'body_file': '/workspace/pr_body.md',
                'head': 'wjwwood:fix_leak',
                'base': 'rolling',
                'reason': 'Open PR for maintainer review',
                'approval_ticket_id': ticket_id,
                'dry_run': False,
            })
            self.assertTrue(res_live['success'])
            self.assertEqual(res_live['pr_url'], 'https://github.com/ros2/rclcpp/pull/1003')
            self.assertEqual(res_live['pr_number'], 1003)
            mock_gh_pr.assert_called_once_with(
                repo='ros2/rclcpp',
                title='Fix memory leak',
                body='Fixes #123 with `SIGINT` handling\n',
                head='wjwwood:fix_leak',
                base='rolling',
            )
            self.assertIn('template_warnings', res_live)

        # 6. Test edit_pull_request with compliant ros2 PR template (no ### Additional Information required)
        compliant_body = (
            "## Description\n"
            "Document signal handling.\n\n"
            "### Is this user-facing behavior change?\n"
            "No, documentation changes only.\n\n"
            "### Did you use Generative AI?\n"
            "Yes, Claude Opus 5.5\n"
        )
        (session_dir / 'edit_body.md').write_text(compliant_body, encoding='utf-8')
        res_edit_pending = self._call('edit_pull_request', {
            'session_id': 'session-1',
            'pr_url': 'ros2/launch#1025',
            'body_file': '/workspace/edit_body.md',
            'reason': 'Update PR description to follow ros2 template',
        })
        self.assertEqual(res_edit_pending['status'], 'PENDING_APPROVAL')
        self.assertNotIn('template_warnings', res_edit_pending)
        edit_ticket_id = res_edit_pending['ticket_id']

        self._call('respond_approval_request', {
            'ticket_id': edit_ticket_id,
            'approve': True,
            'maintainer': 'wjwwood',
        })
        with patch('ros_maintainer_agent_harness.server._execute_github_edit_pr') as mock_gh_edit:
            mock_gh_edit.return_value = {
                'success': True,
                'pr_url': 'https://github.com/ros2/launch/pull/1025',
                'pr_number': 1025,
                'title': 'Document signal handling',
            }
            res_edit_live = self._call('edit_pull_request', {
                'session_id': 'session-1',
                'pr_url': 'ros2/launch#1025',
                'reason': 'Update PR description to follow ros2 template',
                'approval_ticket_id': edit_ticket_id,
                'dry_run': False,
            })
            self.assertTrue(res_edit_live['success'])
            self.assertEqual(res_edit_live['pr_url'], 'https://github.com/ros2/launch/pull/1025')
            self.assertNotIn('template_warnings', res_edit_live)
            mock_gh_edit.assert_called_once_with(
                repo='ros2/launch',
                pr_number=1025,
                title=None,
                body=compliant_body,
            )

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

    @patch('ros_maintainer_agent_harness.server.do_scaffold_from_pr')
    def test_scaffold_session_from_pr_tool(self, mock_scaffold):
        from ros_maintainer_agent_harness.pr_harvester import PRMetadata
        from ros_maintainer_agent_harness.scaffolder import ScaffoldResult
        dummy_meta = PRMetadata(
            owner='ros2', repo='rclcpp', number=160, title='Fix timer', body='',
            base_ref='rolling', head_ref='fix', head_repo_owner='ros2',
            head_repo_url='', is_fork=False, url='https://github.com/ros2/rclcpp/pull/160',
            detected_distro='rolling', changed_files=['timer.cpp'],
        )
        mock_scaffold.return_value = ScaffoldResult(
            session_id='pr-rclcpp-160',
            session_dir=Path('/tmp/sessions/pr-rclcpp-160'),
            pr_metadata=dummy_meta,
            worktree_path=Path('/tmp/sessions/pr-rclcpp-160/src/rclcpp'),
            distro='rolling',
            task_file=Path('/tmp/sessions/pr-rclcpp-160/TASK.md'),
            timeline_path=Path('/tmp/sessions/pr-rclcpp-160/timeline.md'),
        )

        res = self._call('scaffold_session_from_pr', {
            'pr_ref': 'ros2/rclcpp#160',
        })
        self.assertTrue(res['success'])
        self.assertEqual(res['session_id'], 'pr-rclcpp-160')
        self.assertEqual(res['distro'], 'rolling')

    def test_push_release_and_run_bloom_release(self):
        import argparse
        from unittest.mock import MagicMock
        from ros_maintainer_agent_harness.cli import handle_release
        from ros_maintainer_agent_harness.worktree import read_session_metadata

        origin_remote = self.ws_root / 'ros2' / 'launch.git'
        subprocess.run(['git', 'init', '--bare', str(origin_remote)], check=True)

        session_dir = self.workspace.sessions_dir / 'pr-launch-release'
        repo_dir = session_dir / 'src' / 'launch'
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-b', 'rolling'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'remote', 'add', 'origin', str(origin_remote)], cwd=str(repo_dir), check=True)
        (repo_dir / 'package.xml').write_text('<version>3.10.1</version>\n')
        subprocess.run(['git', 'add', '.'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'push', 'origin', 'rolling'], cwd=str(repo_dir), check=True)

        # Simulate catkin_prepare_release --no-push on local release-3.10.2 branch
        subprocess.run(['git', 'checkout', '-b', 'release-3.10.2'], cwd=str(repo_dir), check=True)
        (repo_dir / 'CHANGELOG.rst').write_text('3.10.2 changelog\n')
        subprocess.run(['git', 'add', 'CHANGELOG.rst'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'commit', '-m', 'Update changelogs'], cwd=str(repo_dir), check=True)
        (repo_dir / 'package.xml').write_text('<version>3.10.2</version>\n')
        subprocess.run(['git', 'add', 'package.xml'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'commit', '-m', '3.10.2'], cwd=str(repo_dir), check=True)
        subprocess.run(['git', 'tag', '3.10.2'], cwd=str(repo_dir), check=True)

        # 1. Non-existent local tag -> REJECTED
        res_bad_tag = self._call('push_release', {
            'session_id': 'pr-launch-release',
            'repo_path': 'launch',
            'tag': '9.9.9',
            'target_branch': 'rolling',
            'reason': 'Push non-existent tag',
        })
        self.assertFalse(res_bad_tag['success'])
        self.assertEqual(res_bad_tag['status'], 'REJECTED')

        # 2. Valid local tag without approval ticket -> PENDING_APPROVAL
        res_pending = self._call('push_release', {
            'session_id': 'pr-launch-release',
            'repo_path': 'launch',
            'tag': '3.10.2',
            'target_branch': 'rolling',
            'reason': 'Push launch 3.10.2 release commit and tag to rolling',
        })
        self.assertFalse(res_pending['success'])
        self.assertEqual(res_pending['status'], 'PENDING_APPROVAL')
        ticket_id = res_pending['ticket_id']

        # 3. Approve ticket and push via git_push(..., tag='3.10.2') delegation
        self._call('respond_approval_request', {
            'ticket_id': ticket_id,
            'approve': True,
            'maintainer': 'wjwwood',
        })
        res_pushed = self._call('git_push', {
            'session_id': 'pr-launch-release',
            'repo_path': 'launch',
            'branch': 'rolling',
            'tag': '3.10.2',
            'reason': 'Push launch 3.10.2 release commit and tag to rolling',
            'approval_ticket_id': ticket_id,
        })
        self.assertTrue(res_pushed['success'])
        self.assertEqual(res_pushed['status'], 'APPROVED')
        self.assertEqual(res_pushed['tag'], '3.10.2')

        # Verify remote has both rolling branch at tag commit and refs/tags/3.10.2
        remote_tags = subprocess.run(
            ['git', f'--git-dir={origin_remote}', 'tag', '-l', '3.10.2'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(remote_tags, '3.10.2')

        # 4. Run bloom release without ticket -> PENDING_APPROVAL
        bloom_pending = self._call('run_bloom_release', {
            'session_id': 'pr-launch-release',
            'repository': 'launch',
            'rosdistro': 'rolling',
            'reason': 'Run bloom-release for launch 3.10.2 into rolling',
        })
        self.assertFalse(bloom_pending['success'])
        self.assertEqual(bloom_pending['status'], 'PENDING_APPROVAL')
        bloom_ticket_id = bloom_pending['ticket_id']

        # 5. Approve bloom ticket and run with mocked bloom-release executable
        self._call('respond_approval_request', {
            'ticket_id': bloom_ticket_id,
            'approve': True,
            'maintainer': 'wjwwood',
        })
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = (
            '==> Generating pull request to distro file located at '
            "'https://raw.githubusercontent.com/ros/rosdistro/master/rolling/distribution.yaml'\n"
            'Pull request opened at: https://github.com/ros/rosdistro/pull/45678\n'
        )
        mock_proc.stderr = ''
        with patch(
            'ros_maintainer_agent_harness.server._find_bloom_release_executable',
            return_value='/usr/bin/bloom-release',
        ):
            with patch('subprocess.run', return_value=mock_proc):
                bloom_ok = self._call('run_bloom_release', {
                    'session_id': 'pr-launch-release',
                    'repository': 'launch',
                    'rosdistro': 'rolling',
                    'reason': 'Run bloom-release for launch 3.10.2 into rolling',
                    'approval_ticket_id': bloom_ticket_id,
                })
        self.assertTrue(bloom_ok['success'])
        self.assertEqual(bloom_ok['status'], 'APPROVED')
        self.assertEqual(bloom_ok['rosdistro_pr_url'], 'https://github.com/ros/rosdistro/pull/45678')

        meta = read_session_metadata(session_dir)
        self.assertEqual(
            meta.get('last_rosdistro_pr_url'),
            'https://github.com/ros/rosdistro/pull/45678',
        )
        self.assertIn(
            'https://github.com/ros/rosdistro/pull/45678',
            meta.get('rosdistro_pr_urls', []),
        )

        # 6. Test CLI handle_release dry-run for both push and bloom with approved tickets
        rel_push_args = argparse.Namespace(
            workspace=str(self.ws_root),
            release_action='push',
            session='pr-launch-release',
            repo_path='launch',
            tag='3.10.2',
            target_branch='rolling',
            remote='origin',
            reason='Dry run release push',
            approval_ticket_id=ticket_id,
            dry_run=True,
            json=True,
        )
        self.assertEqual(handle_release(rel_push_args), 0)

        rel_bloom_args = argparse.Namespace(
            workspace=str(self.ws_root),
            release_action='bloom',
            session='pr-launch-release',
            repository='launch',
            repository_opt=None,
            rosdistro='rolling',
            track='rolling',
            interactive=False,
            pretend=True,
            no_pull_request=False,
            pull_request_only=False,
            reason='Dry run bloom release',
            approval_ticket_id=bloom_ticket_id,
            dry_run=True,
            json=True,
        )
        with patch(
            'ros_maintainer_agent_harness.server._find_bloom_release_executable',
            return_value='/usr/bin/bloom-release',
        ):
            with patch('subprocess.run', return_value=mock_proc):
                self.assertEqual(handle_release(rel_bloom_args), 0)


if __name__ == '__main__':
    unittest.main()
