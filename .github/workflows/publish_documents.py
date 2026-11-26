import json,pathlib,hashlib,tempfile,urllib.request,urllib.parse,urllib.error,subprocess,threading,concurrent.futures,time,sys
REPO='Vareria-s/photostyle'
ROOT=pathlib.Path('.github/workflows')
manifest=json.loads((ROOT/'files-manifest.json').read_text(encoding='utf-8'))
files=manifest['files']
assert manifest['repository']==REPO and len({d['sha256'] for d in files})==len(files)
for d in files:
    assert len(d['sha256'])==64 and all(c in '0123456789abcdef' for c in d['sha256'])
    assert d['filename']=='doc-'+d['sha256']+'.pdf' and 0<d['bytes']<2*1024**3
    assert d['release'].startswith('documents-2026-11-26-')
    assert d['sources'] and all(urllib.parse.urlsplit(u).scheme in ['https','http'] for u in d['sources'])
for tag in {d['release'] for d in files}:assert sum(d['release']==tag for d in files)<=800
if '--validate-only' in sys.argv:
    print('Validated',len(files),'unique PDFs;',sum(d['bytes'] for d in files),'bytes');sys.exit(0)
def gh(*args):
    p=subprocess.run(['gh',*args],capture_output=True,text=True)
    if p.returncode:raise RuntimeError(p.stderr[:600])
    return p.stdout
existing={}
for tag in sorted({d['release'] for d in files}):
    try:data=json.loads(gh('api',f'repos/{REPO}/releases/tags/{tag}'))
    except RuntimeError:
        gh('release','create',tag,'--repo',REPO,'--title','PhotoStyle — проверенные PDF, '+tag.rsplit('-',1)[-1],'--notes','Оригинальные инструкции и документы соответствия производителей. Файлы проверяются по SHA-256 перед загрузкой; авторские права принадлежат правообладателям. Регион документа и применимость к конкретной партии проверяются отдельно.','--latest=false')
        data=json.loads(gh('api',f'repos/{REPO}/releases/tags/{tag}'))
    existing[tag]={a['name']:a for a in data['assets']}
host_locks={};blocked_hosts={};guard=threading.Lock();reports=[];done=0
def lock_for(url):
    host=urllib.parse.urlsplit(url).hostname
    with guard:return host_locks.setdefault(host,threading.Lock())
def one(d):
    global done
    record={k:d[k] for k in ['sha256','filename','release','bytes','url']}
    old=existing[d['release']].get(d['filename'])
    if old and old.get('digest')=='sha256:'+d['sha256'] and old['size']==d['bytes']:
        record['status']='published';record['reused']=True;return record
    if old:
        record['status']='conflict';record['error']='Existing asset digest does not match; left untouched';return record
    errors=[]
    with tempfile.TemporaryDirectory(prefix='photostyle-') as folder:
        path=pathlib.Path(folder)/d['filename']
        for source in d['sources']:
            try:
                with lock_for(source):
                    host=urllib.parse.urlsplit(source).hostname
                    if blocked_hosts.get(host,0)>time.monotonic():raise RuntimeError('Source host cooling down after rate limit; deferred')
                    time.sleep(0.8)
                    req=urllib.request.Request(source,headers={'User-Agent':'PhotoStyle-Document-Migration/1.0','Accept':'application/pdf,application/octet-stream;q=0.9,*/*;q=0.1'})
                    with urllib.request.urlopen(req,timeout=120) as response,path.open('wb') as output:
                        digest=hashlib.sha256();size=0;magic=b''
                        while True:
                            chunk=response.read(256*1024)
                            if not chunk:break
                            if not magic:magic=chunk[:5]
                            size+=len(chunk)
                            if size>d['bytes']:raise ValueError('Source file changed (size)')
                            digest.update(chunk);output.write(chunk)
                if size!=d['bytes'] or digest.hexdigest()!=d['sha256'] or magic!=b'%PDF-':raise ValueError('Source file changed or is not the verified PDF')
                gh('release','upload',d['release'],str(path),'--repo',REPO)
                record['status']='published';record['source']=source;break
            except Exception as error:
                errors.append(type(error).__name__+': '+str(error)[:220])
                if isinstance(error,urllib.error.HTTPError) and error.code==429:
                    retry=error.headers.get('Retry-After','300')
                    blocked_hosts[urllib.parse.urlsplit(source).hostname]=time.monotonic()+max(300,int(retry) if retry.isdigit() else 300)
                    errors.append('Rate limited; no further attempts for this file');break
        else:record['status']='unavailable'
        if record.get('status')!='published':record['status']='unavailable';record['errors']=errors
    with guard:
        done+=1
        if done%20==0:print('Processed',done,'of',len(files),flush=True)
    return record
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for r in pool.map(one,files):
            reports.append(r)
            pathlib.Path('publication-report.json').write_text(json.dumps({'total':len(files),'processed':len(reports),'files':reports},ensure_ascii=False,indent=2),encoding='utf-8')
finally:
    report={'total':len(files),'published':sum(r['status']=='published' for r in reports),'unavailable':sum(r['status']!='published' for r in reports),'files':reports}
    pathlib.Path('publication-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    tag=sorted(existing)[-1]
    gh('release','upload',tag,'publication-report.json','--repo',REPO,'--clobber')
    print('PUBLICATION_RESULT',json.dumps({k:v for k,v in report.items() if k!='files'}),flush=True)
