"""
Flask web UI for the serial guardian.

Kept deliberately light (inline templates, no build step, no CDN
dependencies) since this runs on a Pi Zero W and needs to work even with
no internet access once it's set up.

NOTE ON EXPOSURE: none of this has any authentication. Everything below
-- including "stop the whole service", "pause monitoring", and "erase all
sessions" -- is reachable by anyone who can reach the Pi on your network.
Fine on a trusted home LAN, worth knowing if that's not your situation.
"""
import json
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template_string, request

import fields

STATUS_COLORS = {
    "NORMAL": "#63d68a",
    "ANOMALY": "#e8b64b",
    "EXCEPTION": "#e55a5a",
    "STOPPED": "#5bc9c9",
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

  .topbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }
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

  /* Right-aligned action toolbar: one-word label per group, short buttons,
     full explanation lives in the title= tooltip rather than on the page. */
  .toolbar { display: flex; gap: 0.9rem; flex-wrap: wrap; justify-content: flex-end; }
  .tb-group {
    display: flex; flex-direction: column; align-items: flex-start; gap: 0.3rem;
    padding-left: 0.9rem; border-left: 1px solid var(--line);
  }
  .tb-group:first-child { padding-left: 0; border-left: none; }
  .tb-label {
    font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-dim);
  }
  .tb-buttons { display: flex; gap: 0.35rem; }
  .tb-btn {
    font-family: var(--mono); font-size: 0.78rem; background: var(--panel); color: var(--ink);
    border: 1px solid var(--line); border-radius: 5px; padding: 0.3rem 0.6rem; cursor: pointer;
  }
  .tb-btn:hover { border-color: var(--cyan); color: var(--cyan); }
  .tb-btn.danger:hover { border-color: var(--red); color: var(--red); }

  pre.term {
    background: #0a0c0d; border: 1px solid var(--line); border-radius: 6px;
    padding: 0.9rem 1rem; height: 55vh; overflow-y: auto;
    white-space: pre-wrap; word-break: break-word; margin: 0;
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

  details.fc summary { cursor: pointer; color: var(--ink-dim); margin-bottom: 0.6rem; }
  details.fc[open] summary { color: var(--ink); }
  .fc table input {
    font-family: var(--mono); background: var(--bg); color: var(--ink);
    border: 1px solid var(--line); border-radius: 4px; padding: 0.3rem 0.5rem; width: 100%;
  }
</style>
</head>
<body>
<header>
  <div class="brand">serial-guardian<span class="cursor"></span></div>
  <nav>
    <a href="/" class="{{ 'active' if active=='live' else '' }}">Live</a>
    <a href="/sessions" class="{{ 'active' if active=='sessions' else '' }}">Sessions</a>
    <a href="/sessions?status=EXCEPTION" class="{{ 'active' if active=='exceptions' else '' }}">Exceptions</a>
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
        service_stop_title = (
            "Stop the guardian service entirely. This page goes down too.\n"
            f"Restart over SSH: sudo systemctl start {cfg.SERVICE_NAME}"
        )
        body = f"""
        <div class="topbar">
          <div>
            <p id="connLine" style="margin:0"></p>
            <p id="subLine" class="hint" style="margin:0.25rem 0 0"></p>
          </div>
          <div class="toolbar">
            <div class="tb-group">
              <div class="tb-label">Device</div>
              <div class="tb-buttons">
                <button id="stopItem" class="tb-btn"
                        title="Send Ctrl-C so the board halts at the REPL on its next update instead of sleeping"
                        onclick="doStop()">Stop</button>
                <button id="resumeItem" class="tb-btn" style="display:none"
                        title="Send Ctrl-D to resume normal operation"
                        onclick="doResume()">Resume</button>
                <button class="tb-btn"
                        title="Reboot the board now, whatever state it's in (halted, running, or asleep -- uses the hardware reset line if one is wired and it's not connected)"
                        onclick="doResetDevice()">Reset</button>
              </div>
            </div>
            <div class="tb-group">
              <div class="tb-label">Monitoring</div>
              <div class="tb-buttons">
                <button id="pauseMonItem" class="tb-btn"
                        title="Stop touching the serial port so mpremote or a terminal can use it. Nothing is logged while paused."
                        onclick="doPauseMon()">Pause</button>
                <button id="resumeMonItem" class="tb-btn" style="display:none"
                        title="Reopen the serial port and resume monitoring"
                        onclick="doResumeMon()">Resume</button>
              </div>
            </div>
            <div class="tb-group">
              <div class="tb-label">Service</div>
              <div class="tb-buttons">
                <button class="tb-btn"
                        title="Restart the guardian service. Brief interruption, reconnects on its own."
                        onclick="doService('restart')">Restart</button>
                <button class="tb-btn danger" title="{service_stop_title}"
                        onclick="doService('stop')">Stop</button>
              </div>
            </div>
            <div class="tb-group">
              <div class="tb-label">Data</div>
              <div class="tb-buttons">
                <button class="tb-btn danger"
                        title="Permanently delete every session log and the index. Cannot be undone."
                        onclick="doEraseSessions()">Erase</button>
              </div>
            </div>
          </div>
        </div>

        <div class="stats">{stats_html}</div>
        <pre class="term" id="tail"></pre>
        <script>
        function fmt(line) {{
          const escd = line.replace(/&/g,'&amp;').replace(/</g,'&lt;');
          if (line.includes('Traceback') || line.includes('MemoryError') ||
              line.includes('Exception handler: caught exception')) {{
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

          document.getElementById('stopItem').style.display = s.halted ? 'none' : 'inline-block';
          document.getElementById('resumeItem').style.display = s.halted ? 'inline-block' : 'none';
          document.getElementById('pauseMonItem').style.display = s.monitoring ? 'inline-block' : 'none';
          document.getElementById('resumeMonItem').style.display = s.monitoring ? 'none' : 'inline-block';
        }}

        async function doStop() {{ await fetch('/api/stop', {{method:'POST'}}); }}
        async function doResume() {{ await fetch('/api/resume', {{method:'POST'}}); }}
        async function doResetDevice() {{
          if (!confirm('Reset the device now?')) return;
          await fetch('/api/device/reset', {{method:'POST'}});
        }}
        async function doPauseMon() {{
          if (!confirm('Pause monitoring? This frees {cfg.SERIAL_PORT} for mpremote or another tool -- nothing will be logged until you resume.')) return;
          await fetch('/api/monitoring/pause', {{method:'POST'}});
        }}
        async function doResumeMon() {{ await fetch('/api/monitoring/resume', {{method:'POST'}}); }}
        async function doService(action) {{
          const msgs = {{
            restart: 'Restart the guardian service now? This briefly interrupts monitoring.',
            stop: 'Stop the guardian service? This page goes unreachable until you SSH in and run:\\nsudo systemctl start {cfg.SERVICE_NAME}'
          }};
          if (!confirm(msgs[action])) return;
          try {{ await fetch('/api/service/' + action, {{method:'POST'}}); }} catch (e) {{}}
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
        _delayed_systemctl("stop")
        return jsonify({"ok": True, "action": "stop"})

    # ------------------------------------------------------------------ #
    # Sessions page, with configurable extra columns
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

        fc_rows_json = json.dumps(extractors)
        body = f"""
        <div class="filters">{filters}</div>
        <table>
          <tr><th>id</th><th>status</th><th>duration</th><th>size</th>{extra_headers}<th></th></tr>
          {rows or f'<tr><td colspan="{5 + len(extractors)}">no sessions recorded yet</td></tr>'}
        </table>

        <details class="fc panel" style="margin-top:1.25rem">
          <summary>Configure extra columns ({len(extractors)})</summary>
          <table id="fcTable">
            <tr><th>Column</th><th>Regex (exactly one capture group)</th><th></th></tr>
          </table>
          <div class="controls" style="margin-top:0.6rem">
            <button class="btn" onclick="fcAddRow()">+ add column</button>
            <button class="btn" onclick="fcSave()">Save</button>
            <span id="fcMsg"></span>
          </div>
          <div class="hint">Changes apply immediately to sessions already on disk, not just new ones --
            these are read from the raw log at view time, not baked in when a session finishes.</div>
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
            msg.style.color = 'var(--green)'; msg.textContent = 'saved -- reloading...';
            setTimeout(() => location.reload(), 500);
          }}
        }}
        fcRender();
        </script>
        """
        active = "exceptions" if status == "EXCEPTION" else "sessions"
        return render("Sessions", active, body)

    @app.route("/api/field-config", methods=["GET", "POST"])
    def api_field_config():
        if request.method == "GET":
            return jsonify({"extractors": fields.load(cfg)})
        payload = request.get_json(silent=True) or {}
        errors = fields.save(cfg, payload.get("extractors", []))
        return jsonify({"ok": not errors, "errors": errors})

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

    return app
