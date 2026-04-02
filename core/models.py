"""SQLAlchemy ORM models for all PolyFarm tables."""

import uuid
from datetime import datetime, date
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, Date,
    Text, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

class SystemConfig(Base):
    """Global flags: trading_mode, emergency_stop, etc."""
    __tablename__ = "system_config"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── AGENT COMMUNICATION BUS ──────────────────────────────────────────────────

class AgentTask(Base):
    """Priority message queue between CEO and subagents."""
    __tablename__ = "agent_tasks"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow)
    dispatched_by = Column(String, nullable=False)
    assigned_to = Column(String, nullable=False)
    task_type = Column(String, nullable=False)
    payload = Column(Text, nullable=False)          # JSON
    status = Column(String, default="pending")       # pending|running|done|failed
    result = Column(Text)                            # JSON
    error = Column(Text)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    priority = Column(Integer, default=5)            # 1=urgent 10=background

    __table_args__ = (
        Index("idx_tasks_status_assignee", "status", "assigned_to"),
        Index("idx_tasks_priority", "priority", "created_at"),
    )


# ─── RESEARCH ─────────────────────────────────────────────────────────────────

class ResearchSpec(Base):
    """Wallet analysis output, awaiting CEO approval before bot creation."""
    __tablename__ = "research_specs"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow)
    target_address = Column(String, nullable=False, unique=True)
    estimated_daily_capital = Column(Float)
    win_rate_resolved = Column(Float)
    avg_roi_resolved = Column(Float)
    primary_categories = Column(Text)               # JSON array
    exit_behavior = Column(String)                  # hold|sell|mixed
    farming_risk = Column(String)                   # low|medium|high
    scaling_floor = Column(Float)
    poll_interval_seconds = Column(Integer, default=30)
    confidence_score = Column(Float)
    analysis_summary = Column(Text)
    status = Column(String, default="pending")      # pending|approved|rejected|implemented
    reviewed_by = Column(String)
    review_notes = Column(Text)
    bot_id = Column(String, ForeignKey("bot_registry.id"))


# ─── BOT REGISTRY ─────────────────────────────────────────────────────────────

class BotRegistry(Base):
    """Active and inactive copy-trading bots."""
    __tablename__ = "bot_registry"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow)
    name = Column(String, nullable=False)
    target_address = Column(String, nullable=False, unique=True)
    wallet_address = Column(String, default="")     # our execution wallet (Phase 3)
    poll_interval_sec = Column(Integer, default=30)
    our_capital = Column(Float, default=100.0)      # our starting capital for this bot (USD)
    target_daily_capital = Column(Float, default=2000.0)  # target's estimated daily volume (auto-updated weekly)
    paper_mode = Column(Boolean, default=True)
    active = Column(Boolean, default=True)
    paused = Column(Boolean, default=False)
    research_spec_id = Column(String)
    last_activity_at = Column(DateTime)
    total_trades = Column(Integer, default=0)


# ─── DEDUPLICATION ────────────────────────────────────────────────────────────

class SeenTransaction(Base):
    """Prevents replaying the same on-chain tx twice."""
    __tablename__ = "seen_transactions"

    bot_id = Column(String, ForeignKey("bot_registry.id"), primary_key=True)
    tx_hash = Column(String, primary_key=True)
    seen_at = Column(DateTime, default=datetime.utcnow)


# ─── TRADE PIPELINE ───────────────────────────────────────────────────────────

class TargetTrade(Base):
    """Every trade detected from a watched wallet — full audit log."""
    __tablename__ = "target_trades"

    id = Column(String, primary_key=True, default=_uuid)
    bot_id = Column(String, ForeignKey("bot_registry.id"), nullable=False)
    detected_at = Column(DateTime, default=datetime.utcnow)
    tx_hash = Column(String, nullable=False)
    market_id = Column(String, nullable=False)
    question = Column(Text)                         # market question text
    outcome = Column(String, nullable=False)        # YES|NO
    side = Column(String, nullable=False)           # BUY|SELL
    trade_type = Column(String, nullable=False)     # TRADE|MERGE|REDEEM
    target_size = Column(Float, nullable=False)
    target_price = Column(Float)
    scaled_size = Column(Float)                     # after proportional sizing
    status = Column(String, default="pending")      # pending|executed|skipped|paper
    skip_reason = Column(Text)

    __table_args__ = (
        Index("idx_target_trades_bot", "bot_id", "detected_at"),
    )


class PaperTrade(Base):
    """Hypothetical trades logged in paper mode — mirrors live order table."""
    __tablename__ = "paper_trades"

    id = Column(String, primary_key=True, default=_uuid)
    bot_id = Column(String, ForeignKey("bot_registry.id"), nullable=False)
    target_trade_id = Column(String, ForeignKey("target_trades.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    market_id = Column(String, nullable=False)
    question = Column(Text)
    outcome = Column(String, nullable=False)
    side = Column(String, nullable=False)           # BUY|SELL
    hypothetical_size = Column(Float, nullable=False)
    hypothetical_price = Column(Float, nullable=False)
    hypothetical_value = Column(Float)              # size * price
    market_resolved = Column(Boolean, default=False)
    winning_outcome = Column(String)                # YES|NO|null
    hypothetical_pnl = Column(Float)               # calculated after resolution

    __table_args__ = (
        Index("idx_paper_trades_bot", "bot_id", "created_at"),
    )


class Order(Base):
    """Live CLOB orders — written in Phase 3."""
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=_uuid)
    bot_id = Column(String, ForeignKey("bot_registry.id"), nullable=False)
    target_trade_id = Column(String, ForeignKey("target_trades.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    clob_order_id = Column(String)
    market_id = Column(String, nullable=False)
    question = Column(Text)
    outcome = Column(String, nullable=False)
    side = Column(String, nullable=False)
    size = Column(Float, nullable=False)
    limit_price = Column(Float, nullable=False)
    filled_size = Column(Float, default=0.0)
    avg_fill_price = Column(Float)
    status = Column(String, default="open")         # open|filled|partial|cancelled|expired
    cancel_reason = Column(Text)
    submitted_at = Column(DateTime)
    filled_at = Column(DateTime)
    cancelled_at = Column(DateTime)

    __table_args__ = (
        Index("idx_orders_bot", "bot_id", "created_at"),
    )


# ─── PORTFOLIO STATE ──────────────────────────────────────────────────────────

class Position(Base):
    """Current holdings per bot/market/outcome."""
    __tablename__ = "positions"

    id = Column(String, primary_key=True, default=_uuid)
    bot_id = Column(String, ForeignKey("bot_registry.id"), nullable=False)
    market_id = Column(String, nullable=False)
    outcome = Column(String, nullable=False)
    size = Column(Float, default=0.0)
    avg_cost = Column(Float)
    current_price = Column(Float)
    unrealized_pnl = Column(Float)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("bot_id", "market_id", "outcome"),
    )


class DailyPnl(Base):
    """Aggregated P&L per bot per day — powers the dashboard."""
    __tablename__ = "daily_pnl"

    bot_id = Column(String, ForeignKey("bot_registry.id"), primary_key=True)
    date = Column(String, primary_key=True)         # YYYY-MM-DD
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    total_traded_usd = Column(Float, default=0.0)
    num_trades = Column(Integer, default=0)
    num_skipped = Column(Integer, default=0)


# ─── MARKET CACHE ─────────────────────────────────────────────────────────────

class MarketResolution(Base):
    """Cached market resolution data — updated daily by scheduler."""
    __tablename__ = "market_resolutions"

    market_id = Column(String, primary_key=True)
    question = Column(Text)
    category = Column(String)
    resolved = Column(Boolean, default=False)
    winning_outcome = Column(String)                # YES|NO|null
    resolved_at = Column(DateTime)
    last_checked = Column(DateTime, default=datetime.utcnow)


# ─── INFRASTRUCTURE ───────────────────────────────────────────────────────────

class HealthEvent(Base):
    """Event-driven health log — written only when something happens."""
    __tablename__ = "health_events"

    id = Column(String, primary_key=True, default=_uuid)
    timestamp = Column(DateTime, default=datetime.utcnow)
    component = Column(String, nullable=False)      # bot_X, clob_api, etc.
    event_type = Column(String, nullable=False)     # started|died|restarted|failed
    details = Column(Text)                          # JSON


class Alert(Base):
    """Unacknowledged issues that need CEO attention."""
    __tablename__ = "alerts"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow)
    severity = Column(String, nullable=False)       # info|warn|critical
    source = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    acknowledged = Column(Boolean, default=False)
    acknowledged_at = Column(DateTime)
    telegram_sent = Column(Boolean, default=False)
