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

from ros_maintainer_agent_harness.config import (
    dump_default_policy_yaml,
    load_policy,
    HarnessPolicy,
)


class TestConfig(unittest.TestCase):

    def test_default_policy_validation(self):
        policy = HarnessPolicy()

        # Allowed branch patterns
        self.assertTrue(policy.is_branch_push_allowed('maintainer_user/fix_linter'))
        self.assertTrue(policy.is_branch_push_allowed('fix/patch_1'))
        self.assertTrue(policy.is_branch_push_allowed('patch-1'))

        # Blocked base distro branches (hard invariant)
        self.assertFalse(policy.is_branch_push_allowed('main'))
        self.assertFalse(policy.is_branch_push_allowed('master'))
        self.assertFalse(policy.is_branch_push_allowed('rolling'))
        self.assertFalse(policy.is_branch_push_allowed('jazzy'))
        self.assertFalse(policy.is_branch_push_allowed('iron'))
        self.assertFalse(policy.is_branch_push_allowed('humble'))

        # Disallowed non-matching branches
        self.assertFalse(policy.is_branch_push_allowed('feature_xyz'))

        # Allowed repos
        self.assertTrue(policy.is_repository_allowed('ros2/rclcpp'))
        self.assertTrue(policy.is_repository_allowed('ros-tooling/ros-github-scripts'))
        self.assertFalse(policy.is_repository_allowed('unrelated_org/repo'))

    def test_load_policy_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / 'policy.yaml'
            with open(config_file, 'w', encoding='utf-8') as f:
                f.write(dump_default_policy_yaml())

            policy = load_policy(config_file)
            self.assertTrue(policy.is_branch_push_allowed('user/feature_1'))
            self.assertTrue(policy.is_branch_push_allowed('fix/quick_patch'))
            self.assertFalse(policy.is_branch_push_allowed('rolling'))


if __name__ == '__main__':
    unittest.main()
