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

import unittest
from unittest.mock import patch

from ros_maintainer_agent_harness.pr_harvester import (
    PRMetadata,
    detect_ros_distro_from_branch,
    fetch_pr_metadata,
    parse_pr_reference,
)


class TestPRHarvester(unittest.TestCase):

    def test_parse_pr_reference(self):
        # Full URLs
        self.assertEqual(
            parse_pr_reference('https://github.com/ros2/rclcpp/pull/160'),
            ('ros2', 'rclcpp', 160),
        )
        self.assertEqual(
            parse_pr_reference('http://github.com/ros-tooling/setup-ros/pull/42/'),
            ('ros-tooling', 'setup-ros', 42),
        )

        # Shorthand
        self.assertEqual(
            parse_pr_reference('ros2/rclcpp#160'),
            ('ros2', 'rclcpp', 160),
        )
        self.assertEqual(
            parse_pr_reference('ros2/rmw/99'),
            ('ros2', 'rmw', 99),
        )

        # Invalid formats
        with self.assertRaises(ValueError):
            parse_pr_reference('not-a-valid-pr-ref')
        with self.assertRaises(ValueError):
            parse_pr_reference('ros2/rclcpp')

    def test_detect_ros_distro_from_branch(self):
        self.assertEqual(detect_ros_distro_from_branch('rolling'), 'rolling')
        self.assertEqual(detect_ros_distro_from_branch('main'), 'rolling')
        self.assertEqual(detect_ros_distro_from_branch('master'), 'rolling')
        self.assertEqual(detect_ros_distro_from_branch('jazzy'), 'jazzy')
        self.assertEqual(detect_ros_distro_from_branch('jazzy-devel'), 'jazzy')
        self.assertEqual(detect_ros_distro_from_branch('iron'), 'iron')
        self.assertEqual(detect_ros_distro_from_branch('humble'), 'humble')
        self.assertEqual(detect_ros_distro_from_branch('custom_branch'), 'rolling')

    @patch('ros_maintainer_agent_harness.pr_harvester.fetch_pr_metadata_via_gh')
    def test_fetch_pr_metadata(self, mock_gh):
        mock_gh.return_value = PRMetadata(
            owner='ros2',
            repo='rclcpp',
            number=160,
            title='Fix race condition in timer callback',
            body='This PR resolves a mutex deadlock during shutdown.',
            base_ref='jazzy',
            head_ref='fix-timer-race',
            head_repo_owner='contributor',
            head_repo_url='https://github.com/contributor/rclcpp.git',
            is_fork=True,
            url='https://github.com/ros2/rclcpp/pull/160',
            detected_distro='jazzy',
            changed_files=['src/rclcpp/timer.cpp', 'test/test_timer.cpp'],
            labels=['bug', 'jazzy'],
            author='contributor',
        )

        meta = fetch_pr_metadata('ros2/rclcpp#160')
        self.assertEqual(meta.owner, 'ros2')
        self.assertEqual(meta.repo, 'rclcpp')
        self.assertEqual(meta.number, 160)
        self.assertEqual(meta.detected_distro, 'jazzy')
        self.assertEqual(meta.shorthand, 'ros2/rclcpp#160')
        self.assertEqual(meta.suggested_session_id, 'pr-rclcpp-160')
        self.assertTrue(meta.is_fork)


if __name__ == '__main__':
    unittest.main()
