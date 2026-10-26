#!/usr/bin/env python3
"""Explicit source capture, complete byte-bounded planning and reconciliation."""
import argparse
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import inventory
import migration_config
import mv2_plan_adapter as adapter


def require(ok, message):
    if not ok:
        raise ValueError(message)


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def utc_value(value):
    require(isinstance(value, str) and value.endswith('+00:00'), 'Capture time must be explicit UTC')
    text = value[:-6]
    shape = '%Y-%m-%dT%H:%M:%S.%f' if '.' in text else '%Y-%m-%dT%H:%M:%S'
    try:
        return datetime.datetime.strptime(text, shape).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise ValueError('Invalid capture timestamp')


def verify_proof_inputs(*proofs):
    for proof in proofs:
        if proof is not None:
            for location, fingerprint in proof.get('input_fingerprints', {}).items():
                require(adapter.digest(adapter.read_bytes(Path(location))) == fingerprint, 'Capture input changed before output: ' + location)


def capture_command(job, kind):
    require(kind in ('source', 'target'), 'Unexpected capture kind')
    if kind == 'source':
        return migration_config.source_command(job, 'list')
    return migration_config.target_command(job, ['os', 'object', 'list', '--namespace-name', job['target']['namespace'],
        '--bucket-name', job['target']['bucket'], '--fields', 'name,size,md5', '--all', '--output', 'json', '--query', '@'])


def scope(job, kind):
    if kind == 'source':
        return {'kind': 'source', 'scope': 'full', 'db_unique_name': job['source']['db_unique_name'],
                'dbhome': job['source']['dbhome'], 'opcfile': job['source']['opcfile'], 'opcwallet': job['source']['opcwallet']}
    return dict(adapter.target_scope(job), kind='target', scope='full')


def capture(job, kind, output):
    """Capture a native read-only listing, with immutable success provenance."""
    output = Path(os.path.abspath(str(output)))
    stderr = Path(str(output) + '.stderr')
    exit_marker = Path(str(output) + '.exit')
    sidecar = Path(str(output) + '.capture.json')
    for location in (output, stderr, exit_marker, sidecar):
        require(not location.exists() and not location.is_symlink(), 'Capture output already exists: ' + str(location))
    for parent in [output.parent] + list(output.parent.parents):
        require(not parent.is_symlink(), 'Capture output parent is a symlink')
    require(output.parent.is_dir(), 'Create the dedicated capture directory first')
    identity = migration_config.file_identity(job)
    command = capture_command(job, kind)
    record = dict(format_version=1, kind=kind, scope=scope(job, kind), config_sha256=migration_config.digest(job),
                  file_identity=identity, command=command, started_utc=utc(), output=str(output), stderr=str(stderr))
    os.umask(0o077)
    status = None
    try:
        with output.open('xb') as out, stderr.open('xb') as error:
            run = subprocess.run(command, stdout=out, stderr=error, env=migration_config.tool_environment(job))
        status = run.returncode
        raw, errors = adapter.read_bytes(output), adapter.read_bytes(stderr)
        record.update(finished_utc=utc(), exit_code=status, output_sha256=adapter.digest(raw), output_bytes=len(raw),
                      stderr_sha256=adapter.digest(errors), stderr_bytes=len(errors))
        require(status == 0 and not errors.strip(), 'Capture failed or stderr requires review; original output is retained')
        inventory.parse_source(raw) if kind == 'source' else inventory.parse_target(raw)
        require(migration_config.file_identity(job) == identity, 'Tool or credential/configuration file changed during capture')
        record['status'] = 'captured'
    except BaseException as exc:
        record.update(status='failed', finished_utc=utc(), error=str(exc), exit_code=status)
        raise
    finally:
        with exit_marker.open('x', encoding='ascii') as stream:
            stream.write(str(status if record.get('status') == 'captured' else 1) + '\n')
        with sidecar.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write('\n')
    return record


def load_capture(job, location, kind, allow_external=False):
    location = Path(os.path.abspath(str(location)))
    raw = adapter.read_bytes(location)
    fingerprints = {str(location): adapter.digest(raw)}
    def read_evidence(path):
        value = adapter.read_bytes(path)
        fingerprints[str(path)] = adapter.digest(value)
        return value
    sidecar = Path(str(location) + '.capture.json')
    if not sidecar.exists() and not sidecar.is_symlink():
        require(allow_external, 'Missing capture provenance; use this tool to capture a full inventory, or explicitly allow an external scoped snapshot')
        provenance = dict(kind=kind, status='external_unverified_scope', output=str(location), output_sha256=adapter.digest(raw),
                          full_scope_verified=False, scope_basis='Explicit external snapshot only; may omit source objects. No full-migration completeness claim.')
    else:
        record = adapter.parse_json(read_evidence(sidecar))
        require(isinstance(record, dict) and type(record.get('format_version')) is int and record['format_version'] == 1
                and record.get('status') == 'captured' and record.get('kind') == kind and record.get('scope') == scope(job, kind), 'Capture does not bind the requested full scope')
        require(record.get('config_sha256') == migration_config.digest(job) and record.get('command') == capture_command(job, kind)
                and record.get('file_identity') == migration_config.file_identity(job), 'Capture belongs to a different job/tool/source identity')
        require(record.get('output') == str(location) and record.get('output_sha256') == adapter.digest(raw)
                and type(record.get('output_bytes')) is int and record['output_bytes'] == len(raw)
                and type(record.get('exit_code')) is int and record['exit_code'] == 0, 'Captured output or success status changed')
        require(read_evidence(Path(str(location) + '.exit')).strip() == b'0', 'Capture exit marker is not successful')
        error_path = Path(str(location) + '.stderr')
        errors = read_evidence(error_path)
        require(record.get('stderr') == str(error_path) and record.get('stderr_sha256') == adapter.digest(errors)
                and type(record.get('stderr_bytes')) is int and record['stderr_bytes'] == len(errors) and not errors.strip(), 'Capture stderr changed or requires review')
        require(utc_value(record.get('started_utc')) <= utc_value(record.get('finished_utc')), 'Capture timestamps are out of order')
        provenance = dict(record, full_scope_verified=True)
    rows = inventory.parse_source(raw) if kind == 'source' else inventory.parse_target(raw)
    provenance['input_fingerprints'] = fingerprints
    verify_proof_inputs(provenance)
    return rows, provenance


def _selected_rows(source):
    require(isinstance(source, dict) and source, 'A nonempty complete source snapshot is required')
    rows = {}
    for name in sorted(source):
        row = adapter.source_row(source[name])
        require(name == row['name'], 'Source dictionary key differs from row name')
        rows[name] = row
    return rows


def partition_groups(source, desired, cap):
    """Partition a selected subset into safe prefix groups under an exclusive cap."""
    require(type(cap) is int and cap > 0 and set(desired) <= set(source), 'Invalid partition inputs')
    for name in desired:
        require(source[name]['upper_bytes'] < cap, 'An object meets/exceeds the byte cap; no bounded single-part plan is possible: ' + name)
    ordered = sorted(desired)
    for left, right in zip(ordered, ordered[1:]):
        require(not right.startswith(left + '/'), 'Selected names cannot coexist as local files: ' + left)
    scopes = adapter.SourceScopes(source)
    groups = []
    def visit(prefix, context, wanted):
        if not wanted:
            return
        total = sum(source[name]['upper_bytes'] for name in wanted)
        if len(context) == len(wanted) and total < cap and not scopes.interior(prefix):
            adapter.safe_selector(prefix)
            groups.append(dict(selector=prefix, names=wanted, upper_bytes=total))
            return
        require(prefix not in wanted, 'A full selected name is a prefix of excluded/oversize/interior scope and cannot be isolated: ' + prefix)
        common = os.path.commonprefix([context[0], context[-1]])
        extension = ''
        for character in common[len(prefix):]:
            if character not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_/-':
                break
            extension += character
        if extension:
            visit(prefix + extension, context, wanted)
            return
        children = collections.defaultdict(list)
        for name in context:
            if len(name) > len(prefix):
                children[name[len(prefix)]].append(name)
        for character in sorted(children):
            child_context = children[character]
            child_wanted = [name for name in child_context if name in desired]
            if not child_wanted:
                continue
            require(character in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_/-',
                    'Scope needs a filter metacharacter and cannot be separated safely: ' + prefix)
            visit(prefix + character, child_context, child_wanted)
    roots = collections.defaultdict(list)
    for name in sorted(source):
        root = name.split('/', 1)[0] + '/'
        roots[root].append(name)
    for root in sorted(roots):
        wanted = [name for name in roots[root] if name in desired]
        if wanted:
            adapter.safe_selector(root)
            visit(root, roots[root], wanted)
    require(set(name for group in groups for name in group['names']) == set(desired)
            and sum(len(group['names']) for group in groups) == len(desired), 'Partition coverage differs from selected names')
    return groups


def build_plan(job, source_rows, target_rows=None, exclude_target_present=False, provenance=None):
    source = _selected_rows(source_rows)
    target = inventory._target_map(target_rows if target_rows is not None else {})
    require(type(exclude_target_present) is bool and (not exclude_target_present or target_rows is not None), 'Target exclusion requires a complete target snapshot')
    excluded = set(source) & set(target) if exclude_target_present else set()
    for name in excluded:
        expected = inventory._size_row(name, source[name]['display_size'])
        require(expected['lower_bytes'] <= target[name]['size'] <= expected['upper_bytes'],
                'Existing target name has a different size; review it rather than silently excluding: ' + name)
    desired = set(source) - excluded
    cap = job['limits']['batch_bytes']
    groups = partition_groups(source, desired, cap)
    buckets, pending, pending_size = [], [], 0
    for group in groups:
        if pending and pending_size + group['upper_bytes'] >= cap:
            buckets.append(pending)
            pending, pending_size = [], 0
        pending.append(group)
        pending_size += group['upper_bytes']
    if pending:
        buckets.append(pending)
    manifests = []
    for number, packed in enumerate(buckets, 1):
        names = sorted(name for group in packed for name in group['names'])
        manifests.append(dict(id='{:04d}'.format(number), cap_bytes_exclusive=cap, object_count=len(names),
                              upper_bytes=sum(source[name]['upper_bytes'] for name in names),
                              selectors=[group['selector'] for group in packed], names_sha256=adapter.digest_names(names),
                              objects=[source[name] for name in names]))
    snapshot = adapter.json_bytes([source[name] for name in sorted(source)])
    plan = dict(format_version=1, tool='mv2_portable_migration', source=job['source'], target_scope=adapter.target_scope(job),
                binding=dict(config_sha256=migration_config.digest(job), source_snapshot_sha256=adapter.digest(snapshot)),
                cap_bytes_exclusive=cap, source_objects=len(source), source_nominal_bytes=adapter.sum_nominal(source.values()),
                source_upper_bytes=sum(row['upper_bytes'] for row in source.values()), planned_objects=len(desired),
                planned_nominal_bytes=adapter.sum_nominal(source[name] for name in desired),
                planned_upper_bytes=sum(source[name]['upper_bytes'] for name in desired),
                target_exclusion_enabled=exclude_target_present, target_present_excluded_objects=len(excluded),
                excluded_names_sha256=adapter.digest_names(sorted(excluded)),
                target_exclusion_basis='Target name and source displayed-size bounds only; not a source-content or checksum verification.' if excluded else None,
                source_snapshot_file='source_inventory.json', source_snapshot_sha256=adapter.digest(snapshot),
                selector_count=sum(len(batch['selectors']) for batch in manifests), batches=[],
                inventory_provenance=provenance or {'status': 'caller_supplied_parsed_snapshots', 'full_scope_verified': False},
                source_content_verified=False, restore_verified=False,
                status='planned' if manifests else 'no_selected_objects')
    for batch in manifests:
        summary = {key: batch[key] for key in ('id', 'object_count', 'upper_bytes', 'selectors', 'names_sha256')}
        summary['manifest_sha256'] = adapter.digest(adapter.json_bytes(batch))
        plan['batches'].append(summary)
    destination = Path(job['paths']['plan_dir'])
    require(destination.is_absolute(), 'Plan directory must be absolute')
    for parent in [destination] + list(destination.parents):
        require(not parent.is_symlink(), 'Plan path must not contain symlinks')
    require(not destination.exists(), 'Plan directory already exists; use a new plan path')
    os.umask(0o077)
    destination.mkdir(mode=0o700)
    (destination / 'batches').mkdir(mode=0o700)
    try:
        with (destination / 'source_inventory.json').open('xb') as stream:
            stream.write(snapshot)
        for batch in manifests:
            with (destination / 'batches' / ('batch_' + batch['id'] + '.json')).open('xb') as stream:
                stream.write(adapter.json_bytes(batch))
        with (destination / 'plan.json').open('xb') as stream:
            stream.write(adapter.json_bytes(plan))
        adapter.validate_plan_at(destination, job)
    except BaseException as exc:
        with (destination / 'build.failed.json').open('x', encoding='utf-8') as stream:
            json.dump(dict(status='failed', error=str(exc)), stream, indent=2)
        raise
    return plan


def reconcile(job, source_path, target_path, output, original_plan=None, allow_external=False, source_before=None):
    source, source_proof = load_capture(job, source_path, 'source', allow_external)
    target, target_proof = load_capture(job, target_path, 'target', allow_external)
    before, before_proof = load_capture(job, source_before, 'source', allow_external) if source_before else (source, None)
    ordered = False
    if before_proof is not None and all(proof['full_scope_verified'] for proof in (before_proof, target_proof, source_proof)):
        require(utc_value(before_proof['finished_utc']) <= utc_value(target_proof['started_utc'])
                <= utc_value(target_proof['finished_utc']) <= utc_value(source_proof['started_utc']),
                'Verified reconciliation captures must be ordered source-before, target, source-after')
        ordered = True
    compared = inventory.compare_inventory(before, source, target)
    compared['two_source_snapshots_compared'] = source_before is not None
    compared['capture_order_verified'] = ordered
    compared['cutoff_basis'] = ('Two source inventories bracketed by caller; inspect captured UTC timestamps. Non-atomic names/display-size stability does not establish same-size content equality.'
                              if source_before else 'One source snapshot only; availability as listed during its capture. No repeated-source stability or live-source content equality claim.')
    history = None
    if original_plan:
        old_path = Path(os.path.abspath(str(original_plan)))
        old = adapter.validate_plan_at(old_path if old_path.is_dir() else old_path.parent)
        require(old['plan'].get('source') == job['source'], 'Original plan belongs to a different configured source')
        missing = sorted(set(old['source']) - set(source))
        history = dict(original_source_objects=len(old['source']), absent_from_fresh_source_objects=len(missing),
                       missing_source_objects=[dict(old['source'][name], classification=('target_present_unverified' if name in target else
                           'absent_both' if source_proof['full_scope_verified'] and target_proof['full_scope_verified'] else 'target_unknown')) for name in missing],
                       new_source_names=sorted(set(source) - set(old['source'])),
                       deletion_status_verified=False, basis='Snapshot absence only; not an RMAN deletion or physical-deletion finding.')
    full = source_proof['full_scope_verified'] and target_proof['full_scope_verified'] and (before_proof is None or before_proof['full_scope_verified'])
    report = dict(status='inventory_coverage_complete' if compared['inventory_name_and_size_complete'] and full else 'review_required',
                  comparison=compared, source_capture=source_proof, target_capture=target_proof, source_before_capture=before_proof,
                  full_scope_verified=full, historical_accounting=history,
                  source_content_verified=False, restore_verified=False)
    destination = Path(os.path.abspath(str(output)))
    require(not destination.exists() and not destination.is_symlink(), 'Reconciliation output already exists')
    for parent in [destination.parent] + list(destination.parent.parents):
        require(not parent.is_symlink(), 'Reconciliation output parent must not contain symlinks')
    verify_proof_inputs(source_proof, target_proof, before_proof)
    os.umask(0o077)
    destination.mkdir(mode=0o700)
    with (destination / 'reconciliation.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Explicit realm/source/paths job JSON')
    commands = parser.add_subparsers(dest='command')
    for operation in ('capture-source', 'capture-target'):
        child = commands.add_parser(operation)
        child.add_argument('--out', required=True)
    build = commands.add_parser('build')
    build.add_argument('--source-inventory', required=True)
    build.add_argument('--target-inventory')
    build.add_argument('--exclude-target-present', action='store_true')
    build.add_argument('--allow-external-inventory', action='store_true', help='Explicitly scoped external import; no full-scope proof')
    compare = commands.add_parser('reconcile')
    compare.add_argument('--source-inventory', required=True)
    compare.add_argument('--source-before')
    compare.add_argument('--target-inventory', required=True)
    compare.add_argument('--original-plan')
    compare.add_argument('--out', required=True)
    compare.add_argument('--allow-external-inventory', action='store_true')
    args = parser.parse_args(argv)
    require(args.command is not None, 'Select a capture/build/reconcile command')
    job = migration_config.load_config(args.config)
    if args.command.startswith('capture-'):
        result = capture(job, args.command.split('-')[1], args.out)
    elif args.command == 'build':
        source, source_proof = load_capture(job, args.source_inventory, 'source', args.allow_external_inventory)
        target, target_proof = load_capture(job, args.target_inventory, 'target', args.allow_external_inventory) if args.target_inventory else (None, None)
        verify_proof_inputs(source_proof, target_proof)
        result = build_plan(job, source, target, args.exclude_target_present,
                            provenance=dict(source=source_proof, target=target_proof,
                                            full_scope_verified=source_proof['full_scope_verified'] and (target_proof is None or target_proof['full_scope_verified'])))
    else:
        result = reconcile(job, args.source_inventory, args.target_inventory, args.out, args.original_plan,
                           args.allow_external_inventory, args.source_before)
    if args.command == 'build':
        print(json.dumps({key: result[key] for key in ('status', 'source_objects', 'planned_objects', 'target_present_excluded_objects', 'selector_count')}, indent=2))
        print('New plan: ' + job['paths']['plan_dir'])
    elif args.command == 'reconcile':
        print(json.dumps(dict(status=result['status'], source_objects=result['comparison']['source_after_objects'],
                              missing_objects=len(result['comparison']['missing_target_names']),
                              size_mismatches=len(result['comparison']['size_mismatches']), full_scope_verified=result['full_scope_verified']), indent=2))
    else:
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        sys.exit(1)
