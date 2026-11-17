import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/face-regions.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { buildFaceRegions, regionsForObservation, FACE_OVAL_INDICES, pointInPolygon } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));

// Synthetic semantic meshes exercise geometry, not detector accuracy or population coverage.
function mesh() {
  const points = Array.from({length:478}, () => ({x:.5,y:.5,z:0}));
  const put = (id,x,y) => {points[id]={x,y,z:0};};
  FACE_OVAL_INDICES.forEach((id,i) => {const a=-Math.PI/2+i*Math.PI*2/FACE_OVAL_INDICES.length;put(id,.5+.32*Math.cos(a),.5+.4*Math.sin(a));});
  const left=[263,249,390,373,374,380,381,382,362,398,384,385,386,387,388,466];
  const right=[33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246];
  left.forEach((id,i)=>{const a=i*Math.PI*2/16;put(id,.615+.065*Math.cos(a),.4+.025*Math.sin(a));});
  right.forEach((id,i)=>{const a=Math.PI-i*Math.PI*2/16;put(id,.385+.065*Math.cos(a),.4+.025*Math.sin(a));});
  [300,293,334,296,336].forEach((id,i)=>put(id,[.71,.68,.645,.61,.56][i],.32));
  [70,63,105,66,107].forEach((id,i)=>put(id,[.29,.32,.355,.39,.44][i],.32));
  [168,6,197,195,5,4,1,19,94,2].forEach((id,i)=>put(id,.5,[.42,.44,.47,.49,.51,.53,.56,.585,.60,.61][i]));
  for (const [id,x,y] of [[327,.545,.59],[326,.52,.61],[294,.56,.57],[278,.56,.54],[344,.545,.51],[440,.53,.53],[275,.52,.55],[98,.455,.59],[97,.48,.61],[64,.44,.57],[48,.44,.54],[115,.455,.51],[220,.47,.53],[45,.48,.55],[61,.42,.69],[291,.58,.69],[17,.5,.735],[84,.47,.73],[314,.53,.73]]) put(id,x,y);
  return points;
}
const face=mesh();
const first=buildFaceRegions(face,{faceCount:1,imageWidth:640,imageHeight:480});
assert.equal(first.reliable,true,JSON.stringify(first.reasons));
assert.equal(first.regions.length,7);
assert.equal(first.coordinateSpace,'normalized-original-image');
assert.equal(first.facePolygon.length,36);
assert.deepEqual(first.faceOutline,first.facePolygon);
assert.notEqual(first.faceOutline,first.facePolygon);
for(const region of first.regions) assert.ok(region.polygon.every(p=>pointInPolygon(p,first.facePolygon)));
assert.equal(regionsForObservation('cheek',first).length,2);
assert.equal(regionsForObservation('eye_area',first).length,2);
assert.equal(regionsForObservation('unknown',first).length,0);
assert.ok(first.regions.find(r=>r.id==='left_cheek').polygon.reduce((s,p)=>s+p.x,0)/first.regions.find(r=>r.id==='left_cheek').polygon.length>.5);

function transformed(points,{sx=1,sy=1,angle=0,width=640,height=480}={}) {
  const a=angle*Math.PI/180;
  return points.map(p=>{const x=(p.x-.5)*sx*width,y=(p.y-.5)*sy*height;return {...p,x:.5+(x*Math.cos(a)-y*Math.sin(a))/width,y:.5+(x*Math.sin(a)+y*Math.cos(a))/height};});
}
for(const shape of [{sx:.76,sy:1.05,width:640,height:480},{sx:1.1,sy:.8,width:480,height:640},{sx:.8,sy:.85,angle:24,width:640,height:480},{sx:.72,sy:.72,angle:-30,width:480,height:640}]) {
  const result=buildFaceRegions(transformed(face,shape),{faceCount:1,imageWidth:shape.width,imageHeight:shape.height});
  assert.equal(result.reliable,true,JSON.stringify({shape,reasons:result.reasons}));
  assert.ok(Math.abs(result.rollDegrees-(shape.angle??0))<1e-10,'roll accounts for pixel aspect ratio');
  assert.notDeepEqual(result.facePolygon,first.facePolygon,'regions follow each person’s landmarks');
}
assert.deepEqual(buildFaceRegions(face.map(p=>({...p,z:NaN}))),buildFaceRegions(face),'z is not interpreted as physical depth');
assert.deepEqual(buildFaceRegions(face.map(p=>({...p,x:1-p.x}))).regions.map(r=>r.id),first.regions.map(r=>r.id),'anatomical identity survives a mirrored display');

const rejected=[
  [null,{},'no_face'],[face,{faceCount:2},'multiple_faces'],[face.slice(0,100),{},'insufficient_landmarks'],
  [face.map((p,i)=>i===4?{...p,x:NaN}:p),{},'insufficient_landmarks'],
  [face.map((p,i)=>i===4?{...p,x:1.01}:p),{},'face_out_of_frame'],
  [Array.from({length:478},()=>({x:.5,y:.5})),{},'degenerate_geometry'],
  [face,{imageWidth:0},'invalid_dimensions'],
  [transformed(face,{sx:.1,sy:.1}),{},'small_face'],
  [face.map((p,i)=>i===1?{...p,x:.74}:p),{},'pose_too_oblique'],
  [face.map((p,i)=>i===1?{...p,y:.74}:[61,291].includes(i)?{...p,y:.82}:p),{},'pose_too_oblique'],
  [transformed(face,{sx:.6,sy:.6,angle:50}),{},'pose_too_oblique'],
];
for(const [points,options,reason] of rejected){const result=buildFaceRegions(points,options);assert.equal(result.reliable,false);assert.ok(result.reasons.includes(reason),JSON.stringify(result.reasons));assert.deepEqual(result.regions,[]);assert.deepEqual(result.facePolygon,[]);assert.deepEqual(regionsForObservation('nose',result),[]);}
const closed=face.map((p,i)=>[263,249,390,373,374,380,381,382,362,398,384,385,386,387,388,466].includes(i)?{...p,y:.4}:p);
const closedResult=buildFaceRegions(closed);
assert.equal(closedResult.reliable,true);
assert.ok(!closedResult.regions.some(r=>r.id==='left_eye'),'closed eye does not produce an invented eye area');
console.log('Face region tests passed: individualized shape, aspect/roll/mirroring, semantic regions, conservative invalid/pose handling; synthetic geometry is not accuracy validation');
