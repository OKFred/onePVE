import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const html = await readFile(fileURLToPath(new URL('./index.html', import.meta.url)));
const gpuScript = await readFile(fileURLToPath(new URL('./gpu.js', import.meta.url)));
const instance = crypto.randomUUID();
const server = http.createServer((request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  if (request.url === '/gpu.js') {
    response.setHeader('Content-Type', 'application/javascript; charset=utf-8');
    response.end(gpuScript);
    return;
  }
  if (request.url === '/health') {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ ok: true, node: process.version, instance }));
    return;
  }
  if (request.url === '/acceptance-download.txt') {
    response.setHeader('Content-Type', 'text/plain; charset=utf-8');
    response.setHeader('Content-Disposition', 'attachment; filename="acceptance-download.txt"');
    response.end('Agent browser download acceptance\n');
    return;
  }
  if (request.url !== '/' && request.url !== '/index.html') {
    response.writeHead(404).end('Not found');
    return;
  }
  response.setHeader('Content-Type', 'text/html; charset=utf-8');
  response.end(html);
});
server.listen(3000, '127.0.0.1', () => console.log('Acceptance frontend listening on loopback port 3000'));
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
