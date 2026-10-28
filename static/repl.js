/* Browser serial terminal. xterm.js renders locally; the Pi only moves bytes. */
class GuardianRepl {
  constructor({host, feedback, onStatus, onModeChange}) {
    Object.assign(this, {host, feedback, onStatus, onModeChange});
    this.active = false;
    this.ready = false;
    this.generation = 0;
    this.cursor = null;
    this.pending = new Uint8Array();
  }

  setStatus(status) {
    const wasActive = this.active;
    const wasReady = this.ready;
    const changedConnection = this.connection !== status.serial_connections;
    this.active = !!status.terminal_active;
    this.ready = this.active && status.monitoring && status.connected &&
      status.serial_open && !status.stop_armed;
    this.connection = status.serial_connections;
    this.session = status.terminal_session;
    if (changedConnection || wasActive !== this.active || !this.ready) {
      ++this.generation;
      this.pending = new Uint8Array();
    }
    if (wasActive !== this.active) {
      this.cursor = null;
      this.error = '';
      this.host.hidden = !this.active;
      this.feedback.hidden = !this.active;
      this.onModeChange(this.active);
      if (this.active) {
        this.open();
        this.terminal.reset();
        requestAnimationFrame(() => { this.fit.fit(); this.terminal.focus(); });
      }
    }
    if (this.terminal) {
      this.terminal.options.disableStdin = !this.ready;
      this.terminal.options.cursorBlink = this.ready;
      if (this.ready && !wasReady &&
          (this.host.contains(document.activeElement) || document.activeElement.id === 'replItem')) {
        this.terminal.focus();
      }
    }
    this.showFeedback();
  }

  open() {
    if (this.terminal) return;
    const style = getComputedStyle(this.host);
    this.terminal = new Terminal({
      fontFamily:style.fontFamily, fontSize:14, lineHeight:1.5,
      theme:{background:'#0a0c0d', foreground:'#cfe0d8', cursor:'#cfe0d8',
        selectionBackground:'#33413f'},
      cursorStyle:'block', scrollback:500, convertEol:true,
      disableStdin:true
    });
    this.fit = new FitAddon.FitAddon();
    this.terminal.loadAddon(this.fit);
    this.terminal.open(this.host);
    this.terminal.onData(data => this.enqueue(new TextEncoder().encode(data)));
    this.terminal.onBinary(data => this.enqueue(Uint8Array.from(data, c => c.charCodeAt(0) & 255)));
    this.terminal.attachCustomKeyEventHandler(event => {
      if (event.type === 'keydown' && event.ctrlKey && event.key.toLowerCase() === 'c' &&
          this.terminal.hasSelection() && navigator.clipboard) {
        event.preventDefault();
        navigator.clipboard.writeText(this.terminal.getSelection()).catch(() => {});
        return false;
      }
      return true;
    });
    this.resize = new ResizeObserver(() => {
      if (this.active && this.host.clientWidth && this.host.clientHeight) this.fit.fit();
    });
    this.resize.observe(this.host);
  }

  showFeedback() {
    this.feedback.textContent = this.error || (this.ready
      ? '# Ctrl-C interrupt · Ctrl-D restart · >_ leave terminal'
      : '# Waiting for the device — input disabled until connected');
  }

  async toggle() {
    const button = document.getElementById('replItem');
    button.disabled = true;
    try {
      const path = this.active ? '/api/device/repl/close' : '/api/device/repl';
      const response = await fetch(path, {method:'POST'});
      if (!response.ok) throw new Error('Could not change terminal mode.');
      this.onStatus(await response.json());
    } catch (error) {
      this.error = '# ' + error.message;
      this.showFeedback();
    } finally { button.disabled = false; }
  }

  async refresh(isCurrent) {
    const generation = this.generation;
    const query = this.cursor === null ? '' : '?after=' + this.cursor;
    const response = await fetch('/api/device/repl/output' + query, {cache:'no-store'});
    if (!response.ok) throw new Error('Terminal unavailable');
    const result = await response.json();
    if (generation !== this.generation || !isCurrent()) return;
    if (result.clipped) this.terminal.reset();
    if (result.data) {
      const bytes = Uint8Array.from(atob(result.data), char => char.charCodeAt(0));
      this.terminal.write(bytes);
    }
    this.cursor = result.cursor;
    this.onStatus(result.status);
  }

  enqueue(bytes) {
    if (!this.ready) return;
    if (this.pending.length + bytes.length > 65536) {
      this.error = '# Paste limited to 64 KiB. Send smaller sections.';
      this.showFeedback();
      return;
    }
    const next = new Uint8Array(this.pending.length + bytes.length);
    next.set(this.pending); next.set(bytes, this.pending.length);
    this.pending = next;
    if (!this.sending && !this.sendTimer) {
      this.sendTimer = setTimeout(() => { this.sendTimer = null; this.flush(); }, 16);
    }
  }

  async flush() {
    if (this.sending) return;
    this.sending = true;
    const generation = this.generation;
    try {
      while (this.ready && this.pending.length && generation === this.generation) {
        const bytes = this.pending.slice(0, 4096);
        this.pending = this.pending.slice(bytes.length);
        const response = await fetch('/api/device/repl/input', {
          method:'POST', headers:{'Content-Type':'application/octet-stream',
            'X-Serial-Connection':String(this.connection), 'X-Terminal-Session':String(this.session)},
          body:bytes
        });
        if (!response.ok) {
          const result = await response.json();
          throw new Error(result.error || 'Input was not sent.');
        }
        if (generation === this.generation) { this.error = ''; this.showFeedback(); }
      }
    } catch (error) {
      if (generation === this.generation) {
        this.pending = new Uint8Array();
        this.ready = false;
        this.terminal.options.disableStdin = true;
        this.error = '# ' + error.message + ' Pending input discarded.';
        this.showFeedback();
      }
    } finally {
      this.sending = false;
      if (this.ready && this.pending.length) this.flush();
    }
  }
}
