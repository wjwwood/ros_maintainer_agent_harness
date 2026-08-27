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

import datetime
import json
from pathlib import Path
from typing import Any, Dict, Optional


class TimelineLogger:
    """Manages session timeline.md and global audit.jsonl logging."""

    def __init__(self, session_id: str, session_dir: Path, audit_log_path: Optional[Path] = None):
        self.session_id = session_id
        self.session_dir = session_dir
        self.timeline_path = session_dir / 'timeline.md'
        self.audit_log_path = audit_log_path

    def init_timeline(self, topic: Optional[str] = None) -> None:
        """Initialize the timeline.md file if not already present."""
        if not self.timeline_path.exists():
            self.session_dir.mkdir(parents=True, exist_ok=True)
            header = f"# Session Timeline: {self.session_id}"
            if topic:
                header += f" ({topic})"
            header += "\n\n"
            with open(self.timeline_path, 'w', encoding='utf-8') as f:
                f.write(header)
                f.write(f"- **[{self._current_time_str()}]** Session initialized.\n")

    def log_status(self, message: str, milestone: Optional[str] = None) -> str:
        """
        Record a status update or milestone note to timeline.md.
        """
        self.init_timeline()
        time_str = self._current_time_str()

        if milestone:
            entry = f"- **[{time_str}]** 🏆 **Milestone**: {milestone} — {message.strip()}\n"
        else:
            entry = f"- **[{time_str}]** **Status**: {message.strip()}\n"

        with open(self.timeline_path, 'a', encoding='utf-8') as f:
            f.write(entry)

        return entry.strip()

    def log_milestone(self, milestone: str, message: str) -> str:
        """Convenience method to log a milestone to timeline.md."""
        return self.log_status(message=message, milestone=milestone)

    def log_action(
        self,
        action: str,
        target: str,
        reason: str,
        status: str = "APPROVED",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Record a policy-guarded action to both timeline.md and audit.jsonl.
        """
        self.init_timeline()
        now = datetime.datetime.now(datetime.timezone.utc)
        iso_timestamp = now.isoformat()
        time_str = now.strftime('%H:%M:%S')

        # 1. Append to timeline.md
        timeline_entry = f"- **[{time_str}]** **Action ({action})**: `{target}` [{status}] — *{reason.strip()}*\n"
        with open(self.timeline_path, 'a', encoding='utf-8') as f:
            f.write(timeline_entry)

        # 2. Append to audit.jsonl
        record = {
            "timestamp": iso_timestamp,
            "session_id": self.session_id,
            "action": action,
            "status": status,
            "target": target,
            "reason": reason.strip(),
            "details": details or {},
        }

        if self.audit_log_path:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')

        return record

    def _current_time_str(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
