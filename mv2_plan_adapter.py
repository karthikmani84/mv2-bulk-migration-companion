"""Trusted data-only plan adapter and exact live preflight. Python 3.6+."""
import bisect
import collections
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from decimal import Decimal, localcontext

import inventory
import migration_config


UNITS = inventory.UNITS
CELL_SIZE = inventory.SIZE
CAP = 25000000000
BASE = None
_JOB = None
SAFE_SELECTOR = re.compile(r'[A-Za-z0-9_/-]+\Z')
BATCH_ID = re.compile(r'[0-9]{4,}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')


def fail(message):
    raise ValueError(message)


def require(ok, message):
    if not ok:
        fail(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def digest_names(names):
    return digest((''.join(name + '\n' for name in names)).encode('utf-8'))


def configure(job):
    global _JOB, BASE, CAP
    _JOB = copy.deepcopy(job)
    BASE = Path(job['paths']['plan_dir'])
    CAP = job['limits']['batch_bytes']
    require(type(CAP) is int and CAP > 0 and BASE.is_absolute(), 'Invalid configured plan root/cap')


def config():
    require(_JOB is not None, 'Configure the trusted adapter first')
    return {'bin': _JOB['source']['mv2_binary'], 'db': _JOB['source']['db_unique_name'], 'bucket': _JOB['target']['bucket']}


def safe_name(name):
    inventory._name(name)
    require(not name.startswith('/') and '/' in name and '\\' not in name
            and all(part not in ('', '.', '..') for part in name.split('/')), 'Object cannot be staged as a relative file: ' + repr(name))
    return name


def safe_selector(selector):
    require(isinstance(selector, str) and selector and not selector.startswith('/') and '/' in selector
            and SAFE_SELECTOR.fullmatch(selector) is not None
            and all(part not in ('.', '..') for part in selector.split('/')), 'Unsafe literal filter selector: ' + repr(selector))
    return selector


def source_row(row):
    require(isinstance(row, dict), 'Invalid source object record')
    safe_name(row.get('name'))
    parsed = inventory._size_row(row['name'], row.get('display_size'))
    require(isinstance(row.get('nominal_bytes'), str) and Decimal(row['nominal_bytes']) == Decimal(parsed['nominal_bytes'])
            and type(row.get('upper_bytes')) is int and row['upper_bytes'] == parsed['upper_bytes'], 'Source row size fields differ')
    return {key: row[key] for key in ('name', 'display_size', 'nominal_bytes', 'upper_bytes')}


def sum_nominal(rows):
    values = [Decimal(row['nominal_bytes']) for row in rows]
    with localcontext() as context:
        context.prec = max([len(value.as_tuple().digits) for value in values] + [1]) + len(str(len(values))) + 5
        return str(sum(values, Decimal(0)))


def read_bytes(path):
    path = Path(path)
    require(path.is_absolute(), 'Plan files require absolute paths')
    for component in [path.parent] + list(path.parent.parents):
        require(not component.is_symlink(), 'Symlink in plan path: ' + str(component))
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode), 'Plan input is not a regular file')
        parts = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            parts.append(chunk)
        after = os.fstat(descriptor)
        named = os.lstat(str(path))
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        require(signature(before) == signature(after) == signature(named), 'Plan input changed while reading')
        raw = b''.join(parts)
        require(len(raw) == before.st_size, 'Incomplete plan read')
        return raw
    finally:
        os.close(descriptor)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + '\n').encode('utf-8')


def parse_json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=inventory._unique_json, parse_constant=inventory._bad_constant)


class SourceScopes:
    """Literal prefix groups checked against full-source substring context."""
    def __init__(self, names):
        self.names = sorted(names)
        self.interior_roots = {}

    def matching_prefix(self, selector):
        start = bisect.bisect_left(self.names, selector)
        result = []
        for position in range(start, len(self.names)):
            name = self.names[position]
            if not name.startswith(selector):
                break
            result.append(name)
        return result

    def interior(self, selector):
        root = selector.split('/', 1)[0] + '/'
        if root not in self.interior_roots:
            self.interior_roots[root] = [name for name in self.names if name.find(root, 1) >= 0]
        return [name for name in self.interior_roots[root] if name.find(selector, 1) >= 0]

    def validate(self, selector, expected):
        safe_selector(selector)
        actual = self.matching_prefix(selector)
        require(actual and set(actual) == set(expected) and not self.interior(selector),
                'Selector is empty, broader than its selected names, or matches inside another source key: ' + selector)
        return actual


def validate_plan_at(plan_dir, job=None):
    plan_dir = Path(plan_dir)
    require(plan_dir.is_absolute() and not plan_dir.is_symlink(), 'Invalid plan directory')
    require(not (plan_dir / 'build.failed.json').exists(), 'Plan build failed; preserve it and build a new plan directory')
    fingerprints = {}
    def read(location):
        raw = read_bytes(location)
        fingerprints[str(location)] = digest(raw)
        return raw
    plan = parse_json(read(plan_dir / 'plan.json'))
    require(isinstance(plan, dict) and type(plan.get('format_version')) is int and plan['format_version'] == 1,
            'Unsupported data-only plan version')
    cap = plan.get('cap_bytes_exclusive')
    require(type(cap) is int and cap > 0, 'Invalid exclusive byte cap')
    binding = plan.get('binding')
    require(isinstance(binding, dict) and SHA.fullmatch(str(binding.get('config_sha256', ''))) is not None, 'Missing plan configuration binding')
    if job is not None:
        require(cap == job['limits']['batch_bytes'] and binding['config_sha256'] == migration_config.digest(job)
                and plan.get('source') == job['source'] and plan.get('target_scope') == target_scope(job), 'Plan belongs to a different job configuration')
    require(plan.get('source_snapshot_file') == 'source_inventory.json', 'Unexpected source snapshot path')
    source_raw = read(plan_dir / 'source_inventory.json')
    require(digest(source_raw) == plan.get('source_snapshot_sha256') == binding.get('source_snapshot_sha256'), 'Full source snapshot checksum differs')
    source_list = parse_json(source_raw)
    require(isinstance(source_list, list) and source_list, 'Source snapshot is empty or malformed')
    source = {}
    for item in source_list:
        row = source_row(item)
        require(row['name'] not in source, 'Duplicate full source name')
        source[row['name']] = row
    require(type(plan.get('source_objects')) is int and plan['source_objects'] == len(source)
            and plan.get('source_nominal_bytes') == sum_nominal(source.values())
            and type(plan.get('source_upper_bytes')) is int and plan['source_upper_bytes'] == sum(row['upper_bytes'] for row in source.values()),
            'Full source totals differ')
    summaries = plan.get('batches')
    require(isinstance(summaries, list), 'Missing batch summaries')
    scopes, batches, selected, all_selectors = SourceScopes(source), {}, {}, []
    global_coverage = collections.Counter()
    for number, summary in enumerate(summaries, 1):
        require(isinstance(summary, dict) and summary.get('id') == '{:04d}'.format(number), 'Batch ids must be contiguous and unique')
        identifier = summary['id']
        raw = read(plan_dir / 'batches' / ('batch_' + identifier + '.json'))
        require(digest(raw) == summary.get('manifest_sha256'), 'Batch manifest checksum differs')
        batch = parse_json(raw)
        require(isinstance(batch, dict), 'Invalid batch manifest')
        for key in ('id', 'object_count', 'upper_bytes', 'selectors', 'names_sha256'):
            require(key in batch and key in summary and batch[key] == summary[key], 'Plan/manifest field differs: ' + key)
        require(type(batch.get('cap_bytes_exclusive')) is int and batch['cap_bytes_exclusive'] == cap
                and type(batch.get('upper_bytes')) is int and 0 <= batch['upper_bytes'] < cap, 'Batch violates the exclusive byte budget')
        rows = batch.get('objects')
        require(isinstance(rows, list) and rows and type(batch.get('object_count')) is int and batch['object_count'] == len(rows), 'Batch object count differs')
        checked = [source_row(row) for row in rows]
        names = [row['name'] for row in checked]
        require(names == sorted(set(names)) and batch['names_sha256'] == digest_names(names), 'Batch name order/uniqueness/digest differs')
        require(sum(row['upper_bytes'] for row in checked) == batch['upper_bytes'], 'Batch upper-byte sum differs')
        for row in checked:
            require(row['name'] in source and source[row['name']] == row and row['name'] not in selected, 'Batch duplicates or changes full-source row')
            selected[row['name']] = row
        for left, right in zip(names, names[1:]):
            require(not right.startswith(left + '/'), 'Batch contains an unstaggable file/directory name collision')
        selectors = batch.get('selectors')
        require(isinstance(selectors, list) and selectors and len(set(selectors)) == len(selectors), 'Missing or duplicate selectors')
        batch_names, coverage = set(names), collections.Counter()
        for selector in selectors:
            safe_selector(selector)
            covered = scopes.matching_prefix(selector)
            require(covered and all(name in batch_names for name in covered) and not scopes.interior(selector),
                    'Selector is empty, broader than its selected batch, or matches inside another source key')
            coverage.update(covered)
            global_coverage.update(covered)
        require(set(coverage) == batch_names and all(value == 1 for value in coverage.values()), 'Selectors overlap or omit planned names')
        all_selectors.extend(selectors)
        batches[identifier] = batch
    require(len(all_selectors) == len(set(all_selectors)) and type(plan.get('selector_count')) is int and plan['selector_count'] == len(all_selectors), 'Global selector count/uniqueness differs')
    require(set(global_coverage) == set(selected) and all(value == 1 for value in global_coverage.values()), 'Global selector overlap')
    require(type(plan.get('planned_objects')) is int and plan['planned_objects'] == len(selected)
            and plan.get('planned_nominal_bytes') == sum_nominal(selected.values())
            and type(plan.get('planned_upper_bytes')) is int and plan['planned_upper_bytes'] == sum(row['upper_bytes'] for row in selected.values()), 'Planned totals differ')
    excluded = sorted(set(source) - set(selected))
    require(type(plan.get('target_exclusion_enabled')) is bool and type(plan.get('target_present_excluded_objects')) is int
            and plan['target_present_excluded_objects'] == len(excluded) and plan.get('excluded_names_sha256') == digest_names(excluded), 'Excluded-name accounting differs')
    require(plan['target_exclusion_enabled'] or not excluded, 'Unexplained source names excluded from plan')
    for location, expected in fingerprints.items():
        require(digest(read_bytes(location)) == expected, 'Plan input changed during full validation')
    return {'plan': plan, 'batches': batches, 'source': source, 'input_fingerprints': fingerprints}


def target_scope(job):
    return {key: job['target'][key] for key in ('endpoint', 'region', 'namespace', 'bucket')}


def load_batch(number):
    require(_JOB is not None, 'Configure the trusted adapter first')
    require((type(number) is int and number > 0) or (isinstance(number, str)
            and re.fullmatch(r'[0-9]+', number) is not None and int(number) > 0), 'Invalid batch number')
    try:
        identifier = '{:04d}'.format(int(number))
    except (TypeError, ValueError):
        fail('Invalid batch number')
    state = validate_plan_at(BASE, _JOB)
    require(identifier in state['batches'], 'Unknown batch id: ' + identifier)
    return state['batches'][identifier]


def read_inventory(path):
    parsed = inventory.parse_source(read_bytes(Path(path)))
    return [source_row(parsed[name]) for name in sorted(parsed)]


def run_mv2(arguments, log):
    raise RuntimeError('The trusted transfer engine must inject run_mv2')


def check_live(batch, logdir):
    require(_JOB is not None, 'Configure the trusted adapter first')
    current = load_batch(batch['id'])
    require(current == batch, 'Plan changed before live preflight')
    expected = {row['name']: row for row in batch['objects']}
    actual = {}
    for index, selector in enumerate(batch['selectors'], 1):
        destination = Path(logdir) / ('preflight_{:03d}.txt'.format(index))
        output = run_mv2(migration_config.source_command(_JOB, 'list')[1:] + ['--filter', selector], destination)
        rows = read_inventory(Path(output))
        wanted = {name for name in expected if name.startswith(selector)}
        got = {row['name'] for row in rows}
        require(got == wanted, 'Live source filter drift. Missing: {}; extra: {}'.format(sorted(wanted - got)[:3], sorted(got - wanted)[:3]))
        for row in rows:
            require(row['name'] not in actual and row == expected[row['name']], 'Live source size/overlap changed; build a new plan')
            actual[row['name']] = row
    total = sum(row['upper_bytes'] for row in actual.values())
    require(set(actual) == set(expected) and total == batch['upper_bytes'] and total < CAP, 'Live preflight coverage/byte budget differs')
    receipt = dict(batch=batch['id'], object_count=len(actual), upper_bytes=total,
                   size_basis='Rounded display bounds only; not live-source checksums')
    path = Path(logdir) / 'preflight_receipt.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, indent=2)
        stream.write('\n')
    return total
