"""Audit-trail ORM tables. Every pipeline stage's full output is persisted —
the reasoning trail is mandatory, not optional.

Tables attach to the shared app.storage Base; init_db() imports this module
before create_all so they are created alongside the existing schema.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.storage import Base


class AgentRun(Base):
    """One orchestrator pipeline execution for one commodity."""

    __tablename__ = "commodity_agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    commodity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    # RUNNING / COMPLETED / SKIPPED_REGIME / FAILED

    regime_json: Mapped[str | None] = mapped_column(Text)
    events_json: Mapped[str | None] = mapped_column(Text)
    strikes_json: Mapped[str | None] = mapped_column(Text)
    trend_json: Mapped[str | None] = mapped_column(Text)
    event_debate_json: Mapped[str | None] = mapped_column(Text)
    vol_json: Mapped[str | None] = mapped_column(Text)
    judge_json: Mapped[str | None] = mapped_column(Text)
    risk_json: Mapped[str | None] = mapped_column(Text)
    analytics_json: Mapped[str | None] = mapped_column(Text)  # VRP / IV trend / stress
    atm_iv: Mapped[float | None] = mapped_column(Float)   # feeds IV-percentile history
    error: Mapped[str | None] = mapped_column(Text)

    recommendation: Mapped["CommodityRecommendation | None"] = relationship(
        "CommodityRecommendation", back_populates="run", uselist=False
    )


class CommodityRecommendation(Base):
    """Final (post-Risk-Guard) recommendation shown to the human."""

    __tablename__ = "commodity_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_pk: Mapped[int] = mapped_column(ForeignKey("commodity_agent_runs.id"), nullable=False, index=True)
    commodity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)   # BUY / SELL / NO_TRADE
    strategy_type: Mapped[str] = mapped_column(String(24), nullable=False)
    strikes_json: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    reasoning_summary: Mapped[str | None] = mapped_column(Text)
    dissent_json: Mapped[str | None] = mapped_column(Text)
    risk_vetoed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PROPOSED")
    # PROPOSED / APPROVED / REJECTED
    # Calibration (filled ~1 session later by calibration.evaluate_pending):
    realized_move_pct: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(16))
    # short premium: WIN (move stayed inside implied) / LOSS
    # NO_TRADE:      AVOIDED (danger materialized) / MISSED (calm — trade was there)

    run: Mapped[AgentRun] = relationship("AgentRun", back_populates="recommendation")
    decisions: Mapped[list["RecommendationDecision"]] = relationship(
        "RecommendationDecision", back_populates="recommendation"
    )


class CommodityTradeJournal(Base):
    """One row per human APPROVE — the desk journal. Entry context is frozen
    at approval time (regime, VRP, IV trend, confidence); realized P&L is
    back-filled from ClosedTrade once every leg closes (live trades only)."""

    __tablename__ = "commodity_trade_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("commodity_recommendations.id"), nullable=False, index=True
    )
    commodity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)   # live / paper
    alert_id: Mapped[int | None] = mapped_column(Integer)          # live only
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_context_json: Mapped[str | None] = mapped_column(Text)
    lots: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(String(48))


class RecommendationDecision(Base):
    """Human decision audit — one row per approve/reject action."""

    __tablename__ = "commodity_recommendation_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("commodity_recommendations.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(12), nullable=False)   # APPROVE / REJECT
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    recommendation: Mapped[CommodityRecommendation] = relationship(
        "CommodityRecommendation", back_populates="decisions"
    )
