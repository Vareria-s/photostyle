import copy
import importlib.util
import pathlib
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('converter', ROOT / 'converter.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def config():
    return {'account_scope': 'fixture', 'mode': 'preview',
            'categories': {'cat': {'fields': {'Category': 'Fixture'}, 'required': ['Condition'],
                                   'param_fields': {'Condition': 'Состояние'}, 'validated': False}},
            'inventory': {'status_param': 'Статус', 'in_stock': ['В наличии']}}


def source(offers=None):
    offers = offers or [('a', 'p', '10', 'В наличии'), ('b', 'p', '12', 'В наличии')]
    body = ''.join(f'<offer id="{i}" group_id="{g}"><name>Camera</name><description>&lt;p&gt;Good camera&lt;/p&gt;</description><price>{price}</price><currencyId>RUR</currencyId><count>9999</count><categoryId>cat</categoryId><picture>https://example.com/a.jpg</picture><param name="Статус">{status}</param><param name="Состояние">Новое</param></offer>' for i,g,price,status in offers)
    return ('<yml_catalog><shop><categories><category id="cat">Cameras</category></categories><offers>'+body+'</offers></shop></yml_catalog>').encode()


class ConverterTests(unittest.TestCase):
    def test_published_unavailable_products_are_explicitly_retired(self):
        cfg = config()
        cfg.update(mode='production', seller={'Address':'Fixture','ContactPhone':'123','ManagerName':'Fixture'},
                   activation={k:True for k in ['seller_verified','categories_validated','migration_verified',
                                                'avito_validator_passed','account_autoload_verified']},
                   retirement={'confirmed':True,'reasons':['source_unavailable','inventory_unavailable']},
                   guards={'minimum_ratio':0})
        cfg['categories']['cat']['validated'] = True
        initial = source([('a','p','10','В наличии'),('b','q','11','В наличии')])
        previous = c.sync(cfg, initial)['state']
        for label, changed in [
                ('on_order', source([('a','p','10','Под заказ'),('b','q','11','В наличии')])),
                ('hidden', initial.replace(b'<offer id="a"', b'<offer available="false" id="a"'))]:
            with self.subTest(label=label):
                retired = c.sync(cfg, changed, previous)
                self.assertEqual(retired['report']['retirement_count'], 1)
                self.assertIn('<DateEnd>', retired['outputs']['feed.xml'])
                self.assertFalse(retired['state']['items']['p']['published'])
                self.assertTrue(previous['items']['p']['published'])
        only_p = c.sync(cfg, source([('a','p','10','В наличии')]))['state']
        all_retired = c.sync(cfg, source([('a','p','10','Под заказ')]), only_p)
        self.assertEqual(all_retired['report']['candidate_count'], 0)
        self.assertEqual(all_retired['report']['retirement_count'], 1)
        self.assertIn('<DateEnd>', all_retired['outputs']['feed.xml'])

    def test_candidate_guard_compares_the_same_allowlist_scope(self):
        cfg = config()
        cfg.update(account_kind='test', test_allowlist=['p','q','r'], guards={'minimum_ratio':0.7})
        initial = [('a','p','10','В наличии'),('b','q','11','В наличии'),('c','r','12','В наличии'),
                   ('d','x','13','В наличии'),('e','y','14','В наличии'),('f','z','15','В наличии')]
        previous = c.sync(cfg, source(initial))['state']
        self.assertEqual(previous['candidate_count'], 3)
        changed = initial.copy()
        changed[1] = ('b','q','0','В наличии')
        changed[2] = ('c','r','0','В наличии')
        with self.assertRaisesRegex(c.SyncError, 'candidate_count_drop_guard'):
            c.sync(cfg, source(changed), previous)
        narrowed = c.sync(dict(cfg, test_allowlist=['p']), source(initial), previous)
        self.assertEqual(narrowed['report']['candidate_count'], 1)

    def test_rejects_unsafe_input_and_configuration_without_mutation(self):
        cfg = config()
        previous = c.sync(cfg, source())['state']
        saved = copy.deepcopy(previous)
        unsafe = [b'<broken', b'<!DOCTYPE yml_catalog [<!ENTITY x "x">]><yml_catalog/>',
                  source().replace(b'id="b"', b'id="a"')]
        for data in unsafe:
            with self.subTest(data=data[:30]), self.assertRaises(c.SyncError):
                c.sync(cfg, data, previous)
        changed = dict(cfg, account_scope='other')
        with self.assertRaises(c.SyncError):
            c.sync(changed, source(), previous)
        with self.assertRaises(c.SyncError):
            c.sync(dict(cfg, mode='live'), source(), previous)
        with self.assertRaises(c.SyncError):
            c.sync(cfg, source().replace(b'RUR', b'RUR'), dict(previous, source_count=100))
        self.assertEqual(previous, saved)

    def test_eligible_drop_guard_and_reserved_identity_fields(self):
        cfg = config()
        baseline = c.sync(cfg, source([('a','p','10','В наличии'),('b','q','11','В наличии'),('c','r','12','В наличии')]))['state']
        with self.assertRaisesRegex(c.SyncError, 'candidate_count_drop_guard'):
            c.sync(cfg, source([('a','p','10','В наличии'),('b','q','0','В наличии'),('c','r','0','В наличии')]), baseline)
        cfg['categories']['cat']['fields']['Id'] = 'injected-id'
        with self.assertRaisesRegex(c.SyncError, 'reserved'):
            c.sync(cfg, source())

    def test_explicit_text_policy_reports_changes_and_preserves_originals(self):
        cfg = config()
        cfg['text_policies'] = {'title':'truncate_word', 'description':'truncate_word'}
        cfg['limits'] = {'title':12, 'description':15}
        data = source().replace(b'Camera</name>', b'Camera model extended</name>').replace(b'Good camera', b'Good camera with a long description')
        result = c.sync(cfg, data)
        self.assertEqual(result['report']['candidate_count'], 1)
        self.assertIn('<Title>Camera model</Title>', result['outputs']['preview.xml'])
        self.assertIn('transformed_title', result['outputs']['reasons.csv'])
        self.assertIn('Camera model extended', result['outputs']['text-changes.csv'])
        self.assertIn('text_transform_confirmation_required', result['report']['blockers'])

    def test_migration_cannot_duplicate_existing_avito_identity(self):
        cfg = config()
        cfg.update(migration_account_scope='fixture', migration={
            'p':{'Id':'legacy-1','AvitoId':'12345'},
            'q':{'Id':'legacy-2','AvitoId':'12345'}})
        with self.assertRaisesRegex(c.SyncError, 'duplicate_output_AvitoId'):
            c.sync(cfg, source([('a','p','10','В наличии'),('b','q','11','В наличии')]))

    def test_preview_selects_stable_parent_and_repeats_without_publishing(self):
        result = c.sync(config(), source())
        self.assertEqual(result['report']['source_count'], 2)
        self.assertEqual(result['report']['parent_count'], 1)
        self.assertEqual(result['report']['candidate_count'], 1)
        self.assertIn('tilda-p', result['outputs']['preview.xml'])
        self.assertNotIn('feed.xml', result['outputs'])
        self.assertFalse(result['state']['items']['p']['published'])
        second = c.sync(config(), source(), result['state'])
        self.assertEqual(result['outputs']['preview.xml'], second['outputs']['preview.xml'])

    def test_cli_preserves_last_good_files_on_invalid_source(self):
        with tempfile.TemporaryDirectory() as directory:
            d = pathlib.Path(directory)
            (d / 'config.json').write_text(json.dumps(config()), encoding='utf-8')
            (d / 'source.yml').write_bytes(source())
            cmd = [sys.executable, str(ROOT / 'converter.py'), '--config', str(d / 'config.json'),
                   '--source-file', str(d / 'source.yml'), '--state', str(d / 'out/state.json'), '--out', str(d / 'out')]
            good = subprocess.run(cmd, capture_output=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertTrue((d / 'out/preview.xml').is_file())
            before = {p.name: p.read_bytes() for p in (d / 'out').iterdir()}
            (d / 'source.yml').write_bytes(b'<broken')
            bad = subprocess.run(cmd, capture_output=True)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (d / 'out').iterdir()})

    def test_report_distinguishes_draft_readiness_and_invalid_inventory(self):
        result = c.sync(config(), source([('a','p','10','В наличии'), ('b','q','0','Под заказ')]))
        self.assertEqual(result['report']['candidate_count'], 1)
        self.assertEqual(result['report']['avito_ready_count'], 0)
        self.assertEqual(result['report']['reason_counts']['invalid_price'], 1)
        self.assertIn('inventory_status:Под заказ', result['outputs']['reasons.csv'])
        cfg = config()
        cfg['categories'] = {}
        unknown = c.sync(cfg, source())
        self.assertIn('category_unmapped', unknown['report']['blockers'])
        self.assertIn('<Title>Camera</Title>', unknown['outputs']['preview.xml'])
        for bad in [source().replace(b'RUR', b'USD'), source().replace(b'https://example.com/a.jpg', b'bad')]:
            self.assertEqual(c.sync(config(), bad)['report']['candidate_count'], 0)

    def test_migration_update_retirement_and_allowlist_keep_account_identity(self):
        cfg = config()
        cfg.update(mode='production', seller={'Address':'Fixture address','ContactPhone':'123','ManagerName':'Fixture'},
                   migration_account_scope='fixture', migration={'p': {'Id':'legacy-1', 'AvitoId':'12345'}},
                   activation={k: True for k in ['seller_verified','categories_validated','migration_verified',
                                                'avito_validator_passed','account_autoload_verified']},
                   retirement={'confirmed':True,'reasons':['missing','zero_stock']}, guards={'minimum_ratio':0})
        cfg['categories']['cat']['validated'] = True
        first = c.sync(cfg, source([('a','p','10','В наличии'),('b','q','11','В наличии'),('c','r','12','В наличии')]))
        self.assertIn('<Id>legacy-1</Id><AvitoId>12345</AvitoId>', first['outputs']['feed.xml'])
        self.assertTrue(first['state']['items']['p']['published'])
        changed_identity = copy.deepcopy(cfg)
        changed_identity['migration']['p']['AvitoId'] = '99999'
        with self.assertRaisesRegex(c.SyncError, 'published_AvitoId_changed'):
            c.sync(changed_identity, source(), first['state'])
        restricted = dict(cfg, test_allowlist=['p'], account_kind='test')
        limited = c.sync(restricted, source([('a','p','21','В наличии')]), first['state'])
        self.assertNotIn('<DateEnd>', limited['outputs']['feed.xml'])
        unknown_outside_allowlist = source([('a','p','21','В наличии'),('b','q','11','В наличии')]).replace(b'<price>11</price><currencyId>RUR</currencyId><count>9999</count><categoryId>cat</categoryId>', b'<price>11</price><currencyId>RUR</currencyId><count>9999</count><categoryId>unknown</categoryId>')
        isolated = c.sync(restricted, unknown_outside_allowlist, first['state'])
        self.assertEqual(isolated['report']['candidate_count'], 1)
        second = c.sync(cfg, source([('a','p','21','В наличии'),('b','q','11','В наличии')]), first['state'])
        self.assertIn('<Price>21</Price>', second['outputs']['feed.xml'])
        self.assertEqual(second['report']['retirement_count'], 1)
        zero_source = source([('a','p','21','В наличии'),('b','q','11','В наличии')]).replace(b'<price>11</price><currencyId>RUR</currencyId><count>9999</count>', b'<price>11</price><currencyId>RUR</currencyId><count>0</count>')
        retired = c.sync(cfg, zero_source, first['state'])
        self.assertEqual(retired['report']['retirement_count'], 2)
        self.assertFalse(retired['state']['items']['q']['published'])
        with self.assertRaises(c.SyncError):
            c.sync(dict(cfg, guards={'minimum_ratio':0.7}), source([('a','p','21','В наличии')]), first['state'])
        with self.assertRaises(c.SyncError):
            c.sync(dict(cfg, migration_account_scope='client'), source(), first['state'])


if __name__ == '__main__':
    unittest.main()

