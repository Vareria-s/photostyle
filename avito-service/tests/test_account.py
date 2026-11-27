import importlib.util
import io
import copy
import hashlib
import shutil
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

class AccountCLI(unittest.TestCase):
    def test_absent_credentials_does_not_create_snapshot(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, str(ROOT/'account.py'), 'prepare', '--config', str(ROOT/'config.json'), '--source-file', str(ROOT/'fixtures/catalog.yml'), '--previous-state', d+'/missing', '--out', d+'/out', '--plan', d+'/plan.json', '--public-feed-url', 'https://photostyle-avito.pages.dev/feed.xml'], env={k:v for k,v in os.environ.items() if k not in ('AVITO_CLIENT_ID','AVITO_CLIENT_SECRET')}, capture_output=True)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse(Path(d+'/out').exists())


    def test_prepare_commit_preserves_unmatched_and_schedule(self):
        spec = importlib.util.spec_from_file_location('account_under_test', ROOT/'account.py')
        a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
        profile = {'autoload_enabled':True,'feeds_data':[{'feed_name':'shop','feed_url':'https://old.example/feed.xml'},{'feed_name':'unmanaged','feed_url':'https://other.example/feed.xml'}], 'report_email':'owner@example.com','schedule':[{'rate':2,'weekdays':[1],'time_slots':[3]}]}
        old = b'<Ads formatVersion="3" target="Avito.ru"><Ad><Id>legacy</Id><SKU>OLD-A</SKU><Condition>New</Condition><AvitoId>123</AvitoId><Title>Keep</Title><Description>Original</Description><Price>50</Price><Category>Camera</Category><Specifications><Lens>Accepted original</Lens></Specifications><Images><Image url="https://images.example/old.jpg" /></Images></Ad><Ad><Id>unknown</Id><SKU>OLD-B</SKU><Condition>New</Condition><AvitoId>124</AvitoId><Title>Unmatched</Title><Description>Keep all</Description><Price>9</Price><Category>Camera</Category><Specifications><Lens>Accepted original</Lens></Specifications><Images><Image url="https://images.example/old.jpg" /></Images></Ad></Ads>'
        upload = {'upload_id':7,'status':'success','feed_urls':[{'name':'shop','url':'https://frozen.example/feed.xml?private=token'},{'name':'unmanaged','url':'https://frozen.other.example/feed.xml'}]}
        calls=[]; saved=dict(profile); public={}; public_attempts=0; verification_error=True; verification_corruption=None; provider_feed=None
        def transport(method,url,headers,body):
            nonlocal saved, public_attempts
            calls.append((method,url))
            if url.endswith('/token'): return {'access_token':'PRIVATE_TOKEN','token_type':'Bearer','expires_in':86400}
            if url.endswith('/accounts/self'): return {'id':42}
            if '/core/v1/items?' in url: return {'resources':[{'id':123},{'id':124}], 'meta':{'page':1,'per_page':99}}
            if url.endswith('/autoload/v2/profile'):
                if method=='POST': saved=json.loads(body)
                return saved
            if url.endswith('/uploads/last_successful'):
                if provider_feed is not None and not verification_error:
                    return {**upload,'upload_id':8,'feed_urls':[{'name':'shop','url':'https://receipt.example/frozen.xml'},{'name':'unmanaged','url':'https://frozen.other.example/feed.xml'}]}
                return upload
            if url.endswith('/uploads/current'): return {**upload,'upload_id':8 if provider_feed is not None else 7,'feed_urls':[{'name':'shop','url':'https://receipt.example/frozen.xml'}]}
            if url=='https://receipt.example/frozen.xml': return provider_feed if provider_feed is not None else old
            if '/uploads/current/items?' in url:
                identities=[('legacy',123),('unknown',124),('tilda-new-group',125)]
                if verification_corruption=='duplicate': identities=[('legacy',123),('unknown',123),('tilda-new-group',125)]
                if verification_corruption=='swapped': identities=[('legacy',124),('unknown',123),('tilda-new-group',125)]
                return {'items':[{'ad_id':i,'avito_id':n,'feed_name':'shop','section':{'slug':'camera'},'messages':[{'type':'error'}] if verification_error else []} for i,n in identities],'meta':{'page':1,'pages':1,'perPage':100,'total':3}}
            if '/last_successful/items?' in url and provider_feed is not None and not verification_error:
                return {'items':[{'ad_id':i,'avito_id':n,'feed_name':'shop','section':{'slug':'camera'},'messages':[]} for i,n in [('legacy',123),('unknown',124),('tilda-new-group',125)]],'meta':{'page':1,'pages':1,'perPage':100,'total':3}}
            if '/last_successful/items?' in url: return {'items':[{'ad_id':'legacy','avito_id':123,'feed_name':'shop','section':{'slug':'camera'},'messages':[]},{'ad_id':'unknown','avito_id':124,'feed_name':'shop','section':{'slug':'camera'},'messages':[]}], 'meta':{'page':1,'pages':1,'perPage':100,'total':2}}
            if url.endswith('/user-docs/tree'): return {'categories':[{'name':'Camera','slug':'camera'}]}
            if url.endswith('/node/camera/fields'): return {'node':{'name':'Camera','slug':'camera'},'fields':[{'tag':t,'content':[{'field_type':'input','data_type':'string','required':True,'required_by_dependency':False}]} for t in ('Id','Title','Description','Price','Category','Images','SKU','Condition')]}
            if url.startswith('https://frozen.other.example/'): return b'<Ads formatVersion="3" target="Avito.ru" />'
            if url.startswith('https://frozen.example/'): return old
            if url in public:
                if url.endswith('/feed.xml'):
                    public_attempts+=1
                    if public_attempts<3: return b'CDN previous snapshot'
                return public[url]
            raise AssertionError(url)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'AVITO_CLIENT_ID':'PRIVATE_ID','AVITO_CLIENT_SECRET':'PRIVATE_SECRET','RUNNER_TEMP':directory}):
            d=Path(directory); (d/'source.yml').write_bytes((ROOT/'fixtures/catalog.yml').read_bytes().replace(b'group_id="product-1"',b'group_id="legacy"').replace(b'Fixture camera</category>',b'Studio cameras</category>').replace(b'</offers>', '<offer id="new-offer" group_id="new-group"><name>New camera</name><description>&lt;p&gt;New &lt;strong&gt;description&lt;/strong&gt;&lt;/p&gt;</description><price>100</price><currencyId>RUR</currencyId><categoryId>cat</categoryId><picture>https://example.com/new.jpg</picture><param name="Статус">В наличии</param><param name="Артикул система">NEW-SKU</param></offer></offers>'.encode())); args=['prepare','--config',str(ROOT/'config.json'),'--source-file',str(d/'source.yml'),'--previous-state',str(d/'missing'),'--out',str(d/'out'),'--plan',str(d/'private/plan.json'),'--public-feed-url','https://photostyle-avito.pages.dev/feed.xml']
            (d/'previous.json').write_text(json.dumps({'account_scope':'avito-42','managed_feed_name':'shop','items':{}}))
            args[args.index('--previous-state')+1]=str(d/'previous.json')
            (d/'config.json').write_text(json.dumps({'source_facts':{'Condition':{'value':'New','provenance':'user-confirmed'}}}))
            args[args.index('--config')+1]=str(d/'config.json')
            # Provider errors and unsafe URLs must never mutate profile or expose details.
            for failure in ('permission','429','partial','schema','ssrf','cross_account'):
                blocked_args=list(args); blocked_args[blocked_args.index('--out')+1]=str(d/('bad-'+failure))
                def broken(method,url,headers,body):
                    if failure in ('permission','429') and url.endswith('/autoload/v2/profile'): raise OSError('PRIVATE_SECRET https://private.example/?token=PRIVATE_TOKEN')
                    if failure=='partial' and '/last_successful/items?' in url: return {'items':[], 'meta':{'page':1,'pages':1,'perPage':100,'total':2}}
                    if failure=='schema' and '/node/camera/fields' in url: return {'fields':[{'tag':'Title','content':[{'field_type':'unknown','data_type':'string','required':True}]}]}
                    if failure=='ssrf' and url.endswith('/uploads/last_successful'): return {**upload,'feed_urls':[{'name':'shop','url':'http://127.0.0.1/private?secret=PRIVATE_TOKEN'}]}
                    return transport(method,url,headers,body)
                if failure=='cross_account':
                    (d/'foreign.json').write_text(json.dumps({'account_scope':'avito-9','items':{}}))
                    blocked_args[blocked_args.index('--previous-state')+1]=str(d/'foreign.json')
                with self.subTest(failure=failure), mock.patch('sys.stderr',new_callable=io.StringIO) as error:
                    self.assertEqual(a.main(blocked_args,transport=broken),2)
                    self.assertFalse(Path(blocked_args[blocked_args.index('--out')+1]).exists())
                    self.assertNotIn('PRIVATE_SECRET',error.getvalue()); self.assertNotIn('PRIVATE_TOKEN',error.getvalue())
                    self.assertFalse(any(m=='POST' and '/profile' in u for m,u in calls))
            large_pages=[]
            def large_account(method,url,headers,body):
                if '/core/v1/items?' in url:
                    from urllib.parse import urlsplit,parse_qs
                    page=int(parse_qs(urlsplit(url).query)['page'][0]); large_pages.append(page)
                    return {'resources':[{'id':page*1000+i} for i in range(99)] if page<=25 else [],'meta':{'page':page,'per_page':99}}
                return transport(method,url,headers,body)
            large_args=list(args); large_args[large_args.index('--out')+1]=str(d/'large')
            with mock.patch.object(a.time,'sleep') as sleeps:
                self.assertEqual(a.main(large_args,transport=large_account),2)
                self.assertEqual(large_pages,list(range(1,27)))
                self.assertTrue(sleeps.called)
            foreign_args=list(args); foreign_args[foreign_args.index('--out')+1]=str(d/'foreign-sku')
            def with_foreign_sku(method,url,headers,body):
                if url.startswith('https://frozen.other.example/'):
                    return b'<Ads formatVersion="3" target="Avito.ru"><Ad><Id>outside</Id><AvitoId>126</AvitoId><SKU>NEW-SKU</SKU></Ad></Ads>'
                value=copy.deepcopy(transport(method,url,headers,body))
                if '/core/v1/items?' in url: value['resources'].append({'id':126})
                if '/last_successful/items?' in url:
                    value['items'].append({'ad_id':'outside','avito_id':126,'feed_name':'unmanaged','section':{'slug':'camera'},'messages':[]}); value['meta']['total']=3
                return value
            self.assertEqual(a.main(foreign_args,transport=with_foreign_sku),0)
            self.assertNotIn(b'<Id>tilda-new-group</Id>',(d/'foreign-sku/feed.xml').read_bytes())
            self.assertIn('SKU_already_owned_by_unmanaged_feed',(d/'foreign-sku/report.json').read_text())
            (d/'conflict.yml').write_bytes((d/'source.yml').read_bytes().replace(b'</offer></offers>','<param name="Состояние">Б/у</param></offer></offers>'.encode()))
            conflict=list(args); conflict[conflict.index('--source-file')+1]=str(d/'conflict.yml'); conflict[conflict.index('--out')+1]=str(d/'conflict'); conflict[conflict.index('--plan')+1]=str(d/'private/conflict.json')
            def labeled_fields(method,url,headers,body):
                value=copy.deepcopy(transport(method,url,headers,body))
                if '/node/camera/fields' in url:
                    for field in value['fields']:
                        if field['tag']=='Condition': field['label']='Состояние'
                return value
            self.assertEqual(a.main(conflict,transport=labeled_fields),0)
            self.assertNotIn(b'<Id>tilda-new-group</Id>',(d/'conflict/feed.xml').read_bytes())
            self.assertIn('product_fact_contradicts_confirmed_condition',(d/'conflict/report.json').read_text())
            many_source=(d/'source.yml').read_bytes().replace(b'</offers>',(''.join('<offer id="excluded-'+str(i)+'"><name>Excluded</name></offer>' for i in range(5000))+'</offers>').encode())
            (d/'many.yml').write_bytes(many_source)
            many=list(args); many[many.index('--source-file')+1]=str(d/'many.yml'); many[many.index('--out')+1]=str(d/'many'); many[many.index('--plan')+1]=str(d/'private/many.json')
            self.assertEqual(a.main(many,transport=transport),0)
            self.assertLess((d/'many/status.json').stat().st_size,256*1024)
            self.assertFalse(json.loads((d/'private/many.json').read_text())['complete_source_sync'])
            self.assertEqual(json.loads((d/'many/report.json').read_text())['source_sync_coverage'],'partial')
            self.assertEqual(a.main(args, transport=transport),0)
            feed=(d/'out/feed.xml').read_bytes(); self.assertIn(b'<Title>Fixture camera</Title>',feed); self.assertIn(b'<Title>Unmatched</Title>',feed); self.assertIn(b'https://example.com/camera.jpg',feed)
            state=json.loads((d/'out/state.json').read_text()); self.assertEqual(state['account_scope'],'avito-42')
            self.assertEqual(len(state['items']),3); self.assertIn(b'<Id>tilda-new-group</Id>',feed); self.assertIn(b'<Condition>New</Condition>',feed); self.assertIn(b'<Description>New description</Description>',feed)
            pending=copy.deepcopy(state); pending['items'].pop('new-group')
            pending['pending_provider']={'feed_sha256':hashlib.sha256(feed).hexdigest(),'feed_name':'shop','feed_ids':['legacy','unknown','tilda-new-group'],'legacy_avito_ids':{'legacy':'123','unknown':'124'}}
            (d/'pending.json').write_text(json.dumps(pending))
            pending_args=list(args); pending_args[pending_args.index('--previous-state')+1]=str(d/'pending.json'); pending_args[pending_args.index('--out')+1]=str(d/'pending-out')
            self.assertEqual(a.main(pending_args,transport=transport),2)
            self.assertFalse((d/'pending-out').exists())
            text=''.join(p.read_text() for p in (d/'out').iterdir())
            for secret in ('PRIVATE_TOKEN','PRIVATE_ID','PRIVATE_SECRET','private=token','owner@example.com'): self.assertNotIn(secret,text)
            self.assertFalse(any(m=='POST' and '/profile' in u for m,u in calls))
            with mock.patch.object(a.time,'sleep'):
                self.assertEqual(a.main(['commit','--plan',str(d/'private/plan.json')],transport=transport),2)
            recovery=list(args); recovery[recovery.index('--out')+1]=str(d/'recovered'); recovery[recovery.index('--previous-state')+1]=str(d/'out/state.json'); recovery[recovery.index('--plan')+1]=str(d/'private/recovery.json')
            self.assertEqual(a.main(recovery,transport=transport),0)
            self.assertEqual((d/'recovered/feed.xml').read_bytes(),feed)
            self.assertEqual(json.loads((d/'recovered/status.json').read_text())['generated_at'],state['generated_at'])
            shutil.copytree(d/'out',d/'stale-previous')
            old_stamp=(datetime.now(timezone.utc)-timedelta(hours=5)).isoformat().replace('+00:00','Z')
            for name in ('state.json','status.json','report.json'):
                value=json.loads((d/'stale-previous'/name).read_text()); value['generated_at']=old_stamp; value['data_generated_at']=old_stamp
                (d/'stale-previous'/name).write_text(json.dumps(value))
            (d/'changed.yml').write_bytes((d/'source.yml').read_bytes().replace(b'<name>Fixture camera</name>',b'<name>Updated camera</name>').replace(b'<price>100.00</price>',b'<price>432.10</price>'))
            stale=list(args); stale[stale.index('--source-file')+1]=str(d/'changed.yml'); stale[stale.index('--previous-state')+1]=str(d/'stale-previous/state.json'); stale[stale.index('--out')+1]=str(d/'refreshed'); stale[stale.index('--plan')+1]=str(d/'private/refreshed.json')
            self.assertEqual(a.main(stale,transport=transport),0)
            refreshed=(d/'refreshed/feed.xml').read_bytes()
            self.assertIn(b'<Title>Updated camera</Title>',refreshed); self.assertIn(b'<Price>432.10</Price>',refreshed)
            refreshed_state=json.loads((d/'refreshed/state.json').read_text())
            self.assertEqual({i['id'] for i in refreshed_state['items'].values()},{i['id'] for i in state['items'].values()})
            self.assertNotEqual(refreshed_state['generated_at'],old_stamp)
            self.assertFalse(any(m=='POST' and '/profile' in u for m,u in calls))
            public['https://photostyle-avito.pages.dev/feed.xml']=feed
            public['https://photostyle-avito.pages.dev/status.json']=json.loads((d/'recovered/status.json').read_text())
            with mock.patch.object(a.time,'sleep'):
                self.assertEqual(a.main(['commit','--plan',str(d/'private/recovery.json')],transport=transport),0)
            self.assertEqual(public_attempts,3)
            self.assertEqual(saved['schedule'],profile['schedule'])
            self.assertEqual(saved['feeds_data'][1],profile['feeds_data'][1])
            self.assertEqual(saved['feeds_data'][0]['feed_url'],'https://photostyle-avito.pages.dev/feed.xml')
            self.assertEqual(a.main(['commit','--plan',str(d/'private/recovery.json')],transport=transport),0)
            self.assertEqual(sum(m=='POST' and '/profile' in u for m,u in calls),1)
            (d/'reserved-collision.yml').write_bytes((d/'source.yml').read_bytes().replace(b'group_id="new-group"',b'group_id="another-group"'))
            reserved=list(args); reserved[reserved.index('--source-file')+1]=str(d/'reserved-collision.yml'); reserved[reserved.index('--previous-state')+1]=str(d/'out/state.json'); reserved[reserved.index('--out')+1]=str(d/'reserved-collision'); reserved[reserved.index('--plan')+1]=str(d/'private/reserved-collision.json')
            # Isolate the namespace collision from the independent 70% guard:
            # both runs compare the same single allowed source group.
            original_config=(d/'config.json').read_text()
            scoped_config=json.loads(original_config); scoped_config['test_allowlist']=['legacy']
            (d/'config.json').write_text(json.dumps(scoped_config))
            self.assertEqual(a.main(reserved,transport=transport),0)
            (d/'config.json').write_text(original_config)
            self.assertNotIn(b'<Id>tilda-another-group</Id>',(d/'reserved-collision/feed.xml').read_bytes())
            self.assertIn('SKU_reserved_by_pending_source_group',(d/'reserved-collision/report.json').read_text())
            # Daily provider schedule: several fresh hourly versions must coexist.
            shutil.copytree(d/'out',d/'late-provider-state')
            late_state=json.loads((d/'late-provider-state/state.json').read_text()); late_state['generated_at']=old_stamp; late_state['data_generated_at']=old_stamp
            (d/'late-provider-state/state.json').write_text(json.dumps(late_state))
            late=list(stale); late[late.index('--previous-state')+1]=str(d/'late-provider-state/state.json'); late[late.index('--out')+1]=str(d/'hour5'); late[late.index('--plan')+1]=str(d/'private/hour5.json')
            with mock.patch.object(a,'datetime',wraps=datetime) as clock:
                clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=5)
                self.assertEqual(a.main(late,transport=transport),0)
            hour5=(d/'hour5/feed.xml').read_bytes(); self.assertIn(b'<Title>Updated camera</Title>',hour5)
            (d/'hour6.yml').write_bytes((d/'changed.yml').read_bytes().replace(b'Updated camera</name>',b'Hour six camera</name>'))
            next_hour=list(late); next_hour[next_hour.index('--source-file')+1]=str(d/'hour6.yml'); next_hour[next_hour.index('--previous-state')+1]=str(d/'hour5/state.json'); next_hour[next_hour.index('--out')+1]=str(d/'hour6'); next_hour[next_hour.index('--plan')+1]=str(d/'private/hour6.json')
            with mock.patch.object(a,'datetime',wraps=datetime) as clock:
                clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=6)
                self.assertEqual(a.main(next_hour,transport=transport),0)
            ledger=json.loads((d/'hour6/state.json').read_text()); self.assertGreaterEqual(len(ledger['provider_outbox']),3)
            self.assertIn(hashlib.sha256(feed).hexdigest(),{v['feed_sha256'] for v in ledger['provider_outbox']})
            provider_feed=feed
            self.assertEqual(a.main(['verify','--plan',str(d/'private/recovery.json')],transport=transport),2)
            verification_error=False
            for verification_corruption in ('duplicate','swapped'):
                self.assertEqual(a.main(['verify','--plan',str(d/'private/recovery.json')],transport=transport),2)
            verification_corruption=None
            self.assertEqual(a.main(['verify','--plan',str(d/'private/recovery.json')],transport=transport),0)
            accepted=list(next_hour); accepted[accepted.index('--previous-state')+1]=str(d/'hour6/state.json'); accepted[accepted.index('--out')+1]=str(d/'late-receipt'); accepted[accepted.index('--plan')+1]=str(d/'private/late-receipt.json')
            with mock.patch.object(a,'datetime',wraps=datetime) as clock:
                clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=7)
                self.assertEqual(a.main(accepted,transport=transport),0)
            receipt_state=json.loads((d/'late-receipt/state.json').read_text()); self.assertEqual(receipt_state['items']['new-group']['avito_id'],'125')
            self.assertEqual(receipt_state['provider_receipts'][-1]['feed_sha256'],hashlib.sha256(feed).hexdigest())
            self.assertNotEqual(receipt_state['pending_provider']['feed_sha256'],hashlib.sha256(feed).hexdigest())
            self.assertEqual(json.loads((d/'late-receipt/report.json').read_text())['provider_validation'],'pending')
            expired=list(next_hour); expired[expired.index('--previous-state')+1]=str(d/'hour6/state.json'); expired[expired.index('--out')+1]=str(d/'expired'); expired[expired.index('--plan')+1]=str(d/'private/expired.json')
            with mock.patch.object(a,'datetime',wraps=datetime) as clock, mock.patch('sys.stderr',new_callable=io.StringIO) as error:
                clock.now.return_value=datetime.now(timezone.utc)+timedelta(days=9)
                self.assertEqual(a.main(expired,transport=transport),2)
                self.assertIn('provider_outbox_unresolved_over_7_days',error.getvalue())
                self.assertFalse((d/'expired').exists())
            repeated=list(args); repeated[repeated.index('--out')+1]=str(d/'repeat'); repeated[repeated.index('--previous-state')+1]=str(d/'out/state.json')
            self.assertEqual(a.main(repeated,transport=transport),0)
            self.assertTrue((d/'repeat').exists())
            self.assertEqual(sum(m=='POST' and '/profile' in u for m,u in calls),1)

    def test_partial_credentials_are_configuration_failure(self):
        spec=importlib.util.spec_from_file_location('account_partial',ROOT/'account.py'); a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
        with mock.patch.dict(os.environ, {'AVITO_CLIENT_ID':'private'},clear=True), mock.patch('sys.stderr',new_callable=io.StringIO) as error:
            self.assertEqual(a.main(['commit','--plan','unused']),2)
            self.assertIn('incomplete_credentials',error.getvalue())

