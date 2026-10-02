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

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def compute_record_hash(record: Dict[str, Any], prev_hash: str) -> str:
    """Compute a deterministic SHA-256 hash over the audit record payload and previous record hash."""
    payload = {
        k: v for k, v in record.items()
        if k not in ('prev_hash', 'record_hash')
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    digest_input = f"{prev_hash}:{canonical}".encode('utf-8')
    return hashlib.sha256(digest_input).hexdigest()


def get_last_record_hash(audit_log_path: Path) -> str:
    """Return the `record_hash` of the last entry in `audit.jsonl` (or 64 zeroes if empty/new)."""
    genesis = '0' * 64
    if not audit_log_path.exists():
        return genesis
    try:
        last_line = ''
        with open(audit_log_path, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if not last_line:
            return genesis
        rec = json.loads(last_line)
        return str(rec.get('record_hash') or compute_record_hash(rec, str(rec.get('prev_hash') or genesis)))
    except Exception:
        return genesis


def append_audit_record(audit_log_path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    """Append an audit record with `prev_hash` and `record_hash` to `audit.jsonl`."""
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash = get_last_record_hash(audit_log_path)
    enriched = dict(record)
    enriched['prev_hash'] = prev_hash
    enriched['record_hash'] = compute_record_hash(enriched, prev_hash)
    with open(audit_log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(enriched) + '\n')
    return enriched


def verify_audit_chain(audit_log_path: Path) -> Tuple[bool, Optional[str]]:
    """Verify the SHA-256 hash chain of `audit.jsonl`."""
    if not audit_log_path.exists():
        return (True, None)
    expected_prev = '0' * 64
    with open(audit_log_path, 'r', encoding='utf-8') as f:
        for idx, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                return (False, f"Invalid JSON on line {idx}: {e}")
            if 'record_hash' not in rec:
                # Legacy record before hash chaining
                expected_prev = compute_record_hash(rec, expected_prev)
                continue
            actual_prev = str(rec.get('prev_hash') or '')
            if actual_prev != expected_prev:
                return (
                    False,
                    f"Chain broken at line {idx}: expected prev_hash={expected_prev}, got {actual_prev}",
                )
            computed = compute_record_hash(rec, expected_prev)
            if rec.get('record_hash') != computed:
                return (
                    False,
                    f"Record hash mismatch at line {idx}: expected {computed}, got {rec.get('record_hash')}",
                )
            expected_prev = computed
    return (True, None)


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
