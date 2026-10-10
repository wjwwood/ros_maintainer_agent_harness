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
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

_CACHED_BLOOM_TOKENS: Optional[Set[str]] = None


def _get_known_secret_tokens() -> Set[str]:
    """Collect active secret tokens from environment and ~/.config/bloom for exact-match redaction."""
    global _CACHED_BLOOM_TOKENS
    tokens: Set[str] = set()
    for env_key in (
        'GITHUB_TOKEN',
        'GH_TOKEN',
        'GITHUB_ACCESS_TOKEN',
        'ROS_CI_GITHUB_TOKEN',
        'ROS_HOST_GITHUB_TOKEN',
        'ROS_CONTAINER_GITHUB_TOKEN',
        'ROS_MAINTAINER_GATEWAY_TOKEN',
        'OPENCODE_SERVER_PASSWORD',
        'JENKINS_TOKEN',
        'ROS_CI_JENKINS_TOKEN',
    ):
        val = (os.environ.get(env_key) or '').strip()
        if len(val) >= 8 and val.lower() != 'none':
            tokens.add(val)

    if _CACHED_BLOOM_TOKENS is None:
        bloom_tokens: Set[str] = set()
        try:
            bloom_cfg = Path.home() / '.config' / 'bloom'
            if bloom_cfg.is_file():
                data = json.loads(bloom_cfg.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    tok = str(data.get('oauth_token') or '').strip()
                    if len(tok) >= 8:
                        bloom_tokens.add(tok)
        except Exception:
            pass
        _CACHED_BLOOM_TOKENS = bloom_tokens

    tokens.update(_CACHED_BLOOM_TOKENS)
    return tokens


def redact_credentials(text: str) -> str:
    """
    Redact credentials, GitHub PAT/OAuth tokens, and URL userinfo secrets from a string.
    """
    if not text or not isinstance(text, str):
        return text

    cleaned = text

    # 1. Exact-match redaction of known environment / ~/.config/bloom tokens
    for tok in _get_known_secret_tokens():
        if tok in cleaned:
            cleaned = cleaned.replace(tok, '***REDACTED_TOKEN***')

    # 2. URL userinfo with username:password@ (e.g. https://<token>:x-oauth-basic@github.com/...)
    cleaned = re.sub(
        r'((?:https?|git|ssh)://)[^\s/@"\'<>:]+:[^\s/@"\'<>]+@',
        r'\1***:***@',
        cleaned,
    )

    # 3. URL userinfo with single token@ on HTTP(S) URLs (e.g. https://ghp_xxx@github.com/...)
    cleaned = re.sub(
        r'(https?://)(?:gh[opusr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|[A-Fa-f0-9]{20,})@',
        r'\1***@',
        cleaned,
    )

    # 4. Standard GitHub, GitLab, and harness gateway token prefixes
    cleaned = re.sub(
        r'\b(?:gh[opusr]_[A-Za-z0-9_]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|'
        r'glpat-[A-Za-z0-9_-]{20,255}|rmah_tok_[A-Za-z0-9_-]{16,255})\b',
        '***REDACTED_TOKEN***',
        cleaned,
    )

    # 5. 40-char hex GitHub OAuth / classic tokens in x-oauth-basic or Authorization contexts
    cleaned = re.sub(
        r'\b[a-fA-F0-9]{40}(?=:x-oauth-basic\b)',
        '***REDACTED_TOKEN***',
        cleaned,
    )
    cleaned = re.sub(
        r'(Authorization\s*:\s*(?:token|Bearer|Basic)\s+)[^\s"\'\r\n]+',
        r'\1***REDACTED_TOKEN***',
        cleaned,
        flags=re.IGNORECASE,
    )

    # 6. JSON/key-value oauth_token / github_token / api_token / password fields (quoted or unquoted)
    cleaned = re.sub(
        r'("?(?:oauth_token|github_token|access_token|api_token|jenkins_token|'
        r'gateway_token|ros_maintainer_gateway_token|opencode_server_password)"?\s*[:=]\s*["\'])'
        r'([^"\'\s]{6,})(["\'])',
        r'\1***REDACTED_TOKEN***\3',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r'(\b(?:oauth_token|github_token|access_token|api_token|jenkins_token|'
        r'gateway_token|ros_maintainer_gateway_token|opencode_server_password)\s*=\s*)'
        r'([^\s"\'\r\n]{6,})',
        r'\1***REDACTED_TOKEN***',
        cleaned,
        flags=re.IGNORECASE,
    )

    return cleaned


def redact_sensitive_data(value: Any) -> Any:
    """Recursively redact credentials from strings, dicts, and lists."""
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {k: redact_sensitive_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    return value


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
    """Append an audit record with `prev_hash` and `record_hash` to `audit.jsonl` (with credential redaction)."""
    from .auth import get_active_caller

    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash = get_last_record_hash(audit_log_path)
    enriched = dict(redact_sensitive_data(record))
    caller = get_active_caller()
    if caller is not None:
        enriched.setdefault('caller_role', caller.role)
        if caller.token_id:
            enriched.setdefault('token_id', caller.token_id)
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
