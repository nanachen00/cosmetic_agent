import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const source = readFileSync(new URL("../lib/photo-request-limits.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { PhotoRequestGate, PhotoRequestError, photoModelOrigin, readPhotoJson } = await import("data:text/javascript;base64," + Buffer.from(compiled).toString("base64"));
const bytes = text => new TextEncoder().encode(text);
const stream = text => new ReadableStream({ start(controller) { controller.enqueue(bytes(text)); controller.close(); } });

for (const address of ["http://127.0.0.1:8025/", "http://localhost:8025", "http://[::1]:8025/"]) assert.ok(photoModelOrigin(address));
for (const address of ["http://140.119.24.206:8025", "http://127.0.0.1.evil.test:8025", "http://user:secret@127.0.0.1:8025", "http://127.0.0.1:8025/private", "http://127.0.0.1:8025/?token=secret", "file:///etc/passwd", "invalid"]) assert.equal(photoModelOrigin(address), null);

assert.deepEqual(await readPhotoJson(stream('{"sample":true}')), { sample: true });
await assert.rejects(readPhotoJson(stream("not json")), error => error.code === "invalid_request" && error.status === 400);

let cancelled = false;
const oversized = new ReadableStream({ start(controller) { controller.enqueue(bytes("12345")); }, cancel() { cancelled = true; } });
await assert.rejects(readPhotoJson(oversized, { maxBytes: 4 }), error => error instanceof PhotoRequestError && error.status === 413);
assert.equal(cancelled, true);
assert.equal(oversized.locked, false);

cancelled = false;
const stalled = new ReadableStream({ cancel() { cancelled = true; return new Promise(() => {}); } });
const start = performance.now();
await assert.rejects(readPhotoJson(stalled, { timeoutMs: 30 }), error => error.status === 408);
assert.equal(cancelled, true);
assert.ok(performance.now() - start < 1000, "A stalled cancellation callback must not hold the request open");
assert.equal(stalled.locked, false);

const endlessChunks = new ReadableStream({ pull(controller) { controller.enqueue(new Uint8Array(0)); } });
await assert.rejects(readPhotoJson(endlessChunks, { timeoutMs: 20 }), error => error.status === 408);
assert.equal(endlessChunks.locked, false, "An endless microtask stream cannot starve the total deadline");

cancelled = false;
const controller = new AbortController();
const aborted = new ReadableStream({ cancel() { cancelled = true; } });
const reading = readPhotoJson(aborted, { signal: controller.signal });
controller.abort();
await assert.rejects(reading, error => error.code === "request_aborted");
assert.equal(cancelled, true);
assert.equal(aborted.locked, false);

const failed = new ReadableStream({ pull(controller) { controller.error(new Error("private input")); } });
await assert.rejects(readPhotoJson(failed), error => error.code === "invalid_request" && !error.message.includes("private input"));
assert.equal(failed.locked, false);

const gate = new PhotoRequestGate(2, 2, 2, 100);
const release1 = gate.enter();
const release2 = gate.enter();
assert.equal(gate.enter(), null);
release1(); release1();
const release3 = gate.enter();
assert.ok(release3);
assert.equal(gate.enter(), null, "Repeated release must not create extra slots");
release2(); release3();
gate.takeUserQuota("one", 0); gate.takeUserQuota("one", 1);
assert.throws(() => gate.takeUserQuota("one", 2), error => error.code === "rate_limited");
gate.takeUserQuota("two", 3);
assert.throws(() => gate.takeUserQuota("three", 4), error => error.status === 503);
assert.throws(() => gate.takeUserQuota("one", 5), error => error.status === 429, "A full user cache cannot evict an active user's rate history");
gate.takeUserQuota("three", 104);
gate.takeUserQuota("one", 105);

console.log("Photo request limits passed: bounded buffering, stalled-body deadline, abort/error cleanup, idempotent admission, bounded rate memory");
