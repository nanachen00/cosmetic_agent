import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source=readFileSync(new URL('../lib/photo-capture-quality.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {analyzePhotoCaptureQuality:quality}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const polygon=[{x:.25,y:.15},{x:.75,y:.15},{x:.75,y:.85},{x:.25,y:.85}];
function image(fn,width=100,height=100){const data=new Uint8ClampedArray(width*height*4);for(let y=0;y<height;y++)for(let x=0;x<width;x++){const v=fn(x,y,width,height),i=(y*width+x)*4;data[i]=data[i+1]=data[i+2]=v;data[i+3]=255;}return {width,height,data};}
const roi=(x,y,w,h)=>x/w>=.25&&x/w<=.75&&y/h>=.15&&y/h<=.85;
for(const background of [0,255]){
  const result=quality(image((x,y,w,h)=>roi(x,y,w,h)?100+(x*7+y*3)%50:background),polygon);
  assert.equal(result.exposure,'balanced');assert.equal(result.faceRoiUsed,true);assert.equal(result.region,'face');
  assert.ok(result.luminance.mean>=100&&result.luminance.mean<=150);
  assert.equal(result.advisoryOnly,true);assert.equal(result.calibratedForSkinTones,false);
  assert.ok(!result.hints.some(h=>h.code==='check_bright_regions'),'A white background must not produce a bright-face advisory when a normal face ROI is available');
  assert.ok(!result.hints.some(h=>h.code==='check_light'));
}
const darkProxy=quality(image((x,y,w,h)=>roi(x,y,w,h)?22+(x*7+y*3)%24:255),polygon);
assert.equal(darkProxy.exposure,'balanced','low face mean alone cannot label a darker-skin proxy underexposed');
assert.equal(darkProxy.darkFraction,0);
assert.equal(darkProxy.blurHint,false,'A darker-intensity textured proxy does not imply blur');
assert.ok(darkProxy.hints.some(h=>h.code==='check_light'&&h.message==='畫面整體較暗；若想觀察的部位看不清，可增加柔和光源後重拍。'),'Lower-intensity skin receives the same conditional light suggestion without an underexposure claim');
const dimUnclipped=quality(image((x,y,w,h)=>roi(x,y,w,h)?20+(x*7+y*3)%27:80),polygon);
assert.equal(dimUnclipped.exposure,'balanced');assert.equal(dimUnclipped.darkFraction,0);
assert.ok(dimUnclipped.hints.some(h=>h.code==='check_light'));
assert.ok(!dimUnclipped.hints.some(h=>h.code==='dark_clipping'));
assert.equal(dimUnclipped.blurHint,false);
const backlit=quality(image((x,y,w,h)=>roi(x,y,w,h)?(x%8)*2:255),polygon);
assert.equal(backlit.exposure,'possible_backlight');assert.ok(backlit.hints.some(h=>h.code==='possible_backlight'));
assert.ok(!backlit.hints.some(h=>h.code==='check_light'),'Do not duplicate the existing backlight advice');
const bright=quality(image((x,y,w,h)=>roi(x,y,w,h)?252+(x%4):80),polygon);
assert.equal(bright.exposure,'possible_overexposure');assert.ok(bright.brightFraction>.9);
const black=quality(image((x,y)=>x%6),polygon);
assert.equal(black.exposure,'possible_underexposure');assert.ok(black.darkFraction>.9);
assert.ok(!black.hints.some(h=>h.code==='check_light'),'Existing clipping classifications and thresholds are unchanged');
const whiteBackground=quality(image((x,y,w,h)=>roi(x,y,w,h)?100+(x+y)%40:255));
assert.equal(whiteBackground.faceRoiUsed,false);assert.ok(whiteBackground.brightFraction>=.22);
assert.equal(whiteBackground.exposure,'balanced','Large white areas alone do not identify an overexposed face');
assert.ok(whiteBackground.hints.some(h=>h.code==='check_bright_regions'&&h.message==='畫面有較大的接近純白區域；若是想觀察的皮膚，請避開強光後重拍。'));
const clippedFrame=quality(image(()=>255));assert.equal(clippedFrame.exposure,'possible_overexposure');
assert.ok(!clippedFrame.hints.some(h=>h.code==='check_bright_regions'),'No duplicate neutral advice for existing bright clipping');
const smallWhitePatch=quality(image((x)=>x<10?255:110));
assert.ok(!smallWhitePatch.hints.some(h=>h.code==='check_bright_regions'));

const sharp=image((x,y)=>45+x*1.5+((x+y)%2?22:-22));
function boxBlur(input,radius){return image((x,y,w,h)=>{let sum=0,count=0;for(let dy=-radius;dy<=radius;dy++)for(let dx=-radius;dx<=radius;dx++){const a=x+dx,b=y+dy;if(a>=0&&a<w&&b>=0&&b<h){sum+=input.data[(b*w+a)*4];count++;}}return sum/count;},input.width,input.height);}
const clear=quality(sharp,polygon),soft=quality(boxBlur(sharp,4),polygon);
assert.ok(clear.relativeSharpness>soft.relativeSharpness*10,'same-content blur reduces the relative edge statistic');
assert.equal(clear.blurHint,false);assert.equal(soft.blurHint,true);
const flat=quality(image(()=>45),polygon);assert.equal(flat.blurHint,false,'low contrast alone does not grade skin as blurry');
assert.equal(flat.relativeSharpness,null);
for(const invalid of [{width:0,height:3,data:new Uint8Array(0)},{width:3,height:3,data:new Uint8Array(10)},{width:3.1,height:3,data:new Uint8Array(36)}]){const result=quality(invalid);assert.equal(result.valid,false);assert.equal(result.exposure,'uncertain');}
const transparent=image(()=>100);transparent.data.fill(0);assert.equal(quality(transparent).valid,false);
const fallback=quality(image((x,y)=>80+(x+y)%80),[{x:-.1,y:0},{x:.5,y:0},{x:.5,y:1}]);
assert.equal(fallback.valid,true);assert.equal(fallback.region,'frame');assert.equal(fallback.faceRoiUsed,false);assert.ok(fallback.hints.some(h=>h.code==='face_roi_unavailable'));
const noFace=quality(image((x,y)=>80+(x+y)%80));assert.equal(noFace.valid,true);assert.equal(noFace.faceRoiUsed,false);
const large=quality(image((x,y)=>50+(x+y)%100,1000,1000));assert.ok(large.sampleCount<=16000);
const unusualAspect=quality(image((x,y)=>50+(x+y)%100,100000,3));assert.ok(unusualAspect.sampleCount<=16000,'sampling is bounded even for a very wide frame');
console.log('Capture quality tests passed: face ROI/background separation, clipping/backlight, darker-intensity proxy, same-content blur, bounds/fallback; synthetic checks do not establish skin-tone calibration');
