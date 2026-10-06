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


def git_safe_cmd(repo_dir: Path, *args: str) -> list[str]:
    """Build a git command list with safe.directory configured for repo_dir."""
    return ['git', '-c', f'safe.directory={repo_dir.resolve()}', *args]


def get_repo_remote_url(repo_dir: Path, remote: str = 'origin', push: bool = False) -> Optional[str]:
    """Retrieve the URL for a named Git remote."""
    if not repo_dir.exists():
        return None
    try:
        args = ['remote', 'get-url']
        if push:
            args.append('--push')
        args.extend(['--', remote])
        res = subprocess.run(
            git_safe_cmd(repo_dir, *args),
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


def is_remote_tracking_writable(repo_dir: Path, remote: str = 'origin') -> bool:
    """
    Check whether the local remote-tracking ref directory (`refs/remotes/<remote>`)
    is writable by the current host user.

    When a repository is cloned or fetched as `root` inside a session container,
    `.git/refs/remotes/<remote>` may be owned by `root:root`, causing `git push <remote>`
    on the host to print `error: update_ref failed for ref 'refs/remotes/...'` after
    pushing to the remote server.
    """
    if not repo_dir.exists():
        return False
    common_dir = repo_dir / '.git'
    try:
        res = subprocess.run(
            git_safe_cmd(repo_dir, 'rev-parse', '--git-common-dir'),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if res.returncode == 0 and res.stdout.strip():
            cand = Path(res.stdout.strip())
            common_dir = cand if cand.is_absolute() else (repo_dir / cand).resolve()
    except Exception:
        pass

    if not common_dir.exists():
        return True
    if not os.access(common_dir, os.W_OK):
        return False

    packed_refs = common_dir / 'packed-refs'
    if packed_refs.exists() and not os.access(packed_refs, os.W_OK):
        return False

    remote_refs_dir = common_dir / 'refs' / 'remotes' / remote
    check_dir = remote_refs_dir
    while not check_dir.exists() and check_dir != common_dir and check_dir.parent != check_dir:
        check_dir = check_dir.parent
    if check_dir.exists() and not os.access(check_dir, os.W_OK):
        return False

    if remote_refs_dir.is_dir():
        try:
            for item in remote_refs_dir.rglob('*'):
                if not os.access(item, os.W_OK) or (item.stat().st_mode & 0o222) == 0:
                    return False
        except Exception:
            return False

    return True


def strip_harmless_remote_ref_lock_errors(text: str) -> str:
    """
    Strip harmless local remote-tracking ref lock error lines emitted by `git push`
    when `.git/refs/remotes/<remote>` is owned by another user (e.g. container root)
    after the remote push itself succeeded.
    """
    from .audit import redact_credentials

    if not text:
        return ''
    filtered_lines = []
    for line in text.splitlines():
        if re.match(
            r"^error:\s+update_ref failed for ref 'refs/remotes/.*cannot lock ref.*Permission denied",
            line.strip(),
            flags=re.IGNORECASE,
        ):
            continue
        filtered_lines.append(line)
    return redact_credentials('\n'.join(filtered_lines).strip())


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

    cleaned = remote_url.strip().replace('\\', '/')
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
            git_safe_cmd(repo_dir, 'symbolic-ref', '--short', '-q', 'HEAD'),
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
            git_safe_cmd(repo_dir, 'rev-parse', 'HEAD'),
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
    force: bool = False,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Execute git push with policy-enforced arguments.

    If the target ``branch`` does not exist as a local branch ref (for example,
    when the session worktree is checked out on ``pr-<num>`` while pushing to a
    contributor's PR branch ``<head_ref>``), automatically pushes
    ``HEAD:refs/heads/<branch>``.

    Returns:
        (success: bool, output_or_error_message: str)
    """
    from .audit import redact_credentials

    if not repo_dir.exists():
        return (False, f"Repository directory '{repo_dir}' does not exist.")

    cleaned_branch = (branch or '').strip()
    if not cleaned_branch or ':' in cleaned_branch or cleaned_branch.startswith('-'):
        return (
            False,
            f"Invalid branch name '{branch}': refspec syntax (':') and leading dashes are forbidden.",
        )

    refspec = f'refs/heads/{cleaned_branch}:refs/heads/{cleaned_branch}'
    current_branch = get_current_branch(repo_dir)
    has_local_ref = False
    try:
        chk = subprocess.run(
            git_safe_cmd(repo_dir, 'show-ref', '--verify', '--quiet', f'refs/heads/{cleaned_branch}'),
            cwd=str(repo_dir),
            capture_output=True,
            timeout=10,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        has_local_ref = (chk.returncode == 0)
    except Exception:
        pass
    if not has_local_ref or (
        current_branch and current_branch != cleaned_branch and current_branch.startswith('pr-')
    ):
        refspec = f'HEAD:refs/heads/{cleaned_branch}'

    push_target = remote
    lease_arg = '--force-with-lease'
    if not is_remote_tracking_writable(repo_dir, remote):
        resolved_url = get_repo_remote_url(repo_dir, remote, push=True) or get_repo_remote_url(repo_dir, remote)
        if resolved_url:
            if force_with_lease:
                try:
                    rev_res = subprocess.run(
                        git_safe_cmd(
                            repo_dir, 'rev-parse', '--verify', '--quiet',
                            f'refs/remotes/{remote}/{cleaned_branch}',
                        ),
                        cwd=str(repo_dir),
                        capture_output=True,
                        text=True,
                        timeout=10,
                        env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                    )
                    if rev_res.returncode == 0 and rev_res.stdout.strip():
                        lease_arg = f"--force-with-lease=refs/heads/{cleaned_branch}:{rev_res.stdout.strip()}"
                        push_target = resolved_url
                except Exception:
                    pass
            else:
                push_target = resolved_url

    cmd = git_safe_cmd(repo_dir, 'push')
    if force_with_lease:
        cmd.append(lease_arg)
    elif force:
        cmd.append('--force')
    if dry_run:
        cmd.append('--dry-run')
    cmd.extend(['--', push_target, refspec])

    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    default_sock = Path.home() / '.ssh' / 'ssh_auth_sock'
    if (
        (not git_env.get('SSH_AUTH_SOCK') or not Path(git_env['SSH_AUTH_SOCK']).exists())
        and default_sock.exists()
    ):
        git_env['SSH_AUTH_SOCK'] = str(default_sock)

    try:
        res = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=120,
            env=git_env,
        )
    except subprocess.TimeoutExpired:
        return (False, "git push timed out after 120 seconds.")
    except Exception as e:
        return (False, redact_credentials(f"git push failed: {e}"))

    if res.returncode == 0:
        out_clean = strip_harmless_remote_ref_lock_errors(res.stdout)
        err_clean = strip_harmless_remote_ref_lock_errors(res.stderr)
        msg = out_clean or err_clean or f"Successfully pushed {branch} to {remote}."
        return (True, msg)
    else:
        err = redact_credentials(res.stderr.strip() or res.stdout.strip() or "git push failed with unknown error.")
        return (False, err)


VERSION_TAG_PATTERN = re.compile(r'^v?[0-9]+\.[0-9]+\.[0-9]+(?:[._-][0-9A-Za-z._-]+)?$')


def verify_release_tag(repo_dir: Path, tag: str) -> Tuple[bool, str, Optional[str]]:
    """
    Verify that ``tag`` matches a semantic version tag pattern and exists locally in ``repo_dir``.

    Returns:
        (valid: bool, message: str, tag_commit_sha: Optional[str])
    """
    if not repo_dir.exists():
        return (False, f"Repository directory '{repo_dir}' does not exist.", None)

    cleaned_tag = (tag or '').strip()
    if not cleaned_tag or not VERSION_TAG_PATTERN.match(cleaned_tag):
        return (
            False,
            f"Tag '{cleaned_tag}' does not match expected version tag format (e.g. '3.10.2' or 'v1.2.3').",
            None,
        )

    try:
        res = subprocess.run(
            git_safe_cmd(repo_dir, 'rev-parse', '--verify', '--quiet', f'refs/tags/{cleaned_tag}^{{commit}}'),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if res.returncode != 0 or not res.stdout.strip():
            return (
                False,
                f"Tag '{cleaned_tag}' does not exist locally in '{repo_dir}'. "
                "Run 'catkin_prepare_release --no-push' first.",
                None,
            )
        return (True, f"Verified local tag '{cleaned_tag}'.", res.stdout.strip())
    except Exception as e:
        return (False, f"Failed to verify local tag '{cleaned_tag}': {e}", None)


def execute_release_push(
    repo_dir: Path,
    target_branch: str,
    tag: str,
    remote: str = 'origin',
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Push a local release commit (referenced by ``refs/tags/<tag>^{commit}``) to
    ``refs/heads/<target_branch>`` and push ``refs/tags/<tag>`` to ``remote``
    (fast-forward only, never force-pushed).
    """
    from .audit import redact_credentials

    valid, msg, tag_commit_sha = verify_release_tag(repo_dir, tag)
    if not valid or not tag_commit_sha:
        return (False, msg)

    cleaned_branch = (target_branch or '').strip()
    if not cleaned_branch or cleaned_branch.startswith('-') or ':' in cleaned_branch or ' ' in cleaned_branch:
        return (False, f"Invalid target branch name: '{target_branch}'.")

    cleaned_tag = tag.strip()
    branch_refspec = f'{tag_commit_sha}:refs/heads/{cleaned_branch}'
    tag_refspec = f'refs/tags/{cleaned_tag}:refs/tags/{cleaned_tag}'

    push_target = remote
    if not is_remote_tracking_writable(repo_dir, remote):
        resolved_url = get_repo_remote_url(repo_dir, remote, push=True) or get_repo_remote_url(repo_dir, remote)
        if resolved_url:
            push_target = resolved_url

    cmd = git_safe_cmd(repo_dir, 'push')
    if dry_run:
        cmd.append('--dry-run')
    cmd.extend(['--', push_target, branch_refspec, tag_refspec])

    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    default_sock = Path.home() / '.ssh' / 'ssh_auth_sock'
    if (
        (not git_env.get('SSH_AUTH_SOCK') or not Path(git_env['SSH_AUTH_SOCK']).exists())
        and default_sock.exists()
    ):
        git_env['SSH_AUTH_SOCK'] = str(default_sock)

    try:
        res = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=120,
            env=git_env,
        )
    except subprocess.TimeoutExpired:
        return (False, "git release push timed out after 120 seconds.")
    except Exception as e:
        return (False, redact_credentials(f"git release push failed: {e}"))

    if res.returncode == 0:
        out_clean = strip_harmless_remote_ref_lock_errors(res.stdout)
        err_clean = strip_harmless_remote_ref_lock_errors(res.stderr)
        out = out_clean or err_clean or (
            f"Successfully pushed {cleaned_branch} ({tag_commit_sha[:8]}) and tag {cleaned_tag} to {remote}."
        )
        return (True, out)
    else:
        err = redact_credentials(
            res.stderr.strip() or res.stdout.strip() or "git release push failed with unknown error."
        )
        return (False, err)
