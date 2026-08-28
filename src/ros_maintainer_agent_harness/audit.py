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

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def read_audit_records(
    audit_log_path: Path,
    limit: Optional[int] = None,
    session_id: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read and filter structured audit records from audit.jsonl."""
    if not audit_log_path.exists():
        return []

    records = []
    with open(audit_log_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if session_id and rec.get('session_id') != session_id:
                    continue
                if action and rec.get('action') != action:
                    continue
                if status and rec.get('status') != status:
                    continue
                records.append(rec)
            except Exception:
                continue

    if limit is not None and limit > 0:
        return records[-limit:]
    return records


def format_audit_record(record: Dict[str, Any]) -> str:
    """Format a single audit record into a human-readable string."""
    ts = record.get('timestamp', '')
    session = record.get('session_id', '')
    action = record.get('action', '')
    status = record.get('status', '')
    target = record.get('target', '')
    reason = record.get('reason', '')
    return f"[{ts}] [{status}] session={session} action={action} target={target} | reason='{reason}'"
