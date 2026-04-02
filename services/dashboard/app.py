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
            }
            for b in bots_raw
        ]
        bot_names = {b.id: b.name for b in bots_raw}

        # Fetch all trades in window — frontend handles pagination
        paper_raw = session.execute(
            select(PaperTrade).where(PaperTrade.created_at >= since)
            .order_by(desc(PaperTrade.created_at))
        ).scalars().all()

        paper_trades = [
            {
                "time": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "—",
                "bot": bot_names.get(t.bot_id, t.bot_id[:8]),
                "side": t.side,
                "outcome": t.outcome,                          # what we bet (YES/NO)
                "winning_outcome": t.winning_outcome or "",   # what actually won
                "status": _trade_status(t),                   # won/lost/pending
                "size": round(t.hypothetical_size or 0, 2),
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

    return {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "days": days, "trading_mode": mode_row.value if mode_row else "paper",
        "emergency_stop": estop_row.value == "1" if estop_row else False,
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
    }


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

        elif parsed.path == "/api/memory":
            try:
                from agents.ceo.memory import read_memory
                mem = read_memory()
                self._json({"memory": mem, "path": "data/ceo_memory.md"})
            except Exception as e:
                self._json({"memory": "", "error": str(e)})

        elif parsed.path in ("/", "/index.html"):
            self._html(DASHBOARD_HTML)

        elif parsed.path == "/chat":
            self._html(CHAT_HTML)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not _check_auth(self):
            _require_auth(self)
            return

        parsed = urlparse(self.path)

        if parsed.path == "/api/chat":
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
  .bot-row{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border)}
  .bot-row:last-child{border-bottom:none}
  .bot-indicator{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .bot-indicator.active{background:var(--green);box-shadow:0 0 6px var(--green)}.bot-indicator.paused{background:var(--yellow)}.bot-indicator.inactive{background:var(--muted)}
  .bot-name{font-weight:600;font-size:13px}.bot-addr{font-size:11px;color:var(--muted);font-family:monospace}
  .bot-meta{display:flex;gap:16px;margin-left:auto;text-align:right}
  .bot-meta-item{font-size:11px;color:var(--muted)}.bot-meta-item span{display:block;font-size:13px;color:var(--text);font-weight:500}
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
      <div class="nav"><a href="/" class="active">Dashboard</a><a href="/chat">CEO Chat</a></div>
      <div id="mode-badge" class="badge paper"><div class="dot"></div> PAPER</div>
      <span id="refresh-tag" class="refresh-tag">—</span>
    </div>
  </div>
  <div class="stats-grid">
    <div class="stat-card"><div class="label">Paper Trades</div><div class="value blue" id="s-paper">—</div><div class="sub" id="s-window">last 7d</div></div>
    <div class="stat-card"><div class="label">Skipped</div><div class="value" id="s-skipped">—</div><div class="sub" id="s-detected">of — detected</div></div>
    <div class="stat-card"><div class="label">Hyp. Volume</div><div class="value" id="s-vol">—</div><div class="sub">USD</div></div>
    <div class="stat-card"><div class="label">Resolved P&L</div><div class="value" id="s-pnl">—</div><div class="sub" id="s-resolved">— resolved</div></div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value" id="s-wr">—</div><div class="sub" id="s-wl">— W / — L</div></div>
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
        <thead><tr><th>Time (UTC)</th><th>Bot</th><th>Side</th><th>Bet</th><th>Winner</th><th>Size $</th><th>Price</th><th>Status</th><th>P&L</th><th>Market</th></tr></thead>
        <tbody id="trades-tbody"><tr><td colspan="10" style="text-align:center;padding:30px;color:var(--muted)">Loading…</td></tr></tbody>
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

const fmt = (v, d=2) => v == null ? '—' : '$' + Number(v).toFixed(d);
const fmtPct = v => v == null ? '—' : v.toFixed(1) + '%';
const pnlCls = v => v == null ? '' : (v >= 0 ? 'pnl pos' : 'pnl neg');

function statusPill(t) {
  // Status comes from Polymarket resolution data (winning_outcome vs outcome)
  if (t.status === 'won')     return '<span class="pill win">Won</span>';
  if (t.status === 'lost')    return '<span class="pill loss">Lost</span>';
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

async function loadData() {
  const days = document.getElementById('days-select').value;
  const d = await fetch('/api/data?days=' + days).then(r => r.json());

  document.getElementById('mode-badge').innerHTML = `<div class="dot"></div> ${d.trading_mode.toUpperCase()}`;
  document.getElementById('refresh-tag').textContent = 'Updated ' + d.generated;

  const s = d.stats;
  document.getElementById('s-paper').textContent = s.total_paper;
  document.getElementById('s-window').textContent = 'last ' + d.days + 'd';
  document.getElementById('s-skipped').textContent = s.total_skipped;
  document.getElementById('s-detected').textContent = 'of ' + s.total_detected + ' detected';
  document.getElementById('s-vol').textContent = fmt(s.total_volume);

  const pe = document.getElementById('s-pnl');
  pe.textContent = s.resolved_trades > 0 ? (s.total_pnl >= 0 ? '+' : '') + fmt(s.total_pnl) : '—';
  pe.className = 'value ' + (s.total_pnl >= 0 ? 'green' : 'red');
  document.getElementById('s-resolved').textContent = (s.resolved_trades || 0) + ' resolved · ' + (s.pending || 0) + ' pending';
  document.getElementById('s-wr').textContent = s.win_rate != null ? fmtPct(s.win_rate) : '—';
  document.getElementById('s-wl').textContent = (s.wins || 0) + ' W / ' + (s.losses || 0) + ' L';

  const bl = document.getElementById('bots-list');
  bl.innerHTML = d.bots.length ? d.bots.map(b => {
    const st = !b.active ? 'inactive' : b.paused ? 'paused' : 'active';
    return `<div class="bot-row">
      <div class="bot-indicator ${st}"></div>
      <div><div class="bot-name">${b.name}</div><div class="bot-addr">${b.target}</div></div>
      <div class="bot-meta">
        <div class="bot-meta-item"><span>${!b.active ? 'Inactive' : b.paused ? 'Paused' : 'Running'}</span>Status</div>
        <div class="bot-meta-item"><span>${b.paper_mode ? 'Paper' : 'Live'}</span>Mode</div>
        <div class="bot-meta-item"><span>${b.total_trades}</span>Trades</div>
        <div class="bot-meta-item"><span>${b.last_activity}</span>Last Active</div>
      </div>
    </div>`;
  }).join('') : '<div style="text-align:center;padding:30px;color:var(--muted)">No bots registered.</div>';

  allTrades = d.paper_trades;
  resetAndRender();

  const db = document.getElementById('daily-tbody');
  db.innerHTML = d.daily_pnl.length
    ? d.daily_pnl.map(r => `<tr>
        <td>${r.date}</td><td>${r.bot}</td><td>${r.trades}</td>
        <td>${fmt(r.volume)}</td>
        <td class="${pnlCls(r.pnl)}">${r.pnl >= 0 ? '+' : ''}${fmt(r.pnl)}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:16px">No data yet.</td></tr>';
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
        <td style="font-weight:600">${fmt(t.size)}</td>
        <td style="color:var(--muted)">${t.price.toFixed(3)}</td>
        <td>${statusPill(t)}</td>
        <td>${pnlCell(t)}</td>
        <td style="color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.market}">${t.market}</td>
      </tr>`).join('')
    : `<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:20px">No trades match filters.</td></tr>`;

  // Pagination controls
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
    <div class="nav"><a href="/">Dashboard</a><a href="/chat" class="active">CEO Chat</a></div>
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
