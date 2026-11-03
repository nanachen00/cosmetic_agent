"""Deploy only the audited photo page; preserve unrelated frontend work."""
from pathlib import Path
import datetime,hashlib,json,os,shutil,signal,subprocess,time,urllib.request

ROOT=Path('/var/lib/jenkins/tomod');FRONT=ROOT/'frontend';BUNDLE=Path(__file__).resolve().parent
STAMP=datetime.datetime.now().strftime('%Y%m%d-%H%M%S');BACKUP=ROOT/'recovery-backups'/('skin-summary-'+STAMP)

def processes():
    found=[]
    for p in Path('/proc').glob('[0-9]*'):
        try:
            if 'next-server' in (p/'comm').read_text() and (p/'cwd').resolve()==FRONT:found.append(int(p.name))
        except OSError:pass
    return found
def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:3000/skin-photo',timeout=4) as r:return r.status==200
    except Exception:return False

def main():
    old=processes()
    if len(old)!=1:raise RuntimeError('Expected one current frontend')
    audit=json.loads((ROOT/'skin-research-probe.json').read_text())
    for name,key in [('page.tsx','page_text'),('SkinPhoto.module.css','css_text')]:
        current=(FRONT/'app/skin-photo'/name).read_text()
        desired=(BUNDLE/'frontend/app/skin-photo'/name).read_text()
        if current not in (audit[key],desired):raise RuntimeError('Photo source changed since audit: '+name)
    env=dict(os.environ)
    for x in Path(f'/proc/{old[0]}/environ').read_bytes().split(b'\0'):
        if b'=' in x:k,v=x.split(b'=',1);env[k.decode()]=v.decode()
    env['PATH']='/opt/node-v22.23.4/bin:'+env.get('PATH','');env['NODE_ENV']='production'
    old_dist=env.get('NEXT_DIST_DIR','.next');env['NEXT_DIST_DIR']='.next-skin-summary-'+STAMP
    subprocess.run(['/opt/node-v22.23.4/bin/node',str(BUNDLE/'test-summary.cjs'),str(BUNDLE/'frontend/app/skin-photo/reportSummary.ts')],cwd=FRONT,env=env,check=True)
    files=[]
    for name in ['page.tsx','SkinPhoto.module.css','reportSummary.ts']:
        relative=Path('app/skin-photo')/name;target=FRONT/relative;backup=BACKUP/relative;backup.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():shutil.copy2(target,backup)
        data=(BUNDLE/'frontend'/relative).read_bytes();target.write_bytes(data);files.append({'path':str(relative),'sha256':hashlib.sha256(data).hexdigest()})
    public=FRONT/'public/skin-research';public.mkdir(exist_ok=True)
    if not (public/'index.html').exists():(public/'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BLINK 訓練比較紀錄</title><main style="max-width:760px;margin:60px auto;padding:24px;font:16px/1.9 system-ui;color:#66516f"><a href="/skin-photo">← 返回照片分析</a><h1>模型訓練與比較紀錄</h1><p>正在建立原始模型基準與三組 LoRA 對照。完成驗證後會更新此頁。</p><p>使用固定版本 ACNE04 原作者標註，僅作學術研究。驗證集選擇版本，保留測試集最後比較；尚未有改善數據。</p></main>')
    subprocess.run(['/opt/node-v22.23.4/bin/npm','run','build'],cwd=FRONT,env=env,check=True)
    if processes()!=old:raise RuntimeError('Frontend deployment changed while building')
    for record in files:
        if hashlib.sha256((FRONT/record['path']).read_bytes()).hexdigest()!=record['sha256']:raise RuntimeError('Source changed during build')
    def start(e):
        log=(ROOT/'skin-summary-frontend.log').open('a')
        return subprocess.Popen(['/opt/node-v22.23.4/bin/node','node_modules/next/dist/bin/next','start','--hostname','0.0.0.0','--port','3000'],cwd=FRONT,env=e,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    os.kill(old[0],signal.SIGTERM)
    for _ in range(60):
        if not Path(f'/proc/{old[0]}').exists():break
        time.sleep(.2)
    fresh=start(env)
    for _ in range(40):
        if fresh.poll() is not None:break
        if health():
            result={'deployed':True,'dist':env['NEXT_DIST_DIR'],'previous_dist':old_dist,'backup':str(BACKUP),'files':files,'pid':fresh.pid}
            (ROOT/'skin-summary-deployment.json').write_text(json.dumps(result,indent=2));print('SUMMARY_DEPLOYED',flush=True);return
        time.sleep(.5)
    if fresh.poll() is None:fresh.terminate();fresh.wait(timeout=10)
    rollback=dict(env);rollback['NEXT_DIST_DIR']=old_dist;start(rollback);raise RuntimeError('Rolled back failed frontend')

if __name__=='__main__':main()
