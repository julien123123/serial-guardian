"""
Flask web UI for the serial guardian.

Kept deliberately light (inline templates, no build step, no CDN
dependencies) since this runs on a Pi Zero W and needs to work even with
no internet access once it's set up.

NOTE ON EXPOSURE: none of this has any authentication. Everything below
-- including "restart the service", "pause monitoring", "erase all
sessions", and the Settings page -- is reachable by anyone who can reach
the Pi on your network. Fine on a trusted home LAN, worth knowing if
that's not your situation.
"""
import json
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template_string, request

import fields
import settings

STATUS_COLORS = {
    "NORMAL": "#63d68a",
    "ANOMALY": "#e8b64b",
    "EXCEPTION": "#e55a5a",
    "STOPPED": "#5bc9c9",
}

# Small monochrome line-art icons for the toolbar, styled like the
# engraved panel symbols on old tape/transport equipment -- stop/play/
# pause read directly off a Nagra-style transport, reset and power use
# the usual refresh/power glyphs. All use currentColor so CSS state
# classes (.lit / .flashing / etc) can recolor them.
ICONS = {
    "stop": '<svg viewBox="0 0 20 20" fill="currentColor"><rect x="6" y="6" width="8" height="8"/></svg>',
    "play": '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M7 5 L15 10 L7 15 Z"/></svg>',
    "pause": '<svg viewBox="0 0 20 20" fill="currentColor"><rect x="6" y="5" width="3" height="10"/><rect x="11" y="5" width="3" height="10"/></svg>',
    "reset": ('<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" '
              'stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M15.5 7A6 6 0 1 1 14 4.2"/><path d="M11.5 3.3 L15.5 4.2 L14.6 8.2"/></svg>'),
    "trash": ('<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4" '
              'stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M4 6h12"/><path d="M8 6V4h4v2"/><path d="M6 6l1 10h6l1-10"/>'
              '<path d="M9 9v4M11 9v4"/></svg>'),
    "power": ('<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" '
              'stroke-linecap="round"><path d="M10 3v6"/><path d="M6 5.5a6 6 0 1 0 8 0"/></svg>'),
}


def esc(s):
    if s is None:
        return "—"
    return str(s).replace("&", "&amp;").replace("<", "&lt;")


BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · Serial Guardian</title>
<style>
  :root {
    --bg:        #0d1012;
    --panel:     #14181a;
    --line:      #232a2d;
    --ink:       #cfe0d8;
    --ink-dim:   #7c8f88;
    --amber:     #e8b64b;
    --green:     #63d68a;
    --red:       #e55a5a;
    --cyan:      #5bc9c9;
    --mono: ui-monospace, "JetBrains Mono", "SF Mono", "Cascadia Code",
            "Consolas", monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: var(--mono); font-size: 14px; line-height: 1.5;
  }
  a { color: var(--cyan); text-decoration: none; }
  a:hover { text-decoration: underline; }
  a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  code {
    background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
    padding: 0.05rem 0.35rem; font-size: 0.9em;
  }

  header {
    display: flex; align-items: center; gap: 1.25rem;
    padding: 0.9rem 1.25rem; border-bottom: 1px solid var(--line);
    background: var(--panel); flex-wrap: wrap;
  }
  header .brand { font-weight: 600; letter-spacing: 0.02em; color: var(--ink); }
  header .brand .cursor {
    display: inline-block; width: 0.55em; height: 1em; margin-left: 2px;
    background: var(--green); vertical-align: -2px;
    animation: blink 1.1s steps(1) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }

  nav { display: flex; gap: 1rem; margin-left: auto; }
  nav a { color: var(--ink-dim); }
  nav a.active { color: var(--ink); border-bottom: 2px solid var(--green); }

  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-right: 0.4em; }
  .dot.up { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.down { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .dot.halt { background: var(--cyan); box-shadow: 0 0 6px var(--cyan); }
  .dot.pause { background: var(--amber); box-shadow: 0 0 6px var(--amber); }

  main { padding: 1.25rem; max-width: 1080px; margin: 0 auto; }
  .hint { color: var(--ink-dim); font-size: 0.8rem; }

  .stats { display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; }
  a.stat-link { text-decoration: none; }
  .stat {
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
    padding: 0.6rem 0.9rem; min-width: 8em; transition: border-color 0.15s;
  }
  a.stat-link:hover .stat { border-color: var(--cyan); }
  .stat .n { font-size: 1.4rem; font-weight: 600; }
  .stat .l { color: var(--ink-dim); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; }

  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
    padding: 0.8rem 1rem; margin-bottom: 1rem;
  }
  .controls { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.6rem; flex-wrap: wrap; }
  button.btn {
    font-family: var(--mono); font-size: 0.85rem; background: var(--panel); color: var(--ink);
    border: 1px solid var(--line); border-radius: 6px; padding: 0.45rem 0.9rem; cursor: pointer;
  }
  button.btn:hover { border-color: var(--cyan); color: var(--cyan); }
  button.btn:disabled { opacity: 0.45; cursor: default; }

  /* Console: the control strip is welded directly onto the monitor
     window below it -- one bordered unit, like a control panel built
     into the same chassis as the screen, rather than a toolbar floating
     up near the navbar. */
  .console { border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
             margin-bottom: 1rem; background: #0a0c0d; }
  .console-strip {
    display: flex; align-items: center; justify-content: space-between; gap: 1rem;
    padding: 0.7rem 0.9rem; flex-wrap: wrap;
    background: linear-gradient(180deg, #1c2124, #14181a);
    border-bottom: 1px solid var(--line);
  }
  .status-readout {
    background: #0a0c0d; border: 1px solid var(--line); border-radius: 4px;
    padding: 0.35rem 0.7rem; font-size: 0.85rem; line-height: 1.4;
  }

  .toolbar { display: flex; gap: 0.9rem; flex-wrap: wrap; }
  .tb-group {
    display: flex; flex-direction: column; align-items: flex-start; gap: 0.35rem;
    padding-left: 0.9rem; border-left: 1px solid var(--line);
  }
  .tb-group:first-child { padding-left: 0; border-left: none; }
  .tb-label {
    font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.09em; color: var(--ink-dim);
  }
  .tb-buttons { display: flex; gap: 0.4rem; }

  /* Icon "keys" -- brushed-metal chiclet buttons with an inset bevel,
     the way a physical panel switch would be lit rather than labelled. */
  .tb-icon {
    display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 30px; padding: 0;
    background: linear-gradient(180deg, #262c2f, #181d1f);
    border: 1px solid #333b3e; border-radius: 4px;
    color: var(--ink-dim); cursor: pointer;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 1px 2px rgba(0,0,0,0.5);
    transition: color 0.15s, border-color 0.15s;
  }
  .tb-icon svg { width: 16px; height: 16px; display: block; }
  .tb-icon:hover { color: var(--cyan); border-color: #3d474b; }
  .tb-icon:active { box-shadow: inset 0 1px 3px rgba(0,0,0,0.6); transform: translateY(1px); }
  .tb-icon.danger:hover { color: var(--red); }

  .tb-icon.lit       { color: var(--green); filter: drop-shadow(0 0 3px var(--green)); }
  .tb-icon.lit-cyan  { color: var(--cyan);  filter: drop-shadow(0 0 3px var(--cyan)); }
  .tb-icon.lit-amber { color: var(--amber); filter: drop-shadow(0 0 3px var(--amber)); }

  @keyframes lampFlicker {
    0%, 49%   { color: var(--amber); filter: drop-shadow(0 0 4px var(--amber)); }
    50%, 100% { color: var(--ink-dim); filter: none; }
  }
  .tb-icon.flashing { animation: lampFlicker 1s steps(1) infinite; }

  pre.term {
    background: #0a0c0d; border: none; border-radius: 0; margin: 0;
    padding: 0.9rem 1rem; height: 55vh; overflow-y: auto;
    white-space: pre-wrap; word-break: break-word;
  }
  pre.term .l-exc { border-left: 3px solid var(--red); padding-left: 0.5em; color: #f3a9a9; }
  pre.term .l-ok  { border-left: 3px solid var(--green); padding-left: 0.5em; }

  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--line); }
  th { color: var(--ink-dim); font-weight: 500; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.08em; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  td.extra, th.extra { color: var(--ink-dim); font-size: 0.85em; }

  .badge {
    display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.03em;
  }
  .filters { margin-bottom: 0.75rem; display: flex; gap: 0.6rem; }
  .filters a {
    padding: 0.25rem 0.7rem; border: 1px solid var(--line); border-radius: 999px;
    color: var(--ink-dim);
  }
  .filters a.active { color: var(--bg); background: var(--ink); }

  .session-nav { display: flex; justify-content: space-between; margin: 1rem 0; color: var(--ink-dim); }
  .meta { color: var(--ink-dim); margin-bottom: 0.75rem; }
  .meta b { color: var(--ink); }
  footer { text-align: center; color: var(--ink-dim); padding: 2rem 1rem; font-size: 0.78rem; }

  .fc table input {
    font-family: var(--mono); background: var(--bg); color: var(--ink);
    border: 1px solid var(--line); border-radius: 4px; padding: 0.3rem 0.5rem; width: 100%;
  }

  h2.section-title { font-size: 0.95rem; margin: 1.6rem 0 0.9rem; color: var(--ink); }
  h2.section-title:first-child { margin-top: 0; }
  .settings-section { margin-bottom: 1.6rem; }
  .settings-section-label {
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-dim);
    border-bottom: 1px solid var(--line); padding-bottom: 0.4rem; margin-bottom: 0.9rem;
  }
  .field-row { display: block; margin-bottom: 1rem; max-width: 520px; }
  .field-row.checkbox { display: flex; align-items: center; gap: 0.6rem; max-width: none; }
  .field-row.checkbox .field-label { margin-bottom: 0; }
  .field-label { font-size: 0.82rem; color: var(--ink); margin-bottom: 0.3rem; }
  .field-help { font-size: 0.75rem; color: var(--ink-dim); margin-top: 0.3rem; line-height: 1.4; }
  .field-row input[type=text], .field-row input[type=number], .field-row textarea {
    width: 100%; font-family: var(--mono); font-size: 0.85rem; background: var(--bg); color: var(--ink);
    border: 1px solid var(--line); border-radius: 5px; padding: 0.4rem 0.6rem;
  }
  .field-row textarea { min-height: 4.5em; resize: vertical; }
  table.readonly-table td { font-size: 0.85rem; }
  table.readonly-table td.key { color: var(--ink-dim); white-space: nowrap; }
</style>
</head>
<body>
<header>
  <div class="brand">serial-guardian<span class="cursor"></span></div>
  <nav>
    <a href="/" class="{{ 'active' if active=='live' else '' }}">Live</a>
    <a href="/sessions" class="{{ 'active' if active=='sessions' else '' }}">Sessions</a>
    <a href="/settings" class="{{ 'active' if active=='settings' else '' }}">Settings</a>
  </nav>
</header>
<main>
{{ body|safe }}
</main>
<footer>polling every ~1.5s &middot; sessions are numbered sequentially, so id-1 / id+1 is your before/after context</footer>
</body>
</html>
"""


def render(title, active, body):
    return render_template_string(BASE, title=title, active=active, body=body)


def create_app(monitor, cfg):
    app = Flask(__name__)

    # ------------------------------------------------------------------ #
    # Live page
    # ------------------------------------------------------------------ #
    @app.route("/")
    def live():
        st = monitor.get_status()
        stats_html = "".join(
            f'<a class="stat-link" href="/sessions?status={k}"><div class="stat">'
            f'<div class="n" style="color:{STATUS_COLORS[k]}">{v}</div>'
            f'<div class="l">{k}</div></div></a>'
            for k, v in st["stats"].items()
        )
        exc_markers_json = json.dumps(cfg.EXCEPTION_MARKERS)
        body = f"""
        <div class="stats">{stats_html}</div>

        <div class="console">
          <div class="console-strip">
            <div class="status-readout">
              <p id="connLine" style="margin:0"></p>
              <p id="subLine" class="hint" style="margin:0.2rem 0 0"></p>
            </div>
            <div class="toolbar">
              <div class="tb-group">
                <div class="tb-label">Device</div>
                <div class="tb-buttons">
                  <button id="stopItem" class="tb-icon"
                          title="Stop: halt the board at the REPL on its next update instead of sleeping"
                          aria-label="Stop device" onclick="doStop()">{ICONS['stop']}</button>
                  <button id="resumeItem" class="tb-icon" style="display:none"
                          title="Resume normal operation" aria-label="Resume device"
                          onclick="doResume()">{ICONS['play']}</button>
                  <button id="resetItem" class="tb-icon"
                          title="Reset: reboot the board now, whatever state it's in"
                          aria-label="Reset device" onclick="doResetDevice()">{ICONS['reset']}</button>
                </div>
              </div>
              <div class="tb-group">
                <div class="tb-label">Monitoring</div>
                <div class="tb-buttons">
                  <button id="pauseMonItem" class="tb-icon"
                          title="Pause monitoring: free the serial port for mpremote or a terminal"
                          aria-label="Pause monitoring" onclick="doPauseMon()">{ICONS['pause']}</button>
                  <button id="resumeMonItem" class="tb-icon" style="display:none"
                          title="Resume monitoring" aria-label="Resume monitoring"
                          onclick="doResumeMon()">{ICONS['play']}</button>
                  <button class="tb-icon danger"
                          title="Erase all session logs and the index (cannot be undone)"
                          aria-label="Erase all sessions" onclick="doEraseSessions()">{ICONS['trash']}</button>
                </div>
              </div>
              <div class="tb-group">
                <div class="tb-label">Service</div>
                <div class="tb-buttons">
                  <button class="tb-icon"
                          title="Restart the guardian service (brief interruption, reconnects on its own)"
                          aria-label="Restart service" onclick="doRestartService()">{ICONS['power']}</button>
                </div>
              </div>
            </div>
          </div>
          <pre class="term" id="tail"></pre>
        </div>
        <script>
        const EXC_MARKERS = {exc_markers_json};
        function fmt(line) {{
          const escd = line.replace(/&/g,'&amp;').replace(/</g,'&lt;');
          if (EXC_MARKERS.some(m => line.includes(m))) {{
            return '<span class="l-exc">' + escd + '</span>';
          }}
          if (line.includes('{cfg.GOTOSLEEP_MARKER}')) {{
            return '<span class="l-ok">' + escd + '</span>';
          }}
          return escd;
        }}

        function applyStatus(s) {{
          let dot, label, sub = '';
          if (!s.monitoring) {{
            dot = 'pause'; label = 'monitoring paused -- port free for mpremote';
          }} else if (s.halted) {{
            dot = 'halt'; label = 'halted at REPL';
            sub = 'normal wake cycle is paused -- it will not go back to sleep on its own';
          }} else if (s.connected) {{
            dot = 'up'; label = 'connected';
          }} else {{
            dot = 'down'; label = 'disconnected';
            if (s.stop_armed) sub = 'armed -- will halt the next time it wakes';
          }}
          document.getElementById('connLine').innerHTML =
            '<span class="dot ' + dot + '"></span>' + label +
            ' &nbsp;&middot;&nbsp; last session #' + s.last_session_id;
          document.getElementById('subLine').textContent = sub;

          document.getElementById('stopItem').style.display = s.halted ? 'none' : 'inline-flex';
          document.getElementById('resumeItem').style.display = s.halted ? 'inline-flex' : 'none';
          document.getElementById('pauseMonItem').style.display = s.monitoring ? 'inline-flex' : 'none';
          document.getElementById('resumeMonItem').style.display = s.monitoring ? 'none' : 'inline-flex';

          // Lamp states, like indicator lights on the panel rather than
          // relying on text alone:
          document.getElementById('stopItem').classList.toggle('flashing', !!s.stop_armed && !s.halted);
          document.getElementById('resetItem').classList.toggle('lit', !!s.connected);
          document.getElementById('resumeItem').classList.toggle('lit-cyan', !!s.halted);
          document.getElementById('resumeMonItem').classList.toggle('lit-amber', !s.monitoring);
        }}

        async function doStop() {{ await fetch('/api/stop', {{method:'POST'}}); }}
        async function doResume() {{ await fetch('/api/resume', {{method:'POST'}}); }}
        async function doResetDevice() {{ await fetch('/api/device/reset', {{method:'POST'}}); }}
        async function doPauseMon() {{ await fetch('/api/monitoring/pause', {{method:'POST'}}); }}
        async function doResumeMon() {{ await fetch('/api/monitoring/resume', {{method:'POST'}}); }}
        async function doRestartService() {{
          if (!confirm('Restart the guardian service now? This briefly interrupts monitoring.')) return;
          try {{ await fetch('/api/service/restart', {{method:'POST'}}); }} catch (e) {{}}
        }}
        async function doEraseSessions() {{
          if (!confirm('Permanently erase ALL session logs and the index? This cannot be undone.')) return;
          await fetch('/api/sessions/erase', {{method:'POST'}});
        }}

        async function poll() {{
          try {{
            const res = await fetch('/api/tail');
            const data = await res.json();
            const box = document.getElementById('tail');
            const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
            box.innerHTML = data.tail.map(fmt).join('\\n');
            if (atBottom) box.scrollTop = box.scrollHeight;
            applyStatus(data.status);
          }} catch (e) {{ /* pi rebooting, wifi hiccup, etc -- just retry */ }}
          setTimeout(poll, 1500);
        }}
        poll();
        </script>
        """
        return render("Live", "live", body)

    @app.route("/api/tail")
    def api_tail():
        return jsonify({"tail": monitor.get_tail(), "status": monitor.get_status()})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        monitor.request_stop()
        return jsonify(monitor.get_status())

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        monitor.resume()
        return jsonify(monitor.get_status())

    @app.route("/api/device/reset", methods=["POST"])
    def api_device_reset():
        monitor.reset_device()
        return jsonify(monitor.get_status())

    @app.route("/api/monitoring/pause", methods=["POST"])
    def api_monitoring_pause():
        monitor.pause_monitoring()
        return jsonify(monitor.get_status())

    @app.route("/api/monitoring/resume", methods=["POST"])
    def api_monitoring_resume():
        monitor.resume_monitoring()
        return jsonify(monitor.get_status())

    @app.route("/api/sessions/erase", methods=["POST"])
    def api_sessions_erase():
        monitor.erase_all_sessions()
        return jsonify(monitor.get_status())

    # ------------------------------------------------------------------ #
    # systemd service control
    # ------------------------------------------------------------------ #
    def _delayed_systemctl(action):
        def run():
            time.sleep(0.6)   # let the HTTP response for this request go out first
            try:
                subprocess.run(
                    ["sudo", "-n", "systemctl", action, cfg.SERVICE_NAME],
                    timeout=10,
                )
            except Exception:
                pass  # sudoers rule not installed yet, or not running under systemd
        threading.Thread(target=run, daemon=True).start()

    @app.route("/api/service/restart", methods=["POST"])
    def api_service_restart():
        _delayed_systemctl("restart")
        return jsonify({"ok": True, "action": "restart"})

    @app.route("/api/service/stop", methods=["POST"])
    def api_service_stop():
        # No button on the Live page for this anymore (pausing monitoring
        # covers the mpremote use case without taking the whole tool
        # down) -- left in place for scripting, e.g.
        # curl -X POST http://guardian.local:8080/api/service/stop
        _delayed_systemctl("stop")
        return jsonify({"ok": True, "action": "stop"})

    # ------------------------------------------------------------------ #
    # Sessions page
    # ------------------------------------------------------------------ #
    @app.route("/sessions")
    def sessions():
        status = request.args.get("status")
        if status not in (None, "NORMAL", "ANOMALY", "EXCEPTION", "STOPPED"):
            status = None
        records = monitor.recent_sessions(n=200, status=status)
        extractors = fields.load(cfg)

        def link(label, val):
            cls = "active" if status == val else ""
            href = "/sessions" + (f"?status={val}" if val else "")
            return f'<a class="{cls}" href="{href}">{label}</a>'

        filters = (
            link("All", None) + link("Normal", "NORMAL") + link("Anomaly", "ANOMALY")
            + link("Exception", "EXCEPTION") + link("Stopped", "STOPPED")
        )

        extra_headers = "".join(f'<th class="extra">{esc(e["name"])}</th>' for e in extractors)

        rows = ""
        for r in records:
            color = STATUS_COLORS[r["status"]]
            text = monitor.read_session_text(r["id"])
            values = fields.extract(text, extractors)
            extra_cells = "".join(f'<td class="extra">{esc(values[e["name"]])}</td>' for e in extractors)
            rows += (
                f'<tr onclick="location.href=\'/sessions/{r["id"]}\'" style="cursor:pointer">'
                f'<td>#{r["id"]}</td>'
                f'<td><span class="badge" style="background:{color}22;color:{color}">{r["status"]}</span></td>'
                f'<td>{r["duration"]}s</td>'
                f'<td>{r["lines"]} lines</td>'
                f'{extra_cells}'
                f'<td><a href="/sessions/{r["id"]}">view</a></td>'
                f'</tr>'
            )

        body = f"""
        <div class="filters">{filters}</div>
        <table>
          <tr><th>id</th><th>status</th><th>duration</th><th>size</th>{extra_headers}<th></th></tr>
          {rows or f'<tr><td colspan="{5 + len(extractors)}">no sessions recorded yet</td></tr>'}
        </table>
        <p class="hint" style="margin-top:0.9rem">Extra columns are configured on the
          <a href="/settings">Settings</a> page.</p>
        """
        return render("Sessions", "sessions", body)

    @app.route("/sessions/<int:sid>")
    def session_detail(sid):
        rec = monitor.find_record(sid)
        text = monitor.read_session_text(sid)
        if text is None:
            body = f"<p>Session #{sid} has no log on disk"
            body += " (pruned, it was a NORMAL session outside the retention window)." if rec else " -- unknown id."
            body += '</p><p><a href="/sessions">&larr; back to sessions</a></p>'
            return render(f"Session #{sid}", "sessions", body)

        status = rec["status"] if rec else "?"
        color = STATUS_COLORS.get(status, "#888")
        meta = ""
        if rec:
            meta = (
                f'<p class="meta"><b>#{rec["id"]}</b> &middot; '
                f'<span class="badge" style="background:{color}22;color:{color}">{status}</span>'
                f' &middot; {rec["duration"]}s &middot; {rec["lines"]} lines</p>'
            )

        esc_text = text.replace("&", "&amp;").replace("<", "&lt;")
        body = f"""
        {meta}
        <div class="session-nav">
          <a href="/sessions/{sid-1}">&larr; session #{sid-1} (before)</a>
          <a href="/sessions/{sid+1}">session #{sid+1} (after) &rarr;</a>
        </div>
        <pre class="term" style="height:70vh">{esc_text}</pre>
        """
        return render(f"Session #{sid}", "sessions", body)

    # ------------------------------------------------------------------ #
    # Settings page: extra columns + all of config.py's tunables
    # ------------------------------------------------------------------ #
    @app.route("/settings")
    def settings_page():
        extractors = fields.load(cfg)
        fc_rows_json = json.dumps(extractors)

        vals = settings.current_values(cfg)
        sections = [
            ("Serial link", ["SERIAL_PORT", "BAUD", "SERIAL_READ_TIMEOUT"]),
            ("Detection & recovery", ["GOTOSLEEP_MARKER", "BOOT_BANNER_MARKER", "EXCEPTION_MARKERS",
                                       "SOFT_RESET_DELAY", "BOOT_TIMEOUT", "STOP_SEND_DELAY"]),
            ("Hardware reset (GPIO)", ["ENABLE_GPIO_RESET", "GPIO_RESET_PIN", "GPIO_RESET_PULSE"]),
            ("Storage & UI", ["NORMAL_LOG_RETENTION", "LIVE_TAIL_LINES", "MONITORING_PAUSE_POLL_INTERVAL"]),
        ]
        spec_by_key = {k: (kind, label, help_) for k, kind, label, help_ in settings.FIELD_SPECS}

        def render_field(key):
            kind, label, help_ = spec_by_key[key]
            value = vals[key]
            if kind == "bool":
                checked = "checked" if value else ""
                return f"""
                <label class="field-row checkbox">
                  <input type="checkbox" name="{key}" {checked}>
                  <span class="field-label">{esc(label)}</span>
                </label>
                <p class="field-help" style="margin:-0.7rem 0 1rem 1.6rem">{esc(help_)}</p>
                """
            if kind == "list":
                text = "\n".join(value)
                return f"""
                <div class="field-row">
                  <div class="field-label">{esc(label)}</div>
                  <textarea name="{key}">{esc(text)}</textarea>
                  <div class="field-help">{esc(help_)}</div>
                </div>
                """
            input_type = "number" if kind in ("int", "float") else "text"
            step = ' step="any"' if kind == "float" else ""
            return f"""
            <div class="field-row">
              <div class="field-label">{esc(label)}</div>
              <input type="{input_type}"{step} name="{key}" value="{esc(value)}">
              <div class="field-help">{esc(help_)}</div>
            </div>
            """

        sections_html = ""
        for title, keys in sections:
            fields_html = "".join(render_field(k) for k in keys)
            sections_html += f"""
            <div class="settings-section">
              <div class="settings-section-label">{esc(title)}</div>
              {fields_html}
            </div>
            """

        readonly_rows = "".join(
            f'<tr><td class="key">{esc(k)}</td><td>{esc(getattr(cfg, k, None))}</td><td class="hint">{esc(why)}</td></tr>'
            for k, why in settings.NOT_EDITABLE
        )

        body = f"""
        <h2 class="section-title">Session field extraction</h2>
        <div class="panel fc">
          <table id="fcTable">
            <tr><th>Column</th><th>Regex (exactly one capture group)</th><th></th></tr>
          </table>
          <div class="controls" style="margin-top:0.6rem">
            <button class="btn" onclick="fcAddRow()">+ add column</button>
            <button class="btn" onclick="fcSave()">Save columns</button>
            <span id="fcMsg"></span>
          </div>
          <div class="hint">These show up as extra columns on the Sessions page. Changes apply
            immediately to sessions already on disk, not just new ones -- values are pulled from
            each session's raw log at view time, not baked in when it finishes.</div>
        </div>

        <h2 class="section-title">Configuration</h2>
        <form id="settingsForm">
          {sections_html}
          <button type="button" class="btn" onclick="saveSettings()">Save settings</button>
          <span id="settingsMsg"></span>
        </form>
        <p class="hint" style="margin-top:0.6rem">Everything above takes effect immediately --
          no restart needed. A few things aren't editable here at all (see below).</p>

        <details class="panel" style="margin-top:1.25rem">
          <summary>Not editable here ({len(settings.NOT_EDITABLE)})</summary>
          <table class="readonly-table">
            <tr><th>Setting</th><th>Current value</th><th>Why not</th></tr>
            {readonly_rows}
          </table>
        </details>

        <script>
        let fcExtractors = {fc_rows_json};
        function fcRender() {{
          const tbl = document.getElementById('fcTable');
          tbl.querySelectorAll('tr.fc-row').forEach(r => r.remove());
          fcExtractors.forEach((e, i) => {{
            const tr = document.createElement('tr');
            tr.className = 'fc-row';
            const nameInp = document.createElement('input');
            nameInp.value = e.name; nameInp.dataset.i = i; nameInp.dataset.f = 'name';
            const patInp = document.createElement('input');
            patInp.value = e.pattern; patInp.dataset.i = i; patInp.dataset.f = 'pattern';
            const tdName = document.createElement('td'); tdName.appendChild(nameInp);
            const tdPat = document.createElement('td'); tdPat.appendChild(patInp);
            const tdDel = document.createElement('td');
            const delBtn = document.createElement('button');
            delBtn.className = 'btn'; delBtn.textContent = '\\u00d7';
            delBtn.onclick = () => fcRemove(i);
            tdDel.appendChild(delBtn);
            tr.appendChild(tdName); tr.appendChild(tdPat); tr.appendChild(tdDel);
            tbl.appendChild(tr);
          }});
        }}
        function fcAddRow() {{ fcExtractors.push({{name:'', pattern:''}}); fcRender(); }}
        function fcRemove(i) {{ fcExtractors.splice(i,1); fcRender(); }}
        async function fcSave() {{
          document.querySelectorAll('#fcTable input').forEach(inp => {{
            fcExtractors[inp.dataset.i][inp.dataset.f] = inp.value;
          }});
          const res = await fetch('/api/field-config', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{extractors: fcExtractors}})
          }});
          const data = await res.json();
          const msg = document.getElementById('fcMsg');
          if (data.errors && data.errors.length) {{
            msg.style.color = 'var(--red)'; msg.textContent = data.errors.join('; ');
          }} else {{
            msg.style.color = 'var(--green)'; msg.textContent = 'saved';
          }}
        }}
        fcRender();

        async function saveSettings() {{
          const form = document.getElementById('settingsForm');
          const data = {{}};
          form.querySelectorAll('input, textarea').forEach(el => {{
            if (el.type === 'checkbox') {{
              data[el.name] = el.checked;
            }} else if (el.tagName === 'TEXTAREA') {{
              data[el.name] = el.value.split('\\n');
            }} else {{
              data[el.name] = el.value;
            }}
          }});
          const res = await fetch('/api/settings', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify(data)
          }});
          const result = await res.json();
          const msg = document.getElementById('settingsMsg');
          if (result.errors && result.errors.length) {{
            msg.style.color = 'var(--red)'; msg.textContent = result.errors.join('; ');
          }} else {{
            msg.style.color = 'var(--green)'; msg.textContent = 'saved -- applied immediately, no restart needed';
          }}
        }}
        </script>
        """
        return render("Settings", "settings", body)

    @app.route("/api/field-config", methods=["GET", "POST"])
    def api_field_config():
        if request.method == "GET":
            return jsonify({"extractors": fields.load(cfg)})
        payload = request.get_json(silent=True) or {}
        errors = fields.save(cfg, payload.get("extractors", []))
        return jsonify({"ok": not errors, "errors": errors})

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "GET":
            return jsonify(settings.current_values(cfg))
        payload = request.get_json(silent=True) or {}
        errors = settings.save(cfg, payload)
        if not errors:
            monitor.resize_live_tail()
        return jsonify({"ok": not errors, "errors": errors})

    return app
