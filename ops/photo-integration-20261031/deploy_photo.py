"""Install a scoped photo feature, build separately, then replace the frontend."""
from pathlib import Path
import datetime,json,os,shutil,signal,socket,subprocess,sys,time,urllib.request

ROOT=Path('/var/lib/jenkins/tomod')
FRONT=ROOT/'frontend'
EXPERIMENT=ROOT/'experiments/skin-vlm'
BUNDLE=Path(__file__).resolve().parent
STAMP=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
BACKUP=ROOT/'recovery-backups'/('photo-integration-'+STAMP)
BACKUP.mkdir(parents=True,exist_ok=False)

def backup(path):
    if path.exists():
        destination=BACKUP/path.relative_to(ROOT)
        destination.parent.mkdir(parents=True,exist_ok=True)
        if not destination.exists():shutil.copy2(path,destination)

def change(path,old,new):
    source=path.read_text()
    if source.count(old)!=1:raise RuntimeError('Expected one patch anchor: '+str(path))
    backup(path);path.write_text(source.replace(old,new,1))

def frontend_processes():
    found=[]
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if 'next-server' in (proc/'comm').read_text() and (proc/'cwd').resolve()==FRONT:
                found.append(int(proc.name))
        except OSError:pass
    return found

def health(url):
    try:
        with urllib.request.urlopen(url,timeout=5) as response:return response.status==200
    except Exception:return False

def main():
    existing=frontend_processes()
    if len(existing)!=1:raise RuntimeError('Expected one running frontend')
    old_pid=existing[0]
    # Preserve existing runtime configuration without printing credential values.
    current_env={}
    for item in Path(f'/proc/{old_pid}/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            k,v=item.split(b'=',1);current_env[k.decode()]=v.decode()
    env=dict(os.environ);env.update(current_env)
    env['PATH']='/opt/node-v22.23.4/bin:'+env.get('PATH','')
    env['NODE_ENV']='production'
    old_dist=env.get('NEXT_DIST_DIR','.next')
    env['NEXT_DIST_DIR']='.next-photo-'+STAMP
    # Same-origin auth works in exactly the same way as the existing member-auth route.
    for source in (BUNDLE/'frontend').rglob('*'):
        if source.is_file():
            target=FRONT/source.relative_to(BUNDLE/'frontend');backup(target)
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    shutil.copy2(BUNDLE/'server/photo_service.py',EXPERIMENT/'photo_service.py')
    shutil.copy2(EXPERIMENT/'data/public_acne_ccby4.jpg',FRONT/'public/skin-photo-demo.jpg')
    nav=FRONT/'components/AppShell.tsx'
    source=nav.read_text()
    if 'href: "/skin-photo"' not in source:
        anchor='  { href: "/", label: "首頁", icon: "home" },'
        additions='\n  { href: "/skin-photo", label: "照片膚況分析", icon: "search" },'
        # Retain the knowledge-map navigation already visible in the running build.
        if '"/knowledge-map"' not in source:
            additions+='\n  { href: "/knowledge-map", label: "美妝知識地圖", icon: "chart" },'
        change(nav,anchor,anchor+additions)
    proxy=FRONT/'proxy.ts'
    source=proxy.read_text()
    if 'allowCamera = false' not in source:
        change(proxy,'  contentSecurityPolicy: string,\n): NextResponse {','  contentSecurityPolicy: string,\n  allowCamera = false,\n): NextResponse {')
        change(proxy,'"browsing-topics=(), geolocation=(self), microphone=(), camera=(), payment=(), usb=()",',
               '`browsing-topics=(), geolocation=(self), microphone=(), camera=${allowCamera ? "(self)" : "()"}, payment=(), usb=()`,')
        change(proxy,'    contentSecurityPolicy,\n  );\n}\n\nexport const config',
               '    contentSecurityPolicy,\n    request.nextUrl.pathname === "/skin-photo",\n  );\n}\n\nexport const config')
    if '"media-src' not in proxy.read_text():
        change(proxy,'    "img-src \'self\' blob: data: https:",','    "img-src \'self\' blob: data: https:",\n    "media-src \'self\' blob:",')

    print('Building isolated frontend',env['NEXT_DIST_DIR'],flush=True)
    subprocess.run(['/opt/node-v22.23.4/bin/npm','run','build'],cwd=FRONT,env=env,check=True)
    print('BUILD_PASSED',flush=True)
    # Start the photo model only on loopback. Do not expose a new external listener.
    if not health('http://127.0.0.1:8025/health'):
        log=open(EXPERIMENT/'photo-service.log','a')
        process=subprocess.Popen([str(EXPERIMENT/'.venv/bin/python'),'-u',str(EXPERIMENT/'photo_service.py')],cwd=EXPERIMENT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        (EXPERIMENT/'photo-service.pid').write_text(str(process.pid))
    old_start_env=dict(env);old_start_env['NEXT_DIST_DIR']=old_dist
    def start_frontend(start_env):
        log=open(ROOT/'photo-frontend.log','a')
        return subprocess.Popen(['/opt/node-v22.23.4/bin/node','node_modules/next/dist/bin/next','start','--hostname','0.0.0.0','--port','3000'],cwd=FRONT,env=start_env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    if frontend_processes()!=[old_pid]:raise RuntimeError('Frontend changed during build; refusing to stop another deployment')
    print('Activating photo frontend',flush=True)
    os.kill(old_pid,signal.SIGTERM)
    for _ in range(50):
        if not Path(f'/proc/{old_pid}').exists():break
        time.sleep(.2)
    fresh=start_frontend(env)
    for _ in range(40):
        if fresh.poll() is not None:break
        if health('http://127.0.0.1:3000/skin-photo'):
            (ROOT/'photo-deployment.json').write_text(json.dumps({'frontend_pid':fresh.pid,'dist':env['NEXT_DIST_DIR'],'backup':str(BACKUP),'page':'/skin-photo','built':True},indent=2))
            print('PHOTO_DEPLOYED',flush=True);return
        time.sleep(.5)
    if fresh.poll() is None:fresh.terminate();fresh.wait(timeout=10)
    start_frontend(old_start_env)
    raise RuntimeError('New frontend failed; restarted previous build')

if __name__=='__main__':main()
