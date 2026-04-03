"""
PolyFarm Dashboard + CEO Chat — lightweight web server.

Routes:
  GET  /           → dashboard (basic auth required)
  GET  /chat       → CEO chat interface (basic auth required)
  GET  /api/data   → dashboard JSON (basic auth required)
  POST /api/chat   → CEO agent endpoint (basic auth required)

Auth: HTTP Basic Auth. Set DASHBOARD_USER / DASHBOARD_PASSWORD in .env.
"""

import base64
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from sqlalchemy import select, func, desc
from core.database import init_db, get_session
from core.models import BotRegistry, PaperTrade, TargetTrade, DailyPnl, SystemConfig

PORT = int(os.environ.get("DASHBOARD_PORT", 8080))
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_auth(handler) -> bool:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
        return user == DASHBOARD_USER and pwd == DASHBOARD_PASSWORD
    except Exception:
        return False


def _require_auth(handler):
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="PolyFarm"')
    handler.send_header("Content-Type", "text/plain")
    handler.end_headers()
    handler.wfile.write(b"Unauthorized")


# ── Dashboard data ────────────────────────────────────────────────────────────

def get_skipped_trades(days: int = 7) -> dict:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_session() as session:
        bots_raw = session.execute(select(BotRegistry)).scalars().all()
        bot_names = {b.id: b.name for b in bots_raw}
        rows = session.execute(
            select(TargetTrade)
            .where(TargetTrade.status == "skipped")
            .where(TargetTrade.detected_at >= since)
            .order_by(desc(TargetTrade.detected_at))
            .limit(500)
        ).scalars().all()
        trades = [
            {
                "time": r.detected_at.strftime("%Y-%m-%d %H:%M") if r.detected_at else "—",
                "bot": bot_names.get(r.bot_id, r.bot_id[:8] if r.bot_id else "?"),
                "side": r.side,
                "outcome": r.outcome,
                "target_size": round(r.target_size or 0, 2),
                "scaled_size": round(r.scaled_size or 0, 2),
                "price": round(r.target_price or 0, 3),
                "reason": r.skip_reason or "unknown",
                "market": (r.question or r.market_id or "")[:70],
            }
            for r in rows
        ]
    return {"skipped": trades, "total": len(trades), "days": days}


def _trade_status(t) -> str:
    """Derive won/lost/pending from resolution data."""
    if not t.market_resolved:
        return "pending"
    if t.winning_outcome:
        return "won" if (t.outcome or "").upper() == t.winning_outcome.upper() else "lost"
    # Fallback: use pnl sign if winning_outcome not stored yet
    if t.hypothetical_pnl is not None:
        return "won" if t.hypothetical_pnl > 0 else "lost"
    return "pending"


def get_dashboard_data(days: int = 7) -> dict:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    with get_session() as session:
        bots_raw = session.execute(select(BotRegistry)).scalars().all()
        bots = [
            {
                "id": b.id, "name": b.name, "target": b.target_address,
                "active": b.active, "paused": b.paused, "paper_mode": b.paper_mode,
                "total_trades": b.total_trades or 0, "capital": b.target_daily_capital or 0,
                "last_activity": b.last_activity_at.strftime("%Y-%m-%d %H:%M UTC") if b.last_activity_at else "Never",
                "reset_at": b.reset_at.isoformat() if b.reset_at else None,
            }
            for b in bots_raw
        ]
        bot_names = {b.id: b.name for b in bots_raw}

        # Fetch all trades in window — frontend handles pagination
        paper_raw = session.execute(
            select(PaperTrade).where(PaperTrade.created_at >= since)
            .order_by(desc(PaperTrade.created_at))
        ).scalars().all()

        # Fetch target sizes for each paper trade (what the wallet actually traded)
        target_size_map = {}
        target_ids = [t.target_trade_id for t in paper_raw if t.target_trade_id]
        if target_ids:
            tt_rows = session.execute(
                select(TargetTrade.id, TargetTrade.target_size)
                .where(TargetTrade.id.in_(target_ids))
            ).all()
            target_size_map = {row.id: row.target_size for row in tt_rows}

        paper_trades = [
            {
                "time": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "—",
                "bot": bot_names.get(t.bot_id, t.bot_id[:8]),
                "side": t.side,
                "outcome": t.outcome,
                "winning_outcome": t.winning_outcome or "",
                "status": _trade_status(t),
                "size": round(t.hypothetical_size or 0, 2),
                "target_size": round(target_size_map.get(t.target_trade_id) or 0, 2),
                "price": round(t.hypothetical_price or 0, 3),
                "value": round(t.hypothetical_value or 0, 2),
                "market": (t.question or t.market_id or "")[:60],
                "pnl": round(t.hypothetical_pnl, 2) if t.hypothetical_pnl is not None else None,
            }
            for t in paper_raw
        ]

        skip_raw = session.execute(
            select(TargetTrade.skip_reason, func.count(TargetTrade.id))
            .where(TargetTrade.status == "skipped").where(TargetTrade.detected_at >= since)
            .group_by(TargetTrade.skip_reason)
        ).all()
        skip_counts = {r or "unknown": c for r, c in skip_raw}

        # Per-bot skipped counts
        skipped_by_bot_raw = session.execute(
            select(TargetTrade.bot_id, func.count(TargetTrade.id))
            .where(TargetTrade.status == "skipped").where(TargetTrade.detected_at >= since)
            .group_by(TargetTrade.bot_id)
        ).all()
        skipped_by_bot = {bot_names.get(bid, bid): cnt for bid, cnt in skipped_by_bot_raw}

        # Per-bot detected counts
        detected_by_bot_raw = session.execute(
            select(TargetTrade.bot_id, func.count(TargetTrade.id))
            .where(TargetTrade.detected_at >= since)
            .group_by(TargetTrade.bot_id)
        ).all()
        detected_by_bot = {bot_names.get(bid, bid): cnt for bid, cnt in detected_by_bot_raw}

        total_detected = session.execute(
            select(func.count(TargetTrade.id)).where(TargetTrade.detected_at >= since)
        ).scalar_one() or 0

        # Stats derived from status field — accurate for all cases
        resolved = [t for t in paper_trades if t["status"] != "pending"]
        wins = [t for t in resolved if t["status"] == "won"]
        losses = [t for t in resolved if t["status"] == "lost"]
        total_pnl = sum(t["pnl"] for t in resolved if t["pnl"] is not None)
        total_volume = sum(t["value"] for t in paper_trades)
        win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else None

        daily_raw = session.execute(
            select(DailyPnl).where(DailyPnl.date >= since[:10])
            .order_by(DailyPnl.date.desc()).limit(days * len(bots) + 1)
        ).scalars().all()
        daily = [
            {"date": r.date, "bot": bot_names.get(r.bot_id, "?"),
             "trades": r.num_trades, "volume": round(r.total_traded_usd or 0, 2),
             "pnl": round(r.realized_pnl or 0, 2)}
            for r in daily_raw
        ]

        mode_row = session.get(SystemConfig, "trading_mode")
        estop_row = session.get(SystemConfig, "emergency_stop")
        # Read values inside session — avoids DetachedInstanceError after close
        trading_mode = mode_row.value if mode_row else "paper"
        emergency_stop = estop_row.value == "1" if estop_row else False

    return {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "days": days, "trading_mode": trading_mode,
        "emergency_stop": emergency_stop,
        "bots": bots,
        "stats": {
            "total_detected": total_detected, "total_paper": len(paper_trades),
            "total_skipped": sum(skip_counts.values()),
            "total_volume": round(total_volume, 2), "total_pnl": round(total_pnl, 2),
            "resolved_trades": len(resolved), "wins": len(wins), "losses": len(losses),
            "win_rate": win_rate,
            "pending": len(paper_trades) - len(resolved),
        },
        "paper_trades": paper_trades, "skip_reasons": skip_counts, "daily_pnl": daily,
        "skipped_by_bot": skipped_by_bot, "detected_by_bot": detected_by_bot,
    }


# ── Bot management ────────────────────────────────────────────────────────────

def api_add_bot(name: str, wallet: str, our_capital: float, poll_interval: int = 30) -> dict:
    name   = name.strip()
    wallet = wallet.strip()

    if not name:
        return {"error": "Bot name is required."}
    if not wallet.startswith("0x") or len(wallet) != 42:
        return {"error": "Invalid wallet address — must start with 0x and be 42 characters."}
    if our_capital <= 0:
        return {"error": "Starting capital must be greater than $0."}

    with get_session() as session:
        dup_wallet = session.execute(
            select(BotRegistry).where(func.lower(BotRegistry.target_address) == wallet.lower())
        ).scalar_one_or_none()
        if dup_wallet:
            return {"error": f"Wallet already registered as '{dup_wallet.name}'."}

        dup_name = session.execute(
            select(BotRegistry).where(BotRegistry.name == name)
        ).scalar_one_or_none()
        if dup_name:
            return {"error": f"Bot name '{name}' is already taken."}

        from core.models import BotRegistry as BR
        session.add(BR(
            name=name,
            target_address=wallet,
            active=True,
            paper_mode=True,
            poll_interval_sec=poll_interval,
            target_daily_capital=2000.0,   # seed — calibrator updates within minutes
            our_capital=our_capital,
            initial_capital=our_capital,   # locked forever — capital update uses this as base
            total_trades=0,
        ))

    # Trigger bucket + volume calibration immediately in background
    import threading
    def _initial_calibrate(bid):
        try:
            from bots.calibrator import calibrate_buckets, calibrate_bot
            calibrate_buckets(bid)
            calibrate_bot(bid)
        except Exception:
            pass
    with get_session() as session:
        new_bot = session.execute(
            select(BotRegistry).where(func.lower(BotRegistry.target_address) == wallet.lower())
        ).scalar_one_or_none()
        if new_bot:
            threading.Thread(target=_initial_calibrate, args=(new_bot.id,), daemon=True).start()

    return {"success": True, "message": f"Bot '{name}' registered. Calibrating buckets now — will start trading within 30 seconds."}


def api_update_bot(bot_id: str, action: str, new_name: str = "", amount: float = 0.0) -> dict:
    """pause | unpause | deactivate | rename | deposit"""
    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if not bot:
            return {"error": "Bot not found."}
        name = bot.name
        if action == "pause":
            bot.paused = True
        elif action == "unpause":
            bot.paused = False
        elif action == "deactivate":
            bot.active = False
        elif action == "rename":
            new_name = new_name.strip()
            if not new_name:
                return {"error": "New name cannot be empty."}
            dup = session.execute(
                select(BotRegistry).where(BotRegistry.name == new_name).where(BotRegistry.id != bot_id)
            ).scalar_one_or_none()
            if dup:
                return {"error": f"Name '{new_name}' is already taken."}
            bot.name = new_name
            return {"success": True, "message": f"Bot renamed to '{new_name}'."}
        elif action == "reset":
            now = datetime.utcnow()
            old_capital = bot.our_capital or 0
            bot.reset_at   = now
            bot.our_capital = bot.initial_capital or old_capital  # restore starting capital
            from core.models import HealthEvent
            session.add(HealthEvent(
                component=f"reset:{name}",
                event_type="bot_reset",
                details=json.dumps({
                    "bot_name":    name,
                    "reset_at":    now.isoformat(),
                    "capital_was": old_capital,
                    "capital_now": bot.our_capital,
                    "note":        "Bot reset by user. P&L now counts from this point forward.",
                })
            ))
            return {"success": True, "message": f"Bot '{name}' reset. P&L tracking starts fresh from now. Capital restored to ${bot.our_capital:.2f}."}
        elif action == "deposit":
            if amount <= 0:
                return {"error": "Deposit amount must be greater than $0."}
            old_initial  = bot.initial_capital or bot.our_capital or 0.0
            old_capital  = bot.our_capital or 0.0
            bot.initial_capital = round(old_initial + amount, 2)
            bot.our_capital     = round(old_capital + amount, 2)
            # Log deposit so P&L history stays clean
            from core.models import HealthEvent
            session.add(HealthEvent(
                component=f"capital_deposit:{name}",
                event_type="capital_deposit",
                details=json.dumps({
                    "bot_name":      name,
                    "deposit_usd":   amount,
                    "old_initial":   old_initial,
                    "new_initial":   bot.initial_capital,
                    "old_capital":   old_capital,
                    "new_capital":   bot.our_capital,
                    "note":          "Capital added by user. P&L base raised — deposit not counted as profit.",
                    "deposited_at":  datetime.utcnow().isoformat(),
                })
            ))
            return {"success": True, "message": f"Added ${amount:.2f} to '{name}'. New capital: ${bot.our_capital:.2f}. P&L history unchanged."}
        else:
            return {"error": f"Unknown action: {action}"}
    return {"success": True, "message": f"Bot '{name}' {action}d."}


def get_all_bots() -> list:
    with get_session() as session:
        bots = session.execute(select(BotRegistry).order_by(BotRegistry.active.desc())).scalars().all()
        return [
            {
                "id": b.id, "name": b.name, "target": b.target_address,
                "active": b.active, "paused": b.paused, "paper_mode": b.paper_mode,
                "our_capital": b.our_capital or 0,
                "initial_capital": b.initial_capital or b.our_capital or 0,
                "target_daily_capital": b.target_daily_capital or 0,
                "total_trades": b.total_trades or 0,
                "last_activity": b.last_activity_at.strftime("%Y-%m-%d %H:%M UTC") if b.last_activity_at else "Never",
                "buckets": [b.bucket_t1, b.bucket_t2, b.bucket_t3, b.bucket_t4],
                "buckets_ready": all(x is not None for x in [b.bucket_t1, b.bucket_t2, b.bucket_t3, b.bucket_t4]),
                "reset_at": b.reset_at.isoformat() if b.reset_at else None,
            }
            for b in bots
        ]


def get_bot_chart_data(bot_id: str) -> dict:
    """Returns daily P&L time-series for a bot, respecting reset_at."""
    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if not bot:
            return {"error": "Bot not found"}
        name     = bot.name
        reset_at_dt = bot.reset_at
        reset_at    = reset_at_dt.strftime("%Y-%m-%d") if reset_at_dt else None

        # Daily P&L rows
        pnl_query = select(DailyPnl).where(DailyPnl.bot_id == bot_id)
        if reset_at:
            pnl_query = pnl_query.where(DailyPnl.date >= reset_at)
        pnl_rows = {r.date: r for r in session.execute(pnl_query.order_by(DailyPnl.date)).scalars().all()}

        # Resolved trades for win/loss per day
        trade_query = select(PaperTrade).where(
            PaperTrade.bot_id == bot_id,
            PaperTrade.market_resolved == True,
        )
        if reset_at_dt:
            trade_query = trade_query.where(PaperTrade.created_at >= reset_at_dt)
        trades = session.execute(trade_query).scalars().all()

        wins_by_day = {}
        total_by_day = {}
        for t in trades:
            day = t.created_at.strftime("%Y-%m-%d") if t.created_at else None
            if not day:
                continue
            total_by_day[day] = total_by_day.get(day, 0) + 1
            if (t.hypothetical_pnl or 0) > 0:
                wins_by_day[day] = wins_by_day.get(day, 0) + 1

        dates = sorted(set(list(pnl_rows.keys()) + list(total_by_day.keys())))
        daily = []
        cum   = 0.0
        for d in dates:
            row    = pnl_rows.get(d)
            pnl    = round(row.realized_pnl if row else 0, 2)
            cum    = round(cum + pnl, 2)
            t_cnt  = total_by_day.get(d, 0)
            w_cnt  = wins_by_day.get(d, 0)
            daily.append({
                "date":     d,
                "pnl":      pnl,
                "cum_pnl":  cum,
                "trades":   t_cnt,
                "wins":     w_cnt,
                "win_rate": round(w_cnt / t_cnt * 100, 1) if t_cnt else None,
            })

    return {"bot_id": bot_id, "bot_name": name, "reset_at": reset_at, "daily": daily}


# ── HTTP handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if not _check_auth(self):
            _require_auth(self)
            return

        parsed = urlparse(self.path)

        if parsed.path == "/api/data":
            qs = parse_qs(parsed.query)
            days = int(qs.get("days", ["7"])[0])
            self._json(get_dashboard_data(days=days))

        elif parsed.path == "/api/skipped":
            qs = parse_qs(parsed.query)
            days = int(qs.get("days", ["7"])[0])
            self._json(get_skipped_trades(days=days))

        elif parsed.path == "/api/memory":
            try:
                from agents.ceo.memory import read_memory
                mem = read_memory()
                self._json({"memory": mem, "path": "data/ceo_memory.md"})
            except Exception as e:
                self._json({"memory": "", "error": str(e)})

        elif parsed.path == "/api/bots":
            self._json({"bots": get_all_bots()})

        elif parsed.path == "/api/bot_chart":
            qs = parse_qs(parsed.query)
            bot_id = qs.get("bot_id", [""])[0]
            self._json(get_bot_chart_data(bot_id))

        elif parsed.path in ("/", "/index.html"):
            self._html(DASHBOARD_HTML)

        elif parsed.path == "/chat":
            self._html(CHAT_HTML)

        elif parsed.path == "/bots":
            self._html(BOTS_HTML)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not _check_auth(self):
            _require_auth(self)
            return

        parsed = urlparse(self.path)

        if parsed.path == "/api/add_bot":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            self._json(api_add_bot(
                name=body.get("name", ""),
                wallet=body.get("wallet", ""),
                our_capital=float(body.get("our_capital", 0)),
                poll_interval=int(body.get("poll_interval", 30)),
            ))

        elif parsed.path == "/api/update_bot":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            self._json(api_update_bot(
                body.get("bot_id", ""), body.get("action", ""),
                body.get("new_name", ""), float(body.get("amount", 0)),
            ))

        elif parsed.path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            messages = body.get("messages", [])
            session_id = body.get("session_id") or str(uuid.uuid4())

            if not ANTHROPIC_API_KEY:
                self._json({"reply": "⚠️ ANTHROPIC_API_KEY is not set in .env on the server. Add it and restart the dashboard container.", "session_id": session_id})
                return

            try:
                from agents.ceo.agent import chat
                reply, session_id = chat(messages, ANTHROPIC_API_KEY, session_id)
                self._json({"reply": reply, "session_id": session_id})
            except Exception as e:
                self._json({"reply": f"Error: {e}", "session_id": session_id})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyFarm</title>
<style>
  :root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3347;--text:#e2e8f0;--muted:#8892a4;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;--accent:#6366f1;--r:10px;--font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px}
  .container{max-width:1200px;margin:0 auto;padding:16px}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:10px}
  .header h1{font-size:20px;font-weight:700}
  .header h1 span{color:var(--accent)}
  .nav{display:flex;gap:8px}
  .nav a{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600;border:1px solid var(--border);background:var(--surface2);color:var(--text);text-decoration:none}
  .nav a:hover{background:var(--border)}
  .nav a.active{border-color:var(--accent);color:var(--accent);background:rgba(99,102,241,.1)}
  .nav a.green{border-color:var(--green);color:var(--green);background:rgba(34,197,94,.1)}
  .badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:600}
  .badge.paper{background:rgba(99,102,241,.15);color:var(--accent);border:1px solid rgba(99,102,241,.3)}
  .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
  .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px}
  .stat-card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
  .stat-card .value{font-size:22px;font-weight:700;line-height:1}
  .stat-card .value.green{color:var(--green)}.stat-card .value.red{color:var(--red)}.stat-card .value.blue{color:var(--blue)}
  .stat-card .sub{font-size:11px;color:var(--muted);margin-top:4px}
  .section{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);margin-bottom:16px;overflow:hidden}
  .section-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
  .section-title{font-size:13px;font-weight:600}
  .table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{width:100%;border-collapse:collapse;min-width:600px}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:9px 12px;border-bottom:1px solid var(--border);font-size:13px}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:var(--surface2)}
  .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap}
  .pill.buy{background:rgba(34,197,94,.12);color:var(--green)}.pill.sell{background:rgba(239,68,68,.12);color:var(--red)}
  .pill.yes{background:rgba(59,130,246,.12);color:var(--blue)}.pill.no{background:rgba(245,158,11,.12);color:var(--yellow)}
  .pill.pending{background:rgba(99,102,241,.12);color:var(--accent)}.pill.win{background:rgba(34,197,94,.12);color:var(--green)}.pill.loss{background:rgba(239,68,68,.12);color:var(--red)}
  .pnl.pos{color:var(--green);font-weight:600}.pnl.neg{color:var(--red);font-weight:600}
  .bot-row{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s}
  .bot-row:hover{background:var(--surface2)}
  .bot-row.selected{background:rgba(99,102,241,.08);border-left:3px solid var(--accent)}
  .bot-row:last-child{border-bottom:none}
  .bot-indicator{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .bot-indicator.active{background:var(--green);box-shadow:0 0 6px var(--green)}.bot-indicator.paused{background:var(--yellow)}.bot-indicator.inactive{background:var(--muted)}
  .bot-name{font-weight:600;font-size:13px}.bot-addr{font-size:11px;color:var(--muted);font-family:monospace}
  .bot-meta{display:flex;gap:16px;margin-left:auto;text-align:right}
  .bot-meta-item{font-size:11px;color:var(--muted)}.bot-meta-item span{display:block;font-size:13px;color:var(--text);font-weight:500}
  .summary-row{display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--surface2);border-top:2px solid var(--border)}
  .summary-row .bot-name{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  .controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .select{padding:6px 10px;border-radius:6px;font-size:12px;border:1px solid var(--border);background:var(--surface2);color:var(--text);cursor:pointer}
  .footer{text-align:center;color:var(--muted);font-size:11px;padding:16px}
  .refresh-tag{font-size:11px;color:var(--muted)}
  @media(max-width:600px){.stats-grid{grid-template-columns:repeat(2,1fr)}.bot-meta{display:none}td,th{padding:7px 8px;font-size:12px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Poly<span>Farm</span></h1>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <div class="nav"><a href="/" class="active">Dashboard</a><a href="/bots">Manage Bots</a><a href="/chat">CEO Chat</a></div>
      <div id="mode-badge" class="badge paper"><div class="dot"></div> PAPER</div>
      <span id="refresh-tag" class="refresh-tag">—</span>
    </div>
  </div>
  <div class="stats-grid">
    <div class="stat-card"><div class="label">Paper Trades</div><div class="value blue" id="s-paper">—</div><div class="sub" id="s-window">last 7d</div></div>
    <div class="stat-card" id="skipped-card" style="cursor:pointer" onclick="openSkipped()" title="Click to see skipped trade log">
      <div class="label">Skipped ↗</div><div class="value" id="s-skipped">—</div><div class="sub" id="s-detected">of — detected</div>
    </div>
    <div class="stat-card"><div class="label">Hyp. Volume</div><div class="value" id="s-vol">—</div><div class="sub">USD</div></div>
    <div class="stat-card">
      <div class="label">Resolved P&L</div>
      <div class="value" id="s-pnl">—</div>
      <div class="sub" id="s-resolved">— resolved</div>
      <div class="sub" id="s-pnl-pct" style="margin-top:2px;font-size:12px"></div>
    </div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value" id="s-wr">—</div><div class="sub" id="s-wl">— W / — L</div></div>
  </div>

  <!-- Skipped trades modal -->
  <div id="skipped-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;overflow-y:auto">
    <div style="max-width:900px;margin:40px auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:0 0 20px">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border)">
        <div style="font-weight:700;font-size:15px">Skipped Trades Log</div>
        <button onclick="closeSkipped()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;padding:0 4px">✕</button>
      </div>
      <div style="padding:12px 20px 0;font-size:12px;color:var(--muted)" id="skipped-summary"></div>
      <div style="overflow-x:auto;padding:0 8px">
        <table style="width:100%;border-collapse:collapse;min-width:600px;margin-top:8px">
          <thead><tr>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Time</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Bot</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Side</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Out</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Target $</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Scaled $</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Skip Reason</th>
            <th style="font-size:11px;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)">Market</th>
          </tr></thead>
          <tbody id="skipped-tbody"><tr><td colspan="8" style="text-align:center;padding:30px;color:var(--muted)">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
  <div class="section">
    <div class="section-header"><div class="section-title">Bots</div><div id="bots-sub" style="font-size:11px;color:var(--muted)"></div></div>
    <div id="bots-list"><div style="text-align:center;padding:30px;color:var(--muted)">Loading…</div></div>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
    <div class="section-title">Trade Logbook</div>
    <div class="controls">
      <select class="select" id="days-select" onchange="loadData()">
        <option value="1">1 day</option><option value="7" selected>7 days</option>
        <option value="30">30 days</option><option value="90">All time</option>
      </select>
      <select class="select" id="side-filter" onchange="resetAndRender()"><option value="">All sides</option><option value="BUY">BUY</option><option value="SELL">SELL</option></select>
      <select class="select" id="outcome-filter" onchange="resetAndRender()"><option value="">All outcomes</option><option value="YES">YES</option><option value="NO">NO</option></select>
      <select class="select" id="status-filter" onchange="resetAndRender()"><option value="">All statuses</option><option value="won">Won</option><option value="lost">Lost</option><option value="pending">Pending</option></select>
    </div>
  </div>
  <div class="section">
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time (UTC)</th><th>Bot</th><th>Side</th><th>Bet</th><th>Winner</th><th>Target $</th><th>Our $</th><th>Price</th><th>Status</th><th>P&L</th><th>Market</th></tr></thead>
        <tbody id="trades-tbody"><tr><td colspan="11" style="text-align:center;padding:30px;color:var(--muted)">Loading…</td></tr></tbody>
      </table>
    </div>
    <div id="pagination" style="display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-top:1px solid var(--border);font-size:12px;color:var(--muted)">
      <button onclick="changePage(-1)" id="btn-prev" style="padding:5px 12px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:12px;cursor:pointer">← Prev</button>
      <span id="page-info">—</span>
      <button onclick="changePage(1)" id="btn-next" style="padding:5px 12px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:12px;cursor:pointer">Next →</button>
    </div>
  </div>
  <div class="section">
    <div class="section-header"><div class="section-title">Daily Summary</div></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Date</th><th>Bot</th><th>Trades</th><th>Volume $</th><th>Realized P&L</th></tr></thead>
        <tbody id="daily-tbody"><tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted)">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>
  <div class="footer">PolyFarm · auto-refreshes every 30s</div>
</div>
<script>
let allTrades = [];
let filteredTrades = [];
let currentPage = 1;
const PAGE_SIZE = 50;
let allData = null;
let selectedBot = null;

const fmt = (v, d=2) => v == null ? '—' : '$' + Number(v).toFixed(d);
const fmtPct = v => v == null ? '—' : v.toFixed(1) + '%';
const pnlCls = v => v == null ? '' : (v >= 0 ? 'pnl pos' : 'pnl neg');

function statusPill(t) {
  if (t.status === 'won')  return '<span class="pill win">Won</span>';
  if (t.status === 'lost') return '<span class="pill loss">Lost</span>';
  return '<span class="pill pending">Pending</span>';
}

function winnerCell(t) {
  if (!t.winning_outcome) return '<span style="color:var(--muted)">—</span>';
  const cls = t.winning_outcome === 'YES' ? 'yes' : 'no';
  return `<span class="pill ${cls}">${t.winning_outcome}</span>`;
}

function pnlCell(t) {
  if (t.status === 'pending') return '<span style="color:var(--muted)">—</span>';
  if (t.pnl == null)          return '<span style="color:var(--muted)">—</span>';
  return `<span class="${pnlCls(t.pnl)}">${t.pnl >= 0 ? '+' : ''}${fmt(t.pnl)}</span>`;
}

function calcStats(trades) {
  const resolved = trades.filter(t => t.status !== 'pending');
  const wins     = resolved.filter(t => t.status === 'won');
  const losses   = resolved.filter(t => t.status === 'lost');
  const totalPnl = resolved.reduce((s, t) => s + (t.pnl || 0), 0);
  const totalVol = trades.reduce((s, t) => s + (t.value || 0), 0);
  return {
    paper:    trades.length,
    resolved: resolved.length,
    wins:     wins.length,
    losses:   losses.length,
    pending:  trades.length - resolved.length,
    pnl:      totalPnl,
    volume:   totalVol,
    winRate:  resolved.length ? wins.length / resolved.length * 100 : null,
  };
}

function renderKPIs(stats, totalDetected, totalSkipped) {
  document.getElementById('s-paper').textContent = stats.paper;
  document.getElementById('s-skipped').textContent = totalSkipped != null ? totalSkipped : '—';
  document.getElementById('s-detected').textContent = totalDetected != null ? 'of ' + totalDetected + ' detected' : 'filter active';
  document.getElementById('s-vol').textContent = fmt(stats.volume);

  const pe = document.getElementById('s-pnl');
  const pctEl = document.getElementById('s-pnl-pct');
  if (stats.resolved > 0) {
    const sign = stats.pnl >= 0 ? '+' : '';
    pe.textContent = sign + fmt(stats.pnl);
    pe.className = 'value ' + (stats.pnl >= 0 ? 'green' : 'red');
    const pct = stats.volume > 0 ? (stats.pnl / stats.volume * 100) : null;
    if (pct !== null) {
      pctEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '% return on volume';
      pctEl.style.color = pct >= 0 ? 'var(--green)' : 'var(--red)';
    } else { pctEl.textContent = ''; }
  } else {
    pe.textContent = '—'; pe.className = 'value'; pctEl.textContent = '';
  }
  document.getElementById('s-resolved').textContent = (stats.resolved || 0) + ' resolved · ' + (stats.pending || 0) + ' pending';
  document.getElementById('s-wr').textContent = stats.winRate != null ? fmtPct(stats.winRate) : '—';
  document.getElementById('s-wl').textContent = (stats.wins || 0) + ' W / ' + (stats.losses || 0) + ' L';
}

function selectBot(name) {
  selectedBot = (selectedBot === name) ? null : name;
  updateView();
}

function updateView() {
  if (!allData) return;
  const d = allData;

  // Filter trades by selected bot
  const botTrades = selectedBot ? d.paper_trades.filter(t => t.bot === selectedBot) : d.paper_trades;
  const stats = calcStats(botTrades);
  const totalDetected = selectedBot ? (d.detected_by_bot[selectedBot] || 0) : d.stats.total_detected;
  const totalSkipped  = selectedBot ? (d.skipped_by_bot[selectedBot] || 0) : d.stats.total_skipped;

  // Update header label
  document.getElementById('bots-sub').textContent = selectedBot ? `Viewing: ${selectedBot} — click again to deselect` : d.bots.length + ' bot(s) · click to filter';

  renderKPIs(stats, totalDetected, totalSkipped);
  renderBots(d.bots, d.paper_trades);

  // Daily table — filter by bot if selected
  const dailyRows = selectedBot ? d.daily_pnl.filter(r => r.bot === selectedBot) : d.daily_pnl;
  const db = document.getElementById('daily-tbody');
  db.innerHTML = dailyRows.length
    ? dailyRows.map(r => `<tr>
        <td>${r.date}</td><td>${r.bot}</td><td>${r.trades}</td>
        <td>${fmt(r.volume)}</td>
        <td class="${pnlCls(r.pnl)}">${r.pnl >= 0 ? '+' : ''}${fmt(r.pnl)}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:16px">No data yet.</td></tr>';

  allTrades = botTrades;
  resetAndRender();
}

function renderBots(bots, allPaperTrades) {
  const bl = document.getElementById('bots-list');
  if (!bots.length) {
    bl.innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">No bots registered.</div>';
    return;
  }

  // Per-bot stats for summary column
  const botStats = {};
  bots.forEach(b => {
    const bt = allPaperTrades.filter(t => t.bot === b.name);
    botStats[b.name] = calcStats(bt);
  });

  const rows = bots.map(b => {
    const st = !b.active ? 'inactive' : b.paused ? 'paused' : 'active';
    const bs = botStats[b.name];
    const selectedClass = selectedBot === b.name ? ' selected' : '';
    const pnlStr = bs.resolved > 0
      ? `<span style="color:${bs.pnl >= 0 ? 'var(--green)' : 'var(--red)'}; font-weight:600">${bs.pnl >= 0 ? '+' : ''}${fmt(bs.pnl)}</span>`
      : '<span style="color:var(--muted)">—</span>';
    return `<div class="bot-row${selectedClass}" onclick="selectBot('${b.name}')">
      <div class="bot-indicator ${st}"></div>
      <div><div class="bot-name">${b.name}</div><div class="bot-addr">${b.target}</div></div>
      <div class="bot-meta">
        <div class="bot-meta-item"><span>${!b.active ? 'Inactive' : b.paused ? 'Paused' : 'Running'}</span>Status</div>
        <div class="bot-meta-item"><span>${b.paper_mode ? 'Paper' : 'Live'}</span>Mode</div>
        <div class="bot-meta-item"><span>${bs.paper}</span>Trades</div>
        <div class="bot-meta-item"><span>${fmt(bs.volume)}</span>Volume</div>
        <div class="bot-meta-item"><span>${pnlStr}</span>P&amp;L</div>
        <div class="bot-meta-item"><span>${b.last_activity}</span>Last Active</div>
        <div class="bot-meta-item">
          <a href="https://polymarket.com/profile/${b.target}" target="_blank" rel="noopener"
             onclick="event.stopPropagation()"
             style="display:inline-block;padding:3px 10px;border-radius:5px;font-size:11px;font-weight:600;background:rgba(99,102,241,.15);color:var(--accent);border:1px solid rgba(99,102,241,.3);text-decoration:none;white-space:nowrap">
            View ↗
          </a>
        </div>
      </div>
    </div>`;
  }).join('');

  // Summary row across all bots
  const allStats = calcStats(allPaperTrades || []);
  const sumPnlStr = allStats.resolved > 0
    ? `<span style="color:${allStats.pnl >= 0 ? 'var(--green)' : 'var(--red)'};font-weight:700">${allStats.pnl >= 0 ? '+' : ''}${fmt(allStats.pnl)}</span>`
    : '<span style="color:var(--muted)">—</span>';
  const summaryRow = `<div class="summary-row">
    <div style="width:8px;height:8px;flex-shrink:0"></div>
    <div><div class="bot-name">Portfolio Total</div><div class="bot-addr">${bots.length} bot(s) combined</div></div>
    <div class="bot-meta">
      <div class="bot-meta-item"><span style="color:var(--text)">—</span>Status</div>
      <div class="bot-meta-item"><span style="color:var(--text)">—</span>Mode</div>
      <div class="bot-meta-item"><span style="color:var(--text);font-weight:700">${allStats.paper}</span>Trades</div>
      <div class="bot-meta-item"><span style="color:var(--text);font-weight:700">${fmt(allStats.volume)}</span>Volume</div>
      <div class="bot-meta-item"><span>${sumPnlStr}</span>P&amp;L</div>
      <div class="bot-meta-item"><span style="color:var(--text)">${allStats.winRate != null ? fmtPct(allStats.winRate) : '—'}</span>Win Rate</div>
    </div>
  </div>`;

  bl.innerHTML = rows + summaryRow;
}

async function loadData() {
  const days = document.getElementById('days-select').value;
  allData = await fetch('/api/data?days=' + days).then(r => r.json());

  document.getElementById('mode-badge').innerHTML = `<div class="dot"></div> ${allData.trading_mode.toUpperCase()}`;
  document.getElementById('refresh-tag').textContent = 'Updated ' + allData.generated;
  document.getElementById('s-window').textContent = 'last ' + allData.days + 'd';

  updateView();
}

function resetAndRender() {
  currentPage = 1;
  applyFiltersAndRender();
}

function changePage(delta) {
  const totalPages = Math.ceil(filteredTrades.length / PAGE_SIZE);
  currentPage = Math.max(1, Math.min(currentPage + delta, totalPages));
  renderPage();
}

function applyFiltersAndRender() {
  const sf  = document.getElementById('side-filter').value;
  const of2 = document.getElementById('outcome-filter').value;
  const stf = document.getElementById('status-filter').value;
  filteredTrades = allTrades.filter(x =>
    (!sf  || x.side    === sf) &&
    (!of2 || x.outcome === of2) &&
    (!stf || x.status  === stf)
  );
  renderPage();
}

function renderPage() {
  const totalPages = Math.max(1, Math.ceil(filteredTrades.length / PAGE_SIZE));
  const start = (currentPage - 1) * PAGE_SIZE;
  const page  = filteredTrades.slice(start, start + PAGE_SIZE);
  const tb = document.getElementById('trades-tbody');

  tb.innerHTML = page.length
    ? page.map(t => `<tr>
        <td style="white-space:nowrap;color:var(--muted)">${t.time}</td>
        <td style="font-weight:500">${t.bot}</td>
        <td><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td>
        <td><span class="pill ${t.outcome.toLowerCase()}">${t.outcome}</span></td>
        <td>${winnerCell(t)}</td>
        <td style="color:var(--muted)">${t.target_size > 0 ? fmt(t.target_size) : '—'}</td>
        <td style="font-weight:600">${fmt(t.size)}</td>
        <td style="color:var(--muted)">${t.price.toFixed(3)}</td>
        <td>${statusPill(t)}</td>
        <td>${pnlCell(t)}</td>
        <td style="color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.market}">${t.market}</td>
      </tr>`).join('')
    : `<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:20px">No trades match filters.</td></tr>`;

  document.getElementById('page-info').textContent =
    filteredTrades.length
      ? `Page ${currentPage} of ${totalPages} · ${filteredTrades.length} trades`
      : 'No trades';
  document.getElementById('btn-prev').disabled = currentPage <= 1;
  document.getElementById('btn-next').disabled = currentPage >= totalPages;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Skipped trades modal ──────────────────────────────────────────────────────
async function openSkipped() {
  document.getElementById('skipped-modal').style.display = 'block';
  document.body.style.overflow = 'hidden';
  const days = document.getElementById('days-select').value;
  const data = await fetch('/api/skipped?days=' + days).then(r => r.json());
  const trades = data.skipped || [];

  document.getElementById('skipped-summary').textContent =
    trades.length + ' skipped trades in last ' + days + 'd — click a row to copy market ID';

  // Count by reason
  const reasons = {};
  trades.forEach(t => { reasons[t.reason] = (reasons[t.reason] || 0) + 1; });

  const tb = document.getElementById('skipped-tbody');
  if (!trades.length) {
    tb.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:30px;color:var(--muted)">No skipped trades in this window.</td></tr>';
    return;
  }
  tb.innerHTML = trades.map(t => `<tr style="cursor:default">
    <td style="white-space:nowrap;color:var(--muted);padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border)">${t.time}</td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);font-weight:500">${esc(t.bot)}</td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border)"><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border)"><span class="pill ${(t.outcome||'').toLowerCase()}">${t.outcome}</span></td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);font-weight:600">$${t.target_size.toFixed(2)}</td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);color:var(--muted)">$${t.scaled_size.toFixed(2)}</td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);color:var(--yellow)">${esc(t.reason)}</td>
    <td style="padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.market)}">${esc(t.market)}</td>
  </tr>`).join('');
}

function closeSkipped() {
  document.getElementById('skipped-modal').style.display = 'none';
  document.body.style.overflow = '';
}

// Close modal on backdrop click
document.getElementById('skipped-modal').addEventListener('click', function(e) {
  if (e.target === this) closeSkipped();
});

loadData();
setInterval(loadData, 30000);
</script>
</body></html>"""


# ── Chat HTML ─────────────────────────────────────────────────────────────────

CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyFarm — CEO</title>
<style>
  :root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3347;--text:#e2e8f0;--muted:#8892a4;--accent:#6366f1;--green:#22c55e;--r:10px;--font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px}
  .layout{display:flex;flex-direction:column;height:100vh;max-width:800px;margin:0 auto;padding:0 16px}
  .header{display:flex;align-items:center;justify-content:space-between;padding:14px 0;border-bottom:1px solid var(--border);flex-shrink:0}
  .header h1{font-size:18px;font-weight:700}.header h1 span{color:var(--accent)}
  .nav{display:flex;gap:8px}
  .nav a{padding:5px 12px;border-radius:6px;font-size:12px;font-weight:600;border:1px solid var(--border);background:var(--surface2);color:var(--text);text-decoration:none}
  .nav a.active{border-color:var(--accent);color:var(--accent);background:rgba(99,102,241,.1)}
  .messages{flex:1;overflow-y:auto;padding:16px 0;display:flex;flex-direction:column;gap:12px}
  .msg{display:flex;gap:10px;max-width:100%}
  .msg.user{flex-direction:row-reverse}
  .avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}
  .avatar.ceo{background:var(--accent);color:#fff}
  .avatar.user{background:var(--surface2);color:var(--muted);border:1px solid var(--border)}
  .bubble{max-width:75%;padding:10px 14px;border-radius:var(--r);font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
  .msg.ceo .bubble{background:var(--surface);border:1px solid var(--border)}
  .msg.user .bubble{background:var(--accent);color:#fff}
  .typing .bubble{color:var(--muted);font-style:italic}
  .input-row{display:flex;gap:10px;padding:14px 0;border-top:1px solid var(--border);flex-shrink:0}
  textarea{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:var(--font);font-size:14px;padding:10px 14px;resize:none;outline:none;min-height:44px;max-height:120px;line-height:1.5}
  textarea:focus{border-color:var(--accent)}
  button{padding:0 18px;height:44px;border-radius:var(--r);background:var(--accent);color:#fff;border:none;font-size:14px;font-weight:600;cursor:pointer;flex-shrink:0}
  button:hover{background:#5254cc}
  button:disabled{opacity:.4;cursor:not-allowed}
  .suggestions{display:flex;gap:8px;flex-wrap:wrap;padding:8px 0 0}
  .chip{padding:5px 12px;border-radius:20px;font-size:12px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);cursor:pointer;white-space:nowrap}
  .chip:hover{border-color:var(--accent);color:var(--accent)}
  @media(max-width:500px){.bubble{max-width:88%}}
</style>
</head>
<body>
<div class="layout">
  <div class="header">
    <h1>Poly<span>Farm</span> <span style="font-weight:400;color:var(--muted);font-size:14px">CEO</span></h1>
    <div class="nav"><a href="/">Dashboard</a><a href="/bots">Manage Bots</a><a href="/chat" class="active">CEO Chat</a></div>
  </div>

  <div class="messages" id="messages">
    <div class="msg ceo">
      <div class="avatar ceo">C</div>
      <div class="bubble">Hey. I'm your PolyFarm CEO. I have live access to the database — ask me anything about performance, bots, trades, or give me a command.

Try one of the suggestions below, or just type.</div>
    </div>
    <div class="suggestions" id="suggestions">
      <div class="chip" onclick="send(this.textContent)">What's the status?</div>
      <div class="chip" onclick="send(this.textContent)">Show me today's trades</div>
      <div class="chip" onclick="send(this.textContent)">How many trades were skipped and why?</div>
      <div class="chip" onclick="send(this.textContent)">Performance summary this week</div>
    </div>
  </div>

  <div>
    <div class="input-row">
      <textarea id="input" placeholder="Ask the CEO…" rows="1" onkeydown="handleKey(event)"></textarea>
      <button id="send-btn" onclick="sendInput()">Send</button>
    </div>
  </div>
</div>

<script>
const history = [];
// Persist session across page refreshes within the same tab
let sessionId = sessionStorage.getItem('ceo_session_id') || null;

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendInput(); }
}

function sendInput() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  send(text);
}

async function send(text) {
  const msgs = document.getElementById('messages');
  const sugg = document.getElementById('suggestions');
  if (sugg) sugg.remove();

  // Add user bubble
  msgs.innerHTML += `<div class="msg user"><div class="avatar user">U</div><div class="bubble">${esc(text)}</div></div>`;
  history.push({ role: 'user', content: text });
  msgs.scrollTop = msgs.scrollHeight;

  // Typing indicator
  const typingId = 'typing-' + Date.now();
  msgs.innerHTML += `<div class="msg ceo typing" id="${typingId}"><div class="avatar ceo">C</div><div class="bubble">Thinking…</div></div>`;
  msgs.scrollTop = msgs.scrollHeight;

  document.getElementById('send-btn').disabled = true;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: history, session_id: sessionId }),
    });
    const data = await res.json();
    const reply = data.reply || '(no response)';

    // Persist session_id returned by server
    if (data.session_id) {
      sessionId = data.session_id;
      sessionStorage.setItem('ceo_session_id', sessionId);
    }

    document.getElementById(typingId)?.remove();
    msgs.innerHTML += `<div class="msg ceo"><div class="avatar ceo">C</div><div class="bubble">${esc(reply)}</div></div>`;
    history.push({ role: 'assistant', content: reply });
  } catch (e) {
    document.getElementById(typingId)?.remove();
    msgs.innerHTML += `<div class="msg ceo"><div class="avatar ceo">C</div><div class="bubble" style="color:#ef4444">Error: ${e.message}</div></div>`;
  }

  document.getElementById('send-btn').disabled = false;
  msgs.scrollTop = msgs.scrollHeight;
  document.getElementById('input').focus();
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}
</script>
</body>
</html>"""


# ── Bot management HTML ───────────────────────────────────────────────────────

BOTS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyFarm — Manage Bots</title>
<style>
  :root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3347;--text:#e2e8f0;--muted:#8892a4;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;--accent:#6366f1;--r:10px;--font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px}
  .container{max-width:960px;margin:0 auto;padding:16px}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:10px}
  .header h1{font-size:20px;font-weight:700}.header h1 span{color:var(--accent)}
  .nav{display:flex;gap:8px}
  .nav a{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600;border:1px solid var(--border);background:var(--surface2);color:var(--text);text-decoration:none}
  .nav a:hover{background:var(--border)}
  .nav a.active{border-color:var(--accent);color:var(--accent);background:rgba(99,102,241,.1)}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:24px;margin-bottom:20px}
  .card-title{font-size:15px;font-weight:700;margin-bottom:20px;display:flex;align-items:center;gap:8px}
  .form-grid{display:grid;grid-template-columns:1fr 2fr 1fr 1fr;gap:12px;align-items:end}
  @media(max-width:700px){.form-grid{grid-template-columns:1fr}}
  .field label{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;font-weight:600}
  .field input{width:100%;padding:9px 12px;border-radius:7px;border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:14px;outline:none;transition:border .15s}
  .field input:focus{border-color:var(--accent)}
  .field input::placeholder{color:var(--muted)}
  .btn{padding:9px 20px;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:opacity .15s}
  .btn:hover{opacity:.85}.btn:disabled{opacity:.4;cursor:not-allowed}
  .btn-primary{background:var(--accent);color:#fff}
  .btn-sm{padding:4px 12px;font-size:11px;font-weight:600;border-radius:5px;cursor:pointer;border:1px solid;transition:opacity .15s}
  .btn-sm:hover{opacity:.75}
  .btn-pause{background:rgba(245,158,11,.12);color:var(--yellow);border-color:rgba(245,158,11,.3)}
  .btn-resume{background:rgba(34,197,94,.12);color:var(--green);border-color:rgba(34,197,94,.3)}
  .btn-deactivate{background:rgba(239,68,68,.08);color:var(--red);border-color:rgba(239,68,68,.25)}
  .alert{padding:12px 16px;border-radius:7px;font-size:13px;margin-bottom:16px;display:none}
  .alert.success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:var(--green)}
  .alert.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:var(--red)}
  table{width:100%;border-collapse:collapse}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:8px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:var(--surface2)}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
  .dot.active{background:var(--green);box-shadow:0 0 5px var(--green)}.dot.paused{background:var(--yellow)}.dot.inactive{background:var(--muted)}
  .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
  .pill.paper{background:rgba(99,102,241,.12);color:var(--accent)}
  .pill.live{background:rgba(34,197,94,.12);color:var(--green)}
  .mono{font-family:monospace;font-size:12px;color:var(--muted)}
  .actions{display:flex;gap:6px;flex-wrap:wrap}
  .hint{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.5}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Poly<span>Farm</span> — Manage Bots</h1>
    <div class="nav">
      <a href="/">Dashboard</a>
      <a href="/bots" class="active">Manage Bots</a>
      <a href="/chat">CEO Chat</a>
    </div>
  </div>

  <!-- Add Bot Form -->
  <div class="card">
    <div class="card-title">➕ Add New Bot</div>
    <div id="form-alert" class="alert"></div>
    <div class="form-grid">
      <div class="field">
        <label>Bot Name</label>
        <input id="f-name" type="text" placeholder="e.g. Alpha" maxlength="40">
      </div>
      <div class="field">
        <label>Target Wallet Address</label>
        <input id="f-wallet" type="text" placeholder="0x... (paste from Polymarket profile URL)">
      </div>
      <div class="field">
        <label>Our Capital ($)</label>
        <input id="f-capital" type="number" placeholder="100" min="1" step="1" value="100">
      </div>
      <div class="field">
        <button class="btn btn-primary" id="add-btn" onclick="addBot()">Add Bot</button>
      </div>
    </div>
    <div class="hint">
      📍 Find the wallet address in the Polymarket profile URL: <code>polymarket.com/profile/<strong>0x…</strong></code><br>
      🤖 Bot starts in paper mode automatically. The weekly calibrator will measure the wallet's volume and tune the scaling ratio within minutes.<br>
      💡 Scaling ratio = Your Capital ÷ Target's Daily Volume — e.g. $100 capital vs $1,000/day target = 10% of every trade copied.
    </div>
  </div>

  <!-- Chart modal -->
  <div id="chart-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;overflow-y:auto">
    <div style="max-width:780px;margin:40px auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border)">
        <div id="chart-title" style="font-weight:700;font-size:15px"></div>
        <button onclick="closeChart()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;padding:0 4px">✕</button>
      </div>
      <div id="chart-body"></div>
    </div>
  </div>

  <!-- Registered Bots -->
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <div class="card-title" style="margin:0">Registered Bots</div>
      <div id="bots-sub" style="font-size:11px;color:var(--muted)">Loading…</div>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>St.</th><th>Name</th><th>Wallet</th>
          <th>Capital</th><th>Target Daily Vol</th>
          <th>Sizing Buckets</th>
          <th>Trades</th><th>Actions</th>
        </tr></thead>
        <tbody id="bots-tbody"><tr><td colspan="10" style="text-align:center;padding:30px;color:var(--muted)">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<script>
async function loadBots() {
  const d = await fetch('/api/bots').then(r => r.json());
  const bots = d.bots || [];
  document.getElementById('bots-sub').textContent = bots.length + ' bot(s) registered';

  const tb = document.getElementById('bots-tbody');
  if (!bots.length) {
    tb.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:30px;color:var(--muted)">No bots registered yet.</td></tr>';
    return;
  }

  tb.innerHTML = bots.map(b => {
    const statusLabel = !b.active ? 'Inactive' : b.paused ? 'Paused' : '';
    const dotCls      = !b.active ? 'inactive' : b.paused ? 'paused' : 'active';
    const shortWallet = b.target.slice(0, 6) + '…' + b.target.slice(-6);
    const ratio       = b.ratio_pct.toFixed(1) + '%';
    const actions = b.active
      ? (b.paused
          ? `<button class="btn-sm btn-resume"  onclick="updateBot('${b.id}','unpause')">Resume</button>`
          : `<button class="btn-sm btn-pause"   onclick="updateBot('${b.id}','pause')">Pause</button>`)
        + `<button class="btn-sm" style="background:rgba(99,102,241,.12);color:var(--accent);border:1px solid rgba(99,102,241,.3)" onclick="renameBot('${b.id}','${b.name}')">Rename</button>`
        + `<button class="btn-sm" style="background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.3)" onclick="depositCapital('${b.id}','${b.name}',${b.our_capital})">+ Capital</button>`
        + `<button class="btn-sm" style="background:rgba(239,68,68,.08);color:var(--red);border:1px solid rgba(239,68,68,.25)" onclick="resetBot('${b.id}','${b.name}')">Reset</button>`
        + `<button class="btn-sm btn-deactivate" onclick="confirmDeactivate('${b.id}','${b.name}')">Deactivate</button>`
        + `<a href="https://polymarket.com/profile/${b.target}" target="_blank" rel="noopener"
              style="display:inline-flex;align-items:center;padding:4px 10px;border-radius:5px;font-size:11px;font-weight:600;background:rgba(99,102,241,.12);color:var(--accent);border:1px solid rgba(99,102,241,.3);text-decoration:none">View ↗</a>`
      : '<span style="color:var(--muted);font-size:11px">Deactivated</span>';
    const graphBtn = `<button class="btn-sm" style="background:rgba(59,130,246,.12);color:var(--blue);border:1px solid rgba(59,130,246,.3)" onclick="openChart('${b.id}','${b.name}')">Graph</button>`;

    const bucketsCell = b.buckets_ready
      ? `<div style="font-size:11px;line-height:1.7;font-family:monospace">
           <div><span style="color:var(--muted)">1%</span> <span style="color:var(--text)">$0 – $${b.buckets[0]}</span></div>
           <div><span style="color:var(--muted)">2%</span> <span style="color:var(--text)">$${b.buckets[0]} – $${b.buckets[1]}</span></div>
           <div><span style="color:var(--muted)">3%</span> <span style="color:var(--text)">$${b.buckets[1]} – $${b.buckets[2]}</span></div>
           <div><span style="color:var(--muted)">4%</span> <span style="color:var(--text)">$${b.buckets[2]} – $${b.buckets[3]}</span></div>
           <div><span style="color:var(--muted)">5%</span> <span style="color:var(--text)">$${b.buckets[3]}+</span></div>
         </div>`
      : `<span style="font-size:11px;color:var(--yellow)">Calibrating…</span>`;

    return `<tr>
      <td style="text-align:center" title="${!b.active ? 'Inactive' : b.paused ? 'Paused' : 'Running'}">
        <span class="dot ${dotCls}"></span>${statusLabel ? `<span style="font-size:11px;color:var(--muted)">${statusLabel}</span>` : ''}
      </td>
      <td style="font-weight:600">${b.name}</td>
      <td class="mono" title="${b.target}">${shortWallet}</td>
      <td>$${b.our_capital.toFixed(0)}</td>
      <td style="color:var(--muted)">$${b.target_daily_capital.toFixed(0)}/day</td>
      <td style="font-weight:600;color:var(--blue)">${ratio}</td>
      <td>${bucketsCell}</td>
      <td>${b.total_trades}</td>
      <td><div class="actions">${actions}${graphBtn}</div></td>
    </tr>`;
  }).join('');
}

function showAlert(msg, type) {
  const el = document.getElementById('form-alert');
  el.textContent = msg;
  el.className = 'alert ' + type;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 6000);
}

async function addBot() {
  const name    = document.getElementById('f-name').value.trim();
  const wallet  = document.getElementById('f-wallet').value.trim();
  const capital = parseFloat(document.getElementById('f-capital').value);
  const btn     = document.getElementById('add-btn');

  if (!name || !wallet || !capital) { showAlert('Please fill in all fields.', 'error'); return; }

  btn.disabled = true;
  btn.textContent = 'Adding…';

  const res = await fetch('/api/add_bot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, wallet, our_capital: capital }),
  }).then(r => r.json());

  btn.disabled = false;
  btn.textContent = 'Add Bot';

  if (res.error) {
    showAlert('❌ ' + res.error, 'error');
  } else {
    showAlert('✅ ' + res.message, 'success');
    document.getElementById('f-name').value   = '';
    document.getElementById('f-wallet').value = '';
    document.getElementById('f-capital').value = '100';
    loadBots();
  }
}

async function updateBot(botId, action) {
  const res = await fetch('/api/update_bot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_id: botId, action }),
  }).then(r => r.json());
  if (res.error) alert(res.error);
  else loadBots();
}

function confirmDeactivate(botId, name) {
  if (confirm('Deactivate "' + name + '"? It will stop tracking but all trade history is kept.')) {
    updateBot(botId, 'deactivate');
  }
}

async function depositCapital(botId, name, currentCapital) {
  const input = prompt(`Add capital to "${name}" (current: $${currentCapital.toFixed(2)})\n\nEnter amount to add ($):`);
  if (!input) return;
  const amount = parseFloat(input);
  if (isNaN(amount) || amount <= 0) { alert('Enter a valid amount greater than $0.'); return; }
  const res = await fetch('/api/update_bot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_id: botId, action: 'deposit', amount }),
  }).then(r => r.json());
  if (res.error) alert(res.error);
  else { alert(res.message); loadBots(); }
}

async function renameBot(botId, currentName) {
  const newName = prompt('Rename "' + currentName + '" to:', currentName);
  if (!newName || newName.trim() === currentName) return;
  const res = await fetch('/api/update_bot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_id: botId, action: 'rename', new_name: newName.trim() }),
  }).then(r => r.json());
  if (res.error) alert(res.error);
  else loadBots();
}

async function resetBot(botId, name) {
  if (!confirm(`Reset "${name}"?\n\nThis clears the P&L start point and restores original capital.\nAll historical trade data is kept — just excluded from current P&L view.`)) return;
  const res = await fetch('/api/update_bot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_id: botId, action: 'reset' }),
  }).then(r => r.json());
  if (res.error) alert(res.error);
  else { alert(res.message); loadBots(); }
}

// ── Chart modal ───────────────────────────────────────────────────────────────
async function openChart(botId, name) {
  document.getElementById('chart-modal').style.display = 'block';
  document.getElementById('chart-title').textContent = name + ' — P&L Chart';
  document.getElementById('chart-body').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading…</div>';
  const d = await fetch('/api/bot_chart?bot_id=' + botId).then(r => r.json());
  renderChart(d);
}

function closeChart() {
  document.getElementById('chart-modal').style.display = 'none';
}

function renderChart(d) {
  const body = document.getElementById('chart-body');
  if (!d.daily || !d.daily.length) {
    body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">No resolved trades yet.</div>';
    return;
  }

  const days      = d.daily;
  const maxAbs    = Math.max(...days.map(x => Math.abs(x.cum_pnl)), 0.01);
  const totalPnl  = days[days.length - 1].cum_pnl;
  const allTrades = days.reduce((s, x) => s + x.trades, 0);
  const allWins   = days.reduce((s, x) => s + x.wins, 0);
  const winRate   = allTrades ? (allWins / allTrades * 100).toFixed(1) : '—';
  const pnlColor  = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
  const sign      = totalPnl >= 0 ? '+' : '';
  const resetNote = d.reset_at ? `<span style="color:var(--muted);font-size:11px">Since reset ${d.reset_at}</span>` : '';

  // SVG line chart
  const W = 700, H = 200, pad = { t: 16, r: 16, b: 28, l: 52 };
  const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
  const n  = days.length;

  const xScale = i => pad.l + (i / Math.max(n - 1, 1)) * cw;
  const yScale = v => pad.t + ch / 2 - (v / maxAbs) * (ch / 2);

  // Build path
  const pts = days.map((x, i) => `${xScale(i).toFixed(1)},${yScale(x.cum_pnl).toFixed(1)}`);
  const linePath = 'M' + pts.join('L');
  const fillPath = linePath + `L${xScale(n-1).toFixed(1)},${(pad.t+ch).toFixed(1)}L${xScale(0).toFixed(1)},${(pad.t+ch).toFixed(1)}Z`;

  // Y-axis labels
  const yLabels = [-maxAbs, -maxAbs/2, 0, maxAbs/2, maxAbs].map(v => ({
    y: yScale(v), label: (v >= 0 ? '+' : '') + v.toFixed(1)
  }));

  // X-axis labels (first, middle, last)
  const xLabels = [0, Math.floor(n/2), n-1].filter((v,i,a)=>a.indexOf(v)===i).map(i => ({
    x: xScale(i), label: days[i].date
  }));

  // Bar chart for daily P&L (small bars below line chart)
  const BH = 60, bpad = { t: 8, b: 20 };
  const bch = BH - bpad.t - bpad.b;
  const maxBar = Math.max(...days.map(x => Math.abs(x.pnl)), 0.01);
  const bars = days.map((x, i) => {
    const bh = Math.max((Math.abs(x.pnl) / maxBar) * bch, 1);
    const bx = xScale(i) - (n > 1 ? cw / (n-1) / 2 : 20);
    const bw = Math.max(n > 1 ? cw / (n-1) * 0.6 : 40, 2);
    const by = x.pnl >= 0 ? bpad.t + bch - bh : bpad.t + bch;
    return `<rect x="${bx.toFixed(1)}" y="${(BH + by).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}"
              fill="${x.pnl >= 0 ? 'rgba(34,197,94,.5)' : 'rgba(239,68,68,.5)'}"/>`;
  }).join('');

  // Tooltip dots
  const dots = days.map((x, i) =>
    `<circle cx="${xScale(i).toFixed(1)}" cy="${yScale(x.cum_pnl).toFixed(1)}" r="3"
       fill="${x.cum_pnl >= 0 ? 'var(--green)' : 'var(--red)'}"
       style="cursor:pointer">
       <title>${x.date}&#10;Daily: ${x.pnl >= 0?'+':''}$${x.pnl}&#10;Cumulative: ${x.cum_pnl >= 0?'+':''}$${x.cum_pnl}&#10;Trades: ${x.trades} (${x.wins} W)</title>
     </circle>`
  ).join('');

  const totalH = H + BH + 16;
  const svg = `
  <svg viewBox="0 0 ${W} ${totalH}" style="width:100%;max-width:${W}px;display:block;margin:0 auto">
    <defs>
      <linearGradient id="fill-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${totalPnl>=0?'#22c55e':'#ef4444'}" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="${totalPnl>=0?'#22c55e':'#ef4444'}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <!-- grid lines -->
    ${yLabels.map(l=>`
      <line x1="${pad.l}" y1="${l.y.toFixed(1)}" x2="${W-pad.r}" y2="${l.y.toFixed(1)}"
            stroke="var(--border)" stroke-width="1" ${l.label==='0.0'?'':'stroke-dasharray="3,3"'}/>
      <text x="${(pad.l-6).toFixed(1)}" y="${(l.y+4).toFixed(1)}" text-anchor="end"
            fill="var(--muted)" font-size="10">${l.label==='0.0'?'$0':l.label}</text>`).join('')}
    <!-- fill area -->
    <path d="${fillPath}" fill="url(#fill-grad)"/>
    <!-- line -->
    <path d="${linePath}" fill="none" stroke="${totalPnl>=0?'var(--green)':'var(--red)'}" stroke-width="2" stroke-linejoin="round"/>
    <!-- dots -->
    ${dots}
    <!-- x labels -->
    ${xLabels.map(l=>`<text x="${l.x.toFixed(1)}" y="${(H-4).toFixed(1)}" text-anchor="middle" fill="var(--muted)" font-size="10">${l.label}</text>`).join('')}
    <!-- daily bars -->
    <text x="${pad.l}" y="${(H+6).toFixed(1)}" fill="var(--muted)" font-size="10">Daily P&amp;L</text>
    ${bars}
  </svg>`;

  body.innerHTML = `
    <div style="display:flex;gap:24px;padding:16px 20px 4px;flex-wrap:wrap">
      <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Cumulative P&L</div>
           <div style="font-size:22px;font-weight:700;color:${pnlColor}">${sign}$${Math.abs(totalPnl).toFixed(2)}</div></div>
      <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Win Rate</div>
           <div style="font-size:22px;font-weight:700">${winRate}${winRate!=='—'?'%':''}</div></div>
      <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Resolved Trades</div>
           <div style="font-size:22px;font-weight:700">${allTrades}</div></div>
      <div style="margin-left:auto;display:flex;align-items:flex-end">${resetNote}</div>
    </div>
    <div style="padding:8px 12px">${svg}</div>
    <div style="padding:4px 20px 8px;font-size:11px;color:var(--muted)">Hover dots for daily details</div>`;
}

document.getElementById('chart-modal').addEventListener('click', function(e) {
  if (e.target === this) closeChart();
});

// Allow Enter key to submit form
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement.closest && document.activeElement.closest('.form-grid')) {
    addBot();
  }
});

loadBots();
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
        print("ERROR: DASHBOARD_USER and DASHBOARD_PASSWORD must be set in .env")
        print("       Dashboard will not start without credentials configured.")
        sys.exit(1)
    init_db()
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"PolyFarm Dashboard running at http://0.0.0.0:{PORT}")
    print(f"  Dashboard: http://0.0.0.0:{PORT}/")
    print(f"  CEO Chat:  http://0.0.0.0:{PORT}/chat")
    print(f"  Auth:      {DASHBOARD_USER} / {'*' * len(DASHBOARD_PASSWORD)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
