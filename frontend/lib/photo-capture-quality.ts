import type { NormalizedPoint, CaptureHint } from './face-regions';

export type CaptureExposure = 'balanced' | 'possible_underexposure' | 'possible_overexposure' | 'possible_backlight' | 'uncertain';
export type CaptureImageData = { width:number; height:number; data:Uint8ClampedArray | Uint8Array };
export type LuminanceSummary = { mean:number; p05:number; p10:number; p50:number; p90:number; p95:number; p99:number; dynamicRange:number };
export type PhotoCaptureQuality = {
  valid:boolean; advisoryOnly:true; calibratedForSkinTones:false;
  region:'face'|'frame'; faceRoiUsed:boolean; sampleCount:number; exposure:CaptureExposure;
  luminance:LuminanceSummary|null; darkFraction:number|null; brightFraction:number|null;
  relativeSharpness:number|null; blurHint:boolean; hints:CaptureHint[];
};

type Stats = { count:number; histogram:number[]; sum:number; squared:number; dark:number; bright:number; lapSum:number; lapSquared:number; lapCount:number };
const stats = ():Stats => ({count:0,histogram:Array(256).fill(0),sum:0,squared:0,dark:0,bright:0,lapSum:0,lapSquared:0,lapCount:0});

function area(points:readonly NormalizedPoint[]):number {
  let sum=0;for(let i=0;i<points.length;i++){const a=points[i],b=points[(i+1)%points.length];sum+=a.x*b.y-b.x*a.y;}return Math.abs(sum)/2;
}
function inside(x:number,y:number,polygon:readonly NormalizedPoint[]):boolean {
  let value=false;for(let i=0,j=polygon.length-1;i<polygon.length;j=i++){
    const a=polygon[i],b=polygon[j];if((a.y>y)!==(b.y>y)&&x<(b.x-a.x)*(y-a.y)/(b.y-a.y)+a.x)value=!value;
  }return value;
}
function percentile(histogram:number[],count:number,p:number):number {
  const rank=Math.max(1,Math.ceil(count*p));let sum=0;
  for(let i=0;i<256;i++){sum+=histogram[i];if(sum>=rank)return i;}return 255;
}
function summarize(value:Stats):LuminanceSummary {
  const at=(p:number)=>percentile(value.histogram,value.count,p);
  return {mean:value.sum/value.count,p05:at(.05),p10:at(.1),p50:at(.5),p90:at(.9),p95:at(.95),p99:at(.99),dynamicRange:at(.95)-at(.05)};
}

/** Uncalibrated capture advice, not skin-tone grading or a hard upload gate.
 * Display-encoded RGB luminance is not physical camera exposure. Sharpness is
 * a resolution/noise-dependent image statistic, never pores/scars/depth.
 */
export function analyzePhotoCaptureQuality(image:CaptureImageData,facePolygon?:readonly NormalizedPoint[]|null):PhotoCaptureQuality {
  const result:PhotoCaptureQuality={valid:false,advisoryOnly:true,calibratedForSkinTones:false,region:'frame',faceRoiUsed:false,sampleCount:0,
    exposure:'uncertain',luminance:null,darkFraction:null,brightFraction:null,relativeSharpness:null,blurHint:false,hints:[]};
  const hint=(code:string,message:string)=>result.hints.push({code,message});
  const {width,height,data}=image??{} as CaptureImageData;
  if(!Number.isSafeInteger(width)||!Number.isSafeInteger(height)||width<3||height<3||width*height>40_000_000||!data||data.length!==width*height*4){
    hint('invalid_image_data','暫時無法檢查畫面，請確認相機已就緒。');return result;
  }
  const validPolygon=Array.isArray(facePolygon)&&facePolygon.length>=3&&facePolygon.length<=64&&facePolygon.every(p=>p&&Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.x>=0&&p.x<=1&&p.y>=0&&p.y<=1)&&area(facePolygon)>.001;
  const polygon=validPolygon?facePolygon!:null;
  if(facePolygon?.length&&!validPolygon)hint('face_roi_unavailable','臉部範圍暫時不穩定，以下僅供畫面檢查。');
  const frame=stats(),face=stats();
  // Bound work for unusual aspect ratios as well as normal camera frames.
  let stride=Math.max(1,Math.ceil(Math.sqrt((width-2)*(height-2)/16000)),Math.ceil((width-2)/16000),Math.ceil((height-2)/16000));
  while(Math.ceil((width-2)/stride)*Math.ceil((height-2)/stride)>16000)stride++;
  const luma=(x:number,y:number):number=>{const i=(y*width+x)*4;return .2126*data[i]+.7152*data[i+1]+.0722*data[i+2];};
  const add=(value:Stats,luminance:number)=>{value.count++;value.sum+=luminance;value.squared+=luminance*luminance;value.histogram[Math.round(luminance)]++;if(luminance<=8)value.dark++;if(luminance>=247)value.bright++;};
  for(let y=1;y<height-1;y+=stride)for(let x=1;x<width-1;x+=stride){
    const i=(y*width+x)*4;if(data[i+3]<250)continue;
    const current=luma(x,y);add(frame,current);
    const selected=polygon&&inside((x+.5)/width,(y+.5)/height,polygon);
    if(selected)add(face,current);
    const target=selected?face:polygon?null:frame;
    if(!target)continue;
    const neighbours=[[x-1,y],[x+1,y],[x,y-1],[x,y+1]];
    if(neighbours.some(([a,b])=>data[(b*width+a)*4+3]<250||(polygon&&!inside((a+.5)/width,(b+.5)/height,polygon))))continue;
    const lap=4*current-luma(x-1,y)-luma(x+1,y)-luma(x,y-1)-luma(x,y+1);
    target.lapSum+=lap;target.lapSquared+=lap*lap;target.lapCount++;
  }
  const selected=polygon&&face.count>=32?face:frame;
  if(polygon&&selected===frame)hint('face_roi_too_small','臉部範圍較小，請在清楚對焦下稍微靠近。');
  if(selected.count<32){hint('insufficient_pixels','目前可用畫面不足，請稍候再拍。');return result;}
  result.valid=true;result.faceRoiUsed=selected===face;result.region=result.faceRoiUsed?'face':'frame';result.sampleCount=selected.count;
  const summary=summarize(selected);result.luminance=summary;
  result.darkFraction=selected.dark/selected.count;result.brightFraction=selected.bright/selected.count;
  result.exposure='balanced';
  const background=frame.count?summarize(frame):summary;
  // Low average intensity alone never identifies underexposure or dark skin.
  const clippedDark=result.darkFraction>=.30&&summary.p90<=48&&summary.dynamicRange<=60;
  const backlit=result.faceRoiUsed&&result.darkFraction>=.12&&summary.p10<=8&&summary.p90<100&&background.p90>180&&background.p50-summary.p50>50&&frame.bright/frame.count>=.05;
  const clippedBright=result.brightFraction>=.22&&summary.p50>220&&summary.p10>185;
  if(backlit){result.exposure='possible_backlight';hint('possible_backlight','背景較亮、臉部有接近純黑的區域；可轉向柔和光源並避免逆光。');}
  else if(clippedDark){result.exposure='possible_underexposure';hint('dark_clipping','畫面有大片接近純黑的區域，請確認柔和、均勻的照明。');}
  else if(clippedBright){result.exposure='possible_overexposure';hint('bright_clipping','畫面有大片接近純白的區域，可避開直射光與強烈閃光。');}
  // Exploratory display advice only: low intensities may also be skin tone,
  // and a white background is not evidence that the face is overexposed.
  // These thresholds are not calibrated camera/phone or skin-tone measures.
  else if(summary.p95<=55)hint('check_light','畫面整體較暗；若想觀察的部位看不清，可增加柔和光源後重拍。');
  if(!result.faceRoiUsed&&result.brightFraction>=.22&&!clippedBright){
    hint('check_bright_regions','畫面有較大的接近純白區域；若是想觀察的皮膚，請避開強光後重拍。');
  }
  const variance=Math.max(0,selected.squared/selected.count-(selected.sum/selected.count)**2);
  if(selected.lapCount>=32&&variance>1){
    const lapVariance=Math.max(0,selected.lapSquared/selected.lapCount-(selected.lapSum/selected.lapCount)**2);
    result.relativeSharpness=lapVariance/(variance+1);
    // Do not grade dark/low-contrast skin texture as blur from mean intensity.
    if(result.faceRoiUsed&&summary.dynamicRange>=30&&lapVariance<12&&result.relativeSharpness<.025){
      result.blurHint=true;hint('check_focus','輪廓可能偏柔，請確認對焦並保持相機穩定。');
    }
  }
  return result;
}
