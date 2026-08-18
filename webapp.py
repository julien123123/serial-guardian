"""
Flask web UI for the serial guardian.

Kept deliberately light (inline templates, no build step, no CDN
dependencies) since this runs on a Pi Zero W and needs to work even with
no internet access once it's set up.
"""

from flask import Flask, jsonify, render_template_string, request

STATUS_COLORS = {
    "NORMAL": "#63d68a",
    "ANOMALY": "#e8b64b",
    "EXCEPTION": "#e55a5a",
    "STOPPED": "#5bc9c9",
}

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
  a:focus-visible, button:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }

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

  main { padding: 1.25rem; max-width: 980px; margin: 0 auto; }

  .stats { display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .stat {
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
    padding: 0.6rem 0.9rem; min-width: 8em;
  }
  .stat .n { font-size: 1.4rem; font-weight: 600; }
  .stat .l { color: var(--ink-dim); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; }

  .controls { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }
  button.btn {
    font-family: var(--mono); font-size: 0.85rem; background: var(--panel); color: var(--ink);
    border: 1px solid var(--line); border-radius: 6px; padding: 0.45rem 0.9rem; cursor: pointer;
  }
  button.btn:hover { border-color: var(--cyan); color: var(--cyan); }
  button.btn.danger:hover { border-color: var(--red); color: var(--red); }
  button.btn:disabled { opacity: 0.45; cursor: default; }
  #stateNote { color: var(--ink-dim); font-size: 0.85rem; }

  pre.term {
    background: #0a0c0d; border: 1px solid var(--line); border-radius: 6px;
    padding: 0.9rem 1rem; height: 58vh; overflow-y: auto;
    white-space: pre-wrap; word-break: break-word; margin: 0;
  }
  pre.term .l-exc { border-left: 3px solid var(--red); padding-left: 0.5em; color: #f3a9a9; }
  pre.term .l-ok  { border-left: 3px solid var(--green); padding-left: 0.5em; }

  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--line); }
  th { color: var(--ink-dim); font-weight: 500; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.08em; }
  tr:hover td { background: rgba(255,255,255,0.02); }

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

    @app.route("/")
    def live():
        st = monitor.get_status()
        stats_html = "".join(
            f'<div class="stat"><div class="n" style="color:{STATUS_COLORS[k]}">{v}</div>'
            f'<div class="l">{k}</div></div>'
            for k, v in st["stats"].items()
        )
        body = f"""
        <p id="connLine"></p>
        <div class="stats">{stats_html}</div>
        <div class="controls">
          <button id="stopBtn" class="btn danger" onclick="doStop()">Stop device on next update</button>
          <button id="resumeBtn" class="btn" onclick="doResume()" style="display:none">Resume</button>
          <span id="stateNote"></span>
        </div>
        <pre class="term" id="tail"></pre>
        <script>
        function fmt(line) {{
          const esc = line.replace(/&/g,'&amp;').replace(/</g,'&lt;');
          if (line.includes('Traceback') || line.includes('MemoryError') ||
              line.includes('Exception handler: caught exception')) {{
            return '<span class="l-exc">' + esc + '</span>';
          }}
          if (line.includes('{cfg.GOTOSLEEP_MARKER}')) {{
            return '<span class="l-ok">' + esc + '</span>';
          }}
          return esc;
        }}
        function applyStatus(s) {{
          const dot = s.halted ? 'halt' : (s.connected ? 'up' : 'down');
          const label = s.halted ? 'halted at REPL' : (s.connected ? 'connected' : 'disconnected');
          document.getElementById('connLine').innerHTML =
            '<span class="dot ' + dot + '"></span>' + label +
            ' &nbsp;&middot;&nbsp; last session #' + s.last_session_id;

          const stopBtn = document.getElementById('stopBtn');
          const resumeBtn = document.getElementById('resumeBtn');
          const note = document.getElementById('stateNote');
          if (s.halted) {{
            stopBtn.style.display = 'none';
            resumeBtn.style.display = 'inline-block';
            note.textContent = 'normal wake cycle is paused -- it will not go back to sleep on its own';
          }} else {{
            resumeBtn.style.display = 'none';
            stopBtn.style.display = 'inline-block';
            note.textContent = s.stop_armed ? 'armed -- will halt the next time it wakes' : '';
          }}
        }}
        async function doStop() {{
          document.getElementById('stopBtn').disabled = true;
          try {{ await fetch('/api/stop', {{method:'POST'}}); }} finally {{
            document.getElementById('stopBtn').disabled = false;
          }}
        }}
        async function doResume() {{
          document.getElementById('resumeBtn').disabled = true;
          try {{ await fetch('/api/resume', {{method:'POST'}}); }} finally {{
            document.getElementById('resumeBtn').disabled = false;
          }}
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

    @app.route("/sessions")
    def sessions():
        status = request.args.get("status")
        if status not in (None, "NORMAL", "ANOMALY", "EXCEPTION", "STOPPED"):
            status = None
        records = monitor.recent_sessions(n=200, status=status)

        def link(label, val):
            cls = "active" if status == val else ""
            href = "/sessions" + (f"?status={val}" if val else "")
            return f'<a class="{cls}" href="{href}">{label}</a>'

        filters = (
            link("All", None) + link("Normal", "NORMAL") + link("Anomaly", "ANOMALY")
            + link("Exception", "EXCEPTION") + link("Stopped", "STOPPED")
        )

        rows = ""
        for r in records:
            color = STATUS_COLORS[r["status"]]
            rows += (
                f'<tr onclick="location.href=\'/sessions/{r["id"]}\'" style="cursor:pointer">'
                f'<td>#{r["id"]}</td>'
                f'<td><span class="badge" style="background:{color}22;color:{color}">{r["status"]}</span></td>'
                f'<td>{r["duration"]}s</td>'
                f'<td>{r["lines"]} lines</td>'
                f'<td><a href="/sessions/{r["id"]}">view</a></td>'
                f'</tr>'
            )
        body = f"""
        <div class="filters">{filters}</div>
        <table>
          <tr><th>id</th><th>status</th><th>duration</th><th>size</th><th></th></tr>
          {rows or '<tr><td colspan="5">no sessions recorded yet</td></tr>'}
        </table>
        """
        active = "exceptions" if status == "EXCEPTION" else "sessions"
        return render("Sessions", active, body)

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

        esc = text.replace("&", "&amp;").replace("<", "&lt;")
        body = f"""
        {meta}
        <div class="session-nav">
          <a href="/sessions/{sid-1}">&larr; session #{sid-1} (before)</a>
          <a href="/sessions/{sid+1}">session #{sid+1} (after) &rarr;</a>
        </div>
        <pre class="term" style="height:70vh">{esc}</pre>
        """
        return render(f"Session #{sid}", "sessions", body)

    return app
