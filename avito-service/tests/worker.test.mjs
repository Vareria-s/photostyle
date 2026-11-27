import test from 'node:test';
import assert from 'node:assert/strict';
import {handle} from '../pages/_worker.js';
const request=(path,method='GET')=>new Request('https://service.test'+path,{method});
test('only fixed routes and GET/HEAD; state and arbitrary proxy stay closed', async()=>{
 let calls=0; const fetcher=()=>{calls++;throw Error('unexpected');};
 assert.equal((await handle(request('/state.json'),fetcher)).status,404);
 assert.equal((await handle(request('/https://evil.test'),fetcher)).status,404);
 assert.equal((await handle(request('/feed.xml','POST'),fetcher)).status,405);
 assert.equal(calls,0);
});
const sha='a'.repeat(40);
function upstream({enabled=true,age=0,statusCode=200,missingFeed=false}={}) {
 const urls=[];
 const fetcher=async(url,options)=>{
  urls.push(String(url));
  if(String(url).endsWith('/current.json')) return Response.json({commit:sha});
  if(String(url).endsWith('/status.json')) return statusCode===200?Response.json({generated_at:new Date(Date.now()-age).toISOString(),production_enabled:enabled,blockers:[]}):new Response(null,{status:statusCode});
  return new Response(missingFeed?null:'<Ads/>',{status:missingFeed?404:200,headers:{'Content-Length':'6'}});
 }; return {fetcher,urls};
}
test('production gate, stale status, missing status/feed fail closed including HEAD',async()=>{
 for(const options of [{enabled:false},{age:5*3600000},{statusCode:503},{missingFeed:true}]) {
  for(const method of ['GET','HEAD']) {
   const {fetcher}=upstream(options); const result=await handle(request('/feed.xml',method),fetcher);
   assert.equal(result.status,503); if(method==='HEAD') assert.equal(await result.text(),'');
  }
 }
});
test('streamed preview and HEAD use immutable commit; mutable pointer cannot mix snapshots',async()=>{
 const {fetcher,urls}=upstream({enabled:false});
 const result=await handle(request('/preview.xml'),fetcher);
 assert.equal(result.status,200);assert.equal(await result.text(),'<Ads/>');
 assert.equal(result.headers.get('X-Snapshot-Commit'),sha);
 assert.equal(urls.length,3); assert.ok(urls.slice(1).every(url=>url.includes('/'+sha+'/')));
 const head=await handle(request('/preview.xml','HEAD'),fetcher);assert.equal(head.status,200);assert.equal(await head.text(),'');
});
test('metadata, redirect/network errors and oversized body fail closed',async()=>{
 const bad=[async()=>{throw Error('network');},async()=>Response.json({commit:'../state.json'}),async()=>new Response('x',{headers:{'Content-Length':'99999999'}})];
 for(const fetcher of bad) assert.equal((await handle(request('/feed.xml'),fetcher)).status,503);
 const {fetcher}=upstream();
 const huge=async(url,opts)=>String(url).endsWith('/feed.xml')?new Response('x',{headers:{'Content-Length':'200000000'}}):fetcher(url,opts);
 assert.equal((await handle(request('/feed.xml'),huge)).status,503);
});
