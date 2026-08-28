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
import re
import subprocess
from typing import Optional, Tuple


def get_repo_remote_url(repo_dir: Path, remote: str = 'origin') -> Optional[str]:
    """Retrieve the URL for a named Git remote."""
    if not repo_dir.exists():
        return None
    res = subprocess.run(
        ['git', 'remote', 'get-url', remote],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return res.stdout.strip()
    return None


def extract_repo_full_name(remote_url: Optional[str]) -> Optional[str]:
    """
    Extract 'owner/repo' from a Git remote URL (HTTPS or SSH).

    Examples:
        - https://github.com/ros2/rclcpp.git -> ros2/rclcpp
        - git@github.com:ros2/rclcpp.git -> ros2/rclcpp
        - https://github.com/wjwwood/rclcpp -> wjwwood/rclcpp
    """
    if not remote_url:
        return None

    # Strip .git suffix
    cleaned = remote_url.strip()
    if cleaned.endswith('.git'):
        cleaned = cleaned[:-4]

    # Match SSH format: git@github.com:owner/repo
    m_ssh = re.match(r'^(?:[\w.-]+@)?[\w.-]+:([^/]+/[^/]+)$', cleaned)
    if m_ssh:
        return m_ssh.group(1)

    # Match HTTPS format: https://github.com/owner/repo
    m_http = re.match(r'^https?://[^/]+/([^/]+/[^/]+)$', cleaned)
    if m_http:
        return m_http.group(1)

    return None


def get_current_branch(repo_dir: Path) -> Optional[str]:
    """Get the current checked-out Git branch name."""
    res = subprocess.run(
        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return res.stdout.strip()
    return None


def get_current_commit_sha(repo_dir: Path) -> Optional[str]:
    """Get the current commit SHA."""
    res = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return res.stdout.strip()
    return None


def execute_git_push(
    repo_dir: Path,
    branch: str,
    remote: str = 'origin',
    force_with_lease: bool = False,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Execute git push with policy-enforced arguments.

    Returns:
        (success: bool, output_or_error_message: str)
    """
    if not repo_dir.exists():
        return (False, f"Repository directory '{repo_dir}' does not exist.")

    cmd = ['git', 'push', remote, branch]
    if force_with_lease:
        cmd.append('--force-with-lease')
    if dry_run:
        cmd.append('--dry-run')

    res = subprocess.run(
        cmd,
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )

    if res.returncode == 0:
        msg = res.stdout.strip() or res.stderr.strip() or f"Successfully pushed {branch} to {remote}."
        return (True, msg)
    else:
        err = res.stderr.strip() or res.stdout.strip() or "git push failed with unknown error."
        return (False, err)
