# Debian 13 Agent guest payload

The PVE installer copies this directory into the new VM and runs, as guest root:

```bash
bash guest/install.sh --manifest /root/onepve-agent.json
```

This installer never changes PVE, host drivers, a client computer, or an existing
browser profile/keyring. It targets Debian13 amd64 with at least4vCPU, fixed8GiB,
and an expanded root filesystem on a disk of at least 32 GiB (the new-VM default).
Cloud-init supplies the user's SSH
public key; passwords, private keys and Codex login tokens do not enter the image.

The JSON manifest fields and defaults are:

```json
{
  "user": "agent",
  "node_version": "26.13.0",
  "mcp_version": "0.0.83",
  "fnm_version": "v1.39.0",
  "codex_version": "0.166.0",
  "viewer_port": 16080,
  "start_desktop": true,
  "gpu_bdf": "",
  "fnm_sha256": "",
  "cache_dir": ""
}
```

The optional runtime cache is prepared **before deleting the old VM**. Its layout
is `runtime/fnm.zip`, `runtime/node.tar.xz`, and `npm/` containing the complete npm
prefix with both pinned packages and its lockfile. `runtime-manifest.json` records
the four exact version fields plus a `files` map of each regular file's relative
path to SHA256 and a `symlinks` map of link paths to relative targets. Unrecorded,
missing, changed files and nonportable/escaping
symlinks are rejected before installation. The official Node archive is installed
into fnm's version directory and still checked through `fnm install/default/exec`.
Without a cache, the same fixed official releases are downloaded over HTTPS.

## Runtime

- Root-owned program/config: `/opt/onepve-agent`, `/etc/onepve-agent`.
- User services: `onepve-agent-{keyring,fixture,chromium,mcp,wayland,vnc,novnc,controls}`.
- Profile: `~/.local/share/onepve-agent/chromium`.
- Persistent manual gate: `~/.local/state/onepve-agent/manual.lock`.
- Code: `~/workspaces`; Node/fnm: `~/.local/share/fnm`.

The installer seeds **Continue where you left off before the first Chromium
launch**, and creates an empty-password GNOME keyring only when no keyring exists.
That is the chosen dedicated-VM autologin policy; stored site credentials are not
protected by a keyring password at rest. Existing profile data and keyrings are
never seeded again. Chromium keeps its standard sandbox and credential backend.
For the newly seeded collection, a startup service calls the standard Secret
Service `Unlock` before Chromium, requiring a prompt-free result and an unlocked
default collection. It never reads stored secrets or invokes a password dialog.

All managed commands use fnm's absolute path and the exact Node version file:

```bash
/opt/onepve-agent/bin/agent-node node --version
/opt/onepve-agent/bin/agent-codex login --device-auth
/opt/onepve-agent/bin/agent-verify
/opt/onepve-agent/bin/agent-mcp-acceptance
```

`codex` is also exposed by an ordinary-user launcher. Device authorization is a
separate user action. The VM's `vm_browser` MCP entry points to loopback8931;
`browser_close` is disabled. The user services do not depend on `.bashrc` loading.

## Displays and mode changes

Initial mode is XFCE/X11. PVE's graphical console remains that rescue desktop.
After the host has assigned a VF and the correct guest driver is installed:

```bash
# Guest root; BDF comes from actual guest discovery, not a fixed PCI slot.
/opt/onepve-agent/bin/agent-set-mode wayland GUEST_PCI_BDF
/opt/onepve-agent/bin/agent-verify --gpu
```

Wayland runs Sway headless at1600×900/100% using the exact selected Intel render
node and the normal user's original D-Bus/keyring. The environment is local to
those processes, not imported over XFCE's user-manager environment. Compositor
readiness requires the actual FD, owned sockets and an Intel GL renderer.

`agent-set-mode x11` switches back to the software desktop. The mode command
stops only the browser/control/VNC/compositor services; SSH, LightDM, the normal
keyring, frontend jobs and noVNC stay. It preserves the profile and manual marker.
A failed Wayland start returns exit2 with `GPU_FAILED_SOFTWARE_READY` after a
successful software fallback. It never forcibly kills an unresolved profile owner.
The PVE caller separately detaches only its owned VF from the target VM.

## Passwordless viewer, authenticated SSH

VNC5900/noVNC6080/CDP9222/MCP8931 listen only on loopback. noVNC uses the existing
SSH key authentication instead of a second VNC password. Example client tunnel:

```bash
ssh -N -T -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:16080:127.0.0.1:6080 \
  -L 127.0.0.1:18931:127.0.0.1:8931 agentvm
```

Open `http://127.0.0.1:16080/vnc.html?autoconnect=1&shared=1&resize=scale&view_only=1`.
The proxy permits only exact loopback Host/Origin values for the configured client
viewer port; duplicate headers, missing/foreign WebSocket origins and DNS-rebinding
hostnames are refused before the VNC backend. Static pages deny framing with
CSP and X-Frame-Options. Directory listings and arbitrary backend routing are off.
No LAN/public passwordless listener is provided. Use a top-level browser tab,
not an iframe. Changing the client port requires changing `viewer_port` too.

To take over: stop Codex input, enable input in noVNC, click **接管浏览器**, confirm;
the marker is written before MCP stops. After manual work click **交还 Codex** and
confirm, set noVNC view-only again, then have Codex list tabs and obtain a fresh
snapshot. A reboot while manual keeps MCP stopped. Browser/VNC/frontend remain
independent of MCP and viewing clients.

## Evidence

`agent-verify [--gpu]` emits JSON for actual Node/Codex, services, loopback binding,
the normal unlocked keyring and Chromium owner/profile/sandbox arguments. GPU mode
adds exact EGLDevice shader/readPixels, Sway renderer/FD, CDP Intel compositing and
WebGL status, Chromium GPU VF FD and renderer Seccomp2. It does not claim that the
GPU process has a full sandbox when the measured baseline does not.

`agent-mcp-acceptance --gpu` uses the actual HTTP MCP protocol to reuse the owned
loopback fixture, type Chinese/English, click, run both Intel WebGL shader tests,
and save a PNG. Browser and work tabs stay open. Real noVNC input, takeover/return,
negative HTTP/WS/iframe tests and restart/session-cookie persistence are separate
live acceptance steps; service-active or a synthetic test alone does not prove
those. Actual website account login requires the user's authorization.

Local checks: `python3 -m unittest discover -s tests -p 'test_guest*.py' -v`.
The real proxy integration runs on Debian with installed websockify, using only
temporary loopback ports and a synthetic VNC backend; it never touches5900/6080.
