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

import os
from pathlib import Path
import re
import subprocess
from typing import Optional, Tuple


def get_repo_remote_url(repo_dir: Path, remote: str = 'origin') -> Optional[str]:
    """Retrieve the URL for a named Git remote."""
    if not repo_dir.exists():
        return None
    try:
        res = subprocess.run(
            ['git', 'remote', 'get-url', '--', remote],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def extract_repo_full_name(remote_url: Optional[str]) -> Optional[str]:
    """
    Extract 'owner/repo' from a Git remote URL (HTTPS, SSH, SCP-syntax, git-protocol, or file path).

    Examples:
        - https://github.com/ros2/rclcpp.git -> ros2/rclcpp
        - git@github.com:ros2/rclcpp.git -> ros2/rclcpp
        - ssh://git@github.com/ros2/rclcpp.git -> ros2/rclcpp
        - ssh://git@github.com:22/ros2/rclcpp.git -> ros2/rclcpp
        - git://github.com/ros2/rclcpp.git -> ros2/rclcpp
        - /path/to/ros2/rclcpp.git -> ros2/rclcpp
        - https://github.com/wjwwood/rclcpp -> wjwwood/rclcpp
    """
    if not remote_url:
        return None

    cleaned = remote_url.strip()
    # Strip URL fragments and query parameters
    cleaned = cleaned.split('#')[0].split('?')[0].rstrip('/')
    if cleaned.endswith('.git'):
        cleaned = cleaned[:-4]

    # Match URI scheme format: [scheme]://[user@]host[:port]/.../owner/repo
    m_uri = re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[\w.-]+@)?[\w.-]+(?::\d+)?(?:/.*)?/([^/]+/[^/]+)$', cleaned)
    if m_uri:
        return m_uri.group(1)

    # Match SCP-like SSH format: [user@]host:owner/repo
    m_scp = re.match(r'^(?:[\w.-]+@)?[\w.-]+:([^/]+/[^/]+)$', cleaned)
    if m_scp:
        return m_scp.group(1)

    # Match local path format with at least 2 directory components: .../owner/repo
    m_path = re.match(r'^.+/([^/]+/[^/]+)$', cleaned)
    if m_path:
        return m_path.group(1)

    return None


def get_current_branch(repo_dir: Path) -> Optional[str]:
    """Get the current checked-out Git branch name (returns None on detached HEAD)."""
    if not repo_dir.exists():
        return None
    try:
        res = subprocess.run(
            ['git', 'symbolic-ref', '--short', '-q', 'HEAD'],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_current_commit_sha(repo_dir: Path) -> Optional[str]:
    """Get the current commit SHA."""
    if not repo_dir.exists():
        return None
    try:
        res = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
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

    cmd = ['git', 'push']
    if force_with_lease:
        cmd.append('--force-with-lease')
    if dry_run:
        cmd.append('--dry-run')
    cmd.extend(['--', remote, branch])

    try:
        res = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
    except subprocess.TimeoutExpired:
        return (False, "git push timed out after 120 seconds.")
    except Exception as e:
        return (False, f"git push failed: {e}")

    if res.returncode == 0:
        msg = res.stdout.strip() or res.stderr.strip() or f"Successfully pushed {branch} to {remote}."
        return (True, msg)
    else:
        err = res.stderr.strip() or res.stdout.strip() or "git push failed with unknown error."
        return (False, err)
