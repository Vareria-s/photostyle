import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class AccountContinuity(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('account_continuity', ROOT/'account.py')
        self.account = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.account)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {'AVITO_CLIENT_ID':'fixture', 'AVITO_CLIENT_SECRET':'fixture', 'RUNNER_TEMP':self.temp.name})
        env.start(); self.addCleanup(env.stop)
        self.posts = []
        self.settings = {}
        self.field_limits = {}
        self.ads = ('<Ads>' + ''.join(f'<Ad><Id>g{i}</Id><AvitoId>{100+i}</AvitoId><SKU>sku{i}</SKU><Title>Old {i}</Title><Description>Old description</Description><Price>50</Price><Category>Camera</Category><Images><Image url="https://images.example/old.jpg"/></Images></Ad>' for i in range(1,7)) + '</Ads>').encode()

    def transport(self, method, url, headers, body):
        if url.endswith('/token'): return {'access_token':'fixture','token_type':'Bearer','expires_in':86400}
        if url.endswith('/accounts/self'): return {'id':42}
        if '/core/v1/items?' in url: return {'resources':[{'id':100+i} for i in range(1,7)],'meta':{'page':1,'per_page':99}}
        if url.endswith('/autoload/v2/profile'):
            if method == 'POST': self.posts.append(body)
            return {'autoload_enabled':True,'feeds_data':[{'feed_name':'shop','feed_url':'https://old.example/feed.xml'}],'report_email':'fixture@example.com','schedule':[{'rate':1,'weekdays':[1],'time_slots':[3]}]}
        if url.endswith(('/uploads/last_successful','/uploads/current')): return {'upload_id':7,'status':'success','feed_urls':[{'name':'shop','url':'https://frozen.example/feed.xml'}]}
        if '/last_successful/items?' in url: return {'items':[{'ad_id':f'g{i}','avito_id':100+i,'feed_name':'shop','section':{'slug':'camera'},'messages':[]} for i in range(1,7)],'meta':{'page':1,'pages':1,'perPage':100,'total':6}}
        if url.endswith('/user-docs/tree'): return {'categories':[{'name':'Camera','slug':'camera'}]}
        if url.endswith('/node/camera/fields'): return {'fields':[{'tag':tag,'content':[{'field_type':'input','data_type':'string','required':True, **({'values_range':{'max':self.field_limits[tag]}} if tag in self.field_limits else {})}]} for tag in ('Id','Title','Description','Price','Category','Images')]}
        if url == 'https://frozen.example/feed.xml': return self.ads
        raise AssertionError(url)

    def offer(self, uid, variant=None, price='99', available=True):
        return f'<offer id="{variant or uid}" group_id="{uid}" available="{str(available).lower()}"><name>{variant or uid}</name><description>Current description</description><price>{price}</price><currencyId>RUR</currencyId><categoryId>camera</categoryId><picture>https://images.example/new.jpg</picture><param name="Статус">В наличии</param><param name="Артикул система">sku{uid[1:]}</param></offer>'

    def source(self, first=None):
        return (first if first is not None else self.offer('g1','z')) + ''.join(self.offer(f'g{i}') for i in range(2,7))

    def run_prepare(self, name, offers, previous=None):
        d = self.directory
        (d/'config.json').write_text(json.dumps(self.settings), encoding='utf-8')
        source = d/(name+'.yml'); source.write_text('<yml_catalog><shop><categories><category id="camera">Camera</category></categories><offers>'+offers+'</offers></shop></yml_catalog>', encoding='utf-8')
        args = ['prepare','--config',str(d/'config.json'),'--source-file',str(source),'--previous-state',str(previous or d/'missing'),'--out',str(d/name),'--plan',str(d/'private'/(name+'.json')),'--public-feed-url','https://photostyle-avito.pages.dev/feed.xml']
        return self.account.main(args, transport=self.transport)

    def test_saved_identity_survives_skipped_selected_variant(self):
        self.assertEqual(self.run_prepare('initial', self.source()), 0)
        previous = self.directory/'initial/state.json'
        for mode in ('unavailable','missing','invalid','group_missing'):
            with self.subTest(mode=mode):
                first = self.offer('g1','a','10')
                if mode == 'unavailable': first += self.offer('g1','z',available=False)
                if mode == 'invalid': first += self.offer('g1','z','bad')
                if mode == 'group_missing': first = ''
                self.assertEqual(self.run_prepare(mode, self.source(first), previous), 0)
                state = json.loads((self.directory/mode/'state.json').read_text())
                self.assertIn('g1', state['items'])
                self.assertEqual(state['items']['g1']['variant_id'], 'z')
                self.assertEqual(state['items']['g1']['source_sku'], 'sku1')
                self.assertEqual(state['items']['g1']['avito_id'], '101')
                self.assertEqual(len({item['id'] for item in state['items'].values()}), len(state['items']))
                self.assertEqual(self.run_prepare(mode+'-return', self.source(self.offer('g1','a','10')+self.offer('g1','z')), self.directory/mode/'state.json'), 0)
                ads = ET.parse(self.directory/(mode+'-return')/'feed.xml').getroot()
                ad = next(ad for ad in ads if ad.findtext('Id') == 'g1')
                self.assertEqual(ad.findtext('Title'), 'z')
                self.assertEqual(ad.findtext('Price'), '99')
                self.assertEqual(ad.findtext('AvitoId'), '101')
                self.assertEqual(next(ad for ad in ads if ad.findtext('Id') == 'g2').findtext('Title'), 'g2')
        self.settings = {'product_overrides':{'g1':{'variant_id':'a'}}}
        self.assertEqual(self.run_prepare('override', self.source(self.offer('g1','a','10')+self.offer('g1','z')), previous), 0)
        state = json.loads((self.directory/'override/state.json').read_text())
        self.assertEqual(state['items']['g1']['variant_id'], 'a')
        self.assertEqual(state['items']['g1']['id'], 'g1')
        self.assertEqual(state['items']['g1']['avito_id'], '101')
        self.assertEqual(self.posts, [])

    def test_source_drop_blocks_before_snapshot_or_profile_write(self):
        self.assertEqual(self.run_prepare('full', self.source()), 0)
        previous = self.directory/'full/state.json'
        original = previous.read_bytes()
        reduced = self.offer('g1','z') + ''.join(self.offer(f'g{i}') for i in range(2,4))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(self.run_prepare('drop', reduced, previous), 2)
            self.assertIn('source_count_drop_guard', error.getvalue())
        self.assertFalse((self.directory/'drop').exists())
        self.assertEqual(previous.read_bytes(), original)
        self.assertFalse((self.directory/'private/drop.json').exists())
        self.assertEqual(self.posts, [])
        self.settings = {'guards':{'minimum_ratio':0.5}}
        self.assertEqual(self.run_prepare('half-allowed', reduced, previous), 0)

    def test_empty_source_description_keeps_proven_historical_text_and_updates_price(self):
        self.ads = self.ads.replace(b'Old description', b'Original')
        self.field_limits = {'Title':50,'Description':40}
        self.settings = {'text_policies':{'title':'truncate_word','description':'truncate_word'}}
        self.assertEqual(self.run_prepare('initial', self.source()), 0)
        previous = self.directory/'initial/state.json'
        for mode, replacement in (('absent',''),('empty','<description/>'),('html-empty','<description>&lt;p&gt; &lt;/p&gt;</description>')):
            with self.subTest(mode=mode):
                first = self.offer('g1','z','432.10').replace('<description>Current description</description>', replacement)
                self.assertEqual(self.run_prepare(mode, self.source(first), previous), 0)
                ads = ET.parse(self.directory/mode/'feed.xml').getroot()
                ad = next(ad for ad in ads if ad.findtext('Id') == 'g1')
                self.assertEqual(ad.findtext('Description'), 'Original')
                self.assertEqual(ad.findtext('Price'), '432.10')
                self.assertEqual(ad.findtext('Title'), 'z')
                self.assertEqual(ad.findtext('AvitoId'), '101')
                state = json.loads((self.directory/mode/'state.json').read_text())
                self.assertEqual(state['items']['g1']['variant_id'], 'z')
                report = json.loads((self.directory/mode/'report.json').read_text())
                self.assertNotIn('g1', {row['product_uid'] for row in report['source_exclusions']})
                self.assertTrue(any(change['product_uid']=='g1' and change['policy']=='historical_description' and change['transformed']=='Original' for change in report['text_transforms']))
        self.assertEqual(self.posts, [])

    def test_configured_word_policy_adapts_matched_and_new_text(self):
        self.field_limits = {'Title':50,'Description':40}
        self.settings = {'text_policies':{'title':'truncate_word','description':'truncate_word'}}
        self.assertEqual(self.run_prepare('initial', self.source()), 0)
        title = 'Camera with very good lens and lots of useful accessories for studio'
        html = '&lt;p&gt;Useful &lt;strong&gt;camera&lt;/strong&gt; description with many details for studio photographers&lt;/p&gt;'
        def long_offer(uid, variant):
            return self.offer(uid, variant, '432.10').replace('<name>'+variant+'</name>','<name>'+title+'</name>').replace('Current description',html)
        self.assertEqual(self.run_prepare('only-long-update', long_offer('g1','z')), 0)
        only = ET.parse(self.directory/'only-long-update/feed.xml').getroot()
        self.assertEqual(next(ad for ad in only if ad.findtext('Id') == 'g1').findtext('Price'), '432.10')
        offers = self.source(long_offer('g1','z')) + long_offer('g7','new')
        self.assertEqual(self.run_prepare('adapted', offers, self.directory/'initial/state.json'), 0)
        ads = ET.parse(self.directory/'adapted/feed.xml').getroot()
        for ident in ('g1','tilda-g7'):
            ad = next(ad for ad in ads if ad.findtext('Id') == ident)
            self.assertEqual(ad.findtext('Title'), 'Camera with very good lens and lots of useful')
            self.assertEqual(ad.findtext('Description'), 'Useful camera description with many')
            self.assertEqual(ad.findtext('Price'), '432.10')
        state = json.loads((self.directory/'adapted/state.json').read_text())
        self.assertEqual(state['items']['g1']['variant_id'], 'z')
        self.assertEqual(state['items']['g1']['avito_id'], '101')
        report = json.loads((self.directory/'adapted/report.json').read_text())
        self.assertTrue(any(change.get('original') == title and change.get('transformed') == 'Camera with very good lens and lots of useful' for change in report['text_transforms']))
        self.assertIn(title, (self.directory/'adapted/text-changes.csv').read_text())
        self.settings = {'text_policies':{'title':'reject','description':'reject'}}
        self.assertEqual(self.run_prepare('reject', offers, self.directory/'initial/state.json'), 0)
        rejected = json.loads((self.directory/'reject/state.json').read_text())
        self.assertEqual(rejected['items']['g1']['variant_id'], 'z')
        exclusions = {row['product_uid']:row['reason'] for row in json.loads((self.directory/'reject/report.json').read_text())['source_exclusions']}
        self.assertIn('text_policy_reject_title', exclusions['g1'])
        # Inactive shorter bounds must not override the active category rule.
        base_transport = self.transport
        def conditional_schema(method, url, headers, body):
            value = base_transport(method, url, headers, body)
            if url.endswith('/node/camera/fields'):
                title_field = next(field for field in value['fields'] if field['tag'] == 'Title')
                rule = title_field['content'][0]
                inactive = {**rule, 'values_range':{'max':10}, 'dependencies':[{'action':'visible','clause':'and','pairs':[{'source_field_tag':'Category','clause':'value','values':['Other']}]}]}
                title_field['content'] = [inactive, rule]
            return value
        self.settings = {'text_policies':{'title':'truncate_word','description':'truncate_word'}}
        self.transport = conditional_schema
        self.assertEqual(self.run_prepare('conditional', offers, self.directory/'initial/state.json'), 0)
        conditional = ET.parse(self.directory/'conditional/feed.xml').getroot()
        self.assertEqual(next(ad for ad in conditional if ad.findtext('Id') == 'g1').findtext('Title'), 'Camera with very good lens and lots of useful')
        def ambiguous_schema(method, url, headers, body):
            value = base_transport(method, url, headers, body)
            if url.endswith('/node/camera/fields'):
                field = next(field for field in value['fields'] if field['tag'] == 'Title')
                field['content'] *= 2
            return value
        self.transport = ambiguous_schema
        with mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(self.run_prepare('ambiguous', offers, self.directory/'initial/state.json'), 2)
            self.assertIn('ambiguous_field_rules', error.getvalue())
        self.assertFalse((self.directory/'ambiguous').exists())
        self.assertEqual(self.posts, [])

    def test_candidate_drop_uses_source_eligibility_not_preserved_ads(self):
        self.assertEqual(self.run_prepare('full', self.source()), 0)
        previous = self.directory/'full/state.json'
        original = previous.read_bytes()
        invalid = ''.join(self.offer(f'g{i}', price='bad' if i < 5 else '99') for i in range(1,7))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(self.run_prepare('invalid', invalid, previous), 2)
            self.assertIn('candidate_count_drop_guard', error.getvalue())
        self.assertFalse((self.directory/'invalid').exists())
        self.assertFalse((self.directory/'private/invalid.json').exists())
        self.assertEqual(previous.read_bytes(), original)
        self.assertEqual(self.posts, [])

    def test_guards_compare_allowlist_and_keep_preview_source_baseline(self):
        self.assertEqual(self.run_prepare('full', self.source()), 0)
        previous = self.directory/'full/state.json'
        self.settings = {'test_allowlist':['g1','g2']}
        self.assertEqual(self.run_prepare('scoped', self.source(), previous), 0)
        state = json.loads((self.directory/'scoped/state.json').read_text())
        self.assertEqual(state['candidate_count'], 2)
        self.assertEqual(state['feed_ad_count'], 6)
        self.assertEqual(state['eligible_uids'], [f'g{i}' for i in range(1,7)])
        invalid = ''.join(self.offer(f'g{i}', price='bad' if i < 3 else '99') for i in range(1,7))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(self.run_prepare('scoped-drop', invalid, self.directory/'scoped/state.json'), 2)
            self.assertIn('candidate_count_drop_guard', error.getvalue())
        # Review repro: this declared old raw-offer count must never be silently reset.
        preview = {'account_scope':'photostyle-client-pending','source_count':100000,'candidate_count':2,'items':{}}
        (self.directory/'preview.json').write_text(json.dumps(preview))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(self.run_prepare('source-review', self.source(), self.directory/'preview.json'), 2)
            self.assertIn('source_count_drop_guard', error.getvalue())
        review = json.loads(previous.read_text()); review['source_count'] = 100000; review['candidate_count'] = 2
        (self.directory/'review.json').write_text(json.dumps(review))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(self.run_prepare('account-source-review', self.source(), self.directory/'review.json'), 2)
            self.assertIn('source_count_drop_guard', error.getvalue())
        # A genuine initial preview is allowed to have a different candidate validator.
        spec = importlib.util.spec_from_file_location('preview_converter', ROOT/'converter.py')
        converter = importlib.util.module_from_spec(spec); spec.loader.exec_module(converter)
        config = json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
        source = ('<yml_catalog><shop><offers>'+self.source()+'</offers></shop></yml_catalog>').encode()
        preview = converter.sync(config, source)['state']
        self.assertEqual(preview['source_count'], 6)
        (self.directory/'preview.json').write_text(json.dumps(preview))
        self.assertEqual(self.run_prepare('bootstrap', self.source(), self.directory/'preview.json'), 0)
        self.assertFalse((self.directory/'source-review').exists())
        self.assertFalse((self.directory/'scoped-drop').exists())
        self.assertEqual(self.posts, [])
