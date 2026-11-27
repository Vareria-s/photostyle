#!/usr/bin/env python3
"""Prepare last-good branch; publish complete staging via one fast-forward ref update."""
import argparse, base64, json, os, pathlib, shutil, subprocess, urllib.error, urllib.request
FILES=('preview.xml','feed.xml','state.json','status.json','report.json','reasons.csv','text-changes.csv')
REPO='Vareria-s/photostyle'
def git(directory,*args):
    return subprocess.run(['git','-C',str(directory),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
def auth():
    token=os.environ.get('GITHUB_TOKEN')
    if token:
        value=base64.b64encode(('x-access-token:'+token).encode()).decode()
        os.environ.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+value)
    return token

def prepare(directory):
    token=auth()
    if not token: raise ValueError('GITHUB_TOKEN missing')
    # Only a confirmed API 404 means a new branch. Network/403/5xx abort.
    request=urllib.request.Request('https://api.github.com/repos/'+REPO+'/branches/avito-feed',headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'PhotoStyle-Avito'})
    exists=True
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            metadata=json.load(response)
            if not metadata.get('commit',{}).get('sha'): raise ValueError('branch API schema')
    except urllib.error.HTTPError as error:
        if error.code!=404: raise
        # Distinguish missing branch from missing/inaccessible repository.
        repo_request=urllib.request.Request('https://api.github.com/repos/'+REPO,headers=request.headers)
        with urllib.request.urlopen(repo_request,timeout=30) as response:
            if json.load(response).get('full_name','').lower()!=REPO.lower(): raise ValueError('repository mismatch')
        exists=False
    directory.mkdir(parents=True,exist_ok=False)
    git(directory,'init');git(directory,'remote','add','origin','https://github.com/'+REPO+'.git')
    if exists:
        git(directory,'fetch','--depth=1','origin','refs/heads/avito-feed')
        git(directory,'checkout','-B','avito-feed','FETCH_HEAD')
        # Never silently reset a damaged snapshot.
        for name in ('state.json','status.json','preview.xml'):
            if not (directory/name).is_file():raise ValueError('previous snapshot incomplete: '+name)
        json.loads((directory/'state.json').read_text(encoding='utf-8-sig'))
    else:git(directory,'checkout','--orphan','avito-feed')

def publish(directory,stage):
    auth()
    if directory.resolve()==stage.resolve():raise ValueError('staging must be separate')
    if git(directory,'branch','--show-current')!='avito-feed':raise ValueError('wrong publication branch')
    staged=set(git(directory,'diff','--cached','--name-only').splitlines())
    if staged-set(FILES)-{'current.json'}:raise ValueError('unrelated staged files')
    for name in FILES:
        if name!='feed.xml' and not (stage/name).is_file():raise ValueError('missing staging output '+name)
    status=json.loads((stage/'status.json').read_text(encoding='utf-8-sig'))
    enabled=status.get('production_enabled')
    if not isinstance(enabled,bool) or enabled!=(stage/'feed.xml').is_file():raise ValueError('feed/production gate mismatch')
    if not status.get('generated_at'):raise ValueError('missing timestamp')
    git(directory,'config','user.name','photostyle-avito[bot]')
    git(directory,'config','user.email','photostyle-avito@users.noreply.github.com')
    for name in FILES:
        target=directory/name
        if (stage/name).exists():shutil.copyfile(stage/name,target)
        else:target.unlink(missing_ok=True)
    tracked=set(git(directory,'ls-files').splitlines())
    selected=[name for name in FILES if (directory/name).exists() or name in tracked]
    git(directory,'add','--all','--',*selected)
    changed=bool(git(directory,'diff','--cached','--name-only'))
    if changed:git(directory,'commit','-m','Avito snapshot '+status['generated_at'])
    commit=git(directory,'rev-parse','HEAD')
    (directory/'current.json').write_text(json.dumps({'commit':commit},separators=(',',':'))+'\n',encoding='utf-8')
    git(directory,'add','--','current.json')
    if git(directory,'diff','--cached','--name-only'):git(directory,'commit','-m','Point HTTP relay at complete immutable snapshot')
    # No force/rebase: concurrent publication fails without changing the remote.
    git(directory,'push','origin','HEAD:refs/heads/avito-feed')
    print('Published immutable snapshot '+commit)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['prepare','publish']);parser.add_argument('--directory',type=pathlib.Path,required=True);parser.add_argument('--stage',type=pathlib.Path)
    args=parser.parse_args()
    try:
        if args.action=='prepare':prepare(args.directory)
        elif args.stage:publish(args.directory,args.stage)
        else:raise ValueError('--stage required')
    except (OSError,ValueError,subprocess.CalledProcessError) as error:
        # Never print command/environment/stderr that could contain authorization.
        if not os.environ.get('GITHUB_TOKEN') and isinstance(error,subprocess.CalledProcessError): print(error.stderr,file=__import__('sys').stderr)
        print('Snapshot operation failed ('+type(error).__name__+'); publication not confirmed, inspect branch before retry.',file=__import__('sys').stderr)
        return 2
    return 0
if __name__=='__main__':raise SystemExit(main())



