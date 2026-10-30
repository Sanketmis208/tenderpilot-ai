"""Agent runs, AI usage metering, tool execution logs, copilot conversations, integrations.

LangGraph checkpoints are persisted by langgraph-checkpoint-postgres in its own tables
(checkpoints, checkpoint_blobs, checkpoint_writes) keyed by AgentRun.thread_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class AgentRun(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "agent_runs"

    kind: Mapped[str] = mapped_column(String(40), index=True)  # tender_analysis | copilot | bid_preparation
    thread_id: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running | interrupted | completed | failed | cancelled
    subject_type: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[uuid.UUID | None]
    steps: Mapped[int] = mapped_column(default=0)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(16), default="rules")  # rules | llm
    history: Mapped[list[Any]] = mapped_column(default=list)
    interrupt_payload: Mapped[dict[str, Any] | None]
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    finished_at: Mapped[datetime | None]


class AiUsageRecord(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "ai_usage_records"

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    tender_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenders.id", ondelete="SET NULL"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(60))
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(default=0)


class ToolExecutionLog(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tool_execution_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    tool: Mapped[str] = mapped_column(String(80), index=True)
    channel: Mapped[str] = mapped_column(String(24))  # mcp_http | mcp_stdio | mcp_inproc | internal
    arguments: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16))  # ok | denied | error | rate_limited
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(default=0)
    is_mutation: Mapped[bool] = mapped_column(default=False)


class CopilotConversation(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "copilot_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tender_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenders.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), default="New conversation")


class CopilotMessage(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "copilot_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("copilot_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = mapped_column(default=list)
    tool_calls: Mapped[list[Any]] = mapped_column(default=list)
    suggestions: Mapped[list[Any]] = mapped_column(default=list)
    mode: Mapped[str] = mapped_column(String(16), default="rules")
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))


class IntegrationConnection(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "integration_connections"

    provider: Mapped[str] = mapped_column(String(40))
    display_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="not_configured")
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
