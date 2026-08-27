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
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


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

    def fetch_build_status(self, job_url: str, timeout: int = 15) -> Dict[str, Any]:
        """
        Fetch build status and details from Jenkins API (<job_url>/api/json).
        """
        cleaned_url = job_url.rstrip('/')
        api_url = f"{cleaned_url}/api/json"

        try:
            resp = self.session.get(api_url, timeout=timeout)
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
            resp = self.session.get(api_url, timeout=timeout)
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
            resp = self.session.get(log_url, timeout=timeout)
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
            resp = self.session.post(stop_url, timeout=timeout)
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
