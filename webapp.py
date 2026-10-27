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
import math
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, render_template_string, request

import exporter
import fields
import labels
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

  /* Bigger, more lamp-like status indicator -- a proper panel light
     rather than a small UI dot. vertical-align: middle keeps it centered
     against the label text at this larger size. */
  .dot {
    width: 14px; height: 14px; border-radius: 50%; display: inline-block;
    margin-right: 0.5em; vertical-align: middle;
  }
  .dot.up { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.down { background: var(--red); box-shadow: 0 0 8px var(--red); }
  .dot.halt { background: var(--cyan); box-shadow: 0 0 8px var(--cyan); }
  .dot.pause { background: var(--amber); box-shadow: 0 0 8px var(--amber); }

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
  /* Single-line status readout -- deliberately never grows a second line
     (extra context lives in this element's title= tooltip instead), so
     the console strip's height never jumps around between states. */
  .status-readout {
    background: #0a0c0d; border: 1px solid var(--line); border-radius: 4px;
    padding: 0.45rem 0.8rem; font-size: 0.85rem; line-height: 1.4;
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

  /* Flashing state is a plain color, toggled on/off by a single shared
     JS timer (see BLINK_INTERVAL_MS below) rather than a per-element CSS
     animation -- a @keyframes animation restarts its own timeline
     whenever a class is first added, so two icons that started flashing
     at different moments would drift out of phase with each other
     forever. Driving both from one clock keeps them in lockstep. */
  .tb-icon.flash-on { color: var(--amber); filter: drop-shadow(0 0 4px var(--amber)); }

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

  /* Labels: free-standing notes pinned between sessions by timestamp, so
     they sit in the same place in the log whatever filter is active. */
  /* Let the last separator's control extend below its border without
     changing the spacing to the hint below the table. */
  .session-table { overflow-x:auto; padding-bottom:7px; margin-bottom:-7px; }
  .session-tools { display:flex; align-items:flex-start; gap:0.75rem; margin-bottom:0.75rem; }
  .session-tools .filters { flex:1; flex-wrap:wrap; margin:0; }
  .export-control { margin-left:auto; flex:none; }
  .export-control summary {
    list-style:none; padding:0.25rem 0.7rem; border:1px solid var(--line);
    border-radius:0; color:var(--ink-dim); cursor:pointer;
  }
  .export-control summary::-webkit-details-marker { display:none; }
  .export-control summary:hover { text-decoration:underline; }
  .export-control[open] summary { color:var(--bg); background:var(--ink); }
  .export-range { display:flex; gap:0.6rem; flex-wrap:wrap; }
  .export-range input { width:8em; background:var(--bg); color:var(--ink); border:1px solid var(--line); padding:0.4rem; font:inherit; }
  .session-toggle { background:none; border:0; padding:0; font:inherit; color:inherit; cursor:pointer; }
  .session-row { cursor:pointer; }
  .session-row td { vertical-align:middle; }
  .session-row:has([aria-expanded=true]) td { background:var(--panel); }
  tr.session-log td { padding:0; }
  tr.session-log pre.term { height:auto; max-height:55vh; border-left:2px solid var(--cyan); }
  .log-message { padding:0.75rem; color:var(--ink-dim); }
  /* Insertion controls overlay the existing border. Closed gaps occupy no
     space, so every session keeps the table's original vertical rhythm. */
  tr.label-gap td { padding:0; height:0; border:0; line-height:0; }
  .label-gap-anchor { position:relative; height:0; }
  .label-insert {
    position:absolute; top:-7px; left:0; width:100%; height:14px; padding:0;
    background:none; border:0; color:var(--ink-dim); cursor:pointer; opacity:0;
    font:inherit; font-size:14px; line-height:14px; text-align:left; z-index:1;
  }
  .label-insert span { display:block; width:2ch; background:var(--bg); }
  .label-insert:hover, .label-insert:focus-visible { opacity:1; }
  .label-gap:has(.label-editor) .label-gap-anchor { height:auto; }
  .label-editor { display:flex; gap:0.5rem; padding:0.5rem 0; flex-wrap:wrap; line-height:1.5; }
  .label-editor input { flex:1; min-width:10em; background:var(--panel); color:var(--ink);
    border:1px solid var(--line); padding:0.4rem; font:inherit; }
  @media (hover:none) { .label-insert { opacity:1; } }
  [hidden] { display:none !important; }
  tr.label-row td {
    background: rgba(232,182,75,0.09); border-left: 3px solid var(--amber);
    color: var(--ink); padding-top: 0.4rem; padding-bottom: 0.4rem;
  }
  tr.label-row:hover td { background: rgba(232,182,75,0.14); }
  .lbl-time { color: var(--ink-dim); font-size: 0.78rem; margin: 0 0.6rem; }
  .lbl-actions { float: right; }
  .lbl-btn {
    font-family: var(--mono); font-size: 0.75rem; background: none; color: var(--ink-dim);
    border: none; cursor: pointer; padding: 0 0.4rem;
  }
  .lbl-btn:hover { color: var(--cyan); }
  .lbl-btn.del:hover { color: var(--red); }

  /* Export panel */
  .export-opts { display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem 1.3rem; }
  .export-opts label { display: inline-flex; align-items: center; gap: 0.4rem; cursor: pointer; }
  .export-opts select {
    font-family: var(--mono); font-size: 0.85rem; background: var(--bg); color: var(--ink);
    border: 1px solid var(--line); border-radius: 4px; padding: 0.25rem 0.4rem;
  }
  details.panel summary { cursor: pointer; }

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
            f'<div class="n" id="stat-{k}" style="color:{STATUS_COLORS[k]}">{v}</div>'
            f'<div class="l">{k}</div></div></a>'
            for k, v in st["stats"].items()
        )
        exc_markers_json = json.dumps(cfg.EXCEPTION_MARKERS)
        body = f"""
        <div class="stats">{stats_html}</div>

        <div class="console">
          <div class="console-strip">
            <div class="status-readout" id="statusReadout">
              <p id="connLine" style="margin:0"></p>
            </div>
            <div class="toolbar">
              <div class="tb-group">
                <div class="tb-label">Device</div>
                <div class="tb-buttons">
                  <button id="stopItem" class="tb-icon"
                          title="Stop: halt the board at the REPL on its next update instead of sleeping. Click again while armed (flashing) to cancel."
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
                          title="Pause monitoring: free the serial port for mpremote or a terminal. If a device stop is pending, waits for it to complete first. Click again while queued (flashing) to cancel just the pause."
                          aria-label="Pause monitoring" onclick="doPauseMon()">{ICONS['pause']}</button>
                  <button id="resumeMonItem" class="tb-icon" style="display:none"
                          title="Resume monitoring" aria-label="Resume monitoring"
                          onclick="doResumeMon()">{ICONS['play']}</button>
                  <button class="tb-icon danger"
                          title="Erase all session logs, the index and all labels (cannot be undone)"
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

        let lastStatus = null;
        function applyBlink() {{
          if (!lastStatus) return;
          const stopShouldFlash = !!lastStatus.stop_armed && !lastStatus.halted;
          const pauseShouldFlash = !!lastStatus.pause_pending;
          document.getElementById('stopItem').classList.toggle('flash-on', stopShouldFlash && blinkOn);
          document.getElementById('pauseMonItem').classList.toggle('flash-on', pauseShouldFlash && blinkOn);
        }}
        let blinkOn = false;
        setInterval(() => {{ blinkOn = !blinkOn; applyBlink(); }}, 500);

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
          if (s.pause_pending) {{
            sub = (sub ? sub + ' -- ' : '') + 'pause queued, waiting for the stop to complete';
          }}

          document.getElementById('connLine').innerHTML =
            '<span class="dot ' + dot + '"></span>' + label +
            ' &nbsp;&middot;&nbsp; last session #' + s.last_session_id;

          // Big counters: update in place on every poll, so they tick up
          // as sessions finish (and reset to 0 after an erase) without a
          // page reload.
          if (s.stats) {{
            for (const k in s.stats) {{
              const el = document.getElementById('stat-' + k);
              const v = String(s.stats[k]);
              if (el && el.textContent !== v) el.textContent = v;
            }}
          }}

          // Single line, always -- extra context lives in the hover
          // tooltip instead of a second line, so the console strip's
          // height never jumps around between states.
          const readout = document.getElementById('statusReadout');
          if (sub) {{ readout.title = sub; }} else {{ readout.removeAttribute('title'); }}

          document.getElementById('stopItem').style.display = s.halted ? 'none' : 'inline-flex';
          document.getElementById('resumeItem').style.display = s.halted ? 'inline-flex' : 'none';
          document.getElementById('pauseMonItem').style.display = s.monitoring ? 'inline-flex' : 'none';
          document.getElementById('resumeMonItem').style.display = s.monitoring ? 'none' : 'inline-flex';

          // Lamp states, like indicator lights on the panel rather than
          // relying on text alone:
          document.getElementById('resetItem').classList.toggle('lit', !!s.connected);
          document.getElementById('resumeItem').classList.toggle('lit-cyan', !!s.halted);
          document.getElementById('resumeMonItem').classList.toggle('lit-amber', !s.monitoring);

          // Flashing (Stop while armed-not-halted, Pause while a pause is
          // queued behind it) is driven by the shared blink clock below
          // so the two always stay in phase with each other -- see applyBlink().
          lastStatus = s;
          applyBlink();
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
          if (!confirm('Permanently erase ALL session logs, the index and all labels? This cannot be undone.')) return;
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
        labels.clear(cfg)   # labels annotate that history; numbering restarts too
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
        records, floor = monitor.recent_sessions_window(n=200, status=status)
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
        ncols = 4 + len(extractors)

        # Labels are pinned by timestamp, NOT by which sessions happen to be
        # on screen: every label newer than the oldest line the view scanned
        # is shown, and it's slotted in just above the first session that
        # started before it. That's why a label stays put under any status
        # filter -- filtering only removes session rows around it.
        pending_labels = sorted(
            (l for l in labels.load(cfg) if l["ts"] >= floor),
            key=lambda l: l["ts"], reverse=True,
        )

        def label_row(l):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(l["ts"]))
            return (
                f'<tr class="label-row" data-label-id="{l["id"]}"><td colspan="{ncols}">'
                f'<span class="lbl-time">{when}</span>'
                f'<span class="lbl-text">{esc(l["text"])}</span>'
                f'<span class="lbl-actions">'
                f'<button type="button" class="lbl-btn" onclick="editLabel({l["id"]})">edit</button>'
                f'<button type="button" class="lbl-btn del" title="Delete label" '
                f'onclick="deleteLabel({l["id"]})">&times;</button>'
                f'</span></td></tr>'
            )

        def gap(ts):
            return (
                f'<tr class="label-gap"><td colspan="{ncols}"><div class="label-gap-anchor">'
                f'<button type="button" class="label-insert" data-ts="{ts}" '
                f'aria-label="Add label here" title="Add label here"><span>+</span></button></div></td></tr>'
            )

        rows = ""
        # Merge labels and sessions before adding insertion points. A midpoint
        # timestamp anchors each strip in the timeline, including filtered views.
        timeline = [(r["start"], "session", r) for r in records]
        timeline += [(l["ts"], "label", l) for l in pending_labels]
        timeline.sort(key=lambda item: item[0], reverse=True)
        for i, (ts, kind, item) in enumerate(timeline):
            above = timeline[i-1][0] if i else max(time.time(), ts + 0.002)
            rows += gap((above + ts) / 2)
            if kind == "label":
                rows += label_row(item)
                continue
            r = item
            color = STATUS_COLORS[r["status"]]
            text = monitor.read_session_text(r["id"]) if extractors else None
            values = fields.extract(text, extractors)
            extra_cells = "".join(f'<td class="extra">{esc(values[e["name"]])}</td>' for e in extractors)
            rows += (
                f'<tr class="session-row" data-session-id="{r["id"]}">'
                f'<td><button type="button" class="session-toggle" title="View session" '
                f'aria-label="View session {r["id"]}" aria-expanded="false" '
                f'aria-controls="log-{r["id"]}">#{r["id"]}</button></td>'
                f'<td><span class="badge" style="background:{color}22;color:{color}">{r["status"]}</span></td>'
                f'<td>{r["duration"]}s</td><td>{r["lines"]} lines</td>{extra_cells}</tr>'
                f'<tr class="session-log" id="log-{r["id"]}" hidden><td colspan="{ncols}"></td></tr>'
            )
        rows += gap(max(0, timeline[-1][0] - 0.002) if timeline else time.time())
        if not records:
            rows += f'<tr><td colspan="{ncols}">no sessions recorded yet</td></tr>'

        filter_name = status.title() if status else "All"
        body = f"""
        <div class="session-tools">
          <div class="filters">{filters}</div>
          <details class="export-control" id="exportPanel">
            <summary aria-label="Export options" aria-controls="exportOptions">Export</summary>
          </details>
        </div>
        <div class="panel export-panel" id="exportOptions" hidden>
          <form id="exportForm" method="post" action="/export">
            <input type="hidden" name="status" value="{status or ''}">
            <p class="hint">{filter_name} sessions in history. Leave the range blank to export this filter.</p>
            <div class="export-opts">
              <div class="export-range">
                <label>From # <input type="number" name="from_id" min="1" step="1" aria-label="First session number"></label>
                <label>To # <input type="number" name="to_id" min="1" step="1" aria-label="Last session number"></label>
              </div>
              <label>Format <select name="format"><option value="json">JSON</option><option value="txt">Plain text</option></select></label>
              <input type="hidden" name="include_labels" value="0">
              <label><input type="checkbox" name="include_labels" value="1" checked> Include labels</label>
              <input type="hidden" name="include_logs" value="0">
              <label><input type="checkbox" name="include_logs" value="1" checked> Include raw log text</label>
              <button class="btn" type="submit">Export</button>
            </div>
            <p class="hint">Range endpoints are included; the status filter always applies.
            Pruned logs export as metadata only.</p>
          </form>
        </div>
        <div class="session-table"><table>
          <thead><tr><th>id</th><th>status</th><th>duration</th><th>size</th>{extra_headers}</tr></thead>
          <tbody>{rows}</tbody>
        </table></div>
        <p class="hint" style="margin-top:0.9rem">Extra columns are configured on the
          <a href="/settings">Settings</a> page.</p>
        <script>
        document.getElementById('exportPanel').addEventListener('toggle', e => {{
          document.getElementById('exportOptions').hidden = !e.target.open;
        }});
        document.getElementById('exportForm').addEventListener('submit', e => {{
          const from = e.target.elements.from_id, to = e.target.elements.to_id;
          if (from.value && to.value && Number(from.value) > Number(to.value)) {{
            e.preventDefault(); to.setCustomValidity('Last session must be at least the first.'); to.reportValidity();
          }}
        }});
        document.querySelectorAll('.export-range input').forEach(input => input.addEventListener('input', () => {{
          document.querySelector('[name=to_id]').setCustomValidity('');
        }}));
        let logRequest = 0;
        document.querySelectorAll('.session-row').forEach(row => {{
          const button = row.querySelector('button');
          row.addEventListener('click', async () => {{
            const requestId = ++logRequest;
            const card = document.getElementById(button.getAttribute('aria-controls'));
            const opening = card.hidden;
            // One log at a time; release the previous DOM text on close.
            document.querySelectorAll('.session-log').forEach(log => {{ log.hidden = true; log.firstElementChild.replaceChildren(); }});
            document.querySelectorAll('.session-toggle').forEach(b => b.setAttribute('aria-expanded', 'false'));
            if (!opening) return;
            card.hidden = false; button.setAttribute('aria-expanded', 'true');
            const cell = card.firstElementChild;
            cell.textContent = 'Loading session…';
            try {{
              const res = await fetch('/api/sessions/' + row.dataset.sessionId + '/log');
              if (!res.ok) throw new Error('Could not load session. Close and reopen to retry.');
              const data = await res.json();
              if (card.hidden || requestId !== logRequest) return;
              const pre = document.createElement('pre'); pre.className = 'term';
              pre.textContent = data.log === null ? 'Raw log unavailable (pruned or missing).' : data.log;
              if (data.truncated) pre.textContent += '\\n… Preview limited to 64K characters. Export this session for the full log.';
              cell.replaceChildren(pre);
            }} catch (err) {{ if (!card.hidden && requestId === logRequest) cell.textContent = err.message; }}
          }});
          // Native delayed tooltip follows the pointer without a timer loop.
          row.title = 'View session';
        }});
        async function labelCall(url, payload) {{
          const res = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(payload || {{}})}});
          return res.json();
        }}
        document.querySelectorAll('.label-insert').forEach(button => button.addEventListener('click', () => {{
          document.querySelectorAll('.label-editor').forEach(editor => editor.remove());
          const form = document.createElement('form'); form.className = 'label-editor';
          const input = document.createElement('input'); input.type = 'text'; input.maxLength = {labels.MAX_LEN};
          input.required = true; input.placeholder = 'Write a label…'; input.setAttribute('aria-label', 'Label text');
          const save = document.createElement('button'); save.className = 'btn'; save.textContent = 'Add label';
          const cancel = document.createElement('button'); cancel.className = 'btn'; cancel.type = 'button'; cancel.textContent = 'Cancel';
          const close = () => {{ form.remove(); button.focus(); }};
          cancel.onclick = close;
          form.addEventListener('keydown', e => {{ if (e.key === 'Escape') {{ e.preventDefault(); close(); }} }});
          form.append(input, save, cancel); button.parentElement.append(form); input.focus();
          form.addEventListener('submit', async e => {{
            e.preventDefault(); save.disabled = true;
            try {{
              const data = await labelCall('/api/labels', {{text:input.value, ts:Number(button.dataset.ts)}});
              if (data.ok) location.reload(); else alert(data.error || 'Could not add label');
            }} catch (err) {{ alert('Could not save label. Please retry.'); }}
            finally {{ save.disabled = false; }}
          }});
        }}));
        async function editLabel(id) {{
          const row = document.querySelector('tr[data-label-id="' + id + '"]');
          const cur = row.querySelector('.lbl-text').textContent;
          const text = prompt('Edit label', cur);
          if (text === null || text.trim() === cur) return;
          try {{
            const data = await labelCall('/api/labels/' + id, {{text:text}});
            if (data.ok) location.reload(); else alert(data.error || 'Could not edit label');
          }} catch (err) {{ alert('Could not edit label. Please retry.'); }}
        }}
        async function deleteLabel(id) {{
          if (!confirm('Delete this label?')) return;
          try {{
            const data = await labelCall('/api/labels/' + id + '/delete');
            if (data.ok) location.reload(); else alert(data.error || 'Could not delete label');
          }} catch (err) {{ alert('Could not delete label. Please retry.'); }}
        }}
        </script>
        """
        return render("Sessions", "sessions", body)

    # ------------------------------------------------------------------ #
    # Labels API
    # ------------------------------------------------------------------ #
    @app.route("/api/labels", methods=["GET", "POST"])
    def api_labels():
        if request.method == "GET":
            return jsonify({"labels": labels.load(cfg)})
        payload = request.get_json(silent=True) or {}
        ts = payload.get("ts")
        if ts is not None:
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts < 0:
                return jsonify({"ok": False, "error": "invalid label position"}), 400
        label, err = labels.add(cfg, payload.get("text"), ts=ts)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "label": label})

    @app.route("/api/labels/<int:lid>", methods=["POST"])
    def api_label_edit(lid):
        payload = request.get_json(silent=True) or {}
        label, err = labels.update(cfg, lid, payload.get("text"))
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "label": label})

    @app.route("/api/labels/<int:lid>/delete", methods=["POST"])
    def api_label_delete(lid):
        err = labels.delete(cfg, lid)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #
    @app.route("/export", methods=["POST"])
    def export_sessions():
        def flag(name):
            # Unchecked checkboxes send nothing, so the form pairs each one
            # with a hidden "0". A request that omits the field entirely
            # (curl, a script) gets the default: on.
            vals = request.form.getlist(name)
            return (not vals) or ("1" in vals)

        status = request.form.get("status") or None
        if status is not None and status not in STATUS_COLORS:
            return "invalid status filter", 400
        try:
            lo = int(request.form["from_id"]) if request.form.get("from_id") else None
            hi = int(request.form["to_id"]) if request.form.get("to_id") else None
        except ValueError:
            return "invalid session range", 400
        if (lo is not None and lo < 1) or (hi is not None and hi < 1) or (lo is not None and hi is not None and lo > hi):
            return "invalid session range", 400

        fmt = "txt" if request.form.get("format") == "txt" else "json"
        gen, filename, mimetype = exporter.build(
            monitor, cfg, status=status, from_id=lo, to_id=hi, fmt=fmt,
            with_labels=flag("include_labels"), with_logs=flag("include_logs"),
        )
        return Response(gen, mimetype=mimetype, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        })

    @app.route("/api/sessions/<int:sid>/log")
    def session_log(sid):
        if monitor.find_record(sid) is None:
            return jsonify({"error": "unknown session"}), 404
        limit = 64 * 1024
        text = monitor.read_session_text(sid, max_chars=limit + 1)
        return jsonify({"log": text[:limit] if text is not None else None,
                        "truncated": text is not None and len(text) > limit})

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
                                       "SOFT_RESET_DELAY", "BOOT_TIMEOUT"]),
            ("Hardware reset (GPIO)", ["ENABLE_GPIO_RESET", "GPIO_RESET_PIN", "GPIO_RESET_PULSE"]),
            ("Storage & UI", ["NORMAL_LOG_RETENTION", "LIVE_TAIL_LINES",
                               "MONITORING_PAUSE_POLL_INTERVAL", "MIN_FREE_DISK_MB"]),
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
