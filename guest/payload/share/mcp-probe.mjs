#!/usr/bin/env node
// Integration probe for this VM's public acceptance fixture. Never closes tabs.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const action = process.argv[2] || 'inspect';
const endpoint = process.argv[3] || 'http://127.0.0.1:8931/mcp';
const headers = { 'content-type': 'application/json', accept: 'application/json, text/event-stream' };
let nextId = 1;
async function send(method, params = {}, notification = false) {
  const id = notification ? undefined : nextId++;
  const body = { jsonrpc: '2.0', method, params };
  if (!notification) body.id = id;
  const response = await fetch(endpoint, { method: 'POST', headers, body: JSON.stringify(body), signal: AbortSignal.timeout(30_000) });
  const session = response.headers.get('mcp-session-id');
  if (session) headers['mcp-session-id'] = session;
  const raw = await response.text();
  if (!response.ok) throw new Error(method + ': HTTP ' + response.status + ': ' + raw.slice(0, 500));
  if (notification) return;
  const message = response.headers.get('content-type')?.includes('event-stream')
    ? raw.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => JSON.parse(line.slice(5))).find(item => item.id === id)
    : JSON.parse(raw);
  if (!message || message.error) throw new Error(method + ': ' + JSON.stringify(message?.error));
  return message.result;
}
function text(result) { return (result.content || []).filter(part => part.type === 'text').map(part => part.text).join('\n'); }
async function tool(name, arguments_ = {}) {
  try {
    await fs.access(path.join(os.homedir(), '.local/state/onepve-agent/manual.lock'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    const result = await send('tools/call', { name, arguments: arguments_ });
    if (result.isError) throw new Error(text(result));
    return result;
  }
  throw new Error('Manual takeover is active; browser access was not attempted.');
}
const initialized = await send('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'onepve-agent-lifecycle-probe', version: '1.0.0' } });
headers['mcp-protocol-version'] = initialized.protocolVersion;
await send('notifications/initialized', {}, true);
const snapshot = text(await tool('browser_snapshot'));
if (!snapshot.includes('Agent browser acceptance') || !snapshot.includes('http://127.0.0.1:3000/')) throw new Error('The selected page is not the dedicated acceptance fixture.');
if (action === 'resize') await tool('browser_resize', { width: 1440, height: 760 });
else if (action === 'marker') {
  const line = snapshot.split('\n').find(value => value.includes('button "Save login marker"'));
  const target = line?.match(/\[ref=([^\]]+)\]/)?.[1];
  if (!target) throw new Error('Fresh snapshot has no marker button ref.');
  await tool('browser_click', { target });
} else if (action !== 'inspect') throw new Error('Use inspect, marker or resize.');
const result = text(await tool('browser_evaluate', { function: '() => ({identity: document.querySelector("#session").textContent, count: document.querySelector("#result").textContent, name: document.querySelector("#name").value, draft: document.querySelector("#draft").value, marker: localStorage.getItem("agent-acceptance-login"), viewport: [innerWidth, innerHeight], scale: devicePixelRatio})' }));
const json = result.match(/### Result\s+([\s\S]*?)(?:\n### |$)/)?.[1]?.trim().replace(/^```(?:json)?\s*|\s*```$/g, '');
if (!json) throw new Error('No structured fixture state in MCP response: ' + result);
console.log(JSON.stringify(JSON.parse(json), null, 2));
