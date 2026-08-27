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
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid


class ApprovalStatus:
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'


@dataclasses.dataclass
class ApprovalRequest:
    ticket_id: str
    session_id: str
    action: str
    target: str
    reason: str
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)
    status: str = ApprovalStatus.PENDING
    created_at: str = ''
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_comment: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ApprovalRequest':
        return cls(
            ticket_id=data['ticket_id'],
            session_id=data.get('session_id', ''),
            action=data['action'],
            target=data['target'],
            reason=data['reason'],
            details=data.get('details', {}),
            status=data.get('status', ApprovalStatus.PENDING),
            created_at=data.get('created_at', ''),
            resolved_at=data.get('resolved_at'),
            resolved_by=data.get('resolved_by'),
            resolution_comment=data.get('resolution_comment'),
        )


class ApprovalManager:
    """Manages maintainer approval tickets for high-stakes actions."""

    def __init__(self, approvals_file: Path):
        self.approvals_file = approvals_file

    def _load_requests(self) -> Dict[str, ApprovalRequest]:
        if not self.approvals_file.exists():
            return {}
        try:
            with open(self.approvals_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {tid: ApprovalRequest.from_dict(req) for tid, req in data.items()}
        except Exception:
            return {}

    def _save_requests(self, requests: Dict[str, ApprovalRequest]) -> None:
        self.approvals_file.parent.mkdir(parents=True, exist_ok=True)
        data = {tid: req.to_dict() for tid, req in requests.items()}
        temp_file = self.approvals_file.with_suffix(f".tmp.{os.getpid()}")
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, self.approvals_file)

    def create_request(
        self,
        session_id: str,
        action: str,
        target: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRequest:
        """Create a new pending approval ticket."""
        ticket_id = f"req-{uuid.uuid4().hex[:8]}"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        req = ApprovalRequest(
            ticket_id=ticket_id,
            session_id=session_id,
            action=action,
            target=target,
            reason=reason,
            details=details or {},
            status=ApprovalStatus.PENDING,
            created_at=now,
        )
        requests = self._load_requests()
        requests[ticket_id] = req
        self._save_requests(requests)
        return req

    def get_request(self, ticket_id: str) -> Optional[ApprovalRequest]:
        requests = self._load_requests()
        return requests.get(ticket_id)

    def list_requests(
        self,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[ApprovalRequest]:
        requests = self._load_requests()
        res = list(requests.values())
        if status:
            res = [r for r in res if r.status.upper() == status.upper()]
        if session_id:
            res = [r for r in res if r.session_id == session_id]
        return sorted(res, key=lambda r: r.created_at, reverse=True)

    def approve_request(
        self,
        ticket_id: str,
        maintainer: str = 'maintainer',
        comment: Optional[str] = None,
    ) -> ApprovalRequest:
        """Approve a pending approval ticket."""
        requests = self._load_requests()
        if ticket_id not in requests:
            raise KeyError(f"Approval ticket '{ticket_id}' not found.")

        req = requests[ticket_id]
        req.status = ApprovalStatus.APPROVED
        req.resolved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        req.resolved_by = maintainer
        req.resolution_comment = comment
        self._save_requests(requests)
        return req

    def reject_request(
        self,
        ticket_id: str,
        maintainer: str = 'maintainer',
        comment: Optional[str] = None,
    ) -> ApprovalRequest:
        """Reject a pending approval ticket."""
        requests = self._load_requests()
        if ticket_id not in requests:
            raise KeyError(f"Approval ticket '{ticket_id}' not found.")

        req = requests[ticket_id]
        req.status = ApprovalStatus.REJECTED
        req.resolved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        req.resolved_by = maintainer
        req.resolution_comment = comment
        self._save_requests(requests)
        return req

    def is_approved(self, ticket_id: str) -> bool:
        req = self.get_request(ticket_id)
        return req is not None and req.status == ApprovalStatus.APPROVED
