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

from ros_maintainer_agent_harness.config import (
    DEFAULT_BLOCKED_BRANCH_PATTERNS,
    GitPushPolicy,
    HarnessPolicy,
    JenkinsCIPolicy,
)


class TestPolicyValidation(unittest.TestCase):

    def setUp(self):
        self.policy = HarnessPolicy(
            github_username='wjwwood',
            git_push=GitPushPolicy(
                allowed_repositories=['ros2/*', 'ros-tooling/*'],
                require_approval_for_external_forks=True,
                require_force_with_lease=True,
            ),
            jenkins_ci=JenkinsCIPolicy(
                max_concurrent_runs_per_pr=1,
                cooldown_seconds=300,
            ),
        )

    def test_base_distro_branches_blocked(self):
        base_branches = [
            'main', 'master', 'rolling', 'jazzy', 'iron', 'humble',
            'galactic', 'foxy', 'eloquent', 'dashing', 'crystal',
            'bouncy', 'ardent', 'kilted', 'lyrical', 'noetic',
        ]
        for branch in base_branches:
            self.assertFalse(self.policy.is_branch_push_allowed(branch))
            allowed, msg, _ = self.policy.validate_git_push(branch_name=branch)
            self.assertFalse(allowed)
            self.assertIn("forbidden by safety policy", msg)

    def test_allowed_branch_patterns(self):
        valid_branches = [
            'wjwwood/fix_memory_leak',
            'user123/refactor_ci',
            'fix/crash_on_shutdown',
            'patch-1',
            'patch-99',
        ]
        for branch in valid_branches:
            self.assertTrue(self.policy.is_branch_push_allowed(branch))
            allowed, _, requires_approval = self.policy.validate_git_push(
                branch_name=branch, repo_full_name='ros2/rclcpp'
            )
            self.assertTrue(allowed)
            self.assertFalse(requires_approval)

    def test_disallowed_branch_patterns(self):
        invalid_branches = [
            'random-branch',
            'feature',
            'foo/bar/baz',  # nested
            '',
        ]
        for branch in invalid_branches:
            self.assertFalse(self.policy.is_branch_push_allowed(branch))
            allowed, msg, _ = self.policy.validate_git_push(branch_name=branch)
            self.assertFalse(allowed)
            self.assertIn("does not match allowed branch naming patterns", msg)

    def test_repository_allowlist(self):
        allowed_repos = ['ros2/rclcpp', 'ros2/rmw', 'ros-tooling/ros-github-scripts']
        for r in allowed_repos:
            self.assertTrue(self.policy.is_repository_allowed(r))

        # When external fork approval is disabled, disallowed repos are blocked immediately
        strict_policy = HarnessPolicy(
            github_username='wjwwood',
            git_push=GitPushPolicy(
                allowed_repositories=['ros2/*', 'ros-tooling/*'],
                require_approval_for_external_forks=False,
            ),
        )
        disallowed_repos = ['other-org/other-repo', 'random/repo']
        for r in disallowed_repos:
            self.assertFalse(strict_policy.is_repository_allowed(r))
            allowed, msg, req_appr = strict_policy.validate_git_push(
                branch_name='wjwwood/test', repo_full_name=r
            )
            self.assertFalse(allowed)
            self.assertFalse(req_appr)
            self.assertIn("not in allowed repositories policy list", msg)

    def test_external_fork_protection(self):
        # Maintainer's own repo/fork
        self.assertFalse(self.policy.is_external_fork('wjwwood/rclcpp'))
        # Standard ROS org
        self.assertFalse(self.policy.is_external_fork('ros2/rclcpp'))
        # External contributor fork
        self.assertTrue(self.policy.is_external_fork('external_user/rclcpp'))

        # External fork push requires approval
        allowed, msg, requires_approval = self.policy.validate_git_push(
            branch_name='wjwwood/fix', repo_full_name='external_user/rclcpp'
        )
        self.assertTrue(allowed)
        self.assertTrue(requires_approval)
        self.assertIn("requires maintainer approval", msg)

    def test_force_push_without_lease_blocked(self):
        allowed, msg, _ = self.policy.validate_git_push(
            branch_name='wjwwood/fix',
            repo_full_name='ros2/rclcpp',
            force=True,
            force_with_lease=False,
        )
        self.assertFalse(allowed)
        self.assertIn("Force push without '--force-with-lease' is prohibited", msg)

        # With lease -> allowed
        allowed, _, _ = self.policy.validate_git_push(
            branch_name='wjwwood/fix',
            repo_full_name='ros2/rclcpp',
            force=True,
            force_with_lease=True,
        )
        self.assertTrue(allowed)

    def test_jenkins_ci_policy_validation(self):
        # 1. Normal run
        allowed, _, req_appr = self.policy.validate_jenkins_ci('ros2/rclcpp#160', active_runs_count=0)
        self.assertTrue(allowed)
        self.assertFalse(req_appr)

        # 2. Exceeded concurrency limit
        allowed, msg, _ = self.policy.validate_jenkins_ci('ros2/rclcpp#160', active_runs_count=1)
        self.assertFalse(allowed)
        self.assertIn("reached maximum limit", msg)

        # 3. Cooldown active
        allowed, msg, _ = self.policy.validate_jenkins_ci(
            'ros2/rclcpp#160', active_runs_count=0, seconds_since_last_run=100
        )
        self.assertFalse(allowed)
        self.assertIn("CI cooldown active", msg)

        # 4. Cooldown expired (350s > 300s)
        allowed, _, _ = self.policy.validate_jenkins_ci(
            'ros2/rclcpp#160', active_runs_count=0, seconds_since_last_run=350
        )
        self.assertTrue(allowed)

    def test_pull_request_creation_requires_approval(self):
        allowed, msg, req_appr = self.policy.validate_pull_request_creation(
            repo_full_name='ros2/rclcpp', base_branch='rolling'
        )
        self.assertTrue(allowed)
        self.assertTrue(req_appr)
        self.assertIn("requires explicit maintainer approval", msg)


if __name__ == '__main__':
    unittest.main()
