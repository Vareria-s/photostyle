// No catalog parsing here: only two small JSON documents and a bounded byte stream.
const BASE='https://raw.githubusercontent.com/Vareria-s/photostyle/';
const ROUTES=new Set(['/health','/status.json','/preview.xml','/feed.xml','/report.json','/reasons.csv','/text-changes.csv']);
const MAX_FILE=100*1024*1024, MAX_JSON=256*1024, TIMEOUT=45000;
async function open(url,fetcher,ttl=0) {
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),TIMEOUT);
 try {
  const response=await fetcher(url,{signal:controller.signal,redirect:'error',headers:{'User-Agent':'PhotoStyle-Avito/1.0'},cf:{cacheEverything:true,cacheTtl:ttl}});
  if(!response.ok) throw Error('upstream_status');
  return {response,close:()=>clearTimeout(timer)};
 } catch(error) { clearTimeout(timer); throw error; }
}
async function json(url,fetcher,ttl) {
 const {response,close}=await open(url,fetcher,ttl); let size=0; const chunks=[];
 try {
  if(Number(response.headers.get('Content-Length'))>MAX_JSON) throw Error('metadata_size');
  const reader=response.body.getReader();
  try {for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>MAX_JSON)throw Error('metadata_size');chunks.push(value);}}
  finally {await reader.cancel().catch(()=>{});}
  const joined=new Uint8Array(size);let offset=0;for(const chunk of chunks){joined.set(chunk,offset);offset+=chunk.length;}
  return JSON.parse(new TextDecoder().decode(joined));
 } finally {close();}
}
function reply(request,body,status=200,type='application/json; charset=utf-8',extra={}) {
 return new Response(request.method==='HEAD'?null:body,{status,headers:{'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',...extra}});
}
export async function handle(request,fetcher=fetch,options={}) {
 const path=new URL(request.url).pathname;
 if(!['GET','HEAD'].includes(request.method))return reply(request,null,405,'text/plain',{Allow:'GET, HEAD'});
 if(path==='/')return reply(request,'<!doctype html><html lang="ru"><meta charset="utf-8"><title>ФотоСтайл — Avito</title><h1>ФотоСтайл — Avito</h1><p>Черновая выгрузка. Приёмка Avito ожидает доступа к аккаунту.</p><p><a href="/health">Статус</a> · <a href="/preview.xml">Preview XML</a> · <a href="/report.json">Отчёт</a></p><p>Production /feed.xml закрыт до подтверждённой активации.</p></html>',200,'text/html; charset=utf-8');
 if(!ROUTES.has(path))return reply(request,null,404);
 try {
  const pointer=await json(BASE+'avito-feed/current.json',fetcher,60);
  if(!/^[a-f0-9]{40}$/.test(pointer.commit))throw Error('invalid_pointer');
  const base=BASE+pointer.commit+'/';
  const status=await json(base+'status.json',fetcher,3600);
  const stamp=Date.parse(status.generated_at), age=Date.now()-stamp;
  const maxAge=Number(options.staleHours??4)*3600000;
  const fresh=Number.isFinite(stamp)&&Number.isFinite(maxAge)&&maxAge>0&&age>=-300000&&age<=maxAge;
  if(typeof status.production_enabled!=='boolean'||!Array.isArray(status.blockers))throw Error('status_schema');
  if(path==='/health'||path==='/status.json')return reply(request,JSON.stringify({...status,snapshot_commit:pointer.commit,stale:!fresh,feed_available:fresh&&status.production_enabled&&status.blockers.length===0}));
  if(path==='/feed.xml'&&(!fresh||!status.production_enabled||status.blockers.length))throw Error('production_closed');
  const {response,close}=await open(base+path.slice(1),fetcher,3600);
  if(Number(response.headers.get('Content-Length'))>MAX_FILE){close();await response.body?.cancel();throw Error('file_size');}
  if(request.method==='HEAD'){close();await response.body?.cancel();return reply(request,null,200,path.endsWith('.xml')?'application/xml; charset=utf-8':path.endsWith('.csv')?'text/csv; charset=utf-8':'application/json; charset=utf-8');}
  let size=0;const reader=response.body.getReader();
  const body=new ReadableStream({async pull(controller){try{const chunk=await reader.read();if(chunk.done){close();controller.close();return;}size+=chunk.value.byteLength;if(size>MAX_FILE)throw Error('file_size');controller.enqueue(chunk.value);}catch(error){close();await reader.cancel().catch(()=>{});controller.error(error);}},async cancel(){close();await reader.cancel();}});
  return reply(request,body,200,path.endsWith('.xml')?'application/xml; charset=utf-8':path.endsWith('.csv')?'text/csv; charset=utf-8':'application/json; charset=utf-8',{'X-Snapshot-Commit':pointer.commit});
 }catch {return reply(request,JSON.stringify({error:'snapshot_unavailable_or_production_closed'}),503);}
}
export default {fetch(request,env){return handle(request,fetch,{staleHours:env?.STALE_HOURS??4});}};
