"""Public CLI publication test against a real local bare git remote."""
import json, pathlib, subprocess, sys, tempfile
SCRIPT=pathlib.Path(__file__).with_name('snapshot.py')
def run(*args, cwd=None):
    result=subprocess.run(args,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if result.returncode: raise RuntimeError(result.stderr)
    return result.stdout.strip()
with tempfile.TemporaryDirectory() as temp:
    root=pathlib.Path(temp); remote=root/'remote.git'; checkout=root/'checkout'; stage=root/'stage'
    run('git','init','--bare',str(remote)); run('git','init',str(checkout))
    run('git','remote','add','origin',str(remote),cwd=checkout)
    run('git','checkout','--orphan','avito-feed',cwd=checkout)
    stage.mkdir()
    for name, content in {'status.json':json.dumps({'production_enabled':False,'generated_at':'2026-11-27T00:00:00Z'}),'state.json':'{}','preview.xml':'<Ads/>','report.json':'{}','reasons.csv':'uid,reason','text-changes.csv':'uid,change'}.items():
        (stage/name).write_text(content)
    run(sys.executable,str(SCRIPT),'publish','--directory',str(checkout),'--stage',str(stage))
    pointer=json.loads(run('git','show','avito-feed:current.json',cwd=remote))
    assert run('git','show',pointer['commit']+':preview.xml',cwd=remote)=='<Ads/>'
    assert json.loads(run('git','show',pointer['commit']+':status.json',cwd=remote))['production_enabled'] is False
    previous=run('git','rev-parse','avito-feed',cwd=remote)
    (stage/'feed.xml').write_text('must reject disabled feed')
    result=subprocess.run([sys.executable,str(SCRIPT),'publish','--directory',str(checkout),'--stage',str(stage)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert result.returncode!=0
    assert run('git','rev-parse','avito-feed',cwd=remote)==previous
    print('snapshot CLI: immutable pointer + atomic publication + failed staging preserves remote: PASS')

    (stage/'feed.xml').unlink()
    (checkout/'pdf-unrelated.txt').write_text('must never commit another service')
    run('git','add','pdf-unrelated.txt',cwd=checkout)
    result=subprocess.run([sys.executable,str(SCRIPT),'publish','--directory',str(checkout),'--stage',str(stage)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert result.returncode!=0, 'publication must reject unrelated staged files'
    assert run('git','rev-parse','avito-feed',cwd=remote)==previous
    print('Unrelated staged files cannot be committed: PASS')
