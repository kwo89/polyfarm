"""
PolyFarm Dashboard — lightweight web server.
Serves a mobile-friendly dashboard at http://SERVER_IP:8080

No auth for now (paper mode only). Add basic auth when going live.
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from sqlalchemy import select, func, desc
from core.database import init_db, get_session
from core.models import BotRegistry, PaperTrade, TargetTrade, DailyPnl, SystemConfig


PORT = int(os.environ.get("DASHBOARD_PORT", 8080))


# ── Data helpers ──────────────────────────────────────────────────────────────

def get_dashboard_data(days: int = 7) -> dict:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    with get_session() as session:
        # Bots
        bots_raw = session.execute(select(BotRegistry)).scalars().all()
        bots = [
            {
                "id": b.id,
                "name": b.name,
                "target": b.target_address,
                "target_short": b.target_address[:6] + "..." + b.target_address[-4:],
                "active": b.active,
                "paused": b.paused,
                "paper_mode": b.paper_mode,
                "total_trades": b.total_trades or 0,
                "last_activity": b.last_activity_at.strftime("%Y-%m-%d %H:%M UTC") if b.last_activity_at else "Never",
                "capital": b.target_daily_capital or 0,
            }
            for b in bots_raw
        ]
        bot_ids = {b.id for b in bots_raw}
        bot_names = {b.id: b.name for b in bots_raw}

        # Paper trades
        paper_raw = session.execute(
            select(PaperTrade)
            .where(PaperTrade.created_at >= since)
            .order_by(desc(PaperTrade.created_at))
            .limit(500)
        ).scalars().all()

        paper_trades = [
            {
                "time": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "—",
                "bot": bot_names.get(t.bot_id, t.bot_id[:8]),
                "side": t.side,
                "outcome": t.outcome,
                "size": round(t.hypothetical_size or 0, 2),
                "price": round(t.hypothetical_price or 0, 3),
                "value": round(t.hypothetical_value or 0, 2),
                "market": (t.question or t.market_id or "")[:60],
                "resolved": t.market_resolved,
                "pnl": round(t.hypothetical_pnl or 0, 2) if t.market_resolved else None,
                "winning": t.winning_outcome,
            }
            for t in paper_raw
        ]

        # Target trades (for skip stats)
        skip_counts = {}
        skip_raw = session.execute(
            select(TargetTrade.skip_reason, func.count(TargetTrade.id))
            .where(TargetTrade.status == "skipped")
            .where(TargetTrade.detected_at >= since)
            .group_by(TargetTrade.skip_reason)
        ).all()
        for reason, count in skip_raw:
            skip_counts[reason or "unknown"] = count

        total_detected = session.execute(
            select(func.count(TargetTrade.id))
            .where(TargetTrade.detected_at >= since)
        ).scalar_one() or 0

        total_paper = len(paper_trades)
        total_skipped = sum(skip_counts.values())

        # Stats
        resolved = [t for t in paper_trades if t["resolved"] and t["pnl"] is not None]
        wins = [t for t in resolved if t["pnl"] > 0]
        losses = [t for t in resolved if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in resolved)
        total_volume = sum(t["value"] for t in paper_trades)
        win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else None

        # Daily P&L
        daily_raw = session.execute(
            select(DailyPnl)
            .where(DailyPnl.date >= since[:10])
            .order_by(DailyPnl.date.desc())
            .limit(days * len(bots) + 1)
        ).scalars().all()

        daily = [
            {
                "date": r.date,
                "bot": bot_names.get(r.bot_id, r.bot_id[:8]),
                "trades": r.num_trades,
                "volume": round(r.total_traded_usd or 0, 2),
                "pnl": round(r.realized_pnl or 0, 2),
            }
            for r in daily_raw
        ]

        # System config
        mode_row = session.get(SystemConfig, "trading_mode")
        estop_row = session.get(SystemConfig, "emergency_stop")
        trading_mode = mode_row.value if mode_row else "paper"
        emergency_stop = estop_row.value == "1" if estop_row else False

    return {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "days": days,
        "trading_mode": trading_mode,
        "emergency_stop": emergency_stop,
        "bots": bots,
        "stats": {
            "total_detected": total_detected,
            "total_paper": total_paper,
            "total_skipped": total_skipped,
            "total_volume": round(total_volume, 2),
            "total_pnl": round(total_pnl, 2),
            "resolved_trades": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
        },
        "paper_trades": paper_trades,
        "skip_reasons": skip_counts,
        "daily_pnl": daily,
    }


# ── HTTP handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default access logs

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/data":
            qs = parse_qs(parsed.query)
            days = int(qs.get("days", ["7"])[0])
            data = get_dashboard_data(days=days)
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path in ("/", "/index.html"):
            html = get_dashboard_html()
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


# ── HTML dashboard ────────────────────────────────────────────────────────────

def get_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>PolyFarm Dashboard</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3347;
    --text: #e2e8f0;
    --muted: #8892a4;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #f59e0b;
    --blue: #3b82f6;
    --accent: #6366f1;
    --radius: 10px;
    --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); font-size: 14px; min-height: 100vh; }

  /* Layout */
  .container { max-width: 1200px; margin: 0 auto; padding: 16px; }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
  .header h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
  .header h1 span { color: var(--accent); }
  .badge { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .badge.paper { background: rgba(99,102,241,0.15); color: var(--accent); border: 1px solid rgba(99,102,241,0.3); }
  .badge.live { background: rgba(239,68,68,0.15); color: var(--red); border: 1px solid rgba(239,68,68,0.3); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }

  /* Grid */
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 16px; }
  .stat-card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; line-height: 1; }
  .stat-card .value.green { color: var(--green); }
  .stat-card .value.red { color: var(--red); }
  .stat-card .value.blue { color: var(--blue); }
  .stat-card .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }

  /* Section */
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 16px; overflow: hidden; }
  .section-header { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--text); }
  .section-sub { font-size: 11px; color: var(--muted); }

  /* Table */
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; min-width: 600px; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); padding: 8px 12px; text-align: left; background: var(--surface2); border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 9px 12px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--surface2); }

  /* Badges */
  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .pill.buy { background: rgba(34,197,94,0.12); color: var(--green); }
  .pill.sell { background: rgba(239,68,68,0.12); color: var(--red); }
  .pill.yes { background: rgba(59,130,246,0.12); color: var(--blue); }
  .pill.no { background: rgba(245,158,11,0.12); color: var(--yellow); }
  .pill.pending { background: rgba(99,102,241,0.12); color: var(--accent); }
  .pill.win { background: rgba(34,197,94,0.12); color: var(--green); }
  .pill.loss { background: rgba(239,68,68,0.12); color: var(--red); }
  .pnl.pos { color: var(--green); font-weight: 600; }
  .pnl.neg { color: var(--red); font-weight: 600; }

  /* Bot status */
  .bot-row { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--border); }
  .bot-row:last-child { border-bottom: none; }
  .bot-indicator { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .bot-indicator.active { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .bot-indicator.paused { background: var(--yellow); }
  .bot-indicator.inactive { background: var(--muted); }
  .bot-name { font-weight: 600; font-size: 13px; }
  .bot-addr { font-size: 11px; color: var(--muted); font-family: monospace; }
  .bot-meta { display: flex; gap: 16px; margin-left: auto; text-align: right; }
  .bot-meta-item { font-size: 11px; color: var(--muted); }
  .bot-meta-item span { display: block; font-size: 13px; color: var(--text); font-weight: 500; }

  /* Controls */
  .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .btn { padding: 6px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; border: 1px solid var(--border); background: var(--surface2); color: var(--text); cursor: pointer; transition: background 0.15s; }
  .btn:hover { background: var(--border); }
  .btn.active { border-color: var(--accent); color: var(--accent); background: rgba(99,102,241,0.1); }
  .select { padding: 6px 10px; border-radius: 6px; font-size: 12px; border: 1px solid var(--border); background: var(--surface2); color: var(--text); cursor: pointer; }

  /* Footer */
  .footer { text-align: center; color: var(--muted); font-size: 11px; padding: 16px; }

  /* Loading */
  .loading { text-align: center; padding: 40px; color: var(--muted); }

  /* Refreshed indicator */
  .refresh-tag { font-size: 11px; color: var(--muted); }

  @media (max-width: 600px) {
    .header h1 { font-size: 18px; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .bot-meta { display: none; }
    td, th { padding: 7px 8px; font-size: 12px; }
  }
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>Poly<span>Farm</span></h1>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <div id="mode-badge" class="badge paper"><div class="dot"></div> PAPER MODE</div>
      <span id="refresh-tag" class="refresh-tag">—</span>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-grid" id="stats-grid">
    <div class="stat-card"><div class="label">Paper Trades</div><div class="value blue" id="s-paper">—</div><div class="sub" id="s-window">last 7d</div></div>
    <div class="stat-card"><div class="label">Trades Skipped</div><div class="value" id="s-skipped">—</div><div class="sub" id="s-detected">of — detected</div></div>
    <div class="stat-card"><div class="label">Hypothetical Vol</div><div class="value" id="s-vol">—</div><div class="sub">USD deployed</div></div>
    <div class="stat-card"><div class="label">Resolved P&amp;L</div><div class="value" id="s-pnl">—</div><div class="sub" id="s-resolved">— resolved trades</div></div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value" id="s-wr">—</div><div class="sub" id="s-wl">— W / — L</div></div>
  </div>

  <!-- Bots -->
  <div class="section">
    <div class="section-header">
      <div class="section-title">Active Bots</div>
      <div class="section-sub" id="bots-sub"></div>
    </div>
    <div id="bots-list"><div class="loading">Loading…</div></div>
  </div>

  <!-- Controls -->
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px;">
    <div class="section-title">Paper Trades</div>
    <div class="controls">
      <span style="font-size:12px;color:var(--muted);">Window:</span>
      <select class="select" id="days-select" onchange="loadData()">
        <option value="1">1 day</option>
        <option value="7" selected>7 days</option>
        <option value="30">30 days</option>
        <option value="90">All time</option>
      </select>
      <select class="select" id="side-filter">
        <option value="">All sides</option>
        <option value="BUY">BUY</option>
        <option value="SELL">SELL</option>
      </select>
      <select class="select" id="outcome-filter">
        <option value="">All outcomes</option>
        <option value="YES">YES</option>
        <option value="NO">NO</option>
      </select>
    </div>
  </div>

  <!-- Trades table -->
  <div class="section">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time (UTC)</th>
            <th>Bot</th>
            <th>Side</th>
            <th>Out</th>
            <th>Size $</th>
            <th>Price</th>
            <th>Status</th>
            <th>P&amp;L</th>
            <th>Market</th>
          </tr>
        </thead>
        <tbody id="trades-tbody"><tr><td colspan="9" class="loading">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Daily P&L -->
  <div class="section">
    <div class="section-header">
      <div class="section-title">Daily Summary</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Date</th><th>Bot</th><th>Trades</th><th>Volume $</th><th>Realized P&amp;L</th></tr>
        </thead>
        <tbody id="daily-tbody"><tr><td colspan="5" class="loading">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="footer">PolyFarm · auto-refreshes every 30s</div>
</div>

<script>
let allTrades = [];

function fmt(v, decimals=2) {
  if (v === null || v === undefined) return '—';
  return '$' + Number(v).toFixed(decimals);
}
function fmtPct(v) {
  if (v === null || v === undefined) return '—';
  return v.toFixed(1) + '%';
}
function pnlClass(v) {
  if (v === null || v === undefined) return '';
  return v >= 0 ? 'pnl pos' : 'pnl neg';
}
function fmtPnl(v) {
  if (v === null || v === undefined) return '<span class="pill pending">Pending</span>';
  const sign = v >= 0 ? '+' : '';
  return `<span class="${pnlClass(v)}">${sign}${fmt(v)}</span>`;
}

async function loadData() {
  const days = document.getElementById('days-select').value;
  try {
    const res = await fetch('/api/data?days=' + days);
    const d = await res.json();
    render(d);
  } catch(e) {
    console.error('Failed to load data', e);
  }
}

function render(d) {
  // Header
  const badge = document.getElementById('mode-badge');
  badge.className = 'badge ' + d.trading_mode;
  badge.innerHTML = `<div class="dot"></div> ${d.trading_mode.toUpperCase()} MODE`;
  document.getElementById('refresh-tag').textContent = 'Updated ' + d.generated;

  const s = d.stats;

  // Stats
  document.getElementById('s-paper').textContent = s.total_paper;
  document.getElementById('s-window').textContent = 'last ' + d.days + 'd';
  document.getElementById('s-skipped').textContent = s.total_skipped;
  document.getElementById('s-detected').textContent = 'of ' + s.total_detected + ' detected';
  document.getElementById('s-vol').textContent = fmt(s.total_volume);
  const pnlEl = document.getElementById('s-pnl');
  const pnlSign = (s.total_pnl || 0) >= 0 ? '+' : '';
  pnlEl.textContent = s.resolved_trades > 0 ? pnlSign + fmt(s.total_pnl) : '—';
  pnlEl.className = 'value ' + ((s.total_pnl||0) >= 0 ? 'green' : 'red');
  document.getElementById('s-resolved').textContent = (s.resolved_trades || 0) + ' resolved';
  document.getElementById('s-wr').textContent = s.win_rate !== null ? fmtPct(s.win_rate) : '—';
  document.getElementById('s-wl').textContent = (s.wins||0) + ' W / ' + (s.losses||0) + ' L';

  // Bots
  const botsList = document.getElementById('bots-list');
  if (!d.bots.length) {
    botsList.innerHTML = '<div class="loading">No bots registered.</div>';
  } else {
    botsList.innerHTML = d.bots.map(b => {
      const state = !b.active ? 'inactive' : b.paused ? 'paused' : 'active';
      const stateLabel = !b.active ? 'Inactive' : b.paused ? 'Paused' : 'Running';
      const modeLabel = b.paper_mode ? 'Paper' : 'Live';
      return `
        <div class="bot-row">
          <div class="bot-indicator ${state}"></div>
          <div>
            <div class="bot-name">${b.name}</div>
            <div class="bot-addr">${b.target}</div>
          </div>
          <div class="bot-meta">
            <div class="bot-meta-item"><span>${stateLabel}</span>Status</div>
            <div class="bot-meta-item"><span>${modeLabel}</span>Mode</div>
            <div class="bot-meta-item"><span>${b.total_trades}</span>Trades</div>
            <div class="bot-meta-item"><span>${fmt(b.capital, 0)}</span>Est. Capital</div>
            <div class="bot-meta-item"><span>${b.last_activity}</span>Last Active</div>
          </div>
        </div>`;
    }).join('');
  }

  // Trades table (with filters)
  allTrades = d.paper_trades;
  renderTradesTable();

  // Daily P&L
  const dailyTbody = document.getElementById('daily-tbody');
  if (!d.daily_pnl.length) {
    dailyTbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px;">No data yet.</td></tr>';
  } else {
    dailyTbody.innerHTML = d.daily_pnl.map(r => {
      const pnlSign = r.pnl >= 0 ? '+' : '';
      return `<tr>
        <td>${r.date}</td>
        <td>${r.bot}</td>
        <td>${r.trades}</td>
        <td>${fmt(r.volume)}</td>
        <td class="${pnlClass(r.pnl)}">${pnlSign}${fmt(r.pnl)}</td>
      </tr>`;
    }).join('');
  }
}

function renderTradesTable() {
  const sideF = document.getElementById('side-filter').value;
  const outF = document.getElementById('outcome-filter').value;
  let trades = allTrades;
  if (sideF) trades = trades.filter(t => t.side === sideF);
  if (outF) trades = trades.filter(t => t.outcome === outF);

  const tbody = document.getElementById('trades-tbody');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px;">No paper trades in this window.</td></tr>';
    return;
  }

  tbody.innerHTML = trades.map(t => {
    const statusHtml = t.resolved
      ? (t.pnl > 0
          ? `<span class="pill win">Win</span>`
          : `<span class="pill loss">Loss</span>`)
      : `<span class="pill pending">Open</span>`;
    return `<tr>
      <td style="white-space:nowrap;color:var(--muted)">${t.time}</td>
      <td style="font-weight:500">${t.bot}</td>
      <td><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td>
      <td><span class="pill ${t.outcome.toLowerCase()}">${t.outcome}</span></td>
      <td style="font-weight:600">${fmt(t.size)}</td>
      <td style="color:var(--muted)">${t.price.toFixed(3)}</td>
      <td>${statusHtml}</td>
      <td>${fmtPnl(t.pnl)}</td>
      <td style="color:var(--muted);max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${t.market}</td>
    </tr>`;
  }).join('');
}

// Wire filter dropdowns
document.getElementById('side-filter').addEventListener('change', renderTradesTable);
document.getElementById('outcome-filter').addEventListener('change', renderTradesTable);

// Load on start + auto-refresh
loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"PolyFarm Dashboard running at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
