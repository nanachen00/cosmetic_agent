// Read-only route smoke for a running local production build.
import assert from 'node:assert/strict';
import fs from 'node:fs';
const base = process.argv[2] || 'http://127.0.0.1:3000';
const target = new URL(base);
assert(['127.0.0.1', 'localhost'].includes(target.hostname), 'smoke target must be local');
const results = [];
for (const path of ['/', '/login', '/chat', '/products', '/profile', '/skin-record', '/skin-report', '/daily-checkin', '/personality-quiz', '/knowledge-map', '/skin-photo', '/api/v1/health', '/member-auth/session']) {
  const response = await fetch(base + path, { redirect: 'manual', signal: AbortSignal.timeout(10000) });
  const expected = path === '/member-auth/session' ? [401] : [200, 307, 308];
  results.push({ path, status: response.status, pass: expected.includes(response.status) });
}
const photo = await fetch(base + '/api/skin-photo', { signal: AbortSignal.timeout(10000) });
const photoBody = await photo.json();
results.push({ path: '/api/skin-photo', status: photo.status, pass: photoBody.ready === true ? photo.status === 200 : photo.status === 503 });
const report = { scope: 'HTTP route smoke, no authenticated or model generation claims', base, results, verdict: results.every(r => r.pass) ? 'PASS' : 'FAIL' };
fs.mkdirSync('../.runtime/reports', { recursive: true });
fs.writeFileSync('../.runtime/reports/http-smoke.json', JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report, null, 2));
assert.equal(report.verdict, 'PASS');
