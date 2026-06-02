"""
dashboard/app.py — Sentinel local web dashboard.

Flask application providing:
  /          — Last 20 signals, colour-coded by priority (auto-refresh 15s)
  /signals   — Full searchable signal log with JSON payload viewer
  /health    — Collector status and system health
  /truth     — Timeline of all captured Trump posts
  /polymarket — Tracked markets with recent activity
  /config    — Edit thresholds (shows "restart required" on save)

All times stored in UTC; displayed in AEST (UTC+10 / UTC+11 DST) in the UI.
Access: http://localhost:5000 — LAN only, no authentication in v1.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, render_template_string, request

logger = logging.getLogger(__name__)

# AEST offset (UTC+10, no DST adjustment — conservative for display)
AEST_OFFSET = timedelta(hours=10)

PRIORITY_COLOURS = {
    "CRITICAL": "#dc2626",  # red
    "HIGH": "#ea580c",      # orange
    "MEDIUM": "#ca8a04",    # amber
    "LOW": "#2563eb",       # blue
    "INFO": "#6b7280",      # grey
}


def _to_aest(utc_str: str) -> str:
    """Convert a UTC ISO8601 string to AEST display string."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        aest_dt = dt + AEST_OFFSET
        return aest_dt.strftime("%d %b %Y %H:%M:%S AEST")
    except (ValueError, AttributeError):
        return utc_str


def _enrich_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Add derived fields for template rendering."""
    signal = dict(signal)
    signal["created_at_aest"] = _to_aest(signal.get("created_at", ""))
    signal["colour"] = PRIORITY_COLOURS.get(signal.get("priority", "INFO"), "#6b7280")
    if isinstance(signal.get("payload"), dict):
        signal["payload_json"] = json.dumps(signal["payload"], indent=2)
    else:
        signal["payload_json"] = str(signal.get("payload", "{}"))
    return signal


# ---------------------------------------------------------------------------
# Templates (inline to keep the project self-contained)
# ---------------------------------------------------------------------------

BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sentinel — {{ page_title }}</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; background: #0f172a; color: #e2e8f0; margin: 0; padding: 0; }
    nav { background: #1e293b; padding: 12px 24px; display: flex; gap: 24px; align-items: center; border-bottom: 1px solid #334155; }
    nav a { color: #94a3b8; text-decoration: none; font-size: 14px; }
    nav a:hover, nav a.active { color: #f1f5f9; }
    nav .brand { font-weight: bold; font-size: 18px; color: #f1f5f9; margin-right: 12px; }
    .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
    .signal-card { background: #1e293b; border-radius: 8px; padding: 16px; margin-bottom: 12px; border-left: 4px solid {{ '#dc2626' }}; }
    .signal-header { display: flex; gap: 12px; align-items: baseline; margin-bottom: 8px; }
    .badge { padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
    .badge-CRITICAL { background: #dc2626; color: white; }
    .badge-HIGH { background: #ea580c; color: white; }
    .badge-MEDIUM { background: #ca8a04; color: white; }
    .badge-LOW { background: #2563eb; color: white; }
    .badge-INFO { background: #6b7280; color: white; }
    .signal-summary { font-size: 14px; color: #cbd5e1; }
    .signal-meta { font-size: 12px; color: #64748b; margin-top: 4px; }
    .payload-block { background: #0f172a; border-radius: 4px; padding: 8px; font-size: 12px; font-family: monospace; white-space: pre-wrap; margin-top: 8px; color: #94a3b8; display: none; }
    .toggle-payload { background: none; border: 1px solid #334155; color: #64748b; padding: 2px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-top: 4px; }
    .toggle-payload:hover { color: #e2e8f0; }
    h1 { font-size: 24px; margin-bottom: 4px; }
    h2 { font-size: 18px; color: #94a3b8; margin-bottom: 16px; }
    .health-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; }
    .health-card { background: #1e293b; border-radius: 8px; padding: 16px; }
    .health-card h3 { font-size: 14px; color: #94a3b8; margin: 0 0 8px; }
    .status-ok { color: #22c55e; }
    .status-warn { color: #eab308; }
    .status-err { color: #ef4444; }
    .filter-bar { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
    .filter-bar select, .filter-bar input { background: #1e293b; border: 1px solid #334155; color: #e2e8f0; padding: 6px 12px; border-radius: 4px; font-size: 14px; }
    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; padding: 8px 12px; border-bottom: 1px solid #334155; color: #64748b; font-size: 12px; font-weight: normal; }
    td { padding: 8px 12px; border-bottom: 1px solid #1e293b; font-size: 13px; vertical-align: top; }
    tr:hover { background: #1e293b; }
    .restart-banner { background: #7c3aed; color: white; padding: 12px 24px; text-align: center; font-size: 14px; }
    .config-form label { display: block; color: #94a3b8; font-size: 12px; margin-top: 12px; margin-bottom: 4px; }
    .config-form input, .config-form textarea { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 6px 10px; border-radius: 4px; width: 100%; font-size: 14px; }
    .btn { padding: 8px 16px; border-radius: 4px; border: none; cursor: pointer; font-size: 14px; }
    .btn-primary { background: #3b82f6; color: white; }
    .btn-primary:hover { background: #2563eb; }
    .empty-state { color: #64748b; text-align: center; padding: 48px; font-size: 14px; }
  </style>
</head>
<body>
<nav>
  <span class="brand">&#9632; Sentinel</span>
  <a href="/" class="{% if page == 'home' %}active{% endif %}">Home</a>
  <a href="/signals" class="{% if page == 'signals' %}active{% endif %}">Signals</a>
  <a href="/truth" class="{% if page == 'truth' %}active{% endif %}">Truth Social</a>
  <a href="/polymarket" class="{% if page == 'polymarket' %}active{% endif %}">Polymarket</a>
  <a href="/health" class="{% if page == 'health' %}active{% endif %}">Health</a>
</nav>
{% block content %}{% endblock %}
<script>
function togglePayload(id) {
  var el = document.getElementById('payload-' + id);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
</script>
</body>
</html>"""

HOME_TEMPLATE = BASE_TEMPLATE.replace(
    "{% block content %}{% endblock %}",
    """{% block content %}
<div class="container">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px;">
    <div>
      <h1>Signal Feed</h1>
      <h2>Last 20 signals · Auto-refreshes every 15s</h2>
    </div>
    <div style="color:#64748b;font-size:12px;">{{ now_aest }}</div>
  </div>
  <div hx-get="/" hx-trigger="every 15s" hx-target="#signal-feed" hx-swap="innerHTML">
    <div id="signal-feed">
      {% if signals %}
        {% for signal in signals %}
        <div class="signal-card" style="border-left-color: {{ signal.colour }}">
          <div class="signal-header">
            <span class="badge badge-{{ signal.priority }}">{{ signal.priority }}</span>
            <span style="font-size:13px;font-weight:bold;">{{ signal.source }}</span>
            <span style="color:#64748b;font-size:12px;">{{ signal.signal_type }}</span>
          </div>
          <div class="signal-summary">{{ signal.summary }}</div>
          <div class="signal-meta">
            {{ signal.created_at_aest }}
            {% if signal.alerted %}<span style="color:#22c55e;margin-left:8px;">&#10003; alerted</span>{% endif %}
          </div>
          <button class="toggle-payload" onclick="togglePayload({{ signal.id }})">JSON payload</button>
          <pre class="payload-block" id="payload-{{ signal.id }}">{{ signal.payload_json }}</pre>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty-state">No signals yet. Collectors are running.</div>
      {% endif %}
    </div>
  </div>
</div>
{% endblock %}"""
)

SIGNALS_TEMPLATE = BASE_TEMPLATE.replace(
    "{% block content %}{% endblock %}",
    """{% block content %}
<div class="container">
  <h1>Signal Log</h1>
  <div class="filter-bar">
    <select onchange="filterSignals(this)" id="source-filter">
      <option value="">All sources</option>
      <option value="truth_social">Truth Social</option>
      <option value="polymarket">Polymarket</option>
      <option value="futures_oil">Futures Oil</option>
      <option value="futures_sp500">Futures S&P 500</option>
      <option value="correlation_detector">Correlation</option>
    </select>
    <select onchange="filterSignals(this)" id="priority-filter">
      <option value="">All priorities</option>
      <option value="CRITICAL">CRITICAL</option>
      <option value="HIGH">HIGH</option>
      <option value="MEDIUM">MEDIUM</option>
      <option value="LOW">LOW</option>
      <option value="INFO">INFO</option>
    </select>
  </div>
  <table>
    <thead>
      <tr>
        <th>Time (AEST)</th>
        <th>Priority</th>
        <th>Source</th>
        <th>Type</th>
        <th>Summary</th>
        <th>Alerted</th>
      </tr>
    </thead>
    <tbody>
      {% if signals %}
        {% for signal in signals %}
        <tr>
          <td style="white-space:nowrap;color:#64748b;">{{ signal.created_at_aest }}</td>
          <td><span class="badge badge-{{ signal.priority }}">{{ signal.priority }}</span></td>
          <td>{{ signal.source }}</td>
          <td>{{ signal.signal_type }}</td>
          <td>
            {{ signal.summary }}
            <button class="toggle-payload" onclick="togglePayload({{ signal.id }})">JSON</button>
            <pre class="payload-block" id="payload-{{ signal.id }}">{{ signal.payload_json }}</pre>
          </td>
          <td>{% if signal.alerted %}<span class="status-ok">&#10003;</span>{% else %}<span class="status-warn">pending</span>{% endif %}</td>
        </tr>
        {% endfor %}
      {% else %}
        <tr><td colspan="6" class="empty-state">No signals recorded yet.</td></tr>
      {% endif %}
    </tbody>
  </table>
</div>
<script>
function filterSignals(el) {
  var source = document.getElementById('source-filter').value;
  var priority = document.getElementById('priority-filter').value;
  var url = '/signals?';
  if (source) url += 'source=' + source + '&';
  if (priority) url += 'priority=' + priority + '&';
  window.location.href = url;
}
</script>
{% endblock %}"""
)

TRUTH_TEMPLATE = BASE_TEMPLATE.replace(
    "{% block content %}{% endblock %}",
    """{% block content %}
<div class="container">
  <h1>Truth Social Posts</h1>
  <h2>All captured Trump posts — newest first</h2>
  {% if signals %}
    {% for signal in signals %}
    <div class="signal-card" style="border-left-color:#dc2626;">
      <div class="signal-header">
        <span class="badge badge-CRITICAL">CRITICAL</span>
        {% if signal.payload.is_reblog %}<span style="color:#64748b;font-size:12px;">retruth</span>{% endif %}
        {% if signal.payload.has_media %}<span style="color:#94a3b8;font-size:12px;">&#128248; media</span>{% endif %}
      </div>
      <div class="signal-summary">{{ signal.payload.text }}</div>
      {% if signal.payload.url %}
      <div style="margin-top:4px;"><a href="{{ signal.payload.url }}" style="color:#3b82f6;font-size:12px;" target="_blank">View on Truth Social &#8599;</a></div>
      {% endif %}
      <div class="signal-meta">{{ signal.created_at_aest }}</div>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty-state">No Truth Social posts captured yet.</div>
  {% endif %}
</div>
{% endblock %}"""
)

POLYMARKET_TEMPLATE = BASE_TEMPLATE.replace(
    "{% block content %}{% endblock %}",
    """{% block content %}
<div class="container">
  <h1>Polymarket Activity</h1>
  <h2>Recent signals from tracked markets</h2>
  {% if signals %}
    {% for signal in signals %}
    <div class="signal-card" style="border-left-color:{{ signal.colour }};">
      <div class="signal-header">
        <span class="badge badge-{{ signal.priority }}">{{ signal.priority }}</span>
        <span style="font-size:13px;font-weight:bold;">{{ signal.signal_type }}</span>
      </div>
      <div class="signal-summary">{{ signal.summary }}</div>
      <div class="signal-meta">{{ signal.created_at_aest }}</div>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty-state">No Polymarket signals yet.</div>
  {% endif %}
</div>
{% endblock %}"""
)

HEALTH_TEMPLATE = BASE_TEMPLATE.replace(
    "{% block content %}{% endblock %}",
    """{% block content %}
<div class="container">
  <h1>System Health</h1>
  <h2>Last checked: {{ now_aest }}</h2>
  <div class="health-grid">
    {% for collector in collectors %}
    <div class="health-card">
      <h3>{{ collector.name }}</h3>
      <div class="{% if collector.status == 'ok' %}status-ok{% elif collector.status == 'warn' %}status-warn{% else %}status-err{% endif %}">
        &#11044; {{ collector.status | upper }}
      </div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">
        Last signal: {{ collector.last_signal }}<br>
        Total signals: {{ collector.total_signals }}
      </div>
    </div>
    {% endfor %}
  </div>
  <div style="margin-top:24px;">
    <h2>Database</h2>
    <div style="font-size:13px;color:#94a3b8;">
      Total signals: {{ total_signals }}<br>
      Unalerted: {{ unalerted_signals }}<br>
      DB path: {{ db_path }}
    </div>
  </div>
</div>
{% endblock %}"""
)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(db, config_path: Optional[str] = None) -> Flask:
    """
    Create and configure the Flask application.

    Args:
        db:          Initialised Database instance.
        config_path: Path to config.yaml (for the config editor page).
    """
    # Use the templates directory for Jinja2 templates if they exist
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    app = Flask(__name__, template_folder=template_dir)
    app.config["SECRET_KEY"] = "sentinel-local-dashboard"
    app.config["DB"] = db
    app.config["CONFIG_PATH"] = config_path
    app.config["RESTART_REQUIRED"] = False

    # ------------------------------------------------------------------
    # Home
    # ------------------------------------------------------------------

    @app.route("/")
    def home():
        signals = [_enrich_signal(s) for s in db.get_recent_signals(limit=20)]
        now_aest = _to_aest(datetime.now(timezone.utc).isoformat())
        return render_template_string(
            HOME_TEMPLATE,
            signals=signals,
            now_aest=now_aest,
            page="home",
            page_title="Home",
        )

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    @app.route("/signals")
    def signals_page():
        source_filter = request.args.get("source")
        priority_filter = request.args.get("priority")
        if source_filter:
            raw = db.get_signals_by_source(source_filter, limit=200)
        else:
            raw = db.get_recent_signals(limit=200)
        if priority_filter:
            raw = [s for s in raw if s.get("priority") == priority_filter]
        signals = [_enrich_signal(s) for s in raw]
        return render_template_string(
            SIGNALS_TEMPLATE,
            signals=signals,
            page="signals",
            page_title="Signals",
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.route("/health")
    def health():
        format_json = request.args.get("format") == "json"
        sources = [
            "truth_social", "polymarket", "futures_oil", "futures_sp500",
            "futures_brent", "futures_natgas", "futures_gold", "futures_dxy",
            "correlation_detector",
        ]
        collectors_info = []
        now = datetime.now(timezone.utc)
        total_signals = db.execute_scalar("SELECT COUNT(*) FROM signals") or 0
        unalerted = db.execute_scalar("SELECT COUNT(*) FROM signals WHERE alerted=0") or 0

        for source in sources:
            rows = db.execute_fetchall(
                "SELECT created_at FROM signals WHERE source=? ORDER BY created_at DESC LIMIT 1",
                (source,),
            )
            if rows:
                last_ts_str = rows[0]["created_at"]
                try:
                    last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    minutes_ago = (now - last_dt).total_seconds() / 60
                    status = "ok" if minutes_ago < 5 else ("warn" if minutes_ago < 30 else "err")
                    last_signal = _to_aest(last_ts_str)
                except (ValueError, TypeError):
                    status = "warn"
                    last_signal = last_ts_str
            else:
                status = "warn"
                last_signal = "never"

            total = db.execute_scalar(
                "SELECT COUNT(*) FROM signals WHERE source=?", (source,)
            ) or 0
            collectors_info.append({
                "name": source.replace("_", " ").title(),
                "source": source,
                "status": status,
                "last_signal": last_signal,
                "total_signals": total,
            })

        if format_json:
            return jsonify({
                "status": "ok",
                "collectors": collectors_info,
                "total_signals": total_signals,
                "unalerted_signals": unalerted,
                "checked_at": now.isoformat(),
            })

        now_aest = _to_aest(now.isoformat())
        db_path = config_path or "./sentinel.db"
        return render_template_string(
            HEALTH_TEMPLATE,
            collectors=collectors_info,
            total_signals=total_signals,
            unalerted_signals=unalerted,
            db_path=db_path,
            now_aest=now_aest,
            page="health",
            page_title="Health",
        )

    # ------------------------------------------------------------------
    # Truth Social
    # ------------------------------------------------------------------

    @app.route("/truth")
    def truth():
        raw = db.get_signals_by_source("truth_social", limit=100)
        signals = [_enrich_signal(s) for s in raw]
        return render_template_string(
            TRUTH_TEMPLATE,
            signals=signals,
            page="truth",
            page_title="Truth Social",
        )

    # ------------------------------------------------------------------
    # Polymarket
    # ------------------------------------------------------------------

    @app.route("/polymarket")
    def polymarket():
        raw = db.get_signals_by_source("polymarket", limit=100)
        signals = [_enrich_signal(s) for s in raw]
        return render_template_string(
            POLYMARKET_TEMPLATE,
            signals=signals,
            page="polymarket",
            page_title="Polymarket",
        )

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the dashboard as a standalone Flask dev server."""
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from sentinel.core.db import Database
    from sentinel.core.config import load_config

    config_path = os.environ.get("SENTINEL_CONFIG", "config.yaml")
    db_path = os.environ.get("SENTINEL_DB", "sentinel.db")

    cfg = load_config(config_path)
    db = Database(db_path)
    db.init()

    application = create_app(db=db, config_path=config_path)
    application.run(
        host=cfg.dashboard.host,
        port=cfg.dashboard.port,
        debug=False,
    )


if __name__ == "__main__":
    main()
