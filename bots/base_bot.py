"""
Core copy-trading bot logic.

Polls a target wallet every N seconds, detects new trades,
applies proportional sizing + risk checks, then logs to paper_trades
(or hands off to execution.py in live mode).

One instance of this class runs per registered bot.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, date

from sqlalchemy import select, func

from core.config import settings
from core.database import get_session
from core.models import (
    BotRegistry, SeenTransaction, TargetTrade, PaperTrade, DailyPnl,
    SystemConfig, Alert,
)
from bots.risk import TradeProposal, RiskDecision, check_trade, calculate_scaled_size
from services.polymarket.data_api import get_wallet_activity

logger = logging.getLogger(__name__)


class CopyBot:
    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self._load_config()

    def _load_config(self):
        with get_session() as session:
            bot = session.get(BotRegistry, self.bot_id)
            if not bot:
                raise ValueError(f"Bot {self.bot_id} not found in registry")
            self.name = bot.name
            self.target_address = bot.target_address
            self.poll_interval = bot.poll_interval_sec
            self.paper_mode = bot.paper_mode
            self.target_daily_capital = bot.target_daily_capital or 2000.0

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        logger.info("[%s] Starting. Target: %s | Mode: %s",
                    self.name, self.target_address, "PAPER" if self.paper_mode else "LIVE")
        while True:
            try:
                if self._is_paused() or self._emergency_stop():
                    logger.debug("[%s] Paused or emergency stop active. Sleeping.", self.name)
                    time.sleep(self.poll_interval)
                    continue

                self._poll_and_process()

            except KeyboardInterrupt:
                logger.info("[%s] Shutting down.", self.name)
                break
            except Exception as e:
                logger.exception("[%s] Unexpected error: %s", self.name, e)
                self._write_alert("warn", f"Bot loop error: {e}")

            time.sleep(self.poll_interval)

    def _poll_and_process(self):
        """Fetch latest activity and process any new trades."""
        try:
            activity = get_wallet_activity(self.target_address, limit=50)
        except Exception as e:
            logger.warning("[%s] Failed to fetch activity: %s", self.name, e)
            return

        new_trades = self._filter_new(activity)
        if not new_trades:
            logger.debug("[%s] No new activity.", self.name)
            return

        logger.info("[%s] Found %d new trade(s).", self.name, len(new_trades))
        for tx in new_trades:
            self._handle_trade(tx)

        self._update_last_activity()

    # ── Deduplication ─────────────────────────────────────────────────────────

    def _filter_new(self, activity: list[dict]) -> list[dict]:
        """Return only activity entries not yet seen by this bot."""
        if not activity:
            return []

        tx_hashes = [a.get("transactionHash", "") for a in activity if a.get("transactionHash")]
        if not tx_hashes:
            return []

        with get_session() as session:
            seen = set(
                row[0] for row in session.execute(
                    select(SeenTransaction.tx_hash).where(
                        SeenTransaction.bot_id == self.bot_id,
                        SeenTransaction.tx_hash.in_(tx_hashes),
                    )
                ).all()
            )

        new = [a for a in activity if a.get("transactionHash") not in seen]

        # Mark all fetched hashes as seen (even skipped ones — avoids re-processing)
        with get_session() as session:
            for tx_hash in tx_hashes:
                if tx_hash not in seen:
                    session.add(SeenTransaction(bot_id=self.bot_id, tx_hash=tx_hash))

        return new

    # ── Trade handling ─────────────────────────────────────────────────────────

    def _handle_trade(self, tx: dict):
        """Process a single detected transaction."""
        tx_hash = tx.get("transactionHash", "")
        market_id = tx.get("conditionId", "") or tx.get("market", "")
        side = (tx.get("side") or "BUY").upper()
        trade_type = (tx.get("type") or "TRADE").upper()
        question = tx.get("title") or tx.get("question") or ""

        # Parse outcome (YES/NO) from asset field if available
        outcome = self._parse_outcome(tx)

        # Parse sizes
        try:
            target_size = float(tx.get("usdcSize") or tx.get("size") or 0)
        except (TypeError, ValueError):
            target_size = 0.0

        try:
            target_price = float(tx.get("price") or 0)
        except (TypeError, ValueError):
            target_price = 0.0

        if target_size <= 0:
            logger.debug("[%s] Skipping tx %s: size=0", self.name, tx_hash[:8])
            return

        # Calculate scaled size
        portfolio_balance = self._get_portfolio_balance()
        target_daily_capital = self._estimate_target_capital()
        scaled_size = calculate_scaled_size(target_size, target_daily_capital, portfolio_balance)

        # Log target trade (always — full audit trail)
        target_trade_id = self._log_target_trade(
            tx_hash, market_id, outcome, side, trade_type, question,
            target_size, target_price, scaled_size
        )

        if scaled_size <= 0:
            self._mark_target_trade(target_trade_id, "skipped", "Scaled size below minimum $1.00")
            logger.info("[%s] Skipping trade (scaled size $%.2f < $1.00)", self.name, scaled_size)
            return

        # Risk check
        proposal = TradeProposal(
            bot_id=self.bot_id,
            market_id=market_id,
            outcome=outcome,
            side=side,
            proposed_size_usd=scaled_size,
            current_price=target_price,
        )
        decision = check_trade(proposal, portfolio_balance)

        if not decision.approved:
            self._mark_target_trade(target_trade_id, "skipped", decision.reason)
            logger.info("[%s] Trade REJECTED: %s", self.name, decision.reason)
            return

        # Execute (paper or live)
        if self.paper_mode:
            self._execute_paper(target_trade_id, market_id, question, outcome, side, scaled_size, target_price)
        else:
            self._execute_live(target_trade_id, market_id, question, outcome, side, scaled_size, target_price)

    def _parse_outcome(self, tx: dict) -> str:
        """
        Extract YES/NO from transaction data.
        Real API has 'outcome' field: e.g. "Yes", "No", "YES", "NO".
        outcomeIndex: 0 = first outcome (typically YES), 1 = second (typically NO).
        """
        outcome = tx.get("outcome") or ""
        if isinstance(outcome, str) and outcome:
            upper = outcome.upper()
            if upper in ("YES", "Y"):
                return "YES"
            if upper in ("NO", "N"):
                return "NO"
        # Fall back to outcomeIndex: 0=YES, 1=NO (Polymarket convention)
        idx = tx.get("outcomeIndex")
        if idx is not None:
            return "YES" if int(idx) == 0 else "NO"
        return "YES"  # Safe default

    # ── Paper execution ────────────────────────────────────────────────────────

    def _execute_paper(
        self, target_trade_id: str, market_id: str, question: str,
        outcome: str, side: str, size: float, price: float
    ):
        value = size * price if side == "BUY" else size
        with get_session() as session:
            paper = PaperTrade(
                id=str(uuid.uuid4()),
                bot_id=self.bot_id,
                target_trade_id=target_trade_id,
                market_id=market_id,
                question=question,
                outcome=outcome,
                side=side,
                hypothetical_size=size,
                hypothetical_price=price,
                hypothetical_value=value,
            )
            session.add(paper)

        self._mark_target_trade(target_trade_id, "paper", None)
        self._update_daily_pnl(size, 0.0)  # traded volume, no realized PnL yet

        logger.info(
            "[%s] 📄 PAPER %s %s %s | $%.2f @ %.3f",
            self.name, side, outcome, market_id[:8], size, price
        )

    # ── Live execution (Phase 3) ───────────────────────────────────────────────

    def _execute_live(
        self, target_trade_id: str, market_id: str, question: str,
        outcome: str, side: str, size: float, price: float
    ):
        """Placeholder — wired up in Phase 3 with execution.py."""
        logger.warning(
            "[%s] Live trading not yet implemented. Trade: %s %s $%.2f",
            self.name, side, outcome, size
        )
        self._mark_target_trade(target_trade_id, "skipped", "live_not_implemented")

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _log_target_trade(
        self, tx_hash, market_id, outcome, side, trade_type, question,
        target_size, target_price, scaled_size
    ) -> str:
        trade_id = str(uuid.uuid4())
        with get_session() as session:
            session.add(TargetTrade(
                id=trade_id,
                bot_id=self.bot_id,
                tx_hash=tx_hash,
                market_id=market_id,
                question=question,
                outcome=outcome,
                side=side,
                trade_type=trade_type,
                target_size=target_size,
                target_price=target_price,
                scaled_size=scaled_size,
                status="pending",
            ))
        return trade_id

    def _mark_target_trade(self, trade_id: str, status: str, skip_reason: Optional[str]):
        with get_session() as session:
            trade = session.get(TargetTrade, trade_id)
            if trade:
                trade.status = status
                trade.skip_reason = skip_reason

    def _get_portfolio_balance(self) -> float:
        """Return current portfolio balance. Uses initial value until positions tracked."""
        return settings.initial_portfolio_usd

    def _estimate_target_capital(self) -> float:
        """Target's estimated deployed capital — set per-bot in registry."""
        return self.target_daily_capital

    def _update_daily_pnl(self, traded_volume: float, realized_pnl: float):
        today = date.today().isoformat()
        with get_session() as session:
            row = session.execute(
                select(DailyPnl).where(
                    DailyPnl.bot_id == self.bot_id,
                    DailyPnl.date == today,
                )
            ).scalar_one_or_none()
            if row:
                row.total_traded_usd += traded_volume
                row.realized_pnl += realized_pnl
                row.num_trades += 1
            else:
                session.add(DailyPnl(
                    bot_id=self.bot_id,
                    date=today,
                    total_traded_usd=traded_volume,
                    realized_pnl=realized_pnl,
                    num_trades=1,
                ))

    def _update_last_activity(self):
        with get_session() as session:
            bot = session.get(BotRegistry, self.bot_id)
            if bot:
                bot.last_activity_at = datetime.utcnow()
                bot.total_trades += 1

    def _is_paused(self) -> bool:
        with get_session() as session:
            bot = session.get(BotRegistry, self.bot_id)
            return bool(bot and bot.paused)

    def _emergency_stop(self) -> bool:
        with get_session() as session:
            row = session.get(SystemConfig, "emergency_stop")
            return bool(row and row.value == "1")

    def _write_alert(self, severity: str, message: str):
        with get_session() as session:
            session.add(Alert(
                id=str(uuid.uuid4()),
                severity=severity,
                source=f"bot:{self.name}",
                message=message,
            ))
