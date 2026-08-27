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

import dataclasses
import fnmatch
from pathlib import Path
import re
from typing import List, Optional
import yaml

DEFAULT_BLOCKED_BRANCH_PATTERNS = [
    r'^(main|master)$',
    r'^(rolling|jazzy|iron|humble|galactic|foxy|eloquent|dashing|crystal|bouncy|ardent)$',
]


@dataclasses.dataclass
class GitPushPolicy:
    allowed_branch_patterns: List[str] = dataclasses.field(
        default_factory=lambda: [r'^wjwwood/.*$', r'^wjwwood-patch-.*$']
    )
    blocked_branch_patterns: List[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_BLOCKED_BRANCH_PATTERNS)
    )
    allowed_repositories: List[str] = dataclasses.field(
        default_factory=lambda: ['ros2/*', 'ros-tooling/*', 'wjwwood/*']
    )
    require_approval_for_external_forks: bool = True
    require_force_with_lease: bool = True


@dataclasses.dataclass
class JenkinsCIPolicy:
    ci_server: str = 'https://ci.ros2.org'
    max_concurrent_runs_per_pr: int = 1
    cooldown_seconds: int = 300
    auto_cancel_superseded: bool = True


@dataclasses.dataclass
class ServerConfig:
    host: str = '127.0.0.1'
    port: int = 8765
    transport: str = 'sse'  # 'sse' or 'stdio'


@dataclasses.dataclass
class HarnessPolicy:
    github_username: str = ''
    signing_key_id: Optional[str] = None
    git_push: GitPushPolicy = dataclasses.field(default_factory=GitPushPolicy)
    jenkins_ci: JenkinsCIPolicy = dataclasses.field(default_factory=JenkinsCIPolicy)
    server: ServerConfig = dataclasses.field(default_factory=ServerConfig)

    def is_branch_push_allowed(self, branch_name: str) -> bool:
        """Check if branch name matches allowed patterns and does not match blocked base branches."""
        # 1. Built-in hard invariant check: Never push to base distro branches
        for blocked_pat in DEFAULT_BLOCKED_BRANCH_PATTERNS:
            if re.match(blocked_pat, branch_name):
                return False

        # 2. Check user-configured blocked patterns
        for blocked_pat in self.git_push.blocked_branch_patterns:
            if re.match(blocked_pat, branch_name):
                return False

        # 3. Check allowed branch patterns
        for allowed_pat in self.git_push.allowed_branch_patterns:
            if re.match(allowed_pat, branch_name):
                return True

        return False

    def is_repository_allowed(self, repo_full_name: str) -> bool:
        """Check if repository full name matches allowed repository patterns."""
        for pattern in self.git_push.allowed_repositories:
            if fnmatch.fnmatch(repo_full_name, pattern):
                return True
        return False


def load_policy(config_path: Path) -> HarnessPolicy:
    """Load policy configuration from a YAML file."""
    if not config_path.exists():
        return HarnessPolicy()

    with open(config_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}

    git_push_data = data.get('policies', {}).get('git_push', {})
    git_push_policy = GitPushPolicy(
        allowed_branch_patterns=git_push_data.get(
            'allowed_branch_patterns', [r'^wjwwood/.*$', r'^wjwwood-patch-.*$']
        ),
        blocked_branch_patterns=git_push_data.get(
            'blocked_branch_patterns', list(DEFAULT_BLOCKED_BRANCH_PATTERNS)
        ),
        allowed_repositories=git_push_data.get(
            'allowed_repositories', ['ros2/*', 'ros-tooling/*', 'wjwwood/*']
        ),
        require_approval_for_external_forks=git_push_data.get(
            'require_approval_for_external_forks', True
        ),
        require_force_with_lease=git_push_data.get('require_force_with_lease', True),
    )

    jenkins_data = data.get('policies', {}).get('jenkins_ci', {})
    jenkins_policy = JenkinsCIPolicy(
        ci_server=jenkins_data.get('ci_server', 'https://ci.ros2.org'),
        max_concurrent_runs_per_pr=jenkins_data.get('max_concurrent_runs_per_pr', 1),
        cooldown_seconds=jenkins_data.get('cooldown_seconds', 300),
        auto_cancel_superseded=jenkins_data.get('auto_cancel_superseded', True),
    )

    server_data = data.get('policies', {}).get('server', {})
    server_config = ServerConfig(
        host=server_data.get('host', '127.0.0.1'),
        port=server_data.get('port', 8765),
        transport=server_data.get('transport', 'sse'),
    )

    identity_data = data.get('identity', {})
    return HarnessPolicy(
        github_username=identity_data.get('github_username', ''),
        signing_key_id=identity_data.get('signing_key_id'),
        git_push=git_push_policy,
        jenkins_ci=jenkins_policy,
        server=server_config,
    )


def dump_default_policy_yaml() -> str:
    """Generate default policy.yaml string."""
    return """# ROS 2 Maintainer Agent Harness - Policy Configuration
version: 1

identity:
  github_username: ""
  signing_key_id: ""

policies:
  git_push:
    allowed_branch_patterns:
      - '^wjwwood/[a-zA-Z0-9_-]+$'
      - '^wjwwood-patch-\\\\d+$'
      - '^fix/[a-zA-Z0-9_-]+$'
    blocked_branch_patterns:
      - '^(main|master|rolling|jazzy|iron|humble|galactic|foxy)$'
    allowed_repositories:
      - 'ros2/*'
      - 'ros-tooling/*'
      - 'wjwwood/*'
    require_approval_for_external_forks: true
    require_force_with_lease: true

  jenkins_ci:
    ci_server: 'https://ci.ros2.org'
    max_concurrent_runs_per_pr: 1
    cooldown_seconds: 300
    auto_cancel_superseded: true

  server:
    host: '127.0.0.1'
    port: 8765
    transport: 'sse'
"""
