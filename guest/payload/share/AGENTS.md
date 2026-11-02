# Dedicated agent browser rules

- Code, Node processes, browser downloads and browser profiles belong to this VM.
- Use the configured `vm_browser` Playwright MCP server for this working browser.
  Do not switch to the client computer's built-in browser or desktop controls.
- The selected Wayland or XFCE display is shared by noVNC and the independent
  Chromium process. Reuse its existing work tabs. PVE console is the XFCE rescue desktop.
- Before any browser input, check whether manual takeover is active. A persistent
  `~/.local/state/onepve-agent/manual.lock` means the human owns this browser.
  Do not remove it, start another MCP process, attach another control tool or
  otherwise bypass it. Only the human's explicit Resume action releases it.
- During takeover, stop issuing browser commands. After explicit resume, reconnect
  MCP, list the open tabs and obtain a fresh snapshot before acting.
- At the end of a task or turn, keep work pages and the browser open. Do not call
  browser_close, close work tabs, send Alt+F4/Ctrl+W, kill Chromium, restart
  LightDM/XFCE or navigate away from a terminal simply as cleanup.
- If a page has a leave confirmation, cancel leaving and retain the page.
- Only close a work page when the user explicitly asks for that closure.
- Persisted profile data preserves cookies/storage; it is not a guarantee that
  unsaved page state survives a browser or VM restart.
