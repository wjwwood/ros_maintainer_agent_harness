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
            allowed, msg, _ = self.policy.validate_git_push(branch_name=branch, repo_full_name='ros2/rclcpp')
            self.assertFalse(allowed)
            self.assertIn("forbidden by safety policy", msg)

    def test_allowed_branch_patterns(self):
        valid_branches = [
            'wjwwood/fix_memory_leak',
            'user123/refactor_ci',
            'fix/crash_on_shutdown',
            'fix-unique-junit-test-names',
            'feature/sub/topic-1.2',
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
            '-option-injection',
            'wjwwood/feat:rolling',
            'HEAD:refs/heads/rolling',
            'foo..bar',
            'foo//bar',
            'branch with spaces',
            'trailing/',
            '',
        ]
        for branch in invalid_branches:
            self.assertFalse(self.policy.is_branch_push_allowed(branch))
            allowed, msg, _ = self.policy.validate_git_push(branch_name=branch, repo_full_name='ros2/rclcpp')
            self.assertFalse(allowed)
            self.assertIn("does not match allowed branch naming patterns", msg)

    def test_legacy_policy_yaml_aliases_and_unknown_key_warning(self):
        import tempfile
        from pathlib import Path
        import warnings
        from ros_maintainer_agent_harness.config import load_policy

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / 'policy.yaml'
            policy_path.write_text(
                'github_username: "testuser"\n'
                'policies:\n'
                '  git:\n'
                '    allowed_repositories:\n'
                '      - "ros2/rclcpp"\n'
                '    require_fork_approval: false\n'
                '    enforce_force_with_lease: true\n'
                '  jenkins:\n'
                '    max_concurrent_runs_per_pr: 2\n'
                '    cooldown_seconds: 120\n'
                '  unknown_policy_section:\n'
                '    foo: bar\n'
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                loaded = load_policy(policy_path)
                self.assertEqual(loaded.github_username, 'testuser')
                self.assertEqual(loaded.git_push.allowed_repositories, ['ros2/rclcpp'])
                self.assertFalse(loaded.git_push.require_approval_for_external_forks)
                self.assertTrue(loaded.git_push.require_force_with_lease)
                self.assertEqual(loaded.jenkins_ci.max_concurrent_runs_per_pr, 2)
                self.assertEqual(loaded.jenkins_ci.cooldown_seconds, 120)
                self.assertTrue(
                    any('unknown_policy_section' in str(w.message) for w in caught),
                    'Expected UserWarning for unknown policy key',
                )

    def test_unspecified_repo_rejected_when_allowlist_enforced(self):
        allowed, msg, req_appr = self.policy.validate_git_push(
            branch_name='wjwwood/fix_topic', repo_full_name=None
        )
        self.assertFalse(allowed)
        self.assertTrue(req_appr)
        self.assertIn("Repository allowlist is enforced", msg)

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

        # External fork where contributor opened PR from their fork's 'rolling' or 'main' branch
        for distro_branch in ('rolling', 'jazzy', 'main'):
            allowed_fork, msg_fork, req_appr_fork = self.policy.validate_git_push(
                branch_name=distro_branch, repo_full_name='ciandonovan/launch'
            )
            self.assertTrue(allowed_fork)
            self.assertTrue(req_appr_fork)
            self.assertIn("requires maintainer approval", msg_fork)

            # Still strictly blocked on maintainer's own fork or official upstream repos
            allowed_own, msg_own, _ = self.policy.validate_git_push(
                branch_name=distro_branch, repo_full_name='wjwwood/launch'
            )
            self.assertFalse(allowed_own)
            self.assertIn("forbidden by safety policy", msg_own)

        # Malformed branch names are still rejected even on external contributor forks
        allowed_bad, _, _ = self.policy.validate_git_push(
            branch_name='-option-injection', repo_full_name='ciandonovan/launch'
        )
        self.assertFalse(allowed_bad)

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
