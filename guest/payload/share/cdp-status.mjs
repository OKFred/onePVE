// Read-only browser-level CDP diagnostics. Closing this transport preserves Chromium.
const response = await fetch('http://127.0.0.1:9222/json/version', {signal: AbortSignal.timeout(5000)});
if (!response.ok) throw new Error('Managed CDP endpoint unavailable');
const version = await response.json();
const endpoint = new URL(version.webSocketDebuggerUrl);
if (endpoint.protocol !== 'ws:' || endpoint.hostname !== '127.0.0.1' || endpoint.port !== '9222'
    || !endpoint.pathname.startsWith('/devtools/browser/') || endpoint.search || endpoint.hash) throw new Error('Unexpected CDP endpoint');
const transport = new WebSocket(endpoint.href);
try {
  const result = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('CDP diagnostic deadline exceeded')), 15000);
    transport.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('CDP diagnostic transport failed')); });
    transport.addEventListener('open', () => transport.send(JSON.stringify({id: 1, method: 'SystemInfo.getInfo'})));
    transport.addEventListener('message', message => {
      const decoded = JSON.parse(message.data);
      if (decoded.id !== 1) return;
      clearTimeout(timeout);
      if (decoded.error) reject(new Error('SystemInfo.getInfo failed')); else resolve(decoded.result.gpu);
    });
  });
  const aux = result.auxAttributes ?? {};
  console.log(JSON.stringify({devices: result.devices.map(x => ({vendorId:x.vendorId,deviceId:x.deviceId})),
    featureStatus: result.featureStatus, renderer: aux.glRenderer, vendor: aux.glVendor,
    gpu_sandbox: aux.sandboxed ?? aux.isSandboxed ?? null}));
} finally { transport.close(); }
