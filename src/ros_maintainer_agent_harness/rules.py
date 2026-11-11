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
- Commits should include DCO sign-off: `Signed-off-by: <Maintainer Name> <maintainer@example.com>`.
- Branch naming format: `<username>/<topic_name>` (use underscores, not dashes).
- When fixing a commit on an active PR, prefer amending rather than squashing if preserving history is desired.

## CI Conventions
- If a PR only changes tests, run CI with `--only-fixes-test` to save build farm resources.
- If a job fails with 404 or node disconnection, check the Jenkins queue and restarted jobs
  with `ros-find-restarted-ci` before re-running.

## Pull Requests & Attribution
- When opening a new pull request, prefer generating a pre-filled GitHub compare URL
  (`create-pr` / `create_pull_request` defaults to `--web-url` / `web_url=True`) so the maintainer can click the link,
  review both the diff and the PR title/description in the browser, and click "Create pull request" themselves.
  Only use `--api` / `web_url=False` if the maintainer explicitly asks the agent to submit the PR directly via the
  GitHub API.
- When opening or updating a pull request, always fill out the target repository or organization PR template.
  For `ros2` repositories (`ros2/.github` `.github/PULL_REQUEST_TEMPLATE.md`), include:
  - `## Description`
  - `### Is this user-facing behavior change?`
    (always include and answer, even if just `No, documentation changes only.`)
  - `### Did you use Generative AI?`
  - `### Additional Information`
    (only include when there is a problem or extra context needed to understand the change;
    otherwise omit this section entirely)
- Always include the Generative AI attribution under `### Did you use Generative AI?`, kept simple and concise
  (tool/model name only, no long disclosure paragraph), e.g. `Yes, Claude Opus 5.5` or `Yes, Gemini`.
- Do NOT mention running local builds or tests in PR descriptions (that is assumed).
- When drafting PR descriptions or comments for the maintainer, prefer providing a copy-pasteable markdown block
  directly in the conversation (with a file as an acceptable fallback). When the snippet itself contains inner
  triple-backtick (` ``` `) code blocks, wrap the outer fence in four backticks (` ````markdown `) so it renders
  as a single copy-pasteable block.

## Writing Style & Copy-Pasteable Snippets
When drafting GitHub review comments, issue/PR replies, PR descriptions, or documentation for the maintainer,
apply these rules directly **without** scraping GitHub history to rediscover the maintainer's voice:
- **Voice & Audience**:
  - Write in a natural, conversational first-person tone (`I think...`, `It might make sense to...`,
    `Looks mostly reasonable to me, but I had a few comments:`, `lgtm! Good catch...`, `For context on why...`).
  - Write for an audience of engineers who already know ROS, C++/Python, and the codebase. Jump straight to the
    concrete problem, mechanism, or trade-off without AI throat-clearing or promotional fluff.
  - Provide just enough technical context (exact function, signal, CMake variable, or race window) that the reader
    can understand the problem and solution without looking things up from scratch, needing only to verify that
    what we are saying is true.
- **Formatting & Punctuation (No "AI-isms")**:
  - Use **minimal or no bold text** in prose and bullets. Never use `- **Bold header:** explanation` bullet prefixes.
  - Never use em-dashes (`--` or unicode em-dash); use parentheses, commas, colons, or separate sentences instead.
  - Never use decorative emojis in section headings or bullet points.
  - Use inline backticks for all code symbols, filenames, CMake variables, CLI flags, and commit SHAs.
  - Use **raw GitHub permalink URLs** (`https://github.com/<org>/<repo>/blob/<sha>/path/to/file#L10-L20` or
    `#discussion_r...`) either on their own line or in parentheses rather than `[line 10](url)` Markdown links,
    so GitHub automatically renders inline code snippet previews when pasted.
- **Structure of Review Comments**:
  - Start with a brief 1-sentence overall take when appropriate, then use flat `-` or numbered bullets walking
    through the specific code path.
  - Separate minor items under a simple line like `A few minor things / nits:` or `One optional cleanup:`.
  - Close with a concrete question when inviting discussion (`What do you think about...?`).

## Releases
- Follow the 3-step release workflow when releasing ROS packages:
  1. Generate and polish changelogs (`catkin_generate_changelog`, edit `CHANGELOG.rst`), commit them, and run
     `catkin_prepare_release --no-push` inside the session container to create the local version bump commit and tag.
  2. Push the release commit and tag via `ros-maintainer-harness release push` (or MCP `push_release`), which requires a
     maintainer approval ticket (`action='release_push'`).
  3. Run `bloom-release` on the host via `ros-maintainer-harness release bloom` (or MCP `run_bloom_release`), which
     requires a maintainer approval ticket (`action='bloom_release'`).

## General
- Always review linter errors before launching Jenkins CI.
"""


get_default_maintainer_rules_template = dump_default_rules_md


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

        lines = content.splitlines()
        header_idx = -1
        for idx, line in enumerate(lines):
            if line.strip().lower() == section_header.lower():
                header_idx = idx
                break

        if header_idx != -1:
            lines.insert(header_idx + 1, rule_bullet)
            updated_content = "\n".join(lines) + "\n"
        else:
            # Append new section at bottom
            updated_content = content.rstrip() + f"\n\n{section_header}\n{rule_bullet}\n"

        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.rules_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)
