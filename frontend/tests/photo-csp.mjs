import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const next = pathToFileURL(createRequire(import.meta.url).resolve("next/server")).href;
const { NextRequest } = await import(next);
const compile = ts.transpileModule(readFileSync(new URL("../proxy.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText.replaceAll('"next/server"', JSON.stringify(next));
const before = process.env.NODE_ENV;
try {
  process.env.NODE_ENV = "production";
  const { proxy } = await import("data:text/javascript;base64," + Buffer.from(compile).toString("base64"));
  const response = path => proxy(new NextRequest("http://localhost:3000" + path));
  const script = r => r.headers.get("content-security-policy").split("; ").find(x => x.startsWith("script-src ")).split(" ");
  for (const path of ["/chat", "/login", "/", "/skin-photo-copy", "/face-capture-v1/not-the-worker.js", "/face-capture-v1/face-landmarker-worker.js/extra"]) {
    const r = response(path);
    assert.ok(!script(r).includes("'wasm-unsafe-eval'"), path);
    assert.ok(!script(r).includes("'unsafe-eval'"), path);
    assert.ok(script(r).includes("'strict-dynamic'"));
    assert.match(r.headers.get("permissions-policy"), /camera=\(\)/);
  }
  const page = response("/skin-photo");
  assert.ok(script(page).includes("'wasm-unsafe-eval'"));
  assert.ok(script(page).includes("'strict-dynamic'"));
  assert.ok(script(page).some(x => x.startsWith("'nonce-")));
  assert.ok(!script(page).includes("'unsafe-eval'"));
  assert.match(page.headers.get("permissions-policy"), /camera=\(self\)/);
  const worker = response("/face-capture-v1/face-landmarker-worker.js");
  assert.deepEqual(script(worker), ["script-src", "'self'", "'wasm-unsafe-eval'"]);
  assert.match(worker.headers.get("content-security-policy"), /connect-src 'self';/);
  assert.doesNotMatch(worker.headers.get("content-security-policy"), /connect-src[^;]*https:/);
  assert.match(worker.headers.get("permissions-policy"), /camera=\(\)/);
  assert.equal(worker.headers.get("x-content-type-options"), "nosniff");
  assert.match(worker.headers.get("content-security-policy"), /object-src 'none'/);
  const redirect = response("/chat?flow=customer_quiz");
  assert.equal(redirect.status, 307);
  assert.ok(!script(redirect).includes("'wasm-unsafe-eval'"));
  console.log("Photo CSP passed: exact-route WASM only, JS eval blocked, same-origin worker, camera permission remains scoped.");
} finally {
  if (before === undefined) delete process.env.NODE_ENV;
  else process.env.NODE_ENV = before;
}
