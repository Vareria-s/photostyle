"""Protected, fail-closed Avito account bootstrap. See docs/account-contract.md.

main(argv, transport=None) is the public CLI/API test seam. A transport receives
(method, HTTPS URL, headers, body bytes) and returns decoded JSON or bytes.
No credential, profile, frozen URL or raw provider error is logged.
"""
import argparse
import copy
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from email.utils import parsedate_to_datetime
import http.client
from html import unescape
from html.parser import HTMLParser
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlencode, quote
import xml.etree.ElementTree as ET

MAX_BYTES=100*1024*1024
API='https://api.avito.ru'

class Blocked(ValueError): pass

class ProviderHTTPError(Blocked):
    def __init__(self,status,retry_after=None):
        super().__init__('provider_http_'+str(status)); self.status=status; self.retry_after=retry_after

def require(test, code):
    if not test: raise Blocked(code)

def digest(data): return hashlib.sha256(data).hexdigest()
def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def compact_status(report):
    return {k:v for k,v in report.items() if k not in ('source_exclusions','text_transforms','category_mapping_provenance')}

def stamp(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')

def safe_url(url, api=False):
    p=urlsplit(url)
    require(p.scheme=='https' and p.hostname and not p.username and not p.password and not p.fragment and p.port in (None,443),'unsafe_https_url')
    if api: require(p.hostname=='api.avito.ru','api_host_not_allowed')
    return p

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address):
        super().__init__(host,timeout=45,context=ssl.create_default_context()); self.address=address
    def connect(self):
        sock=socket.create_connection((self.address,443),self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)

def network(method,url,headers,body):
    started=time.monotonic(); p=safe_url(url)
    addresses={entry[4][0] for entry in socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)}
    require(addresses and all(ipaddress.ip_address(a).is_global for a in addresses),'network_private_address_denied')
    # Pin the validated address for this connection; DNS is not resolved again.
    conn=PinnedHTTPS(p.hostname,sorted(addresses)[0])
    try:
        conn.request(method,p.path+('?' + p.query if p.query else ''),body=body,headers=headers)
        response=conn.getresponse()
        if response.status!=200:
            delay=None
            if response.status==429:
                header=response.getheader('Retry-After')
                if header:
                    try: delay=max(0,int(header))
                    except ValueError: delay=max(0,(parsedate_to_datetime(header)-datetime.now(timezone.utc)).total_seconds())
                    require(delay<=120,'provider_retry_after_exceeds_bounded_budget')
            raise ProviderHTTPError(response.status,delay)
        length=response.getheader('Content-Length')
        require(not length or int(length)<=MAX_BYTES,'response_size_limit')
        chunks=[]; size=0
        while True:
            require(time.monotonic()-started<=45,'response_time_limit')
            chunk=response.read(min(64*1024,MAX_BYTES+1-size))
            require(time.monotonic()-started<=45,'response_time_limit')
            if not chunk: break
            size+=len(chunk); require(size<=MAX_BYTES,'response_size_limit'); chunks.append(chunk)
        data=b''.join(chunks)
        if 'json' in (response.getheader('Content-Type') or ''): return json.loads(data)
        return data
    finally: conn.close()

class Client:
    def __init__(self,transport): self.transport=transport or network; self.token=None
    def request(self,method,url,headers,body):
        for attempt in range(3):
            try: return self.transport(method,url,headers,body)
            except ProviderHTTPError as error:
                if error.status!=429 or attempt==2 or (method!='GET' and url!=API+'/token'): raise
                remaining=error.retry_after if error.retry_after is not None else 2**attempt
                while remaining>0:
                    interval=min(remaining,60); time.sleep(interval); remaining-=interval
    def call(self,path,method='GET',data=None):
        url=API+path; safe_url(url,True)
        headers={'User-Agent':'PhotoStyle-Avito/2.0','Accept':'application/json'}
        if self.token: headers['Authorization']='Bearer '+self.token
        body=None
        if data is not None:
            if path=='/token': body=urlencode(data).encode(); headers['Content-Type']='application/x-www-form-urlencoded'
            else: body=encoded(data); headers['Content-Type']='application/json'
        value=self.request(method,url,headers,body)
        if isinstance(value,bytes):
            if method=='POST' and not value: return None
            value=json.loads(value)
        return value
    def authenticate(self):
        reply=self.call('/token','POST',{'grant_type':'client_credentials','client_id':os.environ['AVITO_CLIENT_ID'],'client_secret':os.environ['AVITO_CLIENT_SECRET']})
        require(isinstance(reply,dict) and reply.get('token_type','').lower()=='bearer' and isinstance(reply.get('expires_in'),int) and reply['expires_in']>0 and reply.get('access_token'),'invalid_token_response')
        self.token=reply['access_token']
    def download(self,url):
        safe_url(url)
        value=self.request('GET',url,{'User-Agent':'PhotoStyle-Avito/2.0'},None)
        require(isinstance(value,bytes) and 0<len(value)<=MAX_BYTES,'invalid_feed_response')
        return value

def xml(data,root):
    require(isinstance(data,bytes) and 0<len(data)<=MAX_BYTES,'xml_size_limit')
    require(not re.search(br'<!\s*(DOCTYPE|ENTITY)',data.replace(b'\x00',b''),re.I),'xml_entities_forbidden')
    node=ET.fromstring(data); require(node.tag==root,'unsupported_feed_format'); return node

def private_plan(path):
    base=os.environ.get('RUNNER_TEMP'); require(base,'RUNNER_TEMP_required')
    target=Path(path).resolve(); base=Path(base).resolve()
    require(target.is_relative_to(base) and target!=base,'plan_must_be_in_RUNNER_TEMP')
    require(not any(p.is_symlink() for p in (Path(path),*Path(path).parents)),'private_plan_symlink_denied')
    return target

def save_plan(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.plan-',dir=path.parent)
    try:
        os.chmod(name,0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(encoded(value))
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def owner(client):
    result=client.call('/core/v1/accounts/self'); uid=result.get('id')
    require(isinstance(uid,int) and not isinstance(uid,bool) and uid>0,'owner_id_missing')
    return 'avito-'+str(uid)

def profile(client):
    p=client.call('/autoload/v2/profile')
    require(isinstance(p,dict) and isinstance(p.get('feeds_data'),list) and p['feeds_data'] and isinstance(p.get('schedule'),list) and p['schedule'] and isinstance(p.get('report_email'),str) and p['report_email'] and p.get('autoload_enabled') is True,'existing_enabled_profile_required')
    require(set(p)<= {'feeds_data','schedule','report_email','autoload_enabled','agreement'},'unsupported_profile_properties')
    names=[]
    for f in p['feeds_data']:
        require(isinstance(f,dict) and set(f)=={'feed_name','feed_url'} and f['feed_name'],'invalid_profile_feed'); safe_url(f['feed_url']); names.append(f['feed_name'])
    require(len(set(names))==len(names),'duplicate_profile_feed_names')
    return p

def paged_items(client,which='last_successful'):
    rows=[]; seen=set(); total=None; pages=None
    for page in range(1,1001):
        reply=client.call('/autoload/v4/uploads/'+which+'/items?'+urlencode({'perPage':100,'page':page}))
        meta=reply.get('meta',{}); batch=reply.get('items')
        require(isinstance(batch,list) and meta.get('page')==page and meta.get('perPage')==100 and isinstance(meta.get('pages'),int) and isinstance(meta.get('total'),int),'invalid_upload_items_shape')
        if total is None: total=meta['total']; pages=meta['pages']
        require((total,pages)==(meta['total'],meta['pages']) and 1<=pages<=1000 and page<=pages and len(batch)<=100,'inconsistent_upload_pagination')
        for row in batch:
            key=(row.get('feed_name'),row.get('ad_id'))
            require(key[0] and key[1] and key not in seen and isinstance(row.get('messages'),list),'duplicate_or_invalid_upload_item')
            seen.add(key); rows.append(row)
        if page==pages:
            require(len(rows)==total,'incomplete_upload_items'); return rows
        require(len(batch)==100,'incomplete_upload_page')
    raise Blocked('upload_page_limit')

def core_ids(client):
    ids=set(); window_started=time.monotonic()
    for page in range(1,1001):
        if page>1 and (page-1)%24==0:
            remaining=max(0,61-(time.monotonic()-window_started))
            while remaining>0:
                interval=min(remaining,60); time.sleep(interval); remaining-=interval
            window_started=time.monotonic()
        response=client.call('/core/v1/items?'+urlencode({'per_page':99,'page':page,'status':'active,old,removed,blocked,rejected'},safe=','))
        batch=response.get('resources'); require(isinstance(batch,list) and len(batch)<=99,'invalid_core_items_shape')
        for row in batch:
            value=str(row.get('id') or ''); require(value.isdigit() and value not in ids,'duplicate_core_item'); ids.add(value)
        if len(batch)<99: return ids
        # Conservative 24 requests per 61-second window, within the documented 25/minute.
    raise Blocked('core_item_page_limit')

def tree_slugs(value):
    found=set()
    def visit(nodes):
        require(isinstance(nodes,list),'unsupported_category_tree')
        for n in nodes:
            require(isinstance(n,dict) and n.get('name'),'unsupported_category_tree')
            if n.get('slug'): found.add(n['slug'])
            if n.get('nested'):
                if isinstance(n['nested'],dict):
                    for children in n['nested'].values(): visit(children)
                else: visit(n['nested'])
    visit(value.get('categories')); return found

def predicate(dep,values):
    require(dep.get('action') in ('visible','required','hidden') and dep.get('clause') in ('and','or') and isinstance(dep.get('pairs'),list) and dep['pairs'],'unsupported_field_dependency')
    results=[]
    for pair in dep['pairs']:
        tag=pair.get('source_field_tag'); require(isinstance(tag,str),'unsupported_field_dependency')
        value=values.get(tag,''); clause=pair.get('clause')
        require(clause in ('value','filled','empty'),'unsupported_field_dependency')
        if clause=='value':
            require(isinstance(pair.get('values'),list),'unsupported_field_dependency'); results.append(value in pair['values'])
        else: results.append(bool(value) if clause=='filled' else not bool(value))
    return all(results) if dep['clause']=='and' else any(results)

def active_rules(field,values):
    content=field.get('content'); require(isinstance(content,list) and content,'unsupported_field_schema')
    active=[]
    for rule in content:
        require(isinstance(rule,dict),'unsupported_field_schema')
        deps=rule.get('dependencies') or []
        require(isinstance(deps,list),'unsupported_field_dependency')
        evaluated=[(d['action'],predicate(d,values)) for d in deps]
        visible=[b for a,b in evaluated if a=='visible']
        if not visible or all(visible): active.append((rule,evaluated))
    return active


def adapt_text(ad,schema,settings,uid,changes):
    """Apply the configured policy only to an unambiguous active string bound."""
    values={n.tag:(n.text or '') for n in ad}
    for tag in ('Title','Description'):
        fields=[field for field in schema.get('fields',[]) if field.get('tag')==tag]
        require(len(fields)==1,'text_field_schema_ambiguous')
        active=active_rules(fields[0],values)
        require(len(active)==1,'ambiguous_field_rules')
        rule,_=active[0]
        require(rule.get('data_type')=='string','unsupported_text_field_type')
        maximum=(rule.get('values_range') or {}).get('max')
        if maximum is None: continue
        require(type(maximum) is int and maximum>0,'invalid_text_length_bound')
        node=ad.find(tag); require(node is not None,'missing_source_text')
        original=node.text or ''
        if len(original)<=maximum: continue
        require(settings.get('text_policies',{}).get(tag.lower())=='truncate_word','text_policy_reject_'+tag.lower())
        prefix=original[:maximum+1]
        transformed=prefix.rsplit(' ',1)[0].rstrip() if ' ' in prefix else original
        require(transformed and len(transformed)<=maximum,'text_word_exceeds_limit_'+tag.lower())
        node.text=transformed; values[tag]=transformed
        changes.append({'product_uid':uid,'field':tag.lower(),'policy':'truncate_word','original':original,'transformed':transformed})


def validate(ad,schema,historical=None):
    require(isinstance(schema.get('fields'),list),'unsupported_field_schema')
    values={n.tag:(n.text or '') for n in ad}; tags=set()
    dynamic={'Title','Description','Price','Images'}
    def preserved(node):
        prior=historical.find(node.tag) if historical is not None else None
        return node.tag not in dynamic and prior is not None and ET.tostring(prior)==ET.tostring(node)
    def fields(nodes,parent):
        for field in nodes:
            tag=field.get('tag'); require(isinstance(tag,str) and tag not in tags,'invalid_field_tag'); tags.add(tag)
            active=active_rules(field,values)
            element=parent.find(tag); present=element is not None and (len(element)>0 or bool(element.text))
            if not active:
                require(not present,'field_not_available'); continue
            require(len(active)==1,'ambiguous_field_rules')
            rule,deps=active[0]
            hidden=any(a=='hidden' and b for a,b in deps)
            required=rule.get('required') is True or any(a=='required' and b for a,b in deps)
            require(not rule.get('required_by_dependency') or any(a=='required' for a,b in deps),'unresolved_required_dependency')
            require(not (hidden and present),'hidden_field_present'); require(not required or present,'required_field_missing')
            if not present: continue
            if tag=='Images':
                require(not element.attrib and not (element.text or '').strip() and len(element)>0,'invalid_images_container')
                for image in element:
                    require(image.tag=='Image' and set(image.attrib)=={'url'} and not len(image) and not (image.text or '').strip() and not (image.tail or '').strip(),'invalid_image_structure')
                    safe_url(image.get('url'))
                require(not rule.get('values') and not rule.get('values_link_json') and not rule.get('values_link_xml') and not rule.get('values_range'),'unsupported_image_schema_constraints')
                continue
            if preserved(element): continue
            if field.get('children'):
                fields(field['children'],element); continue
            require(not len(element),'unsupported_nested_field')
            value=element.text or ''; kind=rule.get('data_type'); typ=rule.get('field_type')
            require(kind in ('string','integer','float') and typ in ('input','select','checkbox'),'unsupported_field_type')
            require(not rule.get('values_link_json') and not rule.get('values_link_xml') and not rule.get('is_catalog'),'external_values_schema_unavailable')
            choices=rule.get('values')
            if typ in ('select','checkbox'):
                require(isinstance(choices,list) and choices,'field_values_missing')
                require(value in [v['value'] for v in choices],'field_enum_invalid')
            quantity=len(value) if kind=='string' else Decimal(value)
            if kind!='string': require(quantity.is_finite() and (kind!='integer' or quantity==quantity.to_integral()),'field_number_invalid')
            bounds=rule.get('values_range') or {}
            require((bounds.get('min') is None or quantity>=bounds['min']) and (bounds.get('max') is None or quantity<=bounds['max']),'field_range_invalid')
    fields(schema['fields'],ad)
    require(all(n.tag in tags or n.tag=='AvitoId' or preserved(n) for n in ad),'unknown_feed_field')

def description_text(value):
    class Plain(HTMLParser):
        allowed={'p','br','strong','b','i','em','u','ul','ol','li','div','span','blockquote','h1','h2','h3','h4','table','tr','td','th','tbody','a'}
        def __init__(self): super().__init__(); self.parts=[]
        def handle_starttag(self,tag,attrs):
            require(tag in self.allowed,'description_HTML_not_supported_in_service')
            require(not any(k.lower().startswith('on') or (k=='href' and v and urlsplit(v).scheme not in ('https','http')) for k,v in attrs),'unsafe_description_HTML')
            if tag in ('p','br','li','div','tr'): self.parts.append(' ')
        def handle_endtag(self,tag):
            require(tag in self.allowed,'description_HTML_not_supported_in_service')
            if tag in ('p','li','div','tr'): self.parts.append(' ')
        def handle_data(self,value): self.parts.append(value)
    parser=Plain(); parser.feed(unescape(value)); parser.close()
    return ' '.join(''.join(parser.parts).split())

OUTBOX_DAYS=7
OUTBOX_LIMIT=256

def timestamp_age(value):
    return (datetime.now(timezone.utc)-datetime.fromisoformat(value.replace('Z','+00:00'))).total_seconds()

def reconcile_outbox(client,previous,scope):
    ledger=copy.deepcopy(previous.get('provider_outbox') or [])
    if not ledger and previous.get('pending_provider'):
        ledger=[{**previous['pending_provider'],'created_at':previous.get('data_generated_at') or previous['generated_at'],'status':'pending','scope':scope,'reservations':{uid:{k:item.get(k) for k in ('id','source_sku','variant_id')} for uid,item in previous.get('items',{}).items() if not item.get('published')}}]
    receipts=copy.deepcopy(previous.get('provider_receipts') or [])
    require(len(ledger)<=OUTBOX_LIMIT,'provider_outbox_limit_exceeded')
    for version in ledger:
        require(version.get('scope',scope)==scope and version.get('created_at') and timestamp_age(version['created_at'])>=-300,'invalid_provider_outbox_context')
    diagnostic='not_observed'
    if ledger:
        current=client.call('/autoload/v4/uploads/current')
        require(isinstance(current,dict) and current.get('status') in ('processing','success','success_warning','error'),'invalid_current_upload_status')
        diagnostic=current['status']
        if current['status']=='success':
            names={v['feed_name'] for v in ledger}
            require(len(names)==1,'provider_outbox_feed_ambiguous')
            links=[f for f in current.get('feed_urls',[]) if f.get('name') in names]
            require(len(links)==1,'current_upload_managed_frozen_feed_missing')
            current_digest=digest(client.download(links[0]['url']))
            candidates=[v for v in ledger if v['feed_sha256']==current_digest]
            if candidates:
                version=candidates[-1]
                try: receipt=observe(client,version)
                except Blocked as error:
                    if str(error) not in ('provider_items_incomplete_or_rejected','provider_upload_pending_or_rejected'): raise
                    diagnostic=str(error)
                else:
                    diagnostic='accepted_known_version'
                    receipts=[r for r in receipts if (r['upload_id'],r['feed_sha256'])!=(receipt['upload_id'],receipt['feed_sha256'])]+[receipt]
                    by_id={item['id']:(uid,item) for uid,item in previous.get('items',{}).items()}
                    for context in ledger:
                        for uid,reservation in context.get('reservations',{}).items():
                            if reservation['id'] not in by_id: by_id[reservation['id']]=(uid,reservation)
                    for ident,aid in receipt['avito_ids'].items():
                        if ident in by_id:
                            uid,item=by_id[ident]
                            require(not item.get('avito_id') or str(item['avito_id'])==aid,'receipt_changes_saved_identity')
                            previous.setdefault('items',{})[uid]={**item,'avito_id':aid,'published':True}
                    for context in ledger:
                        if context['created_at']<=version['created_at'] and set(context['feed_ids'])<=set(receipt['avito_ids']):
                            context['status']='accepted' if context['feed_sha256']==current_digest else 'superseded'
            else: diagnostic='current_upload_is_not_a_retained_version'
    ledger=[v for v in ledger if v.get('status','pending')=='pending' or timestamp_age(v['created_at'])<=OUTBOX_DAYS*86400]
    require(not any(v.get('status','pending')=='pending' and timestamp_age(v['created_at'])>OUTBOX_DAYS*86400 for v in ledger),'provider_outbox_unresolved_over_7_days')
    receipts=[r for r in receipts if timestamp_age(r['observed_at'])<=OUTBOX_DAYS*86400][-OUTBOX_LIMIT:]
    return ledger,receipts,diagnostic

def prepare(args,client):
    out=Path(args.out); require(not out.exists() and not out.is_symlink(),'output_directory_must_be_fresh')
    plan_path=private_plan(args.plan); require(not plan_path.is_relative_to(out.resolve()),'private_plan_inside_public_stage')
    public=safe_url(args.public_feed_url); require(not public.query and public.path=='/feed.xml','public_feed_url_invalid')
    settings=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    source=Path(args.source_file).read_bytes() if args.source_file else client.download(args.source_url)
    source_root=xml(source,'yml_catalog'); offers=source_root.findall('./shop/offers/offer'); require(offers,'source_has_no_offers')
    scope=owner(client); previous={}
    if Path(args.previous_state).exists(): previous=json.loads(Path(args.previous_state).read_text(encoding='utf-8-sig'))
    minimum_ratio=settings.get('guards',{}).get('minimum_ratio',0.7)
    require(isinstance(minimum_ratio,(int,float)) and 0<minimum_ratio<=1,'invalid_minimum_ratio')
    source_baseline=previous.get('source_count',0)
    if previous and previous.get('account_scope')!=scope:
        require(previous.get('account_scope')=='photostyle-client-pending' and not any(i.get('published') for i in previous.get('items',{}).values()),'account_scope_changed')
        previous={}
    require(not source_baseline or len(offers)>=source_baseline*minimum_ratio,'source_count_drop_guard')
    before=profile(client); uncommitted_refresh=False
    if previous.get('pending_provider'):
        pending=previous['pending_provider']; fingerprint=digest(encoded(before))
        require(pending.get('profile_before_sha256') and pending.get('profile_after_sha256'),'pending_profile_provenance_missing')
        if fingerprint==pending['profile_before_sha256'] and fingerprint!=pending['profile_after_sha256']:
            recovery_material(args,previous,before)
            require(timestamp_age(previous.get('data_generated_at') or previous['generated_at'])>=-300,'recovery_source_timestamp_in_future')
            uncommitted_refresh=True
        else: require(fingerprint==pending['profile_after_sha256'],'profile_changed_while_awaiting_provider')
    ledger,receipts,provider_diagnostic=reconcile_outbox(client,previous,scope)
    reserved_skus={}
    for version in ledger:
        for uid,reservation in version.get('reservations',{}).items():
            if reservation.get('source_sku'): reserved_skus.setdefault(reservation['source_sku'],set()).add((uid,reservation['id']))
            saved=previous.setdefault('items',{}).get(uid)
            if not saved: previous['items'][uid]={**reservation,'published':False,'avito_id':None}
            else: require(saved['id']==reservation['id'],'outbox_source_identity_changed')
    name=previous.get('managed_feed_name')
    if not name:
        require(len(before['feeds_data'])==1,'managed_feed_ambiguous'); name=before['feeds_data'][0]['feed_name']
    require(sum(f['feed_name']==name for f in before['feeds_data'])==1,'managed_feed_missing')
    upload=client.call('/autoload/v4/uploads/last_successful'); require(upload.get('upload_id') and upload.get('status')=='success','successful_complete_upload_required')
    rows=paged_items(client); inventory=core_ids(client)
    reported={str(r['avito_id']) for r in rows if r.get('avito_id')}
    require(inventory and inventory<=reported,'owner_inventory_not_covered_by_autoload')
    links=[f for f in upload.get('feed_urls',[]) if f.get('name')==name]; require(len(links)==1,'frozen_feed_missing_or_ambiguous')
    raw=client.download(links[0]['url']); old=xml(raw,'Ads'); require(list(old) and all(n.tag=='Ad' for n in old),'invalid_old_ads')
    all_aids=[str(r['avito_id']) for r in rows if r.get('avito_id')]
    require(len(all_aids)==len(set(all_aids)),'duplicate_report_AvitoId_across_feeds')
    old_rows={r['ad_id']:r for r in rows if r['feed_name']==name}
    for saved in previous.get('items',{}).values():
        if ledger and not saved.get('published') and saved.get('id') not in old_rows: continue
        require(saved.get('id') in old_rows,'previous_snapshot_awaiting_provider_or_inventory_changed')
    require(len(old_rows)==len(old),'frozen_feed_report_count_mismatch')
    # Carry source bindings independently of whether the current offer can update.
    # Published XML and AvitoId are still re-proven against the successful feed.
    saved_by_id={}
    for uid,saved in previous.get('items',{}).items():
        require(saved.get('id') and saved['id'] not in saved_by_id,'duplicate_saved_identity')
        saved_by_id[saved['id']]=(uid,saved)
    tree=client.call('/autoload/v1/user-docs/tree'); slugs=tree_slugs(tree); schemas={}; items={uid:copy.deepcopy(saved) for uid,saved in previous.get('items',{}).items() if not saved.get('published') and saved['id'] not in old_rows}; item_keys={}; seen=set(); seen_avito=set()
    for ad in old:
        ident=ad.findtext('Id'); require(ident and ident not in seen and ident in old_rows,'frozen_feed_identity_mismatch'); seen.add(ident)
        row=old_rows[ident]; aid=str(row.get('avito_id') or ''); require(aid.isdigit() and aid not in seen_avito,'unproven_published_identity'); seen_avito.add(aid)
        require(ad.findtext('AvitoId') in (None,aid),'frozen_avito_identity_mismatch')
        slug=row.get('section',{}).get('slug'); require(slug in slugs,'category_slug_missing')
        if slug not in schemas: schemas[slug]=client.call('/autoload/v1/user-docs/node/'+quote(slug,safe='')+'/fields')
        require(not any(m.get('type')=='error' for m in row['messages']),'old_ad_has_provider_errors')
        validate(ad,schemas[slug],ad)
        uid,saved=saved_by_id.get(ident,('legacy:'+ident,{}))
        require(not saved.get('avito_id') or str(saved['avito_id'])==aid,'published_identity_changed')
        items[uid]={**saved,'id':ident,'avito_id':aid,'published':True,'variant_id':saved.get('variant_id'),'ad_xml':ET.tostring(ad,encoding='unicode')}
        item_keys[ident]=uid
    groups={}; offer_ids=set(); sku_groups={}
    for offer in offers:
        variant=offer.get('id'); uid=offer.get('group_id') or variant
        require(variant and uid and variant not in offer_ids,'source_duplicate_or_missing_identity'); offer_ids.add(variant)
        groups.setdefault(uid,[]).append(offer)
        sku=(offer.findtext("param[@name='Артикул система']") or '').strip()
        if sku: sku_groups.setdefault(sku,set()).add(uid)
    old_skus={}; account_skus={}; account_ids=set(); namespace_blocker=None
    previous_by_id={i['id']:i for i in previous.get('items',{}).values() if i.get('id')}
    profile_names={f['feed_name'] for f in before['feeds_data']}
    if any(r['feed_name'] not in profile_names for r in rows): namespace_blocker='report_feed_not_in_current_profile'
    for feed_name in profile_names:
        try:
            frozen_links=[f for f in upload.get('feed_urls',[]) if f.get('name')==feed_name]
            require(len(frozen_links)==1,'frozen_account_feed_missing_or_ambiguous')
            feed=old if feed_name==name else xml(client.download(frozen_links[0]['url']),'Ads')
            report_rows={r['ad_id']:r for r in rows if r['feed_name']==feed_name}
            require(len(report_rows)==len(feed),'account_feed_report_count_mismatch')
            feed_seen=set()
            for ad in feed:
                ident=ad.findtext('Id'); require(ad.tag=='Ad' and ident in report_rows and ident not in feed_seen,'account_feed_identity_mismatch'); feed_seen.add(ident)
                row=report_rows[ident]; aid=str(row.get('avito_id') or '')
                require(aid.isdigit() and ad.findtext('AvitoId') in (None,aid) and not any(m.get('type')=='error' for m in row['messages']),'account_feed_identity_unproven')
                key=(feed_name,ident); account_ids.add(key)
                sku=ad.findtext('SKU') or ad.findtext('Article') or ad.findtext('VendorCode')
                saved=previous_by_id.get(ident,{}) if feed_name==name else {}
                if not sku and saved and (not saved.get('avito_id') or str(saved['avito_id'])==aid): sku=saved.get('source_sku')
                if sku:
                    account_skus.setdefault(sku,set()).add(key)
                    if feed_name==name: old_skus.setdefault(sku,set()).add(ident)
                else: namespace_blocker='account_feed_SKU_namespace_incomplete'
        except Exception:
            namespace_blocker='account_feed_identity_namespace_unavailable'
    if len(account_skus)!=len(account_ids) or any(len(ids)!=1 for ids in account_skus.values()): namespace_blocker=namespace_blocker or 'account_feed_SKU_namespace_ambiguous'
    matched={}; reasons={}; learned_categories={}; learned_category_values={}; text_changes=[]; eligible_uids=set()
    allowlist=settings.get('test_allowlist')
    for uid,variants in groups.items():
        previous_item=previous.get('items',{}).get(uid,{})
        override=settings.get('product_overrides',{}).get(uid,{}).get('variant_id')
        preferred=override or previous_item.get('variant_id')
        selected=next((o for o in variants if o.get('id')==preferred),None) if preferred else min(variants,key=lambda o:o.get('id'))
        require(not override or selected is not None,'configured_variant_missing:'+uid)
        if selected is None: reasons[uid]='saved_variant_missing'; continue
        sku=(selected.findtext("param[@name='Артикул система']") or '').strip()
        if sku and len(sku_groups[sku])!=1: reasons[uid]='duplicate_source_SKU'; continue
        ident=previous_item.get('id')
        if not ident and uid in old_rows: ident=uid
        if not ident and sku and len(account_skus.get(sku,set()))==1 and len(old_skus.get(sku,set()))==1: ident=next(iter(old_skus[sku]))
        if not ident or ident not in old_rows: reasons[uid]='old_Id_matches_SKU_but_namespace_unproven' if sku in old_rows else 'stable_identity_not_proven'; continue
        require(ident not in matched,'multiple_source_groups_match_old_ad')
        ad=next(ad for ad in old if ad.findtext('Id')==ident)
        old_item=items[item_keys[ident]]
        require(item_keys[ident] in (uid,'legacy:'+ident),'source_binding_changed')
        if previous_item:
            require(previous_item.get('id')==ident and (not previous_item.get('avito_id') or str(previous_item['avito_id'])==old_item['avito_id']),'published_identity_changed')
        candidate=copy.deepcopy(ad)
        status=selected.findtext("param[@name='Статус']")
        if selected.get('available')=='false' or status!='В наличии': reasons[uid]='inventory_unavailable_preserved'; continue
        changes=[]
        try:
            price=Decimal(selected.findtext('price','')); require(price.is_finite() and price>0 and selected.findtext('currencyId') in ('RUR','RUB'),'invalid_source_price')
            original_description=selected.findtext('description','')
            description=description_text(original_description)
            if description!=original_description: changes.append({'product_uid':uid,'field':'description','policy':'html_to_text','original':original_description,'transformed':description})
            title=(selected.findtext('name') or '').strip()
            if not description:
                historical_description=ad.findtext('Description') or ''
                description=historical_description if historical_description.strip() else title
                changes.append({'product_uid':uid,'field':'description','policy':'historical_description' if historical_description.strip() else 'factual_title_description','original':original_description,'transformed':description})
            require(title and description,'missing_source_text')
            for tag,value in (('Title',title),('Description',description),('Price',format(price,'f'))):
                element=candidate.find(tag); require(element is not None,'missing_original_dynamic_field'); element.text=value
            pictures=[(n.text or '').strip() for n in selected.findall('picture')]
            require(pictures,'source_images_missing')
            for picture in pictures: safe_url(picture)
            images=candidate.find('Images')
            if images is None: images=ET.SubElement(candidate,'Images')
            images.clear()
            for picture in dict.fromkeys(pictures): ET.SubElement(images,'Image',{'url':picture})
            schema=schemas[old_rows[ident]['section']['slug']]
            adapt_text(candidate,schema,settings,uid,changes)
            validate(candidate,schema,ad)
        except (Blocked,InvalidOperation) as error:
            reasons[uid]='source_update_validation_failed_preserved'
            if isinstance(error,Blocked) and str(error).startswith(('text_policy_reject_','text_word_exceeds_limit_')): reasons[uid]+=':'+str(error)
            continue
        eligible_uids.add(uid)
        if allowlist is not None and uid not in allowlist: reasons[uid]='outside_allowlist_preserved'; continue
        text_changes.extend(changes)
        position=list(old).index(ad); old.remove(ad); old.insert(position,candidate)
        matched[ident]=uid; del items[item_keys[ident]]
        category_id=selected.findtext('categoryId')
        learned_categories.setdefault(category_id,set()).add(old_rows[ident]['section']['slug'])
        if ad.findtext('Category'): learned_category_values.setdefault(category_id,set()).add(ad.findtext('Category'))
        items[uid]={**old_item,'source_sku':sku or previous_item.get('source_sku'),'variant_id':selected.get('id'),'ad_xml':ET.tostring(candidate,encoding='unicode')}
    # CREATE requires a complete old SKU namespace, exact unique category name,
    # unique source SKU and explicit source facts for every provider requirement.
    new_count=0
    names={}; category_paths={}
    def category_names(nodes,path=()):
        for node in nodes:
            current_path=path+(node['name'],)
            if node.get('slug'):
                names.setdefault(node['name'],set()).add(node['slug']); category_paths.setdefault(current_path,set()).add(node['slug'])
            nested=node.get('nested') or []
            if isinstance(nested,dict):
                for children in nested.values(): category_names(children,current_path)
            else: category_names(nested,current_path)
    category_names(tree['categories'])
    category_nodes={c.get('id'):c for c in source_root.findall('./shop/categories/category')}
    categories={key:(c.text or '').strip() for key,c in category_nodes.items()}
    def rental_category(key):
        visited=set()
        while key in category_nodes:
            require(key not in visited,'category_parent_cycle'); visited.add(key)
            if any(word in categories[key].casefold() for word in ('прокат','аренд')): return True
            key=category_nodes[key].get('parentId')
        return False
    def exclude(uid,reason):
        nonlocal reasons
        reasons[uid]=reason
    full_old_skus=namespace_blocker is None
    for uid,variants in groups.items():
        if uid in matched.values(): continue
        previous_candidate=previous.get('items',{}).get(uid,{})
        if previous_candidate and (not ledger or previous_candidate.get('published')): continue
        override=settings.get('product_overrides',{}).get(uid,{}).get('variant_id')
        preferred=override or previous_candidate.get('variant_id')
        chosen=next((o for o in variants if o.get('id')==preferred),None) if preferred else min(variants,key=lambda o:o.get('id'))
        require(not override or chosen is not None,'configured_variant_missing:'+uid)
        if chosen is None: exclude(uid,'pending_variant_missing'); continue
        params={p.get('name'):(p.text or '').strip() for p in chosen.findall('param')}
        sku=params.get('Артикул система','')
        if not sku: exclude(uid,'source_SKU_missing_new_identity_unproven'); continue
        if len(sku_groups.get(sku,set()))!=1: exclude(uid,'duplicate_source_SKU'); continue
        if any(owner_uid!=uid for owner_uid,_ in reserved_skus.get(sku,set())): exclude(uid,'SKU_reserved_by_pending_source_group'); continue
        if sku in old_skus: continue
        if sku in account_skus: exclude(uid,'SKU_already_owned_by_unmanaged_feed'); continue
        if not full_old_skus: exclude(uid,namespace_blocker or 'account_identity_namespace_not_proven_for_new_items'); continue
        if rental_category(chosen.findtext('categoryId')): exclude(uid,'rental_CREATE_not_supported_in_service'); continue
        category_id=chosen.findtext('categoryId'); category=categories.get(category_id)
        path=[]; visited=set(); cursor=category_id
        while cursor in category_nodes:
            require(cursor not in visited,'category_parent_cycle'); visited.add(cursor)
            path.insert(0,categories[cursor]); cursor=category_nodes[cursor].get('parentId')
        path_matches=set().union(*(slugs for candidate_path,slugs in category_paths.items() if len(path)>1 and len(candidate_path)>=len(path) and candidate_path[-len(path):]==tuple(path)))
        learned=learned_categories.get(category_id,set())
        options=learned or path_matches or names.get(category,set())
        if learned and path_matches and learned!=path_matches: options=set()
        if len(options)!=1: exclude(uid,'category_exact_unique_mapping_missing'); continue
        slug=next(iter(options))
        if slug not in schemas: schemas[slug]=client.call('/autoload/v1/user-docs/node/'+quote(slug,safe='')+'/fields')
        schema=schemas[slug]
        if params.get('Статус')!='В наличии' or chosen.get('available')=='false': continue
        changes=[]
        try:
            ident=previous_candidate.get('id') or 'tilda-'+uid
            require(ident not in seen and all(existing_id!=ident for _,existing_id in account_ids),'new_identity_collides_across_account_feeds')
            price=Decimal(chosen.findtext('price','')); require(price.is_finite() and price>0 and chosen.findtext('currencyId') in ('RUR','RUB'),'invalid_new_price')
            from html import unescape
            title=(chosen.findtext('name') or '').strip()
            original_description=chosen.findtext('description') or ''
            description=description_text(original_description) if original_description else title
            require(title and description,'new_source_text_missing')
            if description!=original_description: changes.append({'product_uid':uid,'field':'description','policy':'html_to_text' if original_description else 'factual_title_description','original':original_description,'transformed':description})
            learned_values=learned_category_values.get(category_id,set())
            category_value=next(iter(learned_values)) if len(learned_values)==1 else schema.get('node',{}).get('name',category)
            facts={'Id':ident,'Title':title,'Description':description,'Price':format(price,'f'),'Category':category_value}
            for field in schema['fields']:
                tag=field['tag']
                if tag in facts or tag in ('AvitoId','DateEnd','Images'): continue
                trusted=settings.get('source_facts',{}).get(tag,{})
                if tag in ('SKU','Article','VendorCode'):
                    require(tag not in params or params[tag]==sku,'source_SKU_facts_conflict'); facts[tag]=sku
                elif tag in params or field.get('label') in params:
                    explicit=params[tag] if tag in params else params[field['label']]
                    require(not(tag=='Condition' and trusted.get('provenance')=='user-confirmed' and trusted.get('value') in ('Новое','Новый','New') and explicit not in ('Новое','Новый','New')),'product_fact_contradicts_confirmed_condition')
                    facts[tag]=explicit
                elif trusted.get('provenance')=='user-confirmed' and trusted.get('value'):
                    value=trusted['value']
                    if tag=='Condition' and value in ('Новое','Новый'):
                        enum={v['value'] for rule in field.get('content',[]) for v in (rule.get('values') or [])}
                        if enum:
                            allowed=enum & {'Новое','Новый'}; require(len(allowed)==1,'confirmed_condition_enum_ambiguous'); value=next(iter(allowed))
                    facts[tag]=value
            candidate=ET.Element('Ad')
            for tag,value in facts.items(): ET.SubElement(candidate,tag).text=value
            pictures=[(p.text or '').strip() for p in chosen.findall('picture')]; require(pictures,'new_images_missing')
            images=ET.SubElement(candidate,'Images')
            for picture in dict.fromkeys(pictures): safe_url(picture); ET.SubElement(images,'Image',{'url':picture})
            adapt_text(candidate,schema,settings,uid,changes)
            validate(candidate,schema)
        except (Blocked,InvalidOperation) as error:
            exclude(uid,str(error) if isinstance(error,Blocked) else 'new_source_price_invalid'); continue
        eligible_uids.add(uid)
        if allowlist is not None and uid not in allowlist: exclude(uid,'outside_allowlist'); continue
        text_changes.extend(changes)
        old.append(candidate); new_count+=1
        items[uid]={'id':ident,'source_sku':sku,'avito_id':None,'published':False,'variant_id':chosen.get('id'),'ad_xml':ET.tostring(candidate,encoding='unicode')}
        reasons.pop(uid,None)
    candidate_count=sum(allowlist is None or uid in allowlist for uid in eligible_uids)
    if 'eligible_uids' in previous:
        candidate_baseline=sum(allowlist is None or uid in allowlist for uid in previous['eligible_uids'])
    else:
        # Old account snapshots counted retained feed ads, including unmanaged
        # legacy rows. Only established source bindings share the current unit.
        bound=[uid for uid,item in previous.get('items',{}).items() if item.get('variant_id')]
        candidate_baseline=sum(allowlist is None or uid in allowlist for uid in bound)
    require(not candidate_baseline or candidate_count>=candidate_baseline*minimum_ratio,'candidate_count_drop_guard')
    require(matched or new_count,'no_safe_source_updates_profile_preserved')
    raw=b'<?xml version="1.0" encoding="utf-8"?>\n'+ET.tostring(old,encoding='utf-8')+b'\n'
    require(client.call('/autoload/v4/uploads/last_successful').get('upload_id')==upload['upload_id'],'upload_changed_during_prepare')
    complete=len(groups)==len(matched)+new_count
    generated=stamp()
    if uncommitted_refresh and digest(raw)==previous['pending_provider']['feed_sha256'] and timestamp_age(previous.get('data_generated_at') or previous['generated_at'])<=14400: generated=previous.get('data_generated_at') or previous['generated_at']
    latest_accepted=any(r['feed_sha256']==digest(raw) for r in receipts)
    report={'generated_at':generated,'production_enabled':True,'account_scope':scope,'blockers':[], 'data_generated_at':generated,'phase':'awaiting_scheduled_provider_result','provider_validation':'accepted' if latest_accepted else 'pending','provider_diagnostic':provider_diagnostic,'provider_status_as_of':'prepare','provider_status_channel':'GitHub Actions: account verify step','observed_last_successful_upload_id':upload['upload_id'],'preserved_fields_provenance':'historical_successful_frozen_feed_and_v4_item_without_errors','candidate_count':len(old),'source_count':len(offers),'preserved_count':len(old_rows),'text_transforms':text_changes,'category_mapping_provenance':{key:sorted(value) for key,value in learned_categories.items()},'source_sync_complete':complete,'source_sync_coverage':'complete' if complete else 'partial','source_update_count':len(matched),'new_source_count':new_count,'excluded_source_count':len(groups)-len(matched)-new_count,'limitations':['ambiguous_or_incomplete_source_products_excluded'],'source_exclusions':[{'product_uid':u,'reason':r} for u,r in reasons.items()]}
    report['candidate_count']=candidate_count
    report['feed_ad_count']=len(old)
    state={'version':1,'account_scope':scope,'managed_feed_name':name,'generated_at':generated,'data_generated_at':generated,'items':items,'source_count':len(offers),'candidate_count':candidate_count,'eligible_uids':sorted(eligible_uids),'candidate_scope':'validated_source_groups','feed_ad_count':len(old)}
    expected={'feed_sha256':digest(raw),'feed_name':name,'feed_ids':[a.findtext('Id') for a in old],'legacy_avito_ids':{i['id']:i['avito_id'] for i in items.values() if i.get('published') and i.get('avito_id')}}
    state['pending_provider']=expected
    outputs={'feed.xml':raw,'preview.xml':raw,'state.json':encoded(state),'status.json':encoded(compact_status(report)),'report.json':encoded(report),'reasons.csv':b'product_uid,offer_uid,decision,reasons\n','text-changes.csv':b'product_uid,offer_uid,field,original,transformed\n'}
    text_csv=io.StringIO(newline=''); writer=csv.writer(text_csv)
    writer.writerow(['product_uid','offer_uid','field','original','transformed'])
    for change in text_changes:
        writer.writerow([change['product_uid'],items[change['product_uid']].get('variant_id'),change['field'],change['original'],change['transformed']])
    outputs['text-changes.csv']=text_csv.getvalue().encode('utf-8')
    after=copy.deepcopy(before)
    for f in after['feeds_data']:
        if f['feed_name']==name: f['feed_url']=args.public_feed_url
    plan={'version':1,'scope':scope,'profile_before':before,'profile_after':after,'feed_sha256':digest(raw),'public_feed_url':args.public_feed_url,'generated_at':generated,'legacy_avito_ids':{i['id']:i['avito_id'] for i in items.values() if i.get('published') and i.get('avito_id')},'feed_ids':[a.findtext('Id') for a in old],'upload_before':upload['upload_id'],'phase':'prepared','ready_for_profile_switch':True,'complete_source_sync':complete,'source_fact_provenance':{k:v['provenance'] for k,v in settings.get('source_facts',{}).items() if v.get('provenance')=='user-confirmed'}}
    expected['profile_before_sha256']=digest(encoded(before)); expected['profile_after_sha256']=digest(encoded(after))
    expected.update(scope=scope,created_at=generated,status='accepted' if latest_accepted else 'pending',reservations={uid:{k:item.get(k) for k in ('id','source_sku','variant_id')} for uid,item in items.items() if not item.get('published')})
    existing=next((v for v in ledger if v['feed_sha256']==expected['feed_sha256']),None)
    if existing:
        created=existing['created_at']; existing.update(expected); existing['created_at']=created
    else:
        require(len(ledger)<OUTBOX_LIMIT,'provider_outbox_limit_exceeded'); ledger.append(copy.deepcopy(expected))
    state['provider_outbox']=ledger; state['provider_receipts']=receipts
    report['provider_versions_pending']=sum(v['status']=='pending' for v in ledger)
    report['provider_versions_retained']=len(ledger)
    if receipts: report['last_provider_receipt']={k:receipts[-1][k] for k in ('upload_id','feed_sha256','observed_at')}
    outputs['state.json']=encoded(state); outputs['status.json']=encoded(compact_status(report)); outputs['report.json']=encoded(report)
    save_plan(plan_path,plan)
    out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.account-',dir=out.parent) as temporary:
        stage=Path(temporary)/'stage'; stage.mkdir()
        for filename,data in outputs.items(): (stage/filename).write_bytes(data)
        os.replace(stage,out)

def recovery_material(args,previous,before):
    """Validate the immutable locally published candidate and exact original profile."""
    pending=previous['pending_provider']; directory=Path(args.previous_state).parent
    raw=(directory/'feed.xml').read_bytes()
    require(digest(raw)==pending['feed_sha256'],'recovery_snapshot_digest_mismatch')
    ads=xml(raw,'Ads'); require({a.findtext('Id') for a in ads}==set(pending['feed_ids']),'recovery_snapshot_identity_mismatch')
    after=copy.deepcopy(before)
    matches=[f for f in after['feeds_data'] if f['feed_name']==pending['feed_name']]
    require(len(matches)==1,'recovery_managed_feed_missing'); matches[0]['feed_url']=args.public_feed_url
    require(digest(encoded(after))==pending['profile_after_sha256'],'recovery_target_profile_mismatch')
    report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
    require(report.get('production_enabled') is True and report.get('blockers')==[],'recovery_previous_snapshot_not_ready')
    return raw,after,report

def commit(args,client):
    path=private_plan(args.plan); plan=json.loads(path.read_text(encoding='utf-8'))
    require(plan.get('version')==1 and plan.get('ready_for_profile_switch') is True,'plan_not_ready_for_account_activation')
    require(owner(client)==plan['scope'],'account_scope_changed')
    for attempt in range(4):
        try:
            feed=client.download(plan['public_feed_url']); require(digest(feed)==plan['feed_sha256'],'published_feed_digest_mismatch')
            status_url=plan['public_feed_url'].rsplit('/',1)[0]+'/status.json'
            status=client.transport('GET',status_url,{},None)
            if isinstance(status,bytes): status=json.loads(status)
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(status['generated_at'].replace('Z','+00:00'))).total_seconds()
            require(status.get('account_scope')==plan['scope'] and status.get('production_enabled') is True and status.get('blockers')==[] and status.get('generated_at')==plan['generated_at'] and -300<=age<=14400,'public_snapshot_not_fresh_or_ready')
            break
        except Exception:
            if attempt==3: raise Blocked('public_snapshot_not_available_fresh_and_matching') from None
            time.sleep(25)
    current=profile(client)
    if current==plan['profile_after']: return
    require(current==plan['profile_before'],'profile_changed_since_prepare')
    try:
        client.call('/autoload/v2/profile','POST',plan['profile_after'])
        require(profile(client)==plan['profile_after'],'profile_post_not_confirmed')
    except Exception:
        # A failed response may conceal a successful POST; restore only our exact write.
        try:
            if profile(client)==plan['profile_after']: client.call('/autoload/v2/profile','POST',plan['profile_before'])
        except Exception: raise Blocked('profile_update_failed_rollback_unconfirmed') from None
        raise Blocked('profile_update_failed_previous_profile_restored') from None
    plan['phase']='awaiting_scheduled_provider_result'; save_plan(path,plan)

def observe(client,expected):
    result=client.call('/autoload/v4/uploads/current')
    require(result.get('upload_id') and result.get('status')=='success','provider_upload_pending_or_rejected')
    links=[f for f in result.get('feed_urls',[]) if f.get('name')==expected['feed_name']]
    require(len(links)==1 and digest(client.download(links[0]['url']))==expected['feed_sha256'],'provider_upload_not_this_snapshot')
    rows=paged_items(client,'current')
    all_aids=[str(r['avito_id']) for r in rows if r.get('avito_id')]
    require(len(all_aids)==len(set(all_aids)),'provider_duplicate_AvitoId')
    relevant=[r for r in rows if r['feed_name']==expected['feed_name']]
    mapping={r['ad_id']:str(r.get('avito_id') or '') for r in relevant}
    require(all(mapping.get(ident)==str(aid) for ident,aid in expected['legacy_avito_ids'].items()),'provider_legacy_identity_changed')
    require(set(mapping)==set(expected['feed_ids']) and all(r.get('avito_id') and not any(m.get('type')=='error' for m in r['messages']) for r in relevant),'provider_items_incomplete_or_rejected')
    require(client.call('/autoload/v4/uploads/current').get('upload_id')==result['upload_id'],'provider_upload_changed_during_verification')
    return {'upload_id':result['upload_id'],'feed_sha256':expected['feed_sha256'],'avito_ids':mapping,'observed_at':stamp()}

def verify(args,client):
    path=private_plan(args.plan); plan=json.loads(path.read_text(encoding='utf-8'))
    require(owner(client)==plan['scope'],'account_scope_changed')
    managed=next(f['feed_name'] for f in plan['profile_after']['feeds_data'] if f['feed_url']==plan['public_feed_url'])
    expected={k:plan[k] for k in ('feed_sha256','feed_ids','legacy_avito_ids')}; expected['feed_name']=managed
    plan['provider_receipt']=observe(client,expected)
    plan['phase']='provider_success'; save_plan(path,plan)

def main(argv=None,transport=None):
    parser=argparse.ArgumentParser(description=__doc__); subs=parser.add_subparsers(dest='action',required=True)
    p=subs.add_parser('prepare'); p.add_argument('--config',required=True); s=p.add_mutually_exclusive_group(required=True); s.add_argument('--source-file'); s.add_argument('--source-url'); p.add_argument('--previous-state',required=True); p.add_argument('--out',required=True); p.add_argument('--public-feed-url',required=True); p.add_argument('--plan',required=True)
    for action in ('commit','verify'): subs.add_parser(action).add_argument('--plan',required=True)
    args=parser.parse_args(argv)
    credentials=[bool(os.environ.get(k)) for k in ('AVITO_CLIENT_ID','AVITO_CLIENT_SECRET')]
    if not any(credentials): print('account_credentials_absent'); return 3
    if not all(credentials): print('account_blocked:incomplete_credentials',file=sys.stderr); return 2
    try:
        client=Client(transport); client.authenticate()
        {'prepare':prepare,'commit':commit,'verify':verify}[args.action](args,client)
        print('account_'+args.action+'_ok'); return 0
    except Blocked as error: print('account_blocked:'+str(error),file=sys.stderr); return 2
    except Exception: print('account_blocked:invalid_response_or_network_or_storage_failure',file=sys.stderr); return 2

if __name__=='__main__': raise SystemExit(main())
