import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const uri = source => "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const read = path => readFileSync(new URL("../" + path, import.meta.url), "utf8");
const nextUri = pathToFileURL(createRequire(import.meta.url).resolve("next/server")).href;
const { NextRequest } = await import(nextUri);
let authRequests = 0;
let malformedUpstream = false;
let modelRequests = 0;
let healthMode = "ready";
let modelMode = "ready";
let trapRequests = 0;
let authDisconnects = 0;
let modelDisconnects = 0;
let lastModelBody;
let modelCookies = 0;
const validReport = () => ({
  status: "needs_confirmation", source: "experimental_vlm", requires_user_confirmation: true, eligible_for_auto_save: false,
  observation: { image_scope: "face", observations: [{ tag: "redness", region: "cheek", evidence: "可見局部泛紅。" }],
    limitations: ["光線會影響判讀。", "照片無法取代實際感受。"], follow_up_question: "是否有不適？" },
  quality: {width:640,height:480,brightness:100,reasons:[]}, inference_seconds: .5,
});
const send = (response, status, value, type = "application/json") => {
  response.writeHead(status, { "Content-Type": type });
  response.end(type === "application/json" ? JSON.stringify(value) : value);
};
const trap = createServer((request, response) => { trapRequests += 1; request.resume(); send(response, 200, {}); });
const auth = createServer((request, response) => {
  authRequests += 1;
  const user = /fixture=([a-z0-9-]+)/.exec(request.headers.cookie || "")?.[1];
  if (user === "auth-redirect") { response.writeHead(307, {Location:`http://127.0.0.1:${trap.address().port}/trap`}); response.end(); return; }
  if (user === "auth-huge") { send(response, 200, {padding:"x".repeat(17000),account:{user_id:user}}); return; }
  if (user === "auth-html") { send(response, 200, "private auth HTML", "text/html"); return; }
  if (user === "auth-malformed") {response.writeHead(200,{"Content-Type":"application/json"});response.end("private invalid JSON");return;}
  if (user?.startsWith("auth-stall")) {
    response.on("close",()=>{authDisconnects += 1;});
    if (user === "auth-stall-body") {response.writeHead(200, {"Content-Type":"application/json"}); response.write("{");}
    return;
  }
  const status = user === "auth-fail" ? 503 : user ? 200 : 401;
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(status === 200 ? { account: { user_id: user } } : { error: "unauthorized" }));
});
const model = createServer((request, response) => {
  if (request.url === "/health") {
    if (healthMode === "redirect") { response.writeHead(307,{Location:`http://127.0.0.1:${trap.address().port}/trap`}); response.end(); return; }
    if (healthMode === "stall") {response.writeHead(200,{"Content-Type":"application/json"}); response.write("{"); return;}
    if (healthMode === "huge") {send(response,200,{ready:true,padding:"x".repeat(5000)});return;}
    if (healthMode === "malformed") {send(response,200,"private health HTML","text/html");return;}
    if (healthMode === "wrong-type") {send(response,200,{ready:"true"});return;}
    if (healthMode === "starting") {send(response,200,{ready:false,failed:false});return;}
    if (healthMode === "failed") {send(response,200,{ready:true,failed:true,busy:false});return;}
    send(response,200,{ready:true,failed:false,busy:healthMode === "busy",source:"private model path",api_key:"fixture secret"});return;
  }
  modelRequests += 1;
  if (request.headers.cookie) modelCookies += 1;
  let raw="";
  request.setEncoding("utf8"); request.on("data",part=>{raw+=part;});
  request.on("end",()=>{
    lastModelBody=JSON.parse(raw);
    if (modelMode === "redirect") {response.writeHead(307,{Location:`http://127.0.0.1:${trap.address().port}/trap`});response.end();return;}
    if (modelMode === "stall") {response.on("close",()=>{modelDisconnects += 1;});response.writeHead(200,{"Content-Type":"application/json"});response.write("{");return;}
    if (malformedUpstream) {response.writeHead(200,{"Content-Type":"application/json"});response.end("private malformed upstream response");return;}
    if (modelMode === "huge") {send(response,200,{...validReport(),raw_output:"x".repeat(65536)});return;}
    if (modelMode === "html") {send(response,200,"private model HTML","text/html");return;}
    if (modelMode === "bad-schema") {send(response,200,{...validReport(),observation:{...validReport().observation,observations:[{tag:["redness"],region:"cheek",evidence:"visible"}]}});return;}
    if (modelMode === "rejected") {send(response,200,{...validReport(),status:"needs_retake",observation:{image_scope:"uncertain",observations:[],limitations:["照片品質不足。"],follow_up_question:"請重拍。"}});return;}
    if (modelMode === "non-skin") {send(response,200,{...validReport(),status:"needs_retake",observation:{image_scope:"non_skin",observations:[],limitations:["照片中未見可分析的皮膚。"],follow_up_question:"請提供皮膚照片。"}});return;}
    if (modelMode === "invalid-output") {send(response,200,{status:"invalid_model_output",error:"private model/path token",raw_output:"private output"});return;}
    if (modelMode === "private-error") {send(response,500,{error:"private path/token",detail:"must not leak"});return;}
    if (modelMode.startsWith("allowed-")) {const [,status,error]=modelMode.split("-");send(response,Number(status),{error,detail:"private trace"});return;}
    send(response,200,{...validReport(),raw_output:"private output",image_sha256:"private hash",path:"private source",
      requires_user_confirmation:false,eligible_for_auto_save:true,quality:{...validReport().quality,path:"private cache",note:"private note"},
      observation:{...validReport().observation,extra:"private field"}});
  });
});
const listen = server => new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const close = server => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const waitUntil = async predicate => {
  for (let i = 0; i < 100; i++) { if (predicate()) return; await delay(10); }
  throw new Error("Photo route fixture timed out");
};

await listen(trap); await listen(auth); await listen(model);
const originalService = process.env.PHOTO_MODEL_BASE_URL;
const originalTimeout = AbortSignal.timeout;
let timeoutCap = Infinity;
AbortSignal.timeout = milliseconds => originalTimeout(Math.min(milliseconds, timeoutCap));
try {
  process.env.PHOTO_MODEL_BASE_URL = "http://127.0.0.1:" + model.address().port;
  const helperUri = uri(compile(read("lib/photo-request-limits.ts")));
  const authUri = uri(`export const serverMemberAuthOrigin = () => "http://127.0.0.1:${auth.address().port}";`);
  const routeSource = compile(read("app/api/skin-photo/route.ts"))
    .replaceAll('"next/server"', JSON.stringify(nextUri))
    .replaceAll('"@/lib/photo-request-limits"', JSON.stringify(helperUri))
    .replaceAll('"@/lib/api-origin"', JSON.stringify(authUri));
  const { GET, POST } = await import(uri(routeSource));
  const request = (user, options = {}) => new NextRequest("http://photo.test/api/skin-photo", {
    method: "POST", headers: { host: "photo.test", origin: options.origin || "http://photo.test", ...(user ? { cookie: "fixture=" + user } : {}), ...options.headers },
    body: options.body || JSON.stringify({ sample: true }), duplex: "half", signal: options.signal,
  });
  let sequence=0;
  const fresh=options=>request("fixture-"+(++sequence),options);
  const denied=async(response,status,error)=>{assert.equal(response.status,status);assert.deepEqual(await response.json(),{error});};
  const authBeforeHealth=authRequests;
  const ready=await GET(); assert.equal(ready.status,200);assert.deepEqual(await ready.json(),{ready:true,busy:false});
  healthMode="busy";const busyReady=await GET();assert.equal(busyReady.status,200);assert.deepEqual(await busyReady.json(),{ready:true,busy:true});
  healthMode="starting";const starting=await GET();assert.equal(starting.status,503);assert.deepEqual(await starting.json(),{ready:false,error:"model_starting"});
  for(const mode of ["redirect","huge","malformed","wrong-type"]){healthMode=mode;const health=await GET();assert.equal(health.status,503);assert.deepEqual(await health.json(),{ready:false,error:"inference_unavailable"});}
  healthMode="failed";const failedHealth=await GET();assert.equal(failedHealth.status,503);assert.deepEqual(await failedHealth.json(),{ready:false,busy:false,error:"inference_unavailable"});
  assert.equal(authRequests,authBeforeHealth,"Anonymous health must never authenticate or analyze photos");
  healthMode="ready";
  for(const origin of ["https://photo.test","http://user:password@photo.test","http://photo.test/path","null","http://evil.test"]){
    const prior=authRequests;await denied(await POST(fresh({origin})),403,"origin_not_allowed");assert.equal(authRequests,prior);
  }
  for (let i = 0; i < 12; i++) assert.equal((await POST(request(null))).status, 401);
  for (let i = 0; i < 6; i++) assert.equal((await POST(request("auth-fail"))).status, 503);
  for(const user of ["auth-redirect","auth-huge","auth-html","auth-malformed"])await denied(await POST(request(user)),503,"auth_unavailable");
  assert.equal(trapRequests,0,"Auth/health redirects must not reach a different listener");
  assert.equal((await POST(request("oversize", { headers: { "content-length": String(6 * 1024 * 1024) } }))).status, 413);
  assert.equal((await POST(request("invalid-json", { body: "not json" }))).status, 400);
  malformedUpstream = true;
  const broken = await POST(request("bad-upstream"));
  assert.equal(broken.status, 503, "An upstream JSON failure must not be reported as a client fault");
  assert.equal((await broken.json()).error, "inference_unavailable");
  malformedUpstream = false;
  const recovered=await POST(request("recovered"));assert.equal(recovered.status,200);
  const clean=await recovered.json();assert.deepEqual(clean.observation,validReport().observation);
  assert.equal(clean.requires_user_confirmation,true);assert.equal(clean.eligible_for_auto_save,false);
  for(const key of ["raw_output","image_sha256","path","error"])assert.ok(!(key in clean));
  assert.ok(!("note" in clean.quality));assert.ok(!("path" in clean.quality));
  assert.equal(clean.input_source,"public_sample");assert.equal(modelCookies,0);
  assert.equal(clean.observation.limitations.length,2,"Keep all user-visible limitations");
  for(const mode of ["huge","html","bad-schema","private-error","redirect"]){modelMode=mode;await denied(await POST(fresh(mode==="redirect"?{body:JSON.stringify({image_base64:Buffer.from("private fixture pixels").toString("base64")})}:{})),503,"inference_unavailable");}
  assert.equal(trapRequests,0,"Photo POST redirects must never forward the image body to a foreign listener");
  for(const [status,error] of [[400,"invalid_image"],[413,"image_too_large"],[429,"busy"],[503,"model_starting"],[503,"inference_unavailable"],[503,"sample_unavailable"]]){
    modelMode=`allowed-${status}-${error}`;await denied(await POST(fresh()),status,error);
  }
  modelMode="invalid-output";const invalidOutput=await POST(fresh());assert.equal(invalidOutput.status,200);
  const invalidPayload=await invalidOutput.json();assert.equal(invalidPayload.status,"invalid_model_output");assert.ok(!("error" in invalidPayload));assert.ok(!("raw_output" in invalidPayload));
  modelMode="rejected";const rejected=await POST(fresh());assert.equal(rejected.status,200);assert.equal((await rejected.json()).observation.image_scope,"uncertain");
  modelMode="non-skin";const nonSkin=await POST(fresh());assert.equal(nonSkin.status,200);assert.equal((await nonSkin.json()).observation.image_scope,"non_skin");
  modelMode="ready";
  const priorInvalid=modelRequests;
  for(const [body,status,error] of [[{image_base64:""},400,"missing_image"],[{image_base64:"%%=="},400,"invalid_image"],[{image_base64:"AB=="},400,"invalid_image"],[{image_base64:"A".repeat(4*1024*1024+4)},413,"image_too_large"]])await denied(await POST(fresh({body:JSON.stringify(body)})),status,error);
  await denied(await POST(fresh({headers:{"content-length":"4, 4"}})),400,"invalid_request");
  assert.equal(modelRequests,priorInvalid,"Invalid file encoding must be rejected before acquiring GPU inference work");
  assert.equal((await POST(fresh({body:JSON.stringify({sample:true,image_base64:"unused private image",note:"context"})}))).status,200);
  assert.deepEqual(lastModelBody,{sample:true,note:"context"},"Sample mode must not forward unused uploaded pixels");
  const upload=await POST(fresh({body:JSON.stringify({image_base64:Buffer.from("fixture bytes").toString("base64"),note:"n".repeat(600)})}));
  assert.equal(upload.status,200);assert.equal((await upload.json()).input_source,"user_photo");assert.equal(lastModelBody.note.length,500);
  // This checks the proxy's encoding boundary; decoding genuine image formats remains the GPU service's job.
  const maximumEncoding=Buffer.alloc(3*1024*1024).toString("base64");
  assert.equal((await POST(fresh({body:JSON.stringify({image_base64:maximumEncoding})}))).status,200);
  assert.equal(lastModelBody.image_base64.length,4*1024*1024);

  // Fast test-only transport deadlines cover both headers and never-ending JSON bodies.
  timeoutCap=80;
  for(const user of ["auth-stall-headers","auth-stall-body"]){await denied(await POST(request(user)),503,"auth_unavailable");}
  await waitUntil(()=>authDisconnects>=2);
  modelMode="stall";await denied(await POST(fresh()),503,"inference_unavailable");await waitUntil(()=>modelDisconnects>=1);
  healthMode="stall";const stalledHealth=await GET();assert.equal(stalledHealth.status,503);
  timeoutCap=Infinity;modelMode="ready";healthMode="ready";

  const priorAbortAuth=authRequests,already=new AbortController();already.abort();
  await denied(await POST(fresh({signal:already.signal})),400,"request_aborted");assert.equal(authRequests,priorAbortAuth);
  const authAbort=new AbortController(),authWaiting=POST(request("auth-stall-body",{signal:authAbort.signal}));
  await waitUntil(()=>authRequests>priorAbortAuth);authAbort.abort();await denied(await authWaiting,400,"request_aborted");

  // Concurrent health calls have an independent bounded admission budget.
  healthMode="stall";const healthControllers=[new AbortController(),new AbortController()];
  const healthPending=healthControllers.map(controller=>GET(new NextRequest("http://photo.test/api/skin-photo",{signal:controller.signal})));
  await delay(20);assert.equal((await GET()).status,503);
  healthControllers.forEach(controller=>controller.abort());await Promise.all(healthPending);
  healthMode="ready";assert.equal((await GET()).status,200);

  let cancelled = 0;
  const controllers = Array.from({ length: 4 }, () => new AbortController());
  const priorAuth = authRequests;
  const pending = controllers.map((controller, index) => POST(request("slow-" + index, {
    body: new ReadableStream({ cancel() { cancelled += 1; } }), signal: controller.signal,
  })));
  await waitUntil(() => authRequests >= priorAuth + 4); await delay(20);
  const capped = await POST(request("overload"));
  assert.equal(capped.status, 429);
  assert.equal((await capped.json()).error, "busy");
  controllers.forEach(controller => controller.abort());
  assert.deepEqual((await Promise.all(pending)).map(response => response.status), [400, 400, 400, 400]);
  assert.equal(cancelled, 4);
  assert.equal((await POST(request("after-abort"))).status, 200);
  modelMode="stall";
  const upstreamControllers=Array.from({length:4},()=>new AbortController()),priorModel=modelRequests;
  const upstreamPending=upstreamControllers.map(controller=>POST(fresh({signal:controller.signal})));
  await waitUntil(()=>modelRequests>=priorModel+4);
  assert.equal((await POST(fresh())).status,429);
  upstreamControllers.forEach(controller=>controller.abort());
  assert.deepEqual((await Promise.all(upstreamPending)).map(response=>response.status),[400,400,400,400]);
  modelMode="ready";assert.equal((await POST(fresh())).status,200,"Upstream cancellation must release every admission slot");
  for (let i = 0; i < 4; i++) assert.equal((await POST(request("rate-user"))).status, 200);
  assert.equal((await POST(request("rate-user"))).status, 429);
  assert.ok(modelRequests > 0);
  assert.equal(modelCookies,0);assert.equal(trapRequests,0);
  console.log("Photo route passed: bounded/sanitized anonymous health and real-schema reports, redirect refusal, auth/upload/upstream abort cleanup, encoding boundaries, stalled/oversized/invalid upstreams and independent admission limits (local fixtures only)");
} finally {
  AbortSignal.timeout=originalTimeout;
  if (originalService === undefined) delete process.env.PHOTO_MODEL_BASE_URL;
  else process.env.PHOTO_MODEL_BASE_URL = originalService;
  await close(auth); await close(model);await close(trap);
}
