#!/usr/bin/env node
// Run inside the Debian VM as its dedicated agent user with Node 26.
// Uses only built-in Node APIs and the actual HTTP MCP endpoint. Work tabs stay open.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const endpoint = 'http://127.0.0.1:8931/mcp';
let fixtureUrl = 'http://127.0.0.1:3000/';
const home = os.homedir();
const manualLock = path.join(home, '.local/state/onepve-agent/manual.lock');
const stamp = new Date().toISOString().replace(/[-:.]/g, '');
const options = {
  reportFile: path.join(home, '.local/state/onepve-agent/mcp-acceptance.json'),
  screenshotFile: path.join(home, 'Downloads/onepve-agent', `mcp-acceptance-${stamp}.png`),
  gpu: false,
};

function parseArguments() {
  const arguments_ = process.argv.slice(2);
  for (let i = 0; i < arguments_.length; i++) {
    const argument = arguments_[i];
    if (argument === '--gpu') { options.gpu = true; continue; }
    if (argument === '--help') {
      process.stdout.write('Usage: node mcp-acceptance.mjs [--fixture-url URL] [--report-file PATH] [--screenshot-file PATH]\nRun in the VM as the agent user. The fixture work page remains open.\n');
      return false;
    }
    if (argument === '--fixture-url') {
      const supplied = arguments_[++i];
      if (!supplied) throw new Error('--fixture-url requires a loopback URL');
      const url = new URL(supplied);
      if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)) throw new Error('Only a VM loopback fixture URL is supported');
      fixtureUrl = url.href;
      continue;
    }
    const property = { '--report-file': 'reportFile', '--screenshot-file': 'screenshotFile' }[argument];
    if (!property || !arguments_[i + 1]) throw new Error(`Unknown or incomplete argument: ${argument}`);
    options[property] = path.resolve(arguments_[++i]);
  }
  return true;
}

async function assertAgentControl() {
  try {
    await fs.lstat(manualLock);
  } catch (error) {
    if (error.code === 'ENOENT') return;
    throw error;
  }
  throw new Error(`Manual takeover is active (${manualLock}); no browser request will be issued.`);
}

function textContent(result) {
  return (result?.content ?? []).filter(item => item.type === 'text').map(item => item.text ?? '').join('\n');
}

class HttpMcpClient {
  constructor() {
    this.nextId = 1;
    this.sessionId = null;
    this.protocolVersion = '2025-03-26';
  }

  async send(method, params, notification = false) {
    const id = notification ? undefined : this.nextId++;
    const body = { jsonrpc: '2.0', method, params };
    if (!notification) body.id = id;
    const headers = {
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
      'mcp-protocol-version': this.protocolVersion,
    };
    if (this.sessionId) headers['mcp-session-id'] = this.sessionId;
    const response = await fetch(endpoint, {
      method: 'POST', headers, body: JSON.stringify(body), signal: AbortSignal.timeout(120_000),
    });
    const returnedSessionId = response.headers.get('mcp-session-id');
    if (returnedSessionId) this.sessionId = returnedSessionId;
    if (!response.ok) {
      const detail = (await response.text()).slice(0, 1200);
      throw new Error(`${method}: HTTP ${response.status}: ${detail}`);
    }
    if (notification) {
      await response.body?.cancel();
      return undefined;
    }
    let message;
    const contentType = response.headers.get('content-type') ?? '';
    if (contentType.includes('text/event-stream')) {
      message = await this.readSseResponse(response, id);
    } else if (contentType.includes('application/json')) {
      message = await response.json();
    } else {
      throw new Error(`${method}: unexpected content type ${contentType || '(missing)'}`);
    }
    if (message?.id !== id) throw new Error(`${method}: JSON-RPC response ID does not match request ${id}`);
    if (message.error) throw new Error(`${method}: JSON-RPC ${message.error.code}: ${message.error.message}`);
    if (!Object.hasOwn(message, 'result')) throw new Error(`${method}: response has no result`);
    return message.result;
  }

  async readSseResponse(response, requestedId) {
    if (!response.body) throw new Error('MCP SSE response has no body');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const parseEvent = event => {
      const data = event.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (!data) return null;
      const message = JSON.parse(data);
      return message?.id === requestedId ? message : null;
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        if (buffer.length > 64 * 1024 * 1024) throw new Error('MCP SSE event exceeds 64 MiB');
        let separator;
        while ((separator = /\r?\n\r?\n/.exec(buffer))) {
          const event = buffer.slice(0, separator.index);
          buffer = buffer.slice(separator.index + separator[0].length);
          const message = parseEvent(event);
          if (message) return message;
        }
        if (done) {
          const message = parseEvent(buffer);
          if (message) return message;
          throw new Error(`MCP SSE ended before JSON-RPC response ${requestedId}`);
        }
      }
    } finally {
      // Only releases this HTTP response stream. No MCP DELETE/session teardown
      // or browser shutdown command is sent.
      await reader.cancel().catch(() => {});
    }
  }
}

function requireRef(snapshot, role, label) {
  const line = snapshot.split('\n').find(candidate => candidate.includes(`${role} "${label}"`) && /\[ref=[^\]]+\]/.test(candidate));
  const ref = line?.match(/\[ref=([^\]]+)\]/)?.[1];
  if (!ref) throw new Error(`Fresh browser snapshot does not expose ${role} "${label}" with a ref`);
  return ref;
}

function readPageState(snapshot) {
  const pageIdentity = snapshot.match(/Page identity:\s*([0-9a-f-]{36})/i)?.[1];
  const count = snapshot.match(/\bCount:\s*(\d+)/)?.[1];
  if (!pageIdentity || count === undefined) throw new Error('Snapshot does not match the project acceptance fixture');
  return { pageIdentity, count: Number(count) };
}

function findFixtureTab(tabText) {
  const wanted = new URL(fixtureUrl);
  for (const line of tabText.split('\n')) {
    const index = line.match(/^\s*-\s*(\d+):\s*/)?.[1];
    if (index === undefined) continue;
    const urls = line.match(/https?:\/\/[^\s)\]]+/g) ?? [];
    for (const candidate of urls) {
      let url;
      try { url = new URL(candidate); } catch { continue; }
      if (url.origin === wanted.origin && url.pathname === wanted.pathname) return Number(index);
    }
  }
  return null;
}

async function main() {
  if (!parseArguments()) return;
  const report = {
    startedAt: new Date().toISOString(), endpoint, fixtureUrl, status: 'running',
    checks: [], page: {}, screenshot: { requestedPath: options.screenshotFile, verifiedPath: null },
    browserShutdownRequested: false, tabClosureRequested: false,
    notVerified: ['noVNC same-screen identity', 'human takeover and explicit resume', 'noVNC disconnect/reconnect', 'desktop Chinese IME', 'browser/VM restart persistence'],
  };
  const pass = (name, detail) => report.checks.push({ name, status: 'passed', detail });
  let currentStep = 'manual-control gate';
  try {
    await assertAgentControl();
    const client = new HttpMcpClient();
    currentStep = 'MCP initialize';
    const initialized = await client.send('initialize', {
      protocolVersion: client.protocolVersion, capabilities: {},
      clientInfo: { name: 'onepve-agent-mcp-acceptance', version: '1.0.0' },
    });
    if (!initialized?.protocolVersion) throw new Error('Server omitted negotiated protocolVersion');
    client.protocolVersion = initialized.protocolVersion;
    report.protocolVersion = initialized.protocolVersion;
    pass(currentStep, `Negotiated ${initialized.protocolVersion}`);
    await client.send('notifications/initialized', {}, true);
    pass('MCP initialized notification', 'Sent successfully');

    currentStep = 'MCP tools/list';
    const tools = new Map();
    let cursor;
    for (let page = 0; page < 20; page++) {
      const listed = await client.send('tools/list', cursor ? { cursor } : {});
      for (const tool of listed.tools ?? []) tools.set(tool.name, tool);
      cursor = listed.nextCursor;
      if (!cursor) break;
    }
    if (cursor) throw new Error('Tool listing exceeded pagination bound');
    for (const required of ['browser_tabs', 'browser_navigate', 'browser_snapshot', 'browser_type', 'browser_click', 'browser_take_screenshot', 'browser_evaluate']) {
      if (!tools.has(required)) throw new Error(`Required MCP tool is missing: ${required}`);
    }
    pass(currentStep, `${tools.size} tools listed; required browser tools present`);

    async function callTool(name, candidates = {}) {
      await assertAgentControl();
      const schema = tools.get(name)?.inputSchema;
      if (!schema?.properties) throw new Error(`${name}: unsupported input schema`);
      const arguments_ = Object.fromEntries(Object.entries(candidates).filter(([key]) => Object.hasOwn(schema.properties, key)));
      for (const required of schema.required ?? []) {
        if (!Object.hasOwn(arguments_, required)) throw new Error(`${name}: required argument ${required} is unavailable`);
      }
      const result = await client.send('tools/call', { name, arguments: arguments_ });
      if (result?.isError) throw new Error(`${name}: ${textContent(result).slice(0, 2000)}`);
      return result;
    }
    async function snapshot() { return textContent(await callTool('browser_snapshot')); }

    currentStep = 'reuse or open fixture tab';
    const tabs = textContent(await callTool('browser_tabs', { action: 'list' }));
    const existingIndex = findFixtureTab(tabs);
    if (existingIndex !== null) {
      await callTool('browser_tabs', { action: 'select', index: existingIndex });
      report.page.reusedExistingTab = true;
      report.page.tabIndex = existingIndex;
    } else {
      const actions = tools.get('browser_tabs').inputSchema.properties.action?.enum;
      if (Array.isArray(actions) && !actions.includes('new')) throw new Error('Server cannot create a separate tab safely');
      await callTool('browser_tabs', { action: 'new' });
      await callTool('browser_navigate', { url: fixtureUrl });
      report.page.reusedExistingTab = false;
    }
    let observed = await snapshot();
    const before = readPageState(observed);
    report.page.pageIdentity = before.pageIdentity;
    report.page.countBefore = before.count;
    pass(currentStep, report.page.reusedExistingTab ? `Reused fixture tab ${existingIndex}` : 'Opened fixture in a new tab without replacing other work pages');

    const nameText = `Codex MCP 中文 / English ${stamp}`;
    const draftText = `未提交 MCP 草稿 / Unsaved English draft ${stamp}`;
    currentStep = 'bilingual name input through snapshot ref';
    const nameRef = requireRef(observed, 'textbox', '姓名 / Name');
    // Current MCP uses target; older releases use ref. callTool filters against
    // the live schema, so only the supported field is sent in either version.
    await callTool('browser_type', { element: '姓名 / Name', target: nameRef, ref: nameRef, text: nameText, submit: false, slowly: false });
    observed = await snapshot();
    if (!observed.includes(nameText)) throw new Error('Fresh snapshot does not contain the entered bilingual name');
    pass(currentStep, 'Fresh snapshot contains the entered Chinese and English name');

    currentStep = 'draft input through fresh snapshot ref';
    const draftRef = requireRef(observed, 'textbox', '未提交草稿 / Unsaved draft');
    await callTool('browser_type', { element: '未提交草稿 / Unsaved draft', target: draftRef, ref: draftRef, text: draftText, submit: false, slowly: false });
    observed = await snapshot();
    if (!observed.includes(draftText)) throw new Error('Fresh snapshot does not contain the unsaved draft');
    pass(currentStep, 'Fresh snapshot contains the entered unsaved draft');

    currentStep = 'button click and page mutation';
    const incrementRef = requireRef(observed, 'button', 'Increment');
    await callTool('browser_click', { element: 'Increment', target: incrementRef, ref: incrementRef, button: 'left' });
    observed = await snapshot();
    const after = readPageState(observed);
    if (after.pageIdentity !== before.pageIdentity) throw new Error('Fixture page identity changed during acceptance');
    if (after.count !== before.count + 1) throw new Error(`Counter changed from ${before.count} to ${after.count}, expected exactly one increment`);
    if (!observed.includes(nameText) || !observed.includes(draftText)) throw new Error('Entered field values did not remain after the click');
    report.page.countAfter = after.count;
    report.page.bilingualName = nameText;
    report.page.unsavedDraft = draftText;
    pass(currentStep, `Count ${before.count} -> ${after.count}; same page identity and both field values retained`);

    if (options.gpu) {
      currentStep = 'Intel WebGL1 and WebGL2 shader draw/readPixels';
      const evaluated = textContent(await callTool('browser_evaluate', {
        function: 'async () => { if (!document.querySelector("#session")?.textContent?.includes(' + JSON.stringify(before.pageIdentity) + ')) throw new Error("Wrong proof page"); return await window.runGpuProbe(); }',
      }));
      let result;
      for (let i = 0; i < evaluated.length; i++) {
        if (evaluated[i] !== '{') continue;
        for (let j = evaluated.lastIndexOf('}'); j > i; j = evaluated.lastIndexOf('}', j - 1)) {
          try { const item = JSON.parse(evaluated.slice(i,j+1)); if (Array.isArray(item.contexts)) { result=item; break; } } catch {}
        }
        if (result) break;
      }
      if (!result || result.contexts.length !== 2) throw new Error('MCP omitted two WebGL results');
      for (const context of result.contexts) {
        if (!context.draw_readback_pass || !/intel/i.test(context.unmasked_renderer)
            || /llvmpipe|softpipe|swiftshader|lavapipe|software/i.test(context.unmasked_renderer)) throw new Error('WebGL hardware proof failed');
      }
      report.webgl = result;
      pass(currentStep, 'Both Intel contexts completed two real shader draw/readPixels colors');
    }

    currentStep = 'MCP screenshot';
    await fs.mkdir(path.dirname(options.screenshotFile), { recursive: true });
    await callTool('browser_take_screenshot', { type: 'png', filename: options.screenshotFile, fullPage: true, scale: 'css' });
    const screenshotStat = await fs.stat(options.screenshotFile);
    if (!screenshotStat.isFile() || screenshotStat.size < 8) throw new Error('Screenshot file was not created');
    const file = await fs.open(options.screenshotFile, 'r');
    try {
      const signature = Buffer.alloc(8);
      await file.read(signature, 0, 8, 0);
      if (!signature.equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) throw new Error('Screenshot does not have a PNG signature');
    } finally { await file.close(); }
    report.screenshot.verifiedPath = options.screenshotFile;
    report.screenshot.bytes = screenshotStat.size;
    pass(currentStep, `PNG file exists (${screenshotStat.size} bytes)`);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.checks.push({ name: currentStep, status: 'failed', detail: error.message });
    process.exitCode = 1;
  } finally {
    report.finishedAt = new Date().toISOString();
    await fs.mkdir(path.dirname(options.reportFile), { recursive: true });
    await fs.writeFile(options.reportFile, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
    const summary = {
      status: report.status,
      checks: report.checks.map(check => `${check.status}: ${check.name} — ${check.detail}`),
      pageIdentity: report.page.pageIdentity ?? null,
      screenshot: report.screenshot.verifiedPath,
      report: options.reportFile,
      browserShutdownRequested: false,
      notVerified: report.notVerified,
    };
    process.stdout.write(JSON.stringify(summary, null, 2) + '\n');
  }
}

await main().catch(error => {
  process.stderr.write(`Acceptance runner failed: ${error.message}\n`);
  process.exitCode = 1;
});
