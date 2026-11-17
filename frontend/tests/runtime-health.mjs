import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import ts from 'typescript';

const next = pathToFileURL(createRequire(import.meta.url).resolve('next/server')).href;
const source = ts.transpileModule(await readFile(new URL('../app/api/runtime/route.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText.replaceAll('"next/server"', JSON.stringify(next));
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const originalCwd = process.cwd(), originalDist = process.env.NEXT_DIST_DIR;
const root = await mkdtemp(path.join(tmpdir(), 'blink-runtime-health-'));
let instance = 0;
async function fresh() { return (await import(moduleUrl + '#' + instance++)).GET; }
try {
  process.chdir(root);
  process.env.NEXT_DIST_DIR = '.next-candidate';
  await mkdir('.next-candidate');
  await writeFile('.next-candidate/BUILD_ID', 'actual-running-build\n');
  const GET = await fresh(), healthy = await GET();
  assert.equal(healthy.status, 200);
  assert.equal(healthy.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await healthy.json(), { status: 'ok', service: 'frontend', buildId: 'actual-running-build' });
  await writeFile('.next-candidate/BUILD_ID', 'file-changed-after-process-start');
  assert.equal((await (await GET()).json()).buildId, 'actual-running-build', 'Do not report a newly selected build as the old running process');
  for (const invalid of ['../private', '/tmp/private', '.next/../private', '.next-secrets?value=private']) {
    process.env.NEXT_DIST_DIR = invalid;
    const response = await (await fresh())();
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { status: 'unavailable', service: 'frontend' });
  }
  process.env.NEXT_DIST_DIR = '.next-candidate';
  for (const invalid of ['', 'a'.repeat(130), 'path/private value', '{"private":"secret"}']) {
    await writeFile('.next-candidate/BUILD_ID', invalid);
    assert.equal((await (await fresh())()).status, 503);
  }
  await rm('.next-candidate/BUILD_ID');
  await writeFile('unrelated-private-file', 'do-not-read');
  await symlink(path.join(root, 'unrelated-private-file'), '.next-candidate/BUILD_ID');
  assert.equal((await (await fresh())()).status, 503);
  process.env.NEXT_DIST_DIR = '.next-link';
  await symlink(path.join(root, '.next-candidate'), '.next-link');
  assert.equal((await (await fresh())()).status, 503);
  console.log('Runtime health passed: running build pinned, no-store metadata only, missing/invalid/symlink paths fail closed');
} finally {
  process.chdir(originalCwd);
  if (originalDist === undefined) delete process.env.NEXT_DIST_DIR;
  else process.env.NEXT_DIST_DIR = originalDist;
  await rm(root, { recursive: true, force: true });
}
