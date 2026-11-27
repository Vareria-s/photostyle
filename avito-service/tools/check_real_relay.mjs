// Exercise the public HTTP seam with the actual 14 MB catalog, never XML-decode it.
import {handle} from '../pages/_worker.js';
import {readFile,stat} from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import {Readable} from 'node:stream';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
const directory=process.argv[2];if(!directory)throw Error('Pass actual snapshot directory');
const sha='b'.repeat(40);
const fetcher=async url=>{
 if(String(url).endsWith('/current.json'))return Response.json({commit:sha});
 assert.ok(String(url).includes('/'+sha+'/'));
 const file=path.join(directory,path.basename(new URL(url).pathname));
 const info=await stat(file);
 return new Response(Readable.toWeb(createReadStream(file)),{headers:{'Content-Length':String(info.size)}});
};
const result=await handle(new Request('https://local.test/preview.xml'),fetcher);
assert.equal(result.status,200);
const bytes=Buffer.from(await result.arrayBuffer());
const expected=await readFile(path.join(directory,'preview.xml'));
assert.equal(createHash('sha256').update(bytes).digest('hex'),createHash('sha256').update(expected).digest('hex'));
assert.equal((await handle(new Request('https://local.test/feed.xml'),fetcher)).status,503);
assert.equal((await handle(new Request('https://local.test/state.json'),fetcher)).status,404);
console.log(`Real relay: ${bytes.length} bytes identical; production closed; state closed: PASS`);
