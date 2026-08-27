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

from .config import dump_default_policy_yaml, load_policy, HarnessPolicy
from .rules import dump_default_rules_md
from .tools_templates import (
    dump_tool_ci_for_pr,
    dump_tool_find_restarted_ci,
    dump_tool_session_status,
)


def dump_default_tools_readme() -> str:
    return """# Shared Maintainer Tools

This directory contains custom executable scripts shared across all active and future agent sessions.

## Available Tools

### `ros-find-restarted-ci`
- **Purpose**: Discovers rescheduled Jenkins jobs on ci.ros2.org and optionally updates GitHub PR
  comment markdown in-place.
- **Usage**: `ros-find-restarted-ci [-u] <PR_OR_COMMENT_URL>`

### `ros-ci-for-pr`
- **Purpose**: Launch Jenkins CI jobs for PRs with test scoping and distro options.
- **Usage**: `ros-ci-for-pr [--distro <distro>] [--only-fixes-test] <PR_URL>`

### `ros-session-status`
- **Purpose**: Log progress updates or milestone notes to session timeline.
- **Usage**: `ros-session-status [-m <MILESTONE>] "<MESSAGE>"`

## Adding New Tools
1. Place executable scripts in `tools/bin/` (with standard `#!/usr/bin/env python3` or
   `#!/usr/bin/env bash` shebang).
2. Ensure executable permissions (`chmod +x tools/bin/<script_name>`).
3. Document the tool and its flags in this README.
"""


def dump_default_requirements_txt() -> str:
    return """# Shared tool dependencies for container environments
pyyaml>=5.3
requests>=2.25.0
PyGithub>=1.55
"""


class WorkspaceLayout:
    """Manages the host workspace directory layout and initialization."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config_dir = self.root / 'config'
        self.policy_path = self.config_dir / 'policy.yaml'
        self.rules_path = self.config_dir / 'maintainer_rules.md'
        self.tools_dir = self.root / 'tools'
        self.tools_bin_dir = self.tools_dir / 'bin'
        self.tools_readme_path = self.tools_dir / 'README.md'
        self.tools_requirements_path = self.tools_dir / 'requirements.txt'
        self.shared_repos_dir = self.root / 'shared_repos'
        self.sessions_dir = self.root / 'sessions'
        self.audit_dir = self.root / 'audit'
        self.audit_log_path = self.audit_dir / 'audit.jsonl'
        self.approvals_path = self.audit_dir / 'approvals.json'
        self.ci_runs_path = self.audit_dir / 'ci_runs.json'

    def is_initialized(self) -> bool:
        return self.policy_path.exists() and self.config_dir.exists()

    def initialize(self) -> None:
        """Create directory structure and initialize default configuration files."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.tools_bin_dir.mkdir(parents=True, exist_ok=True)
        self.shared_repos_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        if not self.policy_path.exists():
            with open(self.policy_path, 'w', encoding='utf-8') as f:
                f.write(dump_default_policy_yaml())

        if not self.rules_path.exists():
            with open(self.rules_path, 'w', encoding='utf-8') as f:
                f.write(dump_default_rules_md())

        if not self.tools_readme_path.exists():
            with open(self.tools_readme_path, 'w', encoding='utf-8') as f:
                f.write(dump_default_tools_readme())

        if not self.tools_requirements_path.exists():
            with open(self.tools_requirements_path, 'w', encoding='utf-8') as f:
                f.write(dump_default_requirements_txt())

        # Populate default executables in tools/bin/
        tool_scripts = {
            'ros-find-restarted-ci': dump_tool_find_restarted_ci(),
            'ros-ci-for-pr': dump_tool_ci_for_pr(),
            'ros-session-status': dump_tool_session_status(),
        }

        for script_name, content in tool_scripts.items():
            script_path = self.tools_bin_dir / script_name
            if not script_path.exists():
                with open(script_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                script_path.chmod(0o755)

    def get_policy(self) -> HarnessPolicy:
        """Load and return the parsed policy."""
        return load_policy(self.policy_path)
