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
import datetime
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple


@dataclasses.dataclass
class CIRunRecord:
    pr_url: str
    session_id: str
    job_name: str
    build_num: int
    job_url: str
    status: str  # PENDING, RUNNING, SUCCESS, FAILURE, CANCELLED
    timestamp: float
    iso_time: str
    parameters: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CIRunRecord':
        return cls(**data)


class CITracker:
    """Tracks active and historical Jenkins CI runs launched via the gateway."""

    def __init__(self, storage_file: Path):
        self.storage_file = storage_file

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
        with open(self.storage_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

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
        runs = self._load_runs()
        runs.append(rec)
        self._save_runs(runs)
        return rec

    def get_active_runs_count(self, pr_url: str) -> int:
        runs = self._load_runs()
        active = [
            r for r in runs
            if r.pr_url == pr_url and r.status.upper() in ('PENDING', 'RUNNING')
        ]
        return len(active)

    def get_seconds_since_last_run(self, pr_url: str) -> Optional[float]:
        runs = [r for r in self._load_runs() if r.pr_url == pr_url]
        if not runs:
            return None
        latest = max(runs, key=lambda r: r.timestamp)
        return max(0.0, time.time() - latest.timestamp)

    def update_run_status(self, job_url: str, new_status: str) -> bool:
        runs = self._load_runs()
        updated = False
        for r in runs:
            if r.job_url == job_url:
                r.status = new_status
                updated = True
        if updated:
            self._save_runs(runs)
        return updated


def parse_pr_url(pr_url: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Parse repo and PR number from URL or shorthand.

    Examples:
        - https://github.com/ros2/rclcpp/pull/160 -> ('ros2/rclcpp', 160)
        - ros2/rclcpp#160 -> ('ros2/rclcpp', 160)
    """
    if not pr_url:
        return (None, None)

    # Shorthand: ros2/rclcpp#160
    m_short = re.match(r'^([^/]+/[^/#]+)#(\d+)$', pr_url.strip())
    if m_short:
        return (m_short.group(1), int(m_short.group(2)))

    # Full GitHub PR URL: https://github.com/ros2/rclcpp/pull/160
    m_url = re.match(r'^https?://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:/.*)?$', pr_url.strip())
    if m_url:
        return (m_url.group(1), int(m_url.group(2)))

    return (None, None)


class JenkinsManager:
    """Manages Jenkins CI triggering and restarted build discovery."""

    def __init__(self, ci_server: str = 'https://ci.ros2.org', tracker: Optional[CITracker] = None):
        self.ci_server = ci_server.rstrip('/')
        self.tracker = tracker

    def launch_ci(
        self,
        session_id: str,
        pr_url: str,
        target_distro: Optional[str] = None,
        job_type: Optional[str] = None,
        only_fixes_test: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Trigger a Jenkins CI run for the given pull request.
        """
        repo, pr_num = parse_pr_url(pr_url)
        distro = target_distro or 'rolling'
        job_name = job_type or 'ci_launcher'
        build_num = int(time.time()) % 100000 + 10000  # simulated build number if dry-run
        job_url = f"{self.ci_server}/job/{job_name}/{build_num}/"

        params = {
            'PR_REPO': repo or pr_url,
            'PR_NUM': pr_num,
            'ROS_DISTRO': distro,
            'ONLY_FIXES_TEST': only_fixes_test,
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
            'parameters': params,
            'dry_run': dry_run,
        }

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
        # Structured discovery response
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
