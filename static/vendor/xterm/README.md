xterm.js 5.5.0 and addon-fit 0.10.0, distributed under the accompanying MIT
licenses. These pinned browser files are bundled locally so Guardian works
without a CDN, npm, a build step, or an internet connection on the Pi.

Sources:
- https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz
- https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz

The archives were checked against their npm registry SHA-512 integrity values
before extracting these files. The terminal emulator runs in the browser;
the Guardian backend only keeps a bounded byte buffer and relays serial data.
