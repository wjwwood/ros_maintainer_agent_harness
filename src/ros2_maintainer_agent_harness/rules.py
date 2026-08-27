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


def dump_default_rules_md() -> str:
    """Return default maintainer_rules.md template."""
    return """# Maintainer Style & Preferences

## Git & Commits
- Commits must include DCO sign-off: `Signed-off-by: William Woodall <wjwwood@google.com>`.
- Branch naming format: `wjwwood/<topic_name>` (use underscores, not dashes).
- When fixing a commit on an active PR, prefer amending rather than squashing if preserving history is desired.

## CI Conventions
- If a PR only changes tests, run CI with `--only-fixes-test` to save build farm resources.
- If a job fails with 404 or node disconnection, check the Jenkins queue and restarted jobs
  with `ros-find-restarted-ci` before re-running.

## General
- Always review linter errors before launching Jenkins CI.
"""


class MaintainerRules:
    """Reader and updater for maintainer_rules.md."""

    def __init__(self, rules_path: Path):
        self.rules_path = rules_path

    def exists(self) -> bool:
        return self.rules_path.exists()

    def load_content(self) -> str:
        """Load raw markdown content."""
        if not self.rules_path.exists():
            return dump_default_rules_md()
        with open(self.rules_path, 'r', encoding='utf-8') as f:
            return f.read()

    def record_preference(self, category: str, rule: str) -> None:
        """
        Append a new rule/preference to the specified category in maintainer_rules.md.
        """
        content = self.load_content() if self.rules_path.exists() else dump_default_rules_md()
        rule_bullet = f"- {rule.strip()}"

        section_header = f"## {category.strip()}"

        if section_header in content:
            # Insert bullet right after section header
            lines = content.splitlines()
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and line.strip().lower() == section_header.lower():
                    new_lines.append(rule_bullet)
                    inserted = True
            updated_content = "\n".join(new_lines) + "\n"
        else:
            # Append new section at bottom
            updated_content = content.rstrip() + f"\n\n{section_header}\n{rule_bullet}\n"

        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.rules_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)
