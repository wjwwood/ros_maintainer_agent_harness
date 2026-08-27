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

from ros_maintainer_agent_harness.ci import (
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

    def test_ci_tracker(self):
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
            since = tracker.get_seconds_since_last_run('ros2/rclcpp#160')
            self.assertIsNotNone(since)
            self.assertGreaterEqual(since, 0.0)

            # Update status to SUCCESS -> active runs becomes 0
            tracker.update_run_status('https://ci.ros2.org/job/ci_launcher/123/', 'SUCCESS')
            self.assertEqual(tracker.get_active_runs_count('ros2/rclcpp#160'), 0)

    def test_jenkins_manager_launch_and_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_file = Path(temp_dir) / 'ci_runs.json'
            tracker = CITracker(tracker_file)
            mgr = JenkinsManager(ci_server='https://ci.ros2.org', tracker=tracker)

            # Test launch
            res = mgr.launch_ci(
                session_id='session-pr-160',
                pr_url='ros2/rclcpp#160',
                target_distro='rolling',
                only_fixes_test=True,
                dry_run=False,
            )
            self.assertTrue(res['success'])
            self.assertEqual(res['target_distro'], 'rolling')
            self.assertTrue(res['only_fixes_test'])
            self.assertIn('job_url', res)

            # Test discovery
            disc = mgr.find_restarted_ci('ros2/rclcpp#160', update_comment=False, dry_run=True)
            self.assertTrue(disc['success'])
            self.assertEqual(disc['pr_num'], 160)


if __name__ == '__main__':
    unittest.main()
