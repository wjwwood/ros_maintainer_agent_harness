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
import tempfile
import unittest

from unittest.mock import MagicMock, patch

from ros_maintainer_agent_harness.ci import (
    CIMonitorService,
    CITracker,
    JenkinsManager,
    parse_pr_url,
)


class TestCI(unittest.TestCase):

    def test_parse_pr_url(self):
        self.assertEqual(
            parse_pr_url('https://github.com/ros2/rclcpp/pull/160'),
            ('ros2/rclcpp', 160),
        )
        self.assertEqual(
            parse_pr_url('https://github.com/ros2/rclcpp/pull/160#issuecomment-123456'),
            ('ros2/rclcpp', 160),
        )
        self.assertEqual(
            parse_pr_url('https://github.com/ros2/rclcpp/pull/160?tab=files'),
            ('ros2/rclcpp', 160),
        )
        self.assertEqual(
            parse_pr_url('https://github.com/ros2/rclcpp/pull/160/files'),
            ('ros2/rclcpp', 160),
        )
        self.assertEqual(
            parse_pr_url('ros2/rosidl_typesupport_fastrtps#99'),
            ('ros2/rosidl_typesupport_fastrtps', 99),
        )
        self.assertEqual(parse_pr_url('invalid_url'), (None, None))

    def test_ci_tracker_extended(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_file = Path(temp_dir) / 'ci_runs.json'
            tracker = CITracker(tracker_file)

            # Initially 0 active runs
            self.assertEqual(tracker.get_active_runs_count('ros2/rclcpp#160'), 0)
            self.assertIsNone(tracker.get_seconds_since_last_run('ros2/rclcpp#160'))

            # Record run
            rec = tracker.record_run(
                pr_url='ros2/rclcpp#160',
                session_id='session-1',
                job_name='ci_launcher',
                build_num=123,
                job_url='https://ci.ros2.org/job/ci_launcher/123/',
                status='RUNNING',
            )
            self.assertEqual(rec.status, 'RUNNING')
            self.assertEqual(tracker.get_active_runs_count('ros2/rclcpp#160'), 1)
            self.assertEqual(len(tracker.get_active_runs()), 1)

            # Lookup by ID / URL
            self.assertIsNotNone(tracker.get_run('https://ci.ros2.org/job/ci_launcher/123/'))
            self.assertIsNotNone(tracker.get_run('123'))
            self.assertIsNotNone(tracker.get_run('ros2/rclcpp#160'))

            # Update run with test summary
            test_summary = {'total': 100, 'passed': 98, 'failed': 2, 'skipped': 0, 'failures': [{'name': 'test_1'}]}
            updated = tracker.update_run(
                job_url='https://ci.ros2.org/job/ci_launcher/123/',
                status='FAILURE',
                duration_seconds=150.5,
                test_summary=test_summary,
                failure_reason='2 tests failed',
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, 'FAILURE')
            self.assertEqual(updated.duration_seconds, 150.5)
            self.assertEqual(tracker.get_active_runs_count('ros2/rclcpp#160'), 0)

            # List runs with filters
            list_res = tracker.list_runs(session_id='session-1', status='FAILURE')
            self.assertEqual(len(list_res), 1)

    def test_jenkins_manager_fetch_build_status(self):
        tracker = MagicMock()
        mgr = JenkinsManager(ci_server='https://ci.ros2.org', tracker=tracker)

        # Mock successful build response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'building': False,
            'result': 'SUCCESS',
            'duration': 120000,
            'estimatedDuration': 130000,
            'fullDisplayName': 'ci_launcher #123',
            'artifacts': [{'fileName': 'results.tar.gz', 'relativePath': 'results.tar.gz'}],
        }

        with patch.object(mgr.session, 'get', return_value=mock_resp):
            res = mgr.fetch_build_status('https://ci.ros2.org/job/ci_launcher/123/')
            self.assertTrue(res['success'])
            self.assertEqual(res['status'], 'SUCCESS')
            self.assertEqual(res['duration_seconds'], 120.0)
            self.assertEqual(len(res['artifacts']), 1)

    def test_jenkins_manager_fetch_test_report(self):
        mgr = JenkinsManager(ci_server='https://ci.ros2.org')

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'totalCount': 10,
            'failCount': 1,
            'skipCount': 0,
            'suites': [
                {
                    'cases': [
                        {
                            'className': 'rclcpp.test_executor',
                            'name': 'test_race_condition',
                            'status': 'FAILED',
                            'errorDetails': 'Expected: true, Actual: false',
                            'errorStackTrace': 'test_executor.cpp:45',
                            'duration': 0.5,
                        },
                        {
                            'className': 'rclcpp.test_executor',
                            'name': 'test_clean_shutdown',
                            'status': 'PASSED',
                            'duration': 0.1,
                        },
                    ]
                }
            ],
        }

        with patch.object(mgr.session, 'get', return_value=mock_resp):
            rep = mgr.fetch_test_report('https://ci.ros2.org/job/ci_launcher/123/')
            self.assertTrue(rep['success'])
            self.assertEqual(rep['total'], 10)
            self.assertEqual(rep['failed'], 1)
            self.assertEqual(rep['passed'], 9)
            self.assertEqual(len(rep['failures']), 1)
            self.assertEqual(rep['failures'][0]['name'], 'test_race_condition')
            self.assertEqual(rep['failures'][0]['error_details'], 'Expected: true, Actual: false')

    def test_jenkins_manager_fetch_console_excerpt(self):
        mgr = JenkinsManager(ci_server='https://ci.ros2.org')

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            "Starting build...\n"
            "Scanning dependencies...\n"
            "/workspace/src/rclcpp/src/executor.cpp:42:10: error: 'mutex' was not declared in this scope\n"
            "   42 |   std::lock_guard<std::mutex> lock(mutex_);\n"
            "FAILED: CMakeFiles/rclcpp.dir/src/executor.cpp.o\n"
            "ninja: build stopped: subcommand failed.\n"
        )

        with patch.object(mgr.session, 'get', return_value=mock_resp):
            excerpt = mgr.fetch_console_excerpt('https://ci.ros2.org/job/ci_launcher/123/', max_lines=10)
            self.assertIn("error: 'mutex' was not declared", excerpt)
            self.assertIn("FAILED: CMakeFiles/rclcpp.dir/src/executor.cpp.o", excerpt)

    def test_jenkins_manager_cancel_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = CITracker(Path(temp_dir) / 'ci_runs.json')
            tracker.record_run(
                pr_url='ros2/rclcpp#160',
                session_id='session-1',
                job_name='ci_launcher',
                build_num=123,
                job_url='https://ci.ros2.org/job/ci_launcher/123/',
            )
            mgr = JenkinsManager(ci_server='https://ci.ros2.org', tracker=tracker)

            mock_resp = MagicMock()
            mock_resp.status_code = 200

            with patch.object(mgr.session, 'post', return_value=mock_resp):
                res = mgr.cancel_job('https://ci.ros2.org/job/ci_launcher/123/')
                self.assertTrue(res['success'])
                self.assertEqual(res['status'], 'CANCELLED')
                self.assertEqual(tracker.get_run('123').status, 'CANCELLED')

    def test_ci_monitor_service_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            tracker_file = temp_path / 'ci_runs.json'
            sessions_dir = temp_path / 'sessions'
            session_dir = sessions_dir / 'session-1'
            session_dir.mkdir(parents=True)
            audit_log = temp_path / 'audit.jsonl'

            tracker = CITracker(tracker_file)
            tracker.record_run(
                pr_url='ros2/rclcpp#160',
                session_id='session-1',
                job_name='ci_launcher',
                build_num=123,
                job_url='https://ci.ros2.org/job/ci_launcher/123/',
                status='RUNNING',
            )

            mgr = JenkinsManager(ci_server='https://ci.ros2.org', tracker=tracker)

            # Mock build status changing to SUCCESS
            mock_build = {
                'success': True,
                'status': 'SUCCESS',
                'building': False,
                'result': 'SUCCESS',
                'duration_seconds': 45.0,
                'artifacts': [],
            }
            mock_test = {'success': True, 'total': 10, 'passed': 10, 'failed': 0, 'skipped': 0}

            with patch.object(mgr, 'fetch_build_status', return_value=mock_build):
                with patch.object(mgr, 'fetch_test_report', return_value=mock_test):
                    service = CIMonitorService(
                        tracker=tracker,
                        jenkins_mgr=mgr,
                        sessions_dir=sessions_dir,
                        audit_log_path=audit_log,
                        poll_interval_seconds=1.0,
                    )
                    # 1. Test synchronous poll
                    completed = service.poll_active_runs_once()
                    self.assertEqual(len(completed), 1)
                    self.assertEqual(completed[0].status, 'SUCCESS')

                    # 2. Test start and stop
                    try:
                        service.start()
                        self.assertTrue(service.is_running())
                    finally:
                        service.stop()
                    self.assertFalse(service.is_running())

                    # Verify timeline entry was recorded
                    timeline_text = (session_dir / 'timeline.md').read_text(encoding='utf-8')
                    self.assertIn('🏆 **Milestone**: Jenkins CI Succeeded', timeline_text)
                    self.assertIn('Build #123 finished successfully in 45.0s', timeline_text)

    def test_jenkins_manager_live_launch_ci_with_gist_and_child_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = CITracker(Path(temp_dir) / 'ci_runs.json')
            mgr = JenkinsManager(
                ci_server='https://ci.ros2.org',
                tracker=tracker,
                auth=('testuser', 'testtoken'),
            )

            def fake_get(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if 'ros2.repos' in url:
                    resp.text = (
                        "repositories:\n"
                        "  ros2/launch:\n"
                        "    type: git\n"
                        "    url: https://github.com/ros2/launch.git\n"
                        "    version: rolling\n"
                    )
                elif '/pulls/1002' in url:
                    resp.json.return_value = {
                        'head': {
                            'ref': 'fix-unique-junit-test-names',
                            'repo': {'full_name': 'sachingp19/launch'},
                        },
                        'base': {
                            'repo': {'full_name': 'ros2/launch'},
                        },
                    }
                elif 'crumbIssuer' in url:
                    resp.json.return_value = {
                        'crumbRequestField': 'Jenkins-Crumb',
                        'crumb': 'crumb-123',
                    }
                elif '/job/ci_launcher/api/json' in url:
                    resp.json.return_value = {'nextBuildNumber': 38000}
                elif 'consoleText' in url:
                    resp.text = (
                        "Started by user testuser\n"
                        "* Linux [![Build Status](http://ci.ros2.org/buildStatus/icon?job=ci_linux&build=25001)]"
                        "(http://ci.ros2.org/job/ci_linux/25001/)\n"
                        "* Windows [![Build Status](http://ci.ros2.org/buildStatus/icon?job=ci_windows&build=26001)]"
                        "(http://ci.ros2.org/job/ci_windows/26001/)\n"
                        "Finished: SUCCESS\n"
                    )
                return resp

            captured_posts = []

            def fake_post(url, **kwargs):
                captured_posts.append((url, kwargs))
                resp = MagicMock()
                resp.status_code = 201
                if 'api.github.com/gists' in url:
                    resp.json.return_value = {
                        'html_url': 'https://gist.github.com/testuser/abc123',
                        'files': {
                            'ros2.repos': {
                                'raw_url': 'https://gist.githubusercontent.com/testuser/abc123/raw/ros2.repos',
                            },
                        },
                    }
                elif 'buildWithParameters' in url:
                    resp.status_code = 302
                elif '/issues/1002/comments' in url:
                    resp.json.return_value = {
                        'html_url': 'https://github.com/ros2/launch/pull/1002#issuecomment-999',
                    }
                return resp

            with patch.object(mgr.session, 'get', side_effect=fake_get):
                with patch.object(mgr.session, 'post', side_effect=fake_post):
                    res = mgr.launch_ci(
                        session_id='pr-launch-1002',
                        pr_url='ros2/launch#1002',
                        target_distro='rolling',
                        only_fixes_test=True,
                        packages=['launch'],
                        comment=True,
                        dry_run=False,
                    )

            self.assertTrue(res['success'])
            self.assertEqual(res['build_num'], 38000)
            self.assertEqual(
                res['gist_url'],
                'https://gist.githubusercontent.com/testuser/abc123/raw/ros2.repos',
            )
            self.assertEqual(len(res['child_jobs']), 2)
            self.assertEqual(res['child_jobs'][0]['job_name'], 'ci_linux')
            self.assertEqual(res['child_jobs'][0]['build_num'], 25001)
            self.assertIn('--packages-up-to launch', res['parameters']['CI_BUILD_ARGS'])
            self.assertIn('--packages-select launch', res['parameters']['CI_TEST_ARGS'])
            self.assertEqual(res['parameters']['CI_EL_RELEASE'], '10')
            self.assertEqual(res['parameters']['CI_SCRIPTS_BRANCH'], 'master')

            # Verify Jenkins buildWithParameters POST used form `data` with `json` field
            jenkins_posts = [kw for url, kw in captured_posts if 'buildWithParameters' in url]
            self.assertEqual(len(jenkins_posts), 1)
            self.assertIn('data', jenkins_posts[0])
            self.assertIn('json', jenkins_posts[0]['data'])
            self.assertFalse(jenkins_posts[0].get('allow_redirects', True))

            self.assertEqual(
                res['comment_url'],
                'https://github.com/ros2/launch/pull/1002#issuecomment-999',
            )
            runs = tracker.list_runs(session_id='pr-launch-1002')
            self.assertEqual(len(runs), 3)

    def test_detect_session_packages_and_auto_launch(self):
        import json
        from ros_maintainer_agent_harness.ci import detect_session_packages
        from ros_maintainer_agent_harness.server import perform_launch_jenkins_ci
        from ros_maintainer_agent_harness.workspace import WorkspaceLayout

        with tempfile.TemporaryDirectory() as temp_dir:
            ws = WorkspaceLayout(Path(temp_dir))
            ws.initialize()
            session_dir = ws.sessions_dir / 'pr-launch-712'
            pkg_dir = session_dir / 'src' / 'launch' / 'launch'
            (pkg_dir / 'actions').mkdir(parents=True)
            (session_dir / 'src' / 'launch' / '.git').mkdir(parents=True)
            (pkg_dir / 'package.xml').write_text(
                '<?xml version="1.0"?>\n<package format="3">\n  <name>launch</name>\n</package>\n',
                encoding='utf-8',
            )
            (session_dir / 'session.json').write_text(
                json.dumps({
                    'session_id': 'pr-launch-712',
                    'pr_ref': 'ros2/launch#712',
                    'pr_url': 'https://github.com/ros2/launch/pull/712',
                    'distro': 'rolling',
                    'changed_files': ['launch/actions/include_launch_description.py'],
                }),
                encoding='utf-8',
            )

            detected = detect_session_packages(session_dir)
            self.assertEqual(detected, ['launch'])

            res = perform_launch_jenkins_ci(
                workspace=ws,
                session_id='pr-launch-712',
                reason='Test auto-detected PR URL and packages',
                dry_run=True,
            )
            self.assertTrue(res['success'])
            self.assertEqual(res['packages'], ['launch'])
            self.assertEqual(res['details']['target_distro'], 'rolling')

    def test_jenkins_manager_auth_retry_on_403(self):
        mgr = JenkinsManager(ci_server='https://ci.ros2.org')
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.text = 'Authentication required'

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            'building': False,
            'result': 'SUCCESS',
            'duration': 12000,
            'estimatedDuration': 15000,
            'artifacts': [],
            'fullDisplayName': 'ci_linux #30675',
        }

        def fake_resolve():
            mgr.auth = ('testuser', 'testtoken')
            mgr.session.auth = mgr.auth
            return mgr.auth

        with patch.object(mgr, '_resolve_github_auth', side_effect=fake_resolve) as mock_resolve:
            with patch.object(mgr.session, 'get', side_effect=[resp_403, resp_200]) as mock_get:
                status = mgr.fetch_build_status('https://ci.ros2.org/job/ci_linux/30675/')
                self.assertTrue(status['success'])
                self.assertEqual(status['status'], 'SUCCESS')
                self.assertEqual(mock_resolve.call_count, 1)
                self.assertEqual(mock_get.call_count, 2)

    def test_find_restarted_ci_live_discovery_and_comment_update(self):
        mgr = JenkinsManager(
            ci_server='https://ci.ros2.org',
            auth=('testuser', 'testtoken'),
        )
        comment_body = (
            "Gist: https://gist.githubusercontent.com/testuser/abcdef123456/raw/ros2.repos\n"
            "BUILD args: --packages-above-and-dependencies rclcpp\n"
            "TEST args: --packages-above rclcpp\n"
            "ROS Distro: rolling\n"
            "Job: ci_launcher\n"
            "ci_launcher ran: https://ci.ros2.org/job/ci_launcher/38000\n"
            "* Linux [![Build Status](http://ci.ros2.org/buildStatus/icon?job=ci_linux&build=25001)]"
            "(http://ci.ros2.org/job/ci_linux/25001/)\n"
            "* Windows [![Build Status](http://ci.ros2.org/buildStatus/icon?job=ci_windows&build=26001)]"
            "(http://ci.ros2.org/job/ci_windows/26001/)\n"
        )

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if 'api.github.com/repos/ros2/rclcpp/issues/160/comments' in url:
                resp.json.return_value = [
                    {
                        'id': 987654,
                        'html_url': 'https://github.com/ros2/rclcpp/pull/160#issuecomment-987654',
                        'body': comment_body,
                    }
                ]
            elif '/job/ci_linux/api/json' in url:
                resp.json.return_value = {
                    'builds': [
                        {'number': 25005, 'result': 'SUCCESS', 'building': False},
                        {'number': 25001, 'result': 'ABORTED', 'building': False},
                    ]
                }
            elif '/job/ci_linux/25005/api/json' in url:
                resp.json.return_value = {
                    'building': False,
                    'result': 'SUCCESS',
                    'actions': [
                        {'causes': [{'upstreamProject': 'ci_launcher', 'upstreamBuild': 38000}]},
                    ],
                }
            elif '/job/ci_linux/25001/api/json' in url:
                resp.json.return_value = {
                    'building': False,
                    'result': 'ABORTED',
                    'actions': [
                        {'causes': [{'upstreamProject': 'ci_launcher', 'upstreamBuild': 38000}]},
                    ],
                }
            elif '/job/ci_windows/api/json' in url:
                resp.json.return_value = {'builds': []}
            elif '/queue/api/json' in url:
                resp.json.return_value = {
                    'items': [
                        {
                            'id': 444,
                            'task': {'name': 'ci_windows'},
                            'causes': [{'upstreamProject': 'ci_launcher', 'upstreamBuild': 38000}],
                            'params': 'CI_ROS2_REPOS_URL=https://gist.githubusercontent.com/testuser/abcdef123456',
                        }
                    ]
                }
            return resp

        mock_patch_resp = MagicMock()
        mock_patch_resp.status_code = 200

        with patch.object(mgr.session, 'get', side_effect=fake_get):
            with patch.object(mgr.session, 'patch', return_value=mock_patch_resp) as mock_patch:
                res = mgr.find_restarted_ci('ros2/rclcpp#160', update_comment=True, dry_run=False)
                self.assertTrue(res['success'])
                self.assertEqual(res['restarted_jobs_found'], 1)
                self.assertEqual(res['restarted_jobs'][0]['job_name'], 'ci_linux')
                self.assertEqual(res['restarted_jobs'][0]['initial_build'], 25001)
                self.assertEqual(res['restarted_jobs'][0]['latest_build'], 25005)
                self.assertEqual(len(res['queued_jobs']), 1)
                self.assertEqual(res['queued_jobs'][0]['job_name'], 'ci_windows')
                self.assertEqual(res['queued_jobs'][0]['queue_item_id'], 444)
                self.assertTrue(res['updated_comment'])
                self.assertIn('/ci_linux/25005/', res['updated_comment_markdown'])
                self.assertEqual(mock_patch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
