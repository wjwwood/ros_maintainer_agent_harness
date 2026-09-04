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

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from typing import List, Optional
import urllib.error
import urllib.request


PR_URL_PATTERN = re.compile(
    r'^(?:https?://github\.com/)?([^/\s]+)/([^/\s#]+)(?:/pull/|#|/)(\d+)/?$'
)

KNOWN_DISTROS = {
    'rolling': 'rolling',
    'jazzy': 'jazzy',
    'iron': 'iron',
    'humble': 'humble',
    'galactic': 'galactic',
    'foxy': 'foxy',
    'eloquent': 'eloquent',
    'dashing': 'dashing',
    'crystal': 'crystal',
    'bouncy': 'bouncy',
    'ardent': 'ardent',
    'main': 'rolling',
    'master': 'rolling',
}


@dataclasses.dataclass
class PRMetadata:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    base_ref: str
    head_ref: str
    head_repo_owner: str
    head_repo_url: str
    is_fork: bool
    url: str
    detected_distro: str
    changed_files: List[str] = dataclasses.field(default_factory=list)
    labels: List[str] = dataclasses.field(default_factory=list)
    author: str = ''
    state: str = 'OPEN'

    @property
    def shorthand(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def suggested_session_id(self) -> str:
        clean_repo = re.sub(r'[^a-zA-Z0-9_-]', '-', self.repo)
        return f"pr-{clean_repo}-{self.number}"


def parse_pr_reference(pr_ref: str) -> tuple[str, str, int]:
    """
    Parse owner, repo, and PR number from a PR URL or shorthand string.

    Examples:
      - 'https://github.com/ros2/rclcpp/pull/160' -> ('ros2', 'rclcpp', 160)
      - 'ros2/rclcpp#160' -> ('ros2', 'rclcpp', 160)
      - 'ros2/rclcpp/160' -> ('ros2', 'rclcpp', 160)
    """
    match = PR_URL_PATTERN.match(pr_ref.strip())
    if not match:
        raise ValueError(
            f"Invalid PR reference '{pr_ref}'. Expected format: "
            "'https://github.com/owner/repo/pull/123' or 'owner/repo#123'."
        )
    owner, repo, number_str = match.groups()
    return owner, repo, int(number_str)


def detect_ros_distro_from_branch(branch_name: str) -> str:
    """Infer the target ROS 2 distribution from a git branch name."""
    clean = branch_name.strip().lower()
    if clean in KNOWN_DISTROS:
        return KNOWN_DISTROS[clean]

    for prefix in ('jazzy', 'iron', 'humble', 'rolling', 'galactic', 'foxy'):
        if clean.startswith(prefix):
            return prefix

    return 'rolling'


def get_github_token() -> Optional[str]:
    """Retrieve GitHub auth token from environment or gh CLI."""
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        return token

    try:
        res = subprocess.run(
            ['gh', 'auth', 'token'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return None


def fetch_pr_metadata_via_gh(owner: str, repo: str, number: int) -> Optional[PRMetadata]:
    """Fetch PR details using GitHub CLI."""
    try:
        cmd = [
            'gh', 'pr', 'view', f"{owner}/{repo}#{number}",
            '--json',
            (
                'number,title,body,baseRefName,headRefName,headRepository,'
                'headRepositoryOwner,url,files,labels,author,state'
            ),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            return None

        data = json.loads(res.stdout)
        head_repo_info = data.get('headRepository') or {}
        head_owner_info = data.get('headRepositoryOwner') or {}
        head_owner = head_owner_info.get('login') or head_repo_info.get('name') or owner
        head_url = head_repo_info.get('url') or f"https://github.com/{head_owner}/{repo}.git"
        is_fork = head_owner.lower() != owner.lower()

        files_list = [f.get('path') for f in data.get('files', []) if isinstance(f, dict) and f.get('path')]
        labels_list = [
            label.get('name') for label in data.get('labels', [])
            if isinstance(label, dict) and label.get('name')
        ]
        base_ref = data.get('baseRefName') or 'rolling'

        return PRMetadata(
            owner=owner,
            repo=repo,
            number=number,
            title=data.get('title') or '',
            body=data.get('body') or '',
            base_ref=base_ref,
            head_ref=data.get('headRefName') or f"patch-{number}",
            head_repo_owner=head_owner,
            head_repo_url=head_url,
            is_fork=is_fork,
            url=data.get('url') or f"https://github.com/{owner}/{repo}/pull/{number}",
            detected_distro=detect_ros_distro_from_branch(base_ref),
            changed_files=files_list,
            labels=labels_list,
            author=(data.get('author') or {}).get('login', ''),
            state=data.get('state', 'OPEN'),
        )
    except Exception:
        return None


def fetch_pr_metadata_via_api(owner: str, repo: str, number: int) -> PRMetadata:
    """Fetch PR details using GitHub REST API."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    req = urllib.request.Request(api_url)
    req.add_header('User-Agent', 'ros_maintainer_agent_harness')
    req.add_header('Accept', 'application/vnd.github.v3+json')

    token = get_github_token()
    if token:
        req.add_header('Authorization', f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API error fetching {owner}/{repo}#{number}: HTTP {e.code} - {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to fetch PR metadata for {owner}/{repo}#{number}: {e}") from e

    head_data = data.get('head', {})
    head_repo = head_data.get('repo') or {}
    head_owner = (head_repo.get('owner') or {}).get('login') or head_data.get('user', {}).get('login') or owner
    head_url = head_repo.get('clone_url') or f"https://github.com/{head_owner}/{repo}.git"
    is_fork = head_owner.lower() != owner.lower()
    base_ref = data.get('base', {}).get('ref', 'rolling')

    # Fetch files if available
    files_list: List[str] = []
    try:
        files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
        f_req = urllib.request.Request(files_url)
        f_req.add_header('User-Agent', 'ros_maintainer_agent_harness')
        if token:
            f_req.add_header('Authorization', f"Bearer {token}")
        with urllib.request.urlopen(f_req, timeout=10) as f_resp:
            f_data = json.loads(f_resp.read().decode('utf-8'))
            files_list = [f.get('filename') for f in f_data if isinstance(f, dict) and f.get('filename')]
    except Exception:
        pass

    labels_list = [
        label.get('name') for label in data.get('labels', [])
        if isinstance(label, dict) and label.get('name')
    ]

    return PRMetadata(
        owner=owner,
        repo=repo,
        number=number,
        title=data.get('title') or '',
        body=data.get('body') or '',
        base_ref=base_ref,
        head_ref=head_data.get('ref') or f"patch-{number}",
        head_repo_owner=head_owner,
        head_repo_url=head_url,
        is_fork=is_fork,
        url=data.get('html_url') or f"https://github.com/{owner}/{repo}/pull/{number}",
        detected_distro=detect_ros_distro_from_branch(base_ref),
        changed_files=files_list,
        labels=labels_list,
        author=(data.get('user') or {}).get('login', ''),
        state=data.get('state', 'OPEN'),
    )


def fetch_pr_metadata(pr_ref: str) -> PRMetadata:
    """
    Harvest full metadata for a GitHub Pull Request from URL or shorthand.
    """
    owner, repo, number = parse_pr_reference(pr_ref)

    # Try gh CLI first
    meta = fetch_pr_metadata_via_gh(owner, repo, number)
    if meta is not None:
        return meta

    # Fallback to direct REST API
    return fetch_pr_metadata_via_api(owner, repo, number)
