import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const source = read('components/skin-photo/PublicPhotoExamples.tsx');
const ast = ts.createSourceFile('PublicPhotoExamples.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let selectNode, lifecycleNode;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'select') selectNode = node;
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'useEffect') lifecycleNode = node.arguments[0];
  ts.forEachChild(node, visit);
}
visit(ast);
assert.ok(selectNode, 'Exercise the actual gallery selection function');
assert.ok(lifecycleNode, 'Exercise the actual effect cleanup rather than an invented unmount');
const extracted = `${selectNode.getText(ast)}\nconst runLifecycle = ${lifecycleNode.getText(ast)};\nglobalThis.select = select; globalThis.mount = runLifecycle;`;
const compiled = ts.transpileModule(extracted, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve = a; reject = b; }); return {promise,resolve,reject}; };
const imageBlob = () => new Blob(['public fixture bytes'], {type:'image/jpeg'});
const response = (blob = imageBlob()) => ({ok:true, blob:async () => blob});
function fakeClock() {
  let now=0,sequence=0; const timers=new Map();
  return {
    setTimeout(callback,delay) {const id=++sequence; timers.set(id,{callback,at:now+delay}); return id;},
    clearTimeout(id) {timers.delete(id);},
    advance(milliseconds) {
      now+=milliseconds;
      for(const [id,timer] of [...timers]) if(timer.at<=now){timers.delete(id);timer.callback();}
    },
    count:()=>timers.size,
  };
}

function harness(fetchResult, overrides = {}) {
  const state = {events:[],selected:[],loading:'',error:'',parentLoading:false,fetches:[]};
  const scope = {
    disabled:false, pending:{current:false}, controller:{current:null}, downloadTimer:{current:null}, mounted:{current:true},
    AbortController, Blob, File, Error, setTimeout, clearTimeout,
    setLoading(value) { state.loading=value; state.events.push(['loading',value]); },
    setError(value) { state.error=value; state.events.push(['error',value]); },
    onLoadingChange(value) { state.parentLoading=value; state.events.push(['parent_loading',value]); },
    fetch(url, options) { state.events.push(['fetch',url]); state.fetches.push({url,options}); return typeof fetchResult === 'function' ? fetchResult(url,options) : fetchResult; },
    async onSelect(file, context) { state.selected.push({file,context}); },
    ...overrides,
  };
  vm.runInNewContext(compiled, scope);
  const cleanup = scope.mount();
  return {state,scope,cleanup,select:scope.select};
}

const manifest = JSON.parse(read('public/skin-photo-examples/manifest.json'));
const portrait = manifest.examples.find(row => row.file === 'portrait.jpg');
assert.ok(portrait);
const download = deferred(), prepared = deferred();
const h = harness(download.promise, {onSelect:async (file,context) => {h.state.selected.push({file,context}); await prepared.promise;}});
const choosing = h.select(portrait);
assert.equal(h.state.parentLoading,true,'Parent admission begins synchronously before the fetch can settle');
assert.ok(h.state.events.findIndex(event=>event[0]==='parent_loading'&&event[1]===true) < h.state.events.findIndex(event=>event[0]==='fetch'));
assert.equal(h.state.fetches[0].options.credentials,'same-origin');
assert.ok(h.state.fetches[0].options.signal instanceof AbortSignal);
await h.select(portrait);
assert.equal(h.state.fetches.length,1,'A second click cannot enqueue another request while downloading');
download.resolve(response());
await new Promise(resolve=>setImmediate(resolve));
assert.equal(h.state.selected.length,1);
assert.equal(h.state.parentLoading,true,'Keep sibling page actions disabled throughout asynchronous photo preparation');
await h.select(portrait);
assert.equal(h.state.fetches.length,1,'Pending parent preparation must also reject duplicate clicks');
assert.equal(h.state.selected[0].file.name,portrait.file);
assert.equal(h.state.selected[0].file.type,'image/jpeg');
assert.equal(h.state.selected[0].context.localizationBlocked,false);
prepared.resolve(); await choosing;
assert.equal(h.state.parentLoading,false); assert.equal(h.state.loading,'');
assert.equal(h.scope.pending.current,false); assert.equal(h.scope.controller.current,null);
h.cleanup();

const disabled = harness(Promise.resolve(response()),{disabled:true});
await disabled.select(portrait); assert.equal(disabled.state.fetches.length,0); disabled.cleanup();

// An abort-insensitive deferred transport must not resurrect a selection after unmount.
const late = deferred(), unmounted = harness(late.promise);
const abandoned = unmounted.select(portrait);
unmounted.cleanup();
assert.equal(unmounted.state.fetches[0].options.signal.aborted,true);
assert.equal(unmounted.scope.mounted.current,false);
const beforeLate = unmounted.state.events.length;
late.resolve(response()); await abandoned;
assert.equal(unmounted.state.selected.length,0);
assert.equal(unmounted.state.events.length,beforeLate,'Late completion cannot update unmounted component/parent state');
assert.equal(unmounted.scope.pending.current,false);

// Also check unmount between response headers and completion of the blob body.
const body = deferred(), midBody = harness(Promise.resolve({ok:true,blob:()=>body.promise}));
const readingBody = midBody.select(portrait);
await Promise.resolve(); midBody.cleanup(); body.resolve(imageBlob()); await readingBody;
assert.equal(midBody.state.selected.length,0);
assert.equal(midBody.state.fetches[0].options.signal.aborted,true);

const cancelledFetch = deferred(), cancelled = harness(cancelledFetch.promise);
const cancelling = cancelled.select(portrait);
cancelled.scope.controller.current.abort(); cancelledFetch.resolve(response()); await cancelling;
assert.equal(cancelled.state.selected.length,0); assert.equal(cancelled.state.error,'');
assert.equal(cancelled.state.parentLoading,false); cancelled.cleanup();

for (const invalidResponse of [
  {ok:false,blob:async()=>{throw new Error('Must not read an HTTP error body');}},
  response({size:1024*1024+1,type:'image/jpeg'}),
  response(new Blob(['not an image'],{type:'application/json'})),
]) {
  const bad=harness(Promise.resolve(invalidResponse)); await bad.select(portrait);
  assert.equal(bad.state.selected.length,0); assert.ok(bad.state.error.length>0);
  assert.equal(bad.state.parentLoading,false); assert.equal(bad.scope.pending.current,false); bad.cleanup();
}
const network=harness(Promise.reject(new Error('offline'))); await network.select(portrait);
assert.equal(network.state.selected.length,0); assert.equal(network.state.parentLoading,false); network.cleanup();
const failedParent=harness(Promise.resolve(response()),{onSelect:async()=>{throw new Error('prepare failed');}});
await failedParent.select(portrait); assert.equal(failedParent.state.parentLoading,false); assert.equal(failedParent.scope.pending.current,false); failedParent.cleanup();

// Fake time proves the total download deadline, without sleeping ten seconds.
// Even an abort-insensitive transport cannot retain the parent admission lock.
const clock=fakeClock(), neverFetch=deferred();
const timed=harness(neverFetch.promise,{setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout});
const expiring=timed.select(portrait);
clock.advance(9999); await Promise.resolve();
assert.equal(timed.state.parentLoading,true); assert.equal(timed.state.error,'');
clock.advance(1); await expiring;
assert.equal(timed.state.fetches[0].options.signal.aborted,true);
assert.equal(timed.state.selected.length,0); assert.match(timed.state.error,/逾時/);
assert.equal(timed.state.parentLoading,false); assert.equal(timed.scope.pending.current,false); assert.equal(clock.count(),0);
neverFetch.resolve(response()); await new Promise(resolve=>setImmediate(resolve));
assert.equal(timed.state.selected.length,0,'A transport completing after the deadline cannot select a stale image'); timed.cleanup();

const bodyClock=fakeClock(), stalledBody=deferred();
const timedBody=harness(Promise.resolve({ok:true,blob:()=>stalledBody.promise}),{setTimeout:bodyClock.setTimeout,clearTimeout:bodyClock.clearTimeout});
const expiringBody=timedBody.select(portrait); await Promise.resolve(); bodyClock.advance(10000); await expiringBody;
assert.match(timedBody.state.error,/逾時/); assert.equal(timedBody.state.parentLoading,false);
assert.equal(timedBody.state.fetches[0].options.signal.aborted,true);
stalledBody.resolve(imageBlob()); await new Promise(resolve=>setImmediate(resolve));
assert.equal(timedBody.state.selected.length,0); timedBody.cleanup();

const prepareClock=fakeClock(), longPrepare=deferred();
const notPreparation=harness(Promise.resolve(response()),{setTimeout:prepareClock.setTimeout,clearTimeout:prepareClock.clearTimeout,onSelect:()=>longPrepare.promise});
const preparing=notPreparation.select(portrait); await new Promise(resolve=>setImmediate(resolve));
assert.equal(prepareClock.count(),0,'Stop the deadline after the image body is downloaded, before parent preparation');
prepareClock.advance(20000); assert.equal(notPreparation.state.error,'');
assert.equal(notPreparation.state.fetches[0].options.signal.aborted,false);
longPrepare.resolve(); await preparing; notPreparation.cleanup();

const unmountClock=fakeClock(), unmountDownload=deferred();
const beforeTimeout=harness(unmountDownload.promise,{setTimeout:unmountClock.setTimeout,clearTimeout:unmountClock.clearTimeout});
const awaitingUnmount=beforeTimeout.select(portrait); beforeTimeout.cleanup();
assert.equal(unmountClock.count(),0,'Unmount clears the timer as well as aborting the download');
unmountClock.advance(20000); unmountDownload.resolve(response()); await awaitingUnmount;
assert.equal(beforeTimeout.state.selected.length,0); assert.equal(beforeTimeout.state.error,'');

const masked=manifest.examples.filter(row=>row.file.startsWith('memi-')||row.file.startsWith('skin-'));
assert.equal(masked.length,4);
for(const example of manifest.examples) {
  assert.equal(example.file,example.file.split('/').at(-1),'Static example paths are filenames, not arbitrary request destinations');
  const bytes=readFileSync(new URL(`../public/skin-photo-examples/${example.file}`,import.meta.url));
  assert.ok(bytes.length>0&&bytes.length<=1024*1024);
  assert.equal(createHash('sha256').update(bytes).digest('hex'),example.sha256,example.file);
  assert.equal(example.appearance_ground_truth,false,'Functional examples do not acquire phenotype gold labels');
}
for(const example of masked) {
  assert.equal(example.localizationBlocked,true);
  const sample=harness(Promise.resolve(response())); await sample.select(example);
  assert.equal(sample.state.selected[0].context.localizationBlocked,true,'Known masks must reach the parent as a blocked localization context'); sample.cleanup();
}
const variants=manifest.examples.filter(row=>row.file.startsWith('portrait'));
assert.equal(variants.length,4); assert.equal(new Set(variants.map(row=>row.original_sha256)).size,1,'Derived stress variants are one original image, not four independent captures');

const metrics=JSON.parse(read('public/skin-research/current-metrics.json'));
assert.equal(metrics.validation_images,80);
assert.equal(metrics.validation_skin_images+metrics.validation_object_controls,80);
assert.equal(metrics.independent_phone_accuracy_measured,false);
const close=(a,b)=>assert.ok(Number.isFinite(a)&&Math.abs(a-b)<1e-12,`${a} differs from independent count calculation ${b}`);
for(const model of Object.values(metrics.metrics)) {
  assert.equal(model.images,80); const f1s=[];
  for(const value of Object.values(model.by_tag)) {
    for(const key of ['tp','fp','fn','tn']) assert.ok(Number.isInteger(value[key])&&value[key]>=0);
    const {tp,fp,fn,tn}=value;
    assert.equal(tp+fp+fn+tn,80,'Accuracy includes all validation images, including negative object controls');
    close(value.accuracy,(tp+tn)/80);
    close(value.precision,tp+fp?tp/(tp+fp):0);
    close(value.recall,tp+fn?tp/(tp+fn):0);
    close(value.f1,2*tp+fp+fn?2*tp/(2*tp+fp+fn):0); f1s.push(value.f1);
  }
  assert.equal(f1s.length,4); close(model.macro_f1,f1s.sort((a,b)=>a-b).reduce((a,b)=>a+b,0)/4);
}
const page=read('app/skin-photo/page.tsx');
assert.match(page,/onLoadingChange=\{setExampleLoading\}/,'The gallery loading lock must actually be connected to the parent');
assert.match(page,/const preparing = preparingPhoto \|\| exampleLoading/,'Parent actions must include the gallery fetch in admission');
console.log('Public photo examples passed: real selection/effect lifecycle, synchronous parent admission, ten-second download deadline, deferred fetch/preparation, duplicate suppression, abort/unmount cleanup, blocked masks, asset hashes and independent metric denominators');
