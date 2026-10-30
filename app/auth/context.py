"""TenantContext: the server-resolved identity every service call operates under."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.auth.permissions import Perm, has_permission
from app.core.errors import PermissionDenied


@dataclass(frozen=True)
class TenantContext:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: str
    email: str = ""
    channel: str = "web"  # web | api | mcp_http | mcp_stdio | worker
    request_id: str | None = None
    ip_address: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)  # api token scopes; empty = role-only

    def can(self, perm: Perm) -> bool:
        if not has_permission(self.role, perm):
            return False
        return not (self.scopes and perm.value not in self.scopes and "*" not in self.scopes)

    def require(self, perm: Perm) -> None:
        if not self.can(perm):
            raise PermissionDenied(f"Your role does not allow '{perm.value}'.")
