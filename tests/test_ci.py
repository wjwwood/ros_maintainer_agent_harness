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


if __name__ == '__main__':
    unittest.main()
