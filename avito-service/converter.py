"""Offline-first Tilda YML -> Avito draft. No network publishing or account access."""
import argparse
import copy
import csv
import html
import io
import json
import re
import sys
import tempfile
import os
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener
import xml.etree.ElementTree as ET

MAX_BYTES = 100 * 1024 * 1024


class SyncError(ValueError):
    pass


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('p', 'br', 'li', 'div', 'tr'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1
        if tag in ('p', 'li', 'div', 'tr'):
            self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def clean(text):
    parser = PlainText()
    parser.feed(html.unescape(text or ''))
    return '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines() if line.strip())


def valid_url(value):
    p = urlsplit(value)
    return p.scheme in ('https', 'http') and bool(p.hostname) and not p.username and not p.password


def truncate_word(value, limit):
    if len(value) <= limit:
        return value
    # A single word longer than the limit is rejected, never split silently.
    prefix = value[:limit + 1]
    return prefix.rsplit(' ', 1)[0].rstrip() if ' ' in prefix else value


def parse_source(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise SyncError('source_empty_or_over_100MiB')
    # Reject declarations in all XML encodings (including UTF-16/32).
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', data.replace(b'\x00', b''), re.I):
        raise SyncError('DTD_and_entities_forbidden')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SyncError('invalid_XML') from e
    if root.tag != 'yml_catalog':
        raise SyncError('expected_yml_catalog')
    offers = root.findall('./shop/offers/offer')
    if not offers:
        raise SyncError('source_has_no_offers')
    ids = [o.get('id') for o in offers]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise SyncError('missing_or_duplicate_offer_ID')
    return root, offers


def sync(config, source_bytes, previous_state=None):
    """Return {outputs: filename->UTF-8 text, state: dict, report: dict}; never mutates inputs."""
    if config.get('mode', 'preview') not in ('preview', 'production'):
        raise SyncError('mode_must_be_preview_or_production')
    scope = config.get('account_scope')
    if not scope or not re.fullmatch(r'[A-Za-z0-9_-]+', scope):
        raise SyncError('explicit_account_scope_required')
    previous = copy.deepcopy(previous_state or {})
    if previous and previous.get('account_scope') != scope:
        raise SyncError('account_scope_changed_use_separate_state_and_migration')
    root, offers = parse_source(source_bytes)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    groups = {}
    for offer in offers:
        groups.setdefault(offer.get('group_id') or offer.get('id'), []).append(offer)
    blockers = set()
    reasons = []
    candidates = {}
    category_counts = {}
    ready_count = 0
    text_changes = []
    inventory = config.get('inventory', {})
    old_items = previous.get('items', {})
    migration = config.get('migration', {})
    if migration and config.get('migration_account_scope') != scope:
        raise SyncError('migration_account_scope_mismatch')
    used_ids = set()
    items = copy.deepcopy(old_items)
    selected = {}
    inactive = {}
    allowlist = config.get('test_allowlist')
    for uid, variants in sorted(groups.items()):
        in_scope = allowlist is None or uid in allowlist
        # Stable minimum variant UID, unless explicitly selected; never substitute on stock changes.
        preferred = config.get('product_overrides', {}).get(uid, {}).get('variant_id')
        chosen = next((o for o in variants if o.get('id') == preferred), None) if preferred else min(variants, key=lambda o: o.get('id'))
        if chosen is None:
            if in_scope:
                blockers.add('configured_variant_missing:' + uid)
            continue
        selected[uid] = chosen
        for v in variants:
            if v is not chosen:
                reasons.append([uid, v.get('id'), 'excluded', 'variant_not_selected'])
        params = {p.get('name'): p.text or '' for p in chosen.findall('param')}
        issues = []
        visibility = chosen.get('available')
        if visibility == 'false':
            issues.append('source_unavailable')
            inactive[uid] = 'source_unavailable'
        if chosen.findtext('count') is not None:
            try:
                quantity = Decimal(chosen.findtext('count'))
                if not quantity.is_finite() or quantity < 0:
                    issues.append('invalid_stock')
                elif quantity == 0:
                    issues.append('zero_stock')
                    inactive[uid] = 'zero_stock'
            except InvalidOperation:
                issues.append('invalid_stock')
        status = params.get(inventory.get('status_param', 'Статус'))
        if status not in inventory.get('in_stock', ['В наличии']):
            issues.append('inventory_status:' + str(status))
            if status in inventory.get('retire_statuses', ['Под заказ']):
                inactive.setdefault(uid, 'inventory_unavailable')
        category_id = chosen.findtext('categoryId', '')
        category_counts[category_id] = category_counts.get(category_id, 0) + 1
        rule = config.get('categories', {}).get(category_id, {})
        fields = dict(rule.get('fields', {}))
        for target, param in rule.get('param_fields', {}).items():
            fields[target] = params.get(param)
        override = config.get('product_overrides', {}).get(uid, {})
        fields.update(override.get('fields', {}))
        reserved = {'Id', 'AvitoId', 'Title', 'Description', 'Price', 'Images', 'DateEnd'}
        if reserved.intersection(fields) or reserved.intersection(config.get('seller', {})):
            raise SyncError('reserved_output_field_use_dedicated_config')
        title = override.get('title', chosen.findtext('name', '')).strip()
        description = clean(override.get('description', chosen.findtext('description', '')))
        warnings = []
        for key, original in [('title', title), ('description', description)]:
            limit = config.get('limits', {}).get(key, 50 if key == 'title' else 7500)
            if len(original) > limit and config.get('text_policies', {}).get(key) == 'truncate_word':
                changed = truncate_word(original, limit)
                if changed != original:
                    text_changes.append([uid, chosen.get('id'), key, original, changed])
                    warnings.append('transformed_' + key)
                    if key == 'title':
                        title = changed
                    else:
                        description = changed
        if in_scope and warnings and config.get('activation', {}).get('text_transform_confirmed') is not True:
            blockers.add('text_transform_confirmation_required')
        if not title or len(title) > config.get('limits', {}).get('title', 50):
            issues.append('title_missing_or_too_long')
        if not description or len(description) > config.get('limits', {}).get('description', 7500):
            issues.append('description_missing_or_too_long')
        try:
            price = Decimal(chosen.findtext('price', ''))
            if not price.is_finite() or price <= 0:
                raise InvalidOperation
        except InvalidOperation:
            price = None
            issues.append('invalid_price')
        if chosen.findtext('currencyId') not in config.get('rub_aliases', ['RUB', 'RUR']):
            issues.append('currency_not_RUB')
        pictures = list(dict.fromkeys((p.text or '').strip() for p in chosen.findall('picture')))
        if not pictures or any(not valid_url(p) for p in pictures):
            issues.append('missing_or_invalid_images')
        if len(pictures) > config.get('limits', {}).get('images', 10):
            issues.append('too_many_images')
        readiness = []
        if not fields.get('Category'):
            readiness.append('category_unmapped')
        if not rule.get('validated'):
            readiness.append('category_not_Avito_validated')
        readiness += ['missing_field:' + f for f in rule.get('required', []) if not fields.get(f)]
        if in_scope:
            blockers.update(readiness)
        identity = migration.get(uid, {})
        old = old_items.get(uid, {})
        ad_id = identity.get('Id') or old.get('id') or 'tilda-' + uid
        avito_id = identity.get('AvitoId') or old.get('avito_id')
        if old.get('published') and old.get('id') != ad_id:
            raise SyncError('published_ID_changed:' + uid)
        if old.get('published') and old.get('avito_id') and old['avito_id'] != avito_id:
            raise SyncError('published_AvitoId_changed:' + uid)
        if ad_id in used_ids:
            raise SyncError('duplicate_output_ID:' + ad_id)
        used_ids.add(ad_id)
        if issues:
            reasons.append([uid, chosen.get('id'), 'excluded', ';'.join(issues + readiness + warnings)])
            continue
        ad = ET.Element('Ad')
        for tag, value in {'Id': ad_id, 'AvitoId': avito_id, **config.get('seller', {}), **fields,
                           'Title': title, 'Description': description, 'Price': format(price, 'f')}.items():
            if value is not None and value != '':
                if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', tag):
                    raise SyncError('invalid_XML_field:' + tag)
                ET.SubElement(ad, tag).text = str(value)
        image_node = ET.SubElement(ad, 'Images')
        for picture in pictures:
            ET.SubElement(image_node, 'Image', {'url': picture})
        candidates[uid] = ad
        if not readiness:
            ready_count += 1
        items[uid] = {'id': ad_id, 'avito_id': avito_id, 'variant_id': chosen.get('id'),
                      'published': old.get('published', False), 'ad_xml': ET.tostring(ad, encoding='unicode')}
        reasons.append([uid, chosen.get('id'), 'candidate', ';'.join(readiness + warnings) or 'ready'])
    eligible_uids = sorted(candidates)
    if allowlist is not None:
        candidates = {uid: ad for uid, ad in candidates.items() if uid in allowlist}
    guards = config.get('guards', {})
    for key, current in [('source_count', len(offers)), ('candidate_count', len(candidates))]:
        baseline = previous.get(key, 0)
        if key == 'candidate_count' and 'eligible_uids' in previous:
            baseline = sum(allowlist is None or uid in allowlist for uid in previous['eligible_uids'])
        if baseline and current < baseline * guards.get('minimum_ratio', 0.7):
            raise SyncError(key + '_drop_guard')
    production = config.get('mode', 'preview') == 'production'
    activation = config.get('activation', {})
    for key in ('seller_verified', 'categories_validated', 'migration_verified', 'avito_validator_passed', 'account_autoload_verified'):
        if activation.get(key) is not True:
            blockers.add('activation:' + key)
    for field in config.get('seller_required', ['Address', 'ContactPhone', 'ManagerName']):
        if not config.get('seller', {}).get(field):
            blockers.add('seller_missing:' + field)
    if config.get('account_kind') == 'test' and (not allowlist or len(allowlist) > 3):
        blockers.add('test_allowlist_requires_1_to_3_UIDs')
    if production and blockers:
        raise SyncError('production_blocked:' + ','.join(sorted(blockers)))
    retirement = config.get('retirement', {})
    retired = []
    ads = ET.Element('Ads', {'formatVersion': '3', 'target': 'Avito.ru'})
    for uid, ad in candidates.items():
        ads.append(ad)
        if production:
            items[uid]['published'] = True
    if retirement.get('confirmed') is True:
        for uid, old in old_items.items():
            if allowlist is not None and uid not in allowlist:
                continue
            cause = 'missing' if uid not in groups else inactive.get(uid)
            if old.get('published') and cause in retirement.get('reasons', []):
                ad = ET.fromstring(old['ad_xml'])
                ET.SubElement(ad, 'DateEnd').text = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
                ads.append(ad)
                retired.append(uid)
                reasons.append([uid, old.get('variant_id'), 'retirement_preview' if not production else 'retired', cause])
                if production:
                    items[uid]['published'] = False
    if production and not len(ads):
        raise SyncError('production_blocked:empty_feed')
    for identity_field in ('Id', 'AvitoId'):
        values = [ad.findtext(identity_field) for ad in ads if ad.findtext(identity_field)]
        if len(values) != len(set(values)):
            raise SyncError('duplicate_output_' + identity_field)
    xml = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(ads, encoding='unicode') + '\n'
    report = {'generated_at': now, 'production_enabled': production, 'account_scope': scope,
              'source_count': len(offers), 'parent_count': len(groups), 'candidate_count': len(candidates),
              'variant_excluded_count': len(offers)-len(groups), 'excluded_parent_count': len(groups)-len(candidates),
              'retirement_count': len(retired), 'blockers': sorted(blockers), 'category_counts': category_counts,
              'avito_ready_count': len(candidates) if not blockers else 0,
              'text_transform_count': len(text_changes),
              'reason_counts': dict(Counter(reason for row in reasons for reason in row[3].split(';'))),
              'preview_notice': 'Draft only; not validated by Avito. count=9999 is not confirmed physical stock.'}
    state = {'version': 1, 'account_scope': scope, 'generated_at': now, 'source_count': len(offers),
             'candidate_count': report['candidate_count'], 'eligible_uids': eligible_uids, 'items': items}
    reason_csv = io.StringIO(newline='')
    writer = csv.writer(reason_csv)
    writer.writerow(['product_uid', 'offer_uid', 'decision', 'reasons'])
    writer.writerows(reasons)
    changes_csv = io.StringIO(newline='')
    changes_writer = csv.writer(changes_csv)
    changes_writer.writerow(['product_uid', 'offer_uid', 'field', 'original', 'transformed'])
    changes_writer.writerows(text_changes)
    outputs = {'preview.xml': xml, 'reasons.csv': reason_csv.getvalue(),
               'text-changes.csv': changes_csv.getvalue(),
               'report.json': json.dumps(report, ensure_ascii=False, indent=2)+'\n',
               'status.json': json.dumps(report, ensure_ascii=False, indent=2)+'\n',
               'state.json': json.dumps(state, ensure_ascii=False, indent=2)+'\n'}
    if production:
        outputs['feed.xml'] = xml
    return {'outputs': outputs, 'state': state, 'report': report}

class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise SyncError('HTTPS_redirect_required')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_source_url(url):
    if urlsplit(url).scheme != 'https' or not valid_url(url):
        raise SyncError('HTTPS_source_required')
    opener = build_opener(SecureRedirect())
    with opener.open(Request(url, headers={'User-Agent': 'PhotoStyle-Avito-Preview/1.0'}), timeout=45) as response:
        length = response.headers.get('Content-Length')
        if length and int(length) > MAX_BYTES:
            raise SyncError('source_over_100MiB')
        # Read is bounded even if Content-Length is absent or dishonest.
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SyncError('source_over_100MiB')
        return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument('--source-file')
    sources.add_argument('--source-url')
    parser.add_argument('--state', required=True, help='previous state input; new state is always OUT/state.json')
    parser.add_argument('--out', required=True, help='unpublished snapshot staging directory')
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        state_path = Path(args.state)
        previous = json.loads(state_path.read_text(encoding='utf-8-sig')) if state_path.exists() else None
        if args.source_file:
            with open(args.source_file, 'rb') as source_file:
                data = source_file.read(MAX_BYTES + 1)
        else:
            data = read_source_url(args.source_url)
        result = sync(config, data, previous)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        # Compute everything first. GitHub workflow publishes the complete directory in one git commit.
        # Local file replacements are atomic individually; this directory must not be served directly.
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=out.parent) as temporary:
            stage = Path(temporary)
            for filename, content in result['outputs'].items():
                (stage / filename).write_text(content, encoding='utf-8', newline='')
            for filename in result['outputs']:
                os.replace(stage / filename, out / filename)
            if 'feed.xml' not in result['outputs']:
                (out / 'feed.xml').unlink(missing_ok=True)
        print(json.dumps({k: v for k, v in result['report'].items() if k != 'category_counts'}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, ET.ParseError) as error:
        print('Conversion failed; previous snapshot unchanged: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
