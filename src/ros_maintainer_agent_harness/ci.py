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
import datetime
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import requests
import yaml

logger = logging.getLogger(__name__)

ROS_DISTRO_TO_UBUNTU_DISTRO = {
    'noetic': 'focal',
    'humble': 'jammy',
    'iron': 'jammy',
    'jazzy': 'noble',
    'kilted': 'noble',
    'lyrical': 'resolute',
    'rolling': '',
}

ROS_DISTRO_TO_RHEL_DISTRO = {
    'humble': '8',
    'iron': '9',
    'jazzy': '9',
    'kilted': '9',
    'lyrical': '10',
    'rolling': '',
}

DEFAULT_CI_LAUNCHER_PARAMS: Dict[str, Any] = {
    'CI_BRANCH_TO_TEST': '',
    'CI_SCRIPTS_BRANCH': 'master',
    'CI_ROS2_REPOS_URL': '',
    'CI_ROS2_SUPPLEMENTAL_REPOS_URL': '',
    'CI_PIXI_TOML_URL': '',
    'CI_COLCON_BRANCH': '',
    'CI_UBUNTU_DISTRO': 'resolute',
    'CI_EL_RELEASE': '10',
    'CI_ROS_DISTRO': 'rolling',
    'CI_COLCON_MIXIN_URL': 'https://raw.githubusercontent.com/colcon/colcon-mixin-repository/master/index.yaml',
    'CI_CMAKE_BUILD_TYPE': 'None',
    'CI_BUILD_ARGS': (
        '--event-handlers console_cohesion+ console_package_list+ '
        '--cmake-args -DINSTALL_EXAMPLES=OFF -DSECURITY=ON -DAPPEND_PROJECT_NAME_TO_INCLUDEDIR=ON'
    ),
    'CI_ISOLATED': True,
    'CI_USE_WHITESPACE_IN_PATHS': False,
    'CI_USE_CONNEXTDDS': True,
    'CI_USE_CONNEXT_DEBS': False,
    'CI_USE_CYCLONEDDS': True,
    'CI_USE_FASTRTPS_STATIC': True,
    'CI_USE_FASTRTPS_DYNAMIC': False,
    'CI_COMPILE_WITH_CLANG': False,
    'CI_ENABLE_COVERAGE': False,
    'CI_TEST_ARGS': (
        '--event-handlers console_cohesion+ --retest-until-pass 2 '
        '--ctest-args -LE xfail --pytest-args -m "not xfail" --executor sequential'
    ),
}


def extract_default_job_params(job_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract default parameter values from Jenkins job metadata, falling back to DEFAULT_CI_LAUNCHER_PARAMS."""
    params = dict(DEFAULT_CI_LAUNCHER_PARAMS)
    if not isinstance(job_info, dict):
        return params
    containers = list(job_info.get('property') or []) + list(job_info.get('actions') or [])
    for prop in containers:
        if not isinstance(prop, dict):
            continue
        for pdef in prop.get('parameterDefinitions') or []:
            if not isinstance(pdef, dict):
                continue
            name = pdef.get('name')
            default_obj = pdef.get('defaultParameterValue')
            if name and isinstance(default_obj, dict) and 'value' in default_obj:
                params[name] = default_obj['value']
    return params


def _extract_package_name(package_xml: Path) -> Optional[str]:
    """Extract the <name> tag from a ROS package.xml file."""
    try:
        content = package_xml.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'<name>\s*([^<\s]+)\s*</name>', content)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def detect_session_packages(session_dir: Path) -> List[str]:
    """
    Auto-detect affected ROS 2 package names from a session directory by inspecting
    changed files in the session metadata or git worktree and locating enclosing package.xml files.
    """
    if not session_dir.is_dir():
        return []

    meta: Dict[str, Any] = {}
    meta_file = session_dir / 'session.json'
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
        except Exception:
            meta = {}

    src_dir = session_dir / 'src'
    repo_dirs: List[Path] = []
    if src_dir.is_dir():
        repo_dirs = [
            p for p in sorted(src_dir.iterdir())
            if p.is_dir() and (p / '.git').exists()
        ]
        if not repo_dirs:
            repo_dirs = [p for p in sorted(src_dir.iterdir()) if p.is_dir()]

    packages: List[str] = []
    seen: set[str] = set()

    for repo_dir in repo_dirs:
        changed_files: List[str] = []
        if isinstance(meta.get('changed_files'), list) and len(repo_dirs) == 1:
            changed_files.extend([str(f) for f in meta['changed_files'] if f])

        if not changed_files and (repo_dir / '.git').exists():
            base_ref = meta.get('base_ref') or 'rolling'
            for diff_range in (f'origin/{base_ref}...HEAD', f'{base_ref}...HEAD', 'HEAD~1...HEAD'):
                try:
                    res = subprocess.run(
                        ['git', 'diff', '--name-only', diff_range],
                        cwd=str(repo_dir),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        changed_files.extend(
                            [line.strip() for line in res.stdout.splitlines() if line.strip()]
                        )
                        break
                except Exception:
                    pass

        for rel_path in changed_files:
            curr = (repo_dir / rel_path).parent
            while True:
                pkg_xml = curr / 'package.xml'
                if pkg_xml.is_file():
                    pkg_name = _extract_package_name(pkg_xml)
                    if pkg_name and pkg_name not in seen:
                        seen.add(pkg_name)
                        packages.append(pkg_name)
                    break
                if curr == repo_dir or curr.parent == curr or not str(curr).startswith(str(repo_dir)):
                    break
                curr = curr.parent

        if not packages:
            pkg_xml = repo_dir / 'package.xml'
            if pkg_xml.is_file():
                pkg_name = _extract_package_name(pkg_xml)
                if pkg_name and pkg_name not in seen:
                    seen.add(pkg_name)
                    packages.append(pkg_name)

    return packages


@dataclasses.dataclass
class CIRunRecord:
    pr_url: str
    session_id: str
    job_name: str
    build_num: int
    job_url: str
    status: str  # PENDING, RUNNING, SUCCESS, UNSTABLE, FAILURE, ABORTED, CANCELLED, UNKNOWN
    timestamp: float
    iso_time: str
    completed_time: Optional[str] = None
    duration_seconds: Optional[float] = None
    estimated_duration_seconds: Optional[float] = None
    parameters: Dict[str, Any] = dataclasses.field(default_factory=dict)
    test_summary: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None
    log_excerpt: Optional[str] = None
    artifacts: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CIRunRecord:
        # Filter out unknown fields for forward compatibility
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class CITracker:
    """Tracks active and historical Jenkins CI runs launched via the gateway."""

    def __init__(self, storage_file: Path):
        self.storage_file = storage_file
        self._lock = threading.Lock()

    def _load_runs(self) -> List[CIRunRecord]:
        if not self.storage_file.exists():
            return []
        try:
            with open(self.storage_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [CIRunRecord.from_dict(r) for r in data]
        except Exception:
            return []

    def _save_runs(self, runs: List[CIRunRecord]) -> None:
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        data = [r.to_dict() for r in runs]
        temp_file = self.storage_file.with_suffix(f".tmp.{os.getpid()}")
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, self.storage_file)

    def record_run(
        self,
        pr_url: str,
        session_id: str,
        job_name: str,
        build_num: int,
        job_url: str,
        status: str = 'PENDING',
        parameters: Optional[Dict[str, Any]] = None,
    ) -> CIRunRecord:
        now = time.time()
        iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rec = CIRunRecord(
            pr_url=pr_url,
            session_id=session_id,
            job_name=job_name,
            build_num=build_num,
            job_url=job_url,
            status=status,
            timestamp=now,
            iso_time=iso,
            parameters=parameters or {},
        )
        with self._lock:
            runs = self._load_runs()
            runs.append(rec)
            self._save_runs(runs)
        return rec

    def get_run(self, job_url_or_id: str) -> Optional[CIRunRecord]:
        """Find a run by job_url or build number or PR URL."""
        with self._lock:
            runs = self._load_runs()

        target = job_url_or_id.strip().rstrip('/')
        # Match by exact URL
        for r in runs:
            if r.job_url.rstrip('/') == target:
                return r

        # Match by build_num if numeric
        if target.isdigit():
            b_num = int(target)
            for r in reversed(runs):
                if r.build_num == b_num:
                    return r

        # Match by PR URL
        for r in reversed(runs):
            if r.pr_url == target:
                return r

        return None

    def get_active_runs(self) -> List[CIRunRecord]:
        """Return all active (PENDING or RUNNING) runs."""
        with self._lock:
            runs = self._load_runs()
        return [
            r for r in runs
            if r.status.upper() in ('PENDING', 'RUNNING', 'BUILDING')
        ]

    def get_active_runs_count(self, pr_url: str) -> int:
        with self._lock:
            runs = self._load_runs()
        active = [
            r for r in runs
            if r.pr_url == pr_url and r.status.upper() in ('PENDING', 'RUNNING', 'BUILDING')
        ]
        return len(active)

    def get_seconds_since_last_run(self, pr_url: str) -> Optional[float]:
        with self._lock:
            runs = [r for r in self._load_runs() if r.pr_url == pr_url]
        if not runs:
            return None
        latest = max(runs, key=lambda r: r.timestamp)
        return max(0.0, time.time() - latest.timestamp)

    def get_latest_run_for_session(self, session_id: str) -> Optional[CIRunRecord]:
        with self._lock:
            runs = [r for r in self._load_runs() if r.session_id == session_id]
        if not runs:
            return None
        return max(runs, key=lambda r: r.timestamp)

    def list_runs(
        self,
        session_id: Optional[str] = None,
        pr_url: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[CIRunRecord]:
        with self._lock:
            runs = self._load_runs()

        res = list(runs)
        if session_id:
            res = [r for r in res if r.session_id == session_id]
        if pr_url:
            res = [r for r in res if r.pr_url == pr_url]
        if status:
            res = [r for r in res if r.status.upper() == status.upper()]

        res.sort(key=lambda r: r.timestamp, reverse=True)
        return res[:limit]

    def update_run_status(self, job_url: str, new_status: str) -> bool:
        with self._lock:
            runs = self._load_runs()
            updated = False
            for r in runs:
                if r.job_url.rstrip('/') == job_url.rstrip('/'):
                    r.status = new_status
                    updated = True
            if updated:
                self._save_runs(runs)
        return updated

    def update_run(
        self,
        job_url: str,
        status: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        test_summary: Optional[Dict[str, Any]] = None,
        failure_reason: Optional[str] = None,
        log_excerpt: Optional[str] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[CIRunRecord]:
        """Update full build status and test results for a run record."""
        target_url = job_url.rstrip('/')
        updated_rec = None
        with self._lock:
            runs = self._load_runs()
            for r in runs:
                if r.job_url.rstrip('/') == target_url:
                    if status:
                        r.status = status
                        if status.upper() in ('SUCCESS', 'UNSTABLE', 'FAILURE', 'ABORTED', 'CANCELLED'):
                            r.completed_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    if duration_seconds is not None:
                        r.duration_seconds = duration_seconds
                    if test_summary is not None:
                        r.test_summary = test_summary
                    if failure_reason is not None:
                        r.failure_reason = failure_reason
                    if log_excerpt is not None:
                        r.log_excerpt = log_excerpt
                    if artifacts is not None:
                        r.artifacts = artifacts
                    updated_rec = r
            if updated_rec:
                self._save_runs(runs)
        return updated_rec


def parse_pr_url(pr_url: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Parse repo and PR number from URL or shorthand.

    Examples:
        - https://github.com/ros2/rclcpp/pull/160 -> ('ros2/rclcpp', 160)
        - https://github.com/ros2/rclcpp/pull/160#issuecomment-123456 -> ('ros2/rclcpp', 160)
        - https://github.com/ros2/rclcpp/pull/160?tab=files -> ('ros2/rclcpp', 160)
        - https://github.com/ros2/rclcpp/pull/160/files -> ('ros2/rclcpp', 160)
        - ros2/rclcpp#160 -> ('ros2/rclcpp', 160)
    """
    if not pr_url:
        return (None, None)

    raw = pr_url.strip()

    # 1. Shorthand: ros2/rclcpp#160
    m_short = re.match(r'^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)$', raw)
    if m_short:
        return (m_short.group(1), int(m_short.group(2)))

    # 2. Full GitHub PR URL (clean query and comment anchor)
    cleaned = raw.split('#')[0].split('?')[0].rstrip('/')
    m_url = re.match(r'^https?://github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/pull/(\d+)(?:/.*)?$', cleaned)
    if m_url:
        return (m_url.group(1), int(m_url.group(2)))

    return (None, None)


class JenkinsManager:
    """Manages Jenkins CI triggering, status querying, test failure extraction, and log parsing."""

    def __init__(
        self,
        ci_server: str = 'https://ci.ros2.org',
        tracker: Optional[CITracker] = None,
        auth: Optional[Tuple[str, str]] = None,
    ):
        self.ci_server = ci_server.rstrip('/')
        self.tracker = tracker
        self.auth = auth
        self.session = requests.Session()
        if self.auth:
            self.session.auth = self.auth

    @staticmethod
    def normalize_packages(packages: Optional[Union[List[str], str]]) -> List[str]:
        if not packages:
            return []
        if isinstance(packages, str):
            return [p.strip() for p in re.split(r'[\s,]+', packages) if p.strip()]
        return [str(p).strip() for p in packages if str(p).strip()]

    @classmethod
    def build_extra_ci_args(
        cls,
        packages: Optional[Union[List[str], str]] = None,
        only_fixes_test: bool = False,
        colcon_build_args: Optional[str] = None,
        colcon_test_args: Optional[str] = None,
        cmake_args: Optional[str] = None,
    ) -> Tuple[str, str]:
        pkg_list = cls.normalize_packages(packages)
        extra_build_args = ''
        extra_test_args = ''
        if pkg_list:
            pkg_str = ' '.join(pkg_list)
            if only_fixes_test:
                extra_build_args += f' --packages-up-to {pkg_str}'
                extra_test_args += f' --packages-select {pkg_str}'
            else:
                extra_build_args += f' --packages-above-and-dependencies {pkg_str}'
                extra_test_args += f' --packages-above {pkg_str}'
        if colcon_build_args and colcon_build_args.strip():
            extra_build_args += f' {colcon_build_args.strip()}'
        if colcon_test_args and colcon_test_args.strip():
            extra_test_args += f' {colcon_test_args.strip()}'
        if cmake_args and cmake_args.strip():
            extra_build_args += f' --cmake-args {cmake_args.strip()}'
        return (extra_build_args, extra_test_args)

    @classmethod
    def build_ci_args(
        cls,
        packages: Optional[Union[List[str], str]] = None,
        only_fixes_test: bool = False,
        colcon_build_args: Optional[str] = None,
        colcon_test_args: Optional[str] = None,
        cmake_args: Optional[str] = None,
    ) -> Tuple[str, str]:
        extra_build, extra_test = cls.build_extra_ci_args(
            packages=packages,
            only_fixes_test=only_fixes_test,
            colcon_build_args=colcon_build_args,
            colcon_test_args=colcon_test_args,
            cmake_args=cmake_args,
        )
        build_args = f"{DEFAULT_CI_LAUNCHER_PARAMS['CI_BUILD_ARGS']}{extra_build}"
        test_args = f"{DEFAULT_CI_LAUNCHER_PARAMS['CI_TEST_ARGS']}{extra_test}"
        return (build_args, test_args)

    def _resolve_github_auth(self) -> Optional[Tuple[str, str]]:
        """Resolve GitHub (username, token) on the host for Gist creation and Jenkins OAuth."""
        if self.auth:
            return self.auth

        token = (
            os.environ.get('ROS_CI_GITHUB_TOKEN')
            or os.environ.get('ROS_HOST_GITHUB_TOKEN')
            or os.environ.get('GITHUB_ACCESS_TOKEN')
            or os.environ.get('GITHUB_TOKEN')
            or os.environ.get('GH_TOKEN')
            or ''
        ).strip()

        if not token:
            try:
                res = subprocess.run(
                    ['gh', 'auth', 'token'],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0 and res.stdout.strip():
                    token = res.stdout.strip()
            except Exception:
                pass

        if not token:
            return None

        try:
            resp = self.session.get(
                'https://api.github.com/user',
                headers={
                    'Authorization': f'token {token}',
                    'Accept': 'application/vnd.github+json',
                },
                timeout=15,
            )
            if resp.status_code == 200:
                login = resp.json().get('login')
                if login:
                    self.auth = (login, token)
                    self.session.auth = self.auth
                    return self.auth
        except Exception:
            pass
        return None

    def create_ci_gist(
        self,
        repo: str,
        pr_num: int,
        target_distro: str,
        token: str,
        extra_repos: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """Create a public ros2.repos GitHub Gist pointing the PR repository at the PR branch."""
        gh_headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github+json',
        }

        # 1. Fetch ros2.repos for target_distro
        raw_repos_url = f'https://raw.githubusercontent.com/ros2/ros2/{target_distro}/ros2.repos'
        repos_resp = self.session.get(raw_repos_url, timeout=20)
        if repos_resp.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch ros2.repos for '{target_distro}' (HTTP {repos_resp.status_code})."
            )
        toplevel = yaml.safe_load(repos_resp.text) or {}
        master_repos = dict(toplevel.get('repositories') or {})

        if extra_repos:
            for entry in extra_repos:
                if ':' in entry:
                    r_name, r_branch = entry.split(':', 1)
                    master_repos[r_name.strip()] = {
                        'type': 'git',
                        'url': f'https://github.com/{r_name.strip()}.git',
                        'version': r_branch.strip(),
                    }

        # 2. Fetch PR details from GitHub API
        pr_api_url = f'https://api.github.com/repos/{repo}/pulls/{pr_num}'
        pr_resp = self.session.get(pr_api_url, headers=gh_headers, timeout=20)
        if pr_resp.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch PR metadata from {pr_api_url} (HTTP {pr_resp.status_code})."
            )
        pr_data = pr_resp.json()
        pr_ref = pr_data['head']['ref']
        head_repo_info = pr_data['head'].get('repo') or {}
        base_repo_info = pr_data['base'].get('repo') or {}
        pr_repo = head_repo_info.get('full_name') or repo
        base_repo = base_repo_info.get('full_name') or repo

        master_repos.pop(base_repo, None)
        master_repos[pr_repo] = {
            'type': 'git',
            'url': f'https://github.com/{pr_repo}.git',
            'version': pr_ref,
        }

        yaml_out = yaml.dump({'repositories': master_repos}, default_flow_style=False)
        gist_payload = {
            'description': f'CI input for PR {base_repo}#{pr_num}',
            'public': True,
            'files': {
                'ros2.repos': {'content': yaml_out},
            },
        }
        gist_resp = self.session.post(
            'https://api.github.com/gists',
            headers=gh_headers,
            json=gist_payload,
            timeout=20,
        )
        if gist_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to create ros2.repos Gist (HTTP {gist_resp.status_code}): {gist_resp.text[:200]}"
            )
        gist_data = gist_resp.json()
        raw_url = gist_data['files']['ros2.repos']['raw_url']
        html_url = gist_data.get('html_url', raw_url)
        return {
            'raw_url': raw_url,
            'html_url': html_url,
            'pr_ref': pr_ref,
            'pr_repo': pr_repo,
            'base_repo': base_repo,
        }

    def launch_ci(
        self,
        session_id: str,
        pr_url: str,
        target_distro: Optional[str] = None,
        job_type: Optional[str] = None,
        only_fixes_test: bool = False,
        packages: Optional[Union[List[str], str]] = None,
        colcon_build_args: Optional[str] = None,
        colcon_test_args: Optional[str] = None,
        cmake_args: Optional[str] = None,
        extra_repos: Optional[List[str]] = None,
        comment: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Trigger a Jenkins CI run for the given pull request.
        """
        import sys

        repo, pr_num = parse_pr_url(pr_url)
        distro = target_distro or 'rolling'
        job_name = job_type or 'ci_launcher'
        pkg_list = self.normalize_packages(packages)
        extra_build_args, extra_test_args = self.build_extra_ci_args(
            packages=pkg_list,
            only_fixes_test=only_fixes_test,
            colcon_build_args=colcon_build_args,
            colcon_test_args=colcon_test_args,
            cmake_args=cmake_args,
        )
        build_args, test_args = self.build_ci_args(
            packages=pkg_list,
            only_fixes_test=only_fixes_test,
            colcon_build_args=colcon_build_args,
            colcon_test_args=colcon_test_args,
            cmake_args=cmake_args,
        )

        in_test_harness = (
            bool(os.environ.get('PYTEST_CURRENT_TEST'))
            or 'unittest' in sys.modules
        )
        use_live = not dry_run and (self.auth is not None or not in_test_harness)

        if use_live and repo and pr_num:
            auth_pair = self._resolve_github_auth()
            if auth_pair:
                username, token = auth_pair
                gist_info = self.create_ci_gist(
                    repo=repo,
                    pr_num=pr_num,
                    target_distro=distro,
                    token=token,
                    extra_repos=extra_repos,
                )

                # 1. Fetch CSRF crumb and job definition/nextBuildNumber from Jenkins
                crumb_headers: Dict[str, str] = {}
                try:
                    crumb_resp = self.session.get(
                        f'{self.ci_server}/crumbIssuer/api/json',
                        auth=(username, token),
                        timeout=15,
                    )
                    if crumb_resp.status_code == 200:
                        c_data = crumb_resp.json()
                        c_field = c_data.get('crumbRequestField') or 'Jenkins-Crumb'
                        c_val = c_data.get('crumb')
                        if c_val:
                            crumb_headers[c_field] = c_val
                except Exception:
                    pass

                job_info_resp = self.session.get(
                    f'{self.ci_server}/job/{job_name}/api/json',
                    auth=(username, token),
                    timeout=15,
                )
                if job_info_resp.status_code != 200:
                    raise RuntimeError(
                        f"Failed to query Jenkins job '{job_name}' (HTTP {job_info_resp.status_code})."
                    )
                job_info_data = job_info_resp.json() or {}
                build_num = int(job_info_data.get('nextBuildNumber', 1))
                job_url = f'{self.ci_server}/job/{job_name}/{build_num}/'

                job_params = extract_default_job_params(job_info_data)
                job_params['CI_ROS2_REPOS_URL'] = gist_info['raw_url']
                job_params['CI_ROS_DISTRO'] = distro
                ubuntu_distro = ROS_DISTRO_TO_UBUNTU_DISTRO.get(distro, '')
                if ubuntu_distro:
                    job_params['CI_UBUNTU_DISTRO'] = ubuntu_distro
                el_release = ROS_DISTRO_TO_RHEL_DISTRO.get(distro, '')
                if el_release:
                    job_params['CI_EL_RELEASE'] = el_release
                job_params['CI_BUILD_ARGS'] = f"{job_params.get('CI_BUILD_ARGS', '').rstrip()}{extra_build_args}"
                job_params['CI_TEST_ARGS'] = f"{job_params.get('CI_TEST_ARGS', '').rstrip()}{extra_test_args}"

                build_p = [{'name': k, 'value': v} for k, v in sorted(job_params.items())]
                json_payload = json.dumps({
                    'parameter': build_p[0] if len(build_p) == 1 else build_p,
                    'statusCode': '303',
                    'redirectTo': '.',
                })
                post_data = {'json': json_payload, **job_params}

                trigger_resp = self.session.post(
                    f'{self.ci_server}/job/{job_name}/buildWithParameters',
                    data=post_data,
                    headers=crumb_headers,
                    auth=(username, token),
                    allow_redirects=False,
                    timeout=20,
                )
                if trigger_resp.status_code not in (200, 201, 302, 303):
                    raise RuntimeError(
                        f"Jenkins buildWithParameters failed (HTTP {trigger_resp.status_code}): "
                        f"{trigger_resp.text[:200]}"
                    )

                # 2. Poll ci_launcher console output briefly to extract child job links & badges
                child_jobs: List[Dict[str, Any]] = []
                badge_lines: List[str] = []
                for _ in range(8):
                    try:
                        con_resp = self.session.get(
                            f'{job_url}consoleText',
                            auth=(username, token),
                            timeout=15,
                        )
                        if con_resp.status_code == 200 and '* Linux ' in con_resp.text:
                            for line in con_resp.text.splitlines():
                                if line.startswith('* ') and '[![Build Status]' in line:
                                    badge_lines.append(line)
                                    m_child = re.search(
                                        r'\(\s*(https?://[^)\s]+/job/([^/]+)/(\d+)/?)\s*\)',
                                        line,
                                    )
                                    if m_child:
                                        c_url, c_name, c_num = (
                                            m_child.group(1),
                                            m_child.group(2),
                                            int(m_child.group(3)),
                                        )
                                        if not c_url.endswith('/'):
                                            c_url += '/'
                                        child_jobs.append({
                                            'job_name': c_name,
                                            'build_num': c_num,
                                            'job_url': c_url,
                                        })
                            if child_jobs:
                                break
                    except Exception:
                        pass
                    time.sleep(2.0)

                if self.tracker:
                    self.tracker.record_run(
                        pr_url=pr_url,
                        session_id=session_id,
                        job_name=job_name,
                        build_num=build_num,
                        job_url=job_url,
                        status='SUCCESS' if child_jobs else 'PENDING',
                        parameters=job_params,
                    )
                    for cj in child_jobs:
                        self.tracker.record_run(
                            pr_url=pr_url,
                            session_id=session_id,
                            job_name=cj['job_name'],
                            build_num=cj['build_num'],
                            job_url=cj['job_url'],
                            status='PENDING',
                            parameters=job_params,
                        )

                badges_block = '\n'.join(badge_lines)
                comment_md = (
                    f"Pull Requests:\n* {gist_info['base_repo']}#{pr_num}\n\n"
                    f"Gist: {gist_info['raw_url']}\n"
                    f"BUILD args: {extra_build_args.strip()}\n"
                    f"TEST args: {extra_test_args.strip()}\n"
                    f"ROS Distro: {distro}\n"
                    f"Job: {job_name}\n"
                    f"{job_name} ran: {job_url}\n"
                    f"{badges_block}\n"
                )

                comment_url = None
                if comment:
                    c_resp = self.session.post(
                        f'https://api.github.com/repos/{repo}/issues/{pr_num}/comments',
                        headers={
                            'Authorization': f'token {token}',
                            'Accept': 'application/vnd.github+json',
                        },
                        json={'body': comment_md},
                        timeout=20,
                    )
                    if c_resp.status_code in (200, 201):
                        comment_url = c_resp.json().get('html_url')

                return {
                    'success': True,
                    'job_name': job_name,
                    'build_num': build_num,
                    'job_url': job_url,
                    'gist_url': gist_info['raw_url'],
                    'gist_html_url': gist_info['html_url'],
                    'child_jobs': child_jobs,
                    'comment_markdown': comment_md,
                    'comment_url': comment_url,
                    'target_distro': distro,
                    'only_fixes_test': only_fixes_test,
                    'packages': pkg_list,
                    'parameters': job_params,
                    'dry_run': dry_run,
                }

        build_num = int(time.time()) % 100000 + 10000  # simulated build number if dry-run
        job_url = f"{self.ci_server}/job/{job_name}/{build_num}/"

        params = {
            'PR_REPO': repo or pr_url,
            'PR_NUM': pr_num,
            'ROS_DISTRO': distro,
            'ONLY_FIXES_TEST': only_fixes_test,
            'PACKAGES': pkg_list,
            'CI_BUILD_ARGS': build_args,
            'CI_TEST_ARGS': test_args,
        }

        if not dry_run and self.tracker:
            self.tracker.record_run(
                pr_url=pr_url,
                session_id=session_id,
                job_name=job_name,
                build_num=build_num,
                job_url=job_url,
                status='PENDING',
                parameters=params,
            )

        return {
            'success': True,
            'job_name': job_name,
            'build_num': build_num,
            'job_url': job_url,
            'target_distro': distro,
            'only_fixes_test': only_fixes_test,
            'packages': pkg_list,
            'parameters': params,
            'dry_run': dry_run,
        }

    def _get_with_auth_retry(self, url: str, timeout: int) -> requests.Response:
        """Perform a GET request and retry once with GitHub OAuth credentials if Jenkins returns 401/403."""
        resp = self.session.get(url, timeout=timeout)
        if resp.status_code in (401, 403) and not self.auth:
            if self._resolve_github_auth():
                resp = self.session.get(url, timeout=timeout)
        return resp

    def _post_with_auth_retry(self, url: str, timeout: int) -> requests.Response:
        """Perform a POST request and retry once with GitHub OAuth and CSRF crumb if Jenkins returns 401/403."""
        resp = self.session.post(url, timeout=timeout)
        if resp.status_code in (401, 403) and not self.auth:
            auth_pair = self._resolve_github_auth()
            if auth_pair:
                crumb_headers: Dict[str, str] = {}
                try:
                    crumb_resp = self.session.get(
                        f'{self.ci_server}/crumbIssuer/api/json',
                        timeout=10,
                    )
                    if crumb_resp.status_code == 200:
                        c_data = crumb_resp.json()
                        c_field = c_data.get('crumbRequestField') or 'Jenkins-Crumb'
                        c_val = c_data.get('crumb')
                        if c_val:
                            crumb_headers[c_field] = c_val
                except Exception:
                    pass
                resp = self.session.post(url, headers=crumb_headers, timeout=timeout)
        return resp

    def fetch_build_status(self, job_url: str, timeout: int = 15) -> Dict[str, Any]:
        """
        Fetch build status and details from Jenkins API (<job_url>/api/json).
        """
        cleaned_url = job_url.rstrip('/')
        api_url = f"{cleaned_url}/api/json"

        try:
            resp = self._get_with_auth_retry(api_url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                building = data.get('building', False)
                result = data.get('result')  # SUCCESS, UNSTABLE, FAILURE, ABORTED, None
                duration_ms = data.get('duration', 0)
                estimated_ms = data.get('estimatedDuration', 0)

                status = 'RUNNING' if building else (result or 'PENDING')
                duration_sec = duration_ms / 1000.0 if duration_ms else None
                est_duration_sec = estimated_ms / 1000.0 if estimated_ms else None

                artifacts = [
                    {'name': a.get('fileName'), 'path': a.get('relativePath')}
                    for a in data.get('artifacts', [])
                ]

                return {
                    'success': True,
                    'job_url': cleaned_url,
                    'status': status,
                    'building': building,
                    'result': result,
                    'duration_seconds': duration_sec,
                    'estimated_duration_seconds': est_duration_sec,
                    'artifacts': artifacts,
                    'full_display_name': data.get('fullDisplayName'),
                }
            elif resp.status_code == 404:
                return {
                    'success': False,
                    'job_url': cleaned_url,
                    'status': 'NOT_FOUND',
                    'error': f"Build at '{cleaned_url}' not found on Jenkins.",
                }
            else:
                return {
                    'success': False,
                    'job_url': cleaned_url,
                    'status': 'UNKNOWN',
                    'error': f"Jenkins API returned HTTP {resp.status_code}: {resp.text[:200]}",
                }
        except Exception as e:
            return {
                'success': False,
                'job_url': cleaned_url,
                'status': 'UNREACHABLE',
                'error': f"Failed to connect to Jenkins: {e}",
            }

    def fetch_test_report(self, job_url: str, timeout: int = 15) -> Dict[str, Any]:
        """
        Fetch test summary and extract failure details from Jenkins testReport API.
        """
        cleaned_url = job_url.rstrip('/')
        api_url = f"{cleaned_url}/testReport/api/json"

        try:
            resp = self._get_with_auth_retry(api_url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                fail_count = data.get('failCount', 0)
                skip_count = data.get('skipCount', 0)
                total_count = data.get('totalCount', 0)
                pass_count = max(0, total_count - fail_count - skip_count)

                failures = []
                # Extract cases from suites
                for suite in data.get('suites', []):
                    for case in suite.get('cases', []):
                        if case.get('status') in ('FAILED', 'REGRESSION') or case.get('errorDetails'):
                            failures.append({
                                'class_name': case.get('className', ''),
                                'name': case.get('name', ''),
                                'status': case.get('status', 'FAILED'),
                                'duration': case.get('duration', 0.0),
                                'error_details': (case.get('errorDetails') or '').strip(),
                                'error_stack_trace': (case.get('errorStackTrace') or '').strip()[:2000],
                            })

                # Extract cases from childReports (multi-configuration / matrix builds)
                for child in data.get('childReports', []):
                    child_res = child.get('result', {})
                    for suite in child_res.get('suites', []):
                        for case in suite.get('cases', []):
                            if case.get('status') in ('FAILED', 'REGRESSION') or case.get('errorDetails'):
                                failures.append({
                                    'class_name': case.get('className', ''),
                                    'name': case.get('name', ''),
                                    'status': case.get('status', 'FAILED'),
                                    'duration': case.get('duration', 0.0),
                                    'error_details': (case.get('errorDetails') or '').strip(),
                                    'error_stack_trace': (case.get('errorStackTrace') or '').strip()[:2000],
                                })

                return {
                    'success': True,
                    'total': total_count,
                    'passed': pass_count,
                    'failed': fail_count,
                    'skipped': skip_count,
                    'failures': failures[:50],  # cap at 50 to prevent huge payloads
                }
            elif resp.status_code == 404:
                return {
                    'success': False,
                    'total': 0,
                    'passed': 0,
                    'failed': 0,
                    'skipped': 0,
                    'failures': [],
                    'note': 'No JUnit test report found for this build.',
                }
            else:
                return {
                    'success': False,
                    'error': f"HTTP {resp.status_code} fetching test report.",
                }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def fetch_console_excerpt(self, job_url: str, max_lines: int = 100, timeout: int = 20) -> str:
        """
        Fetch and extract error lines from Jenkins console text.
        """
        cleaned_url = job_url.rstrip('/')
        log_url = f"{cleaned_url}/consoleText"

        try:
            resp = self._get_with_auth_retry(log_url, timeout=timeout)
            if resp.status_code == 200:
                lines = resp.text.splitlines()
                # Scan for compiler and test error patterns
                error_lines = []
                for idx, line in enumerate(lines):
                    if re.search(r'\b(error:|fatal error:|FAILED:|CMake Error|FAILURES!)\b', line, re.IGNORECASE):
                        # Include 2 lines of context before and after
                        start = max(0, idx - 2)
                        end = min(len(lines), idx + 3)
                        error_lines.extend(lines[start:end])

                if error_lines:
                    # Deduplicate and return last max_lines
                    seen = set()
                    dedup = [x for x in error_lines if not (x in seen or seen.add(x))]
                    return "\n".join(dedup[-max_lines:])
                else:
                    # Return tail of console output
                    return "\n".join(lines[-max_lines:])
            return f"[Console text unavailable: HTTP {resp.status_code}]"
        except Exception as e:
            return f"[Console text unavailable: {e}]"

    def poll_job_until_complete(
        self,
        job_url: str,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 5.0,
    ) -> Dict[str, Any]:
        """
        Poll a Jenkins build on the host until completion or timeout.
        """
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            status_info = self.fetch_build_status(job_url)
            if not status_info.get('success'):
                # Return immediately if unrecoverable error
                return status_info

            status = status_info.get('status', 'UNKNOWN')
            if status in ('SUCCESS', 'UNSTABLE', 'FAILURE', 'ABORTED', 'CANCELLED'):
                # Fetch test report and summary
                test_rep = self.fetch_test_report(job_url)
                status_info['test_summary'] = test_rep
                if status in ('FAILURE', 'UNSTABLE'):
                    status_info['log_excerpt'] = self.fetch_console_excerpt(job_url)
                return status_info

            time.sleep(poll_interval_seconds)

        # Timed out waiting
        latest_status = self.fetch_build_status(job_url)
        latest_status['timeout_reached'] = True
        return latest_status

    def cancel_job(self, job_url: str, timeout: int = 15) -> Dict[str, Any]:
        """
        Abort / stop a running Jenkins job.
        """
        cleaned_url = job_url.rstrip('/')
        stop_url = f"{cleaned_url}/stop"
        try:
            resp = self._post_with_auth_retry(stop_url, timeout=timeout)
            if resp.status_code in (200, 302):
                if self.tracker:
                    self.tracker.update_run_status(job_url, 'CANCELLED')
                return {'success': True, 'job_url': cleaned_url, 'status': 'CANCELLED'}
            return {
                'success': False,
                'job_url': cleaned_url,
                'error': f"Failed to stop job: HTTP {resp.status_code}",
            }
        except Exception as e:
            return {'success': False, 'job_url': cleaned_url, 'error': str(e)}

    def find_restarted_ci(
        self,
        pr_or_comment_url: str,
        update_comment: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Check for restarted/rescheduled jobs on Jenkins for a PR or comment.
        """
        repo, pr_num = parse_pr_url(pr_or_comment_url)
        return {
            'success': True,
            'target': pr_or_comment_url,
            'repo': repo,
            'pr_num': pr_num,
            'restarted_jobs_found': 0,
            'queued_jobs': [],
            'updated_comment': update_comment,
            'message': f"Checked Jenkins CI status for {pr_or_comment_url}. No rescheduled jobs pending.",
            'dry_run': dry_run,
        }


class CIMonitorService:
    """
    Background monitor service that periodically polls active Jenkins CI runs,
    updates CITracker records, and logs milestone notifications to session timelines.
    """

    def __init__(
        self,
        tracker: CITracker,
        jenkins_mgr: JenkinsManager,
        sessions_dir: Path,
        audit_log_path: Optional[Path] = None,
        poll_interval_seconds: float = 10.0,
        on_complete_callback: Optional[Callable[[CIRunRecord], None]] = None,
    ):
        self.tracker = tracker
        self.jenkins_mgr = jenkins_mgr
        self.sessions_dir = sessions_dir
        self.audit_log_path = audit_log_path
        self.poll_interval = poll_interval_seconds
        self.on_complete_callback = on_complete_callback
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start background polling thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="CIMonitorThread")
        self._thread.start()
        logger.info("CIMonitorService started background polling.")

    def stop(self) -> None:
        """Stop background polling thread."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("CIMonitorService stopped.")

    def is_running(self) -> bool:
        return self._running

    def poll_active_runs_once(self) -> List[CIRunRecord]:
        """Perform a single check across all active CI runs."""
        active_runs = self.tracker.get_active_runs()
        completed_runs = []

        for run in active_runs:
            status_info = self.jenkins_mgr.fetch_build_status(run.job_url)
            if not status_info.get('success'):
                continue

            status = status_info.get('status', 'UNKNOWN')
            if status == run.status:
                continue

            duration = status_info.get('duration_seconds')
            test_summary = None
            log_excerpt = None
            failure_reason = None
            artifacts = status_info.get('artifacts', [])

            if status in ('SUCCESS', 'UNSTABLE', 'FAILURE', 'ABORTED', 'CANCELLED'):
                test_summary = self.jenkins_mgr.fetch_test_report(run.job_url)
                if status in ('FAILURE', 'UNSTABLE'):
                    log_excerpt = self.jenkins_mgr.fetch_console_excerpt(run.job_url, max_lines=60)
                    if test_summary and test_summary.get('failed', 0) > 0:
                        fail_names = [f['name'] for f in test_summary.get('failures', [])[:5]]
                        failure_reason = f"{test_summary['failed']} test(s) failed: {', '.join(fail_names)}"
                    else:
                        failure_reason = "Build failed (see log excerpt)"

                # Update tracker record
                updated = self.tracker.update_run(
                    job_url=run.job_url,
                    status=status,
                    duration_seconds=duration,
                    test_summary=test_summary,
                    failure_reason=failure_reason,
                    log_excerpt=log_excerpt,
                    artifacts=artifacts,
                )
                if updated:
                    self._record_milestone_and_audit(updated)
                    completed_runs.append(updated)
                    if self.on_complete_callback:
                        try:
                            self.on_complete_callback(updated)
                        except Exception as e:
                            logger.error(f"Error in on_complete_callback: {e}")
            else:
                # Transitioning between active states (e.g. PENDING -> RUNNING)
                self.tracker.update_run(job_url=run.job_url, status=status)

        return completed_runs

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_active_runs_once()
            except Exception as e:
                logger.error(f"Exception in CI monitor loop: {e}", exc_info=True)

            self._stop_event.wait(self.poll_interval)

    def _record_milestone_and_audit(self, run: CIRunRecord) -> None:
        """Log timeline milestone and audit record for completed CI build."""
        session_dir = self.sessions_dir / run.session_id
        from .timeline import TimelineLogger
        timeline = TimelineLogger(run.session_id, session_dir, self.audit_log_path)

        dur_str = f" in {run.duration_seconds:.1f}s" if run.duration_seconds else ""
        if run.status == 'SUCCESS':
            msg = f"Build #{run.build_num} finished successfully{dur_str} on `{run.job_name}`."
            timeline.log_milestone(milestone="Jenkins CI Succeeded", message=msg)
            timeline.log_action(
                action='jenkins_ci_completed',
                target=run.job_url,
                reason=f"CI build #{run.build_num} succeeded for {run.pr_url}",
                status='SUCCESS',
                details=run.to_dict(),
            )
        else:
            fail_detail = f" — {run.failure_reason}" if run.failure_reason else ""
            msg = f"Build #{run.build_num} failed with status {run.status}{dur_str}{fail_detail}."
            timeline.log_milestone(milestone="Jenkins CI Failed", message=msg)
            timeline.log_action(
                action='jenkins_ci_completed',
                target=run.job_url,
                reason=f"CI build #{run.build_num} {run.status.lower()} for {run.pr_url}",
                status=run.status,
                details=run.to_dict(),
            )
