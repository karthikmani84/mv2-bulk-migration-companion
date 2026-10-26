"""Portable planner/adapter checks with offline synthetic inventories only.

Python 3.6-compatible standard library. No actual MV2, OCI or credential use.
"""
import copy
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INV = load('portable_planner_inventory_test', 'inventory.py')
PLANNER = load('portable_planner_under_test', 'mv2_plan.py')
ADAPTER = load('portable_adapter_under_test', 'mv2_plan_adapter.py')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def names_sha(names):
    return sha((''.join(name + '\n' for name in sorted(names))).encode('utf-8'))


def source_text(rows):
    progress = (' (Approx progress bar) [ 25%][====    ]\r'
                ' (Approx progress bar) [100%][========]\r\n')
    lines = ['\u2502 File Name \u2502 File Size \u2502']
    lines.extend('\u2502 {} \u2502 {} \u2502'.format(name, size) for name, size in rows)
    lines.append('Total Files : {}'.format(len(rows)))
    return progress + '\n'.join(lines) + '\n'


class Planner(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o077)
        self.temporary = tempfile.TemporaryDirectory(prefix='portable_mv2_plan_')
        self.root = Path(self.temporary.name).resolve()
        self.plan_dir = self.root / 'plan'
        self.config = dict(format_version=1, locale='C.UTF-8',
                           source=dict(mv2_binary=str(self.root / 'tools/mv2.bin'),
                                       db_unique_name='LAB_DB', dbhome=str(self.root / 'dbhome'),
                                       opcfile=str(self.root / 'credentials/source.ora'),
                                       opcwallet=str(self.root / 'credentials/cwallet.sso')),
                           target=dict(oci_binary=str(self.root / 'tools/oci'),
                                       endpoint='https://objectstorage.example.test', region='test-region-1',
                                       namespace='test_namespace', bucket='test_backup',
                                       config_file=str(self.root / 'credentials/oci_config'),
                                       profile='MIGRATION_TEST', auth='api_key'),
                           paths=dict(plan_dir=str(self.plan_dir), stage_root=str(self.root / 'stage'),
                                      state_dir=str(self.root / 'state')),
                           limits=dict(batch_bytes=100, reserve_bytes=10,
                                       download_parallel=2, upload_parallel=3))
        for path in (Path(self.config['source']['mv2_binary']), Path(self.config['target']['oci_binary']),
                     Path(self.config['source']['opcfile']), Path(self.config['source']['opcwallet']),
                     Path(self.config['target']['config_file'])):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'SYNTHETIC UNUSED FIXTURE\n')
        Path(self.config['source']['dbhome']).mkdir()
        self.rows = self.source([('file_chunk/lab/a/0001', '30 B'),
                                 ('file_chunk/lab/a/0002', '40 B'),
                                 ('file_chunk/lab/b/0001', '20 B'),
                                 ('sbt_catalog/lab_c/metadata.xml', '10 B')])

    def tearDown(self):
        self.temporary.cleanup()
        os.umask(self.old_umask)

    def source(self, pairs):
        return INV.parse_source(source_text(pairs))

    def target(self, pairs):
        return INV.parse_target(json.dumps(dict(data=[dict(name=name, size=size, md5=None)
                                                     for name, size in pairs])))

    def build(self, rows=None, **kwargs):
        source = self.rows if rows is None else rows
        before = copy.deepcopy(source)
        config_before = copy.deepcopy(self.config)
        plan = PLANNER.build_plan(self.config, source, **kwargs)
        self.assertEqual(source, before)
        self.assertEqual(self.config, config_before)
        return plan

    def validated(self):
        return ADAPTER.validate_plan_at(self.plan_dir, self.config)

    def test_repeated_same_root_inside_another_key_is_refined(self):
        rows = self.source([('abc/def/A', '60 B'), ('abc/XYZ/abc/def/B', '60 B')])
        self.build(rows)
        checked = self.validated()
        for batch in checked['batches'].values():
            for selector in batch['selectors']:
                self.assertTrue(all(name.find(selector, 1) < 0 for name in rows))

    def test_repeated_root_full_selected_key_collision_is_unsplittable(self):
        rows = self.source([('abc/def/A', '60 B'), ('abc/XYZ/abc/def/A/x', '60 B')])
        with self.assertRaises(ValueError):
            self.build(rows)
        self.assertFalse(self.plan_dir.exists())

    def test_prefix_at_zero_and_repeated_later_must_refine(self):
        rows = self.source([('abc/def/abc/def/A', '60 B')])
        self.build(rows)
        checked = self.validated()
        for batch in checked['batches'].values():
            for selector in batch['selectors']:
                self.assertTrue(all(name.find(selector, 1) < 0 for name in rows))

    def assert_partition(self, validated, expected):
        full_source = validated['source']
        partition = []
        batches = validated['batches']
        for identifier, batch in sorted(batches.items()):
            self.assertEqual(batch['id'], identifier)
            names = {row['name'] for row in batch['objects']}
            self.assertEqual(len(names), batch['object_count'])
            self.assertEqual(batch['upper_bytes'], sum(row['upper_bytes'] for row in batch['objects']))
            self.assertLess(batch['upper_bytes'], self.config['limits']['batch_bytes'])
            matched_once = []
            for selector in batch['selectors']:
                self.assertRegex(selector, r'^[A-Za-z0-9_/-]+$')
                self.assertIn('/', selector)
                contains = {name for name in full_source if selector in name}
                starts = {name for name in full_source if name.startswith(selector)}
                self.assertTrue(contains)
                self.assertEqual(contains, starts, 'Interior positive-substring match is unsafe')
                self.assertTrue(contains <= names)
                matched_once.extend(contains)
            self.assertEqual(set(matched_once), names)
            self.assertEqual(len(matched_once), len(names))
            partition.extend(names)
        self.assertEqual(set(partition), set(expected))
        self.assertEqual(len(partition), len(set(partition)))

    def test_build_binds_full_source_and_partitions_strictly_below_cap(self):
        plan = self.build(provenance=dict(source_capture_sha256='a' * 64))
        validated = self.validated()
        self.assertEqual(validated['plan'], plan)
        self.assertEqual(plan['format_version'], 1)
        self.assertEqual(plan['source_objects'], 4)
        self.assertEqual(plan['planned_objects'], 4)
        self.assertEqual(Decimal(plan['source_nominal_bytes']), Decimal(100))
        self.assertEqual(plan['source_upper_bytes'], 100)
        self.assertEqual(plan['planned_upper_bytes'], 100)
        self.assertGreaterEqual(len(validated['batches']), 2)
        self.assert_partition(validated, self.rows)
        raw = (self.plan_dir / 'source_inventory.json').read_bytes()
        self.assertEqual(plan['source_snapshot_file'], 'source_inventory.json')
        self.assertEqual(plan['source_snapshot_sha256'], sha(raw))
        self.assertEqual(plan['binding']['source_snapshot_sha256'], sha(raw))
        self.assertEqual(len(json.loads(raw.decode('utf-8'))), len(self.rows))
        for row in json.loads(raw.decode('utf-8')):
            self.assertEqual(set(row), {'name', 'display_size', 'nominal_bytes', 'upper_bytes'})
        self.assertFalse(Path(self.config['paths']['stage_root']).exists())
        self.assertFalse(Path(self.config['paths']['state_dir']).exists())

    def test_existing_plan_directory_is_never_overwritten(self):
        self.plan_dir.mkdir()
        marker = self.plan_dir / 'keep.txt'
        marker.write_bytes(b'previous material\n')
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(marker.read_bytes(), b'previous material\n')
        self.assertEqual({path.name for path in self.plan_dir.iterdir()}, {'keep.txt'})

    def test_equal_cap_object_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build(self.source([('safe/group/object', '100 B')]))
        self.assertFalse((self.plan_dir / 'plan.json').exists())

    def test_root_object_without_slash_cannot_form_safe_selector(self):
        with self.assertRaises(ValueError):
            self.build(self.source([('ROOT_OBJECT', '1 B')]))

    def test_regex_metacharacter_names_can_share_a_safe_parent(self):
        rows = self.source([('safe/group/piece[1].xml', '20 B'),
                            ('safe/group/piece(2).xml', '20 B')])
        self.build(rows)
        self.assert_partition(self.validated(), rows)

    def test_metacharacters_needed_to_split_selector_fail_closed(self):
        rows = self.source([('unsafe/group[1]/piece.xml', '60 B'),
                            ('unsafe/group[2]/piece.xml', '60 B')])
        with self.assertRaises(ValueError):
            self.build(rows)

    def test_staging_file_ancestor_collision_is_rejected(self):
        rows = self.source([('safe/group/file', '1 B'), ('safe/group/file/child', '1 B')])
        with self.assertRaises(ValueError):
            self.build(rows)

    def test_interior_collisions_refine_to_exact_safe_scopes(self):
        rows = self.source([('safe/a/object', '20 B'), ('archive/safe/a/other', '20 B')])
        self.build(rows)
        self.assert_partition(self.validated(), rows)

    def test_complete_name_interior_collision_is_unsplittable(self):
        rows = self.source([('safe/a/object', '20 B'), ('archive/safe/a/object', '20 B')])
        with self.assertRaises(ValueError):
            self.build(rows)

    def test_exclusion_preserves_full_source_context_and_size_validates(self):
        target = self.target([('file_chunk/lab/a/0001', 30)])
        plan = self.build(target_rows=target, exclude_target_present=True)
        self.assertEqual(plan['source_objects'], 4)
        self.assertEqual(plan['planned_objects'], 3)
        self.assertTrue(plan['target_exclusion_enabled'])
        self.assertEqual(plan['target_present_excluded_objects'], 1)
        self.assertEqual(plan['excluded_names_sha256'], names_sha(target))
        validated = self.validated()
        self.assertEqual(set(validated['source']), set(self.rows))
        self.assert_partition(validated, set(self.rows) - set(target))

    def test_out_of_bounds_target_object_is_never_excluded(self):
        target = self.target([('file_chunk/lab/a/0001', 31)])
        with self.assertRaises(ValueError):
            self.build(target_rows=target, exclude_target_present=True)

    def test_rounded_target_exclusion_accepts_inclusive_bounds(self):
        self.config['limits']['batch_bytes'] = 2000
        rows = self.source([('safe/group/object', '1.00 KB')])
        for index, size in enumerate((rows['safe/group/object']['lower_bytes'],
                                      rows['safe/group/object']['upper_bytes'])):
            self.config['paths']['plan_dir'] = str(self.root / ('rounded_' + str(index)))
            plan = self.build(rows, target_rows=self.target([('safe/group/object', size)]),
                              exclude_target_present=True)
            self.assertEqual(plan['planned_objects'], 0)
            self.assertFalse(plan['source_content_verified'])
            self.assertFalse(plan['restore_verified'])

    def test_invalid_direct_target_size_is_not_silently_excluded(self):
        rows = self.source([('safe/group/object', '1 B')])
        for index, size in enumerate((True, False, -1, '1', None)):
            self.config['paths']['plan_dir'] = str(self.root / ('bad_target_' + str(index)))
            target = {'safe/group/object': dict(name='safe/group/object', size=size, md5=None)}
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    self.build(rows, target_rows=target, exclude_target_present=True)

    def test_exclusion_requires_an_explicit_target_inventory(self):
        with self.assertRaises(ValueError):
            self.build(exclude_target_present=True)

    def test_excluded_interior_collision_still_blocks_unsafe_selector(self):
        rows = self.source([('safe/a/object', '20 B'), ('archive/safe/a/object', '20 B')])
        target = self.target([('archive/safe/a/object', 20)])
        with self.assertRaises(ValueError):
            self.build(rows, target_rows=target, exclude_target_present=True)

    def test_all_target_present_yields_zero_planned_objects(self):
        target = self.target([(name, row['lower_bytes']) for name, row in self.rows.items()])
        plan = self.build(target_rows=target, exclude_target_present=True)
        self.assertEqual(plan['planned_objects'], 0)
        self.assertEqual(plan['target_present_excluded_objects'], len(self.rows))
        self.assert_partition(self.validated(), set())

    def test_config_mismatch_is_rejected_at_load(self):
        self.build()
        changed = copy.deepcopy(self.config)
        changed['target']['bucket'] = 'another_bucket'
        with self.assertRaises(ValueError):
            ADAPTER.validate_plan_at(self.plan_dir, changed)

    def test_source_snapshot_bytes_are_hash_bound(self):
        self.build()
        path = self.plan_dir / 'source_inventory.json'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            self.validated()

    def test_manifest_bytes_are_hash_bound(self):
        self.build()
        first = sorted((self.plan_dir / 'batches').glob('batch_*.json'))[0]
        first.write_bytes(first.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            self.validated()

    def test_seeded_inventory_partitions_hold_across_sizes(self):
        randomizer = random.Random(7052026)
        for index in range(6):
            self.config['paths']['plan_dir'] = str(self.root / ('random_plan_' + str(index)))
            self.plan_dir = Path(self.config['paths']['plan_dir'])
            pairs = [('file_chunk/lab/group_{}/object_{:04d}'.format(randomizer.randrange(4), number),
                      str(randomizer.randrange(1, 45)) + ' B') for number in range(25)]
            rows = self.source(pairs)
            self.build(rows)
            self.assert_partition(self.validated(), rows)

    def prepare_live(self):
        self.build()
        ADAPTER.configure(self.config)
        batch = ADAPTER.load_batch(1)
        logdir = self.root / 'live_preflight'
        logdir.mkdir()
        return batch, logdir

    def install_live_hook(self, mutate=None):
        calls = []
        original = ADAPTER.run_mv2

        def hook(arguments, destination):
            calls.append((arguments, Path(destination)))
            self.assertEqual(arguments[0], 'list')
            self.assertNotIn(self.config['source']['mv2_binary'], arguments)
            self.assertIn('--filter', arguments)
            selector = arguments[arguments.index('--filter') + 1]
            rows = [(name, row['display_size']) for name, row in self.rows.items()
                    if selector in name]
            if mutate is not None:
                rows = mutate(rows, selector)
            Path(destination).write_text(source_text(rows), encoding='utf-8')
            return Path(destination)

        ADAPTER.run_mv2 = hook
        self.addCleanup(setattr, ADAPTER, 'run_mv2', original)
        return calls

    def test_adapter_config_and_live_preflight_use_explicit_context(self):
        batch, logdir = self.prepare_live()
        self.assertEqual(ADAPTER.CAP, self.config['limits']['batch_bytes'])
        self.assertEqual(ADAPTER.BASE, self.plan_dir)
        self.assertEqual(ADAPTER.config(), dict(bin=self.config['source']['mv2_binary'],
                                               db='LAB_DB', bucket='test_backup'))
        calls = self.install_live_hook()
        self.assertEqual(ADAPTER.check_live(batch, logdir), batch['upper_bytes'])
        self.assertEqual(len(calls), len(batch['selectors']))
        for arguments, destination in calls:
            self.assertIn(self.config['source']['dbhome'], arguments)
            self.assertIn(self.config['source']['opcfile'], arguments)
            self.assertIn(self.config['source']['opcwallet'], arguments)
            self.assertEqual(destination.parent, logdir)

    def test_live_preflight_rejects_added_names(self):
        batch, logdir = self.prepare_live()
        self.install_live_hook(lambda rows, selector: rows + [(selector + 'unexpected', '1 B')])
        with self.assertRaises(ValueError):
            ADAPTER.check_live(batch, logdir)

    def test_live_preflight_rejects_missing_names(self):
        batch, logdir = self.prepare_live()
        self.install_live_hook(lambda rows, selector: rows[1:])
        with self.assertRaises(ValueError):
            ADAPTER.check_live(batch, logdir)

    def test_live_preflight_rejects_changed_display_even_equal_bounds(self):
        batch, logdir = self.prepare_live()

        def changed(rows, unused_selector):
            name, size = rows[0]
            rows[0] = (name, size.replace(' B', '.00 B'))
            return rows

        self.install_live_hook(changed)
        with self.assertRaises(ValueError):
            ADAPTER.check_live(batch, logdir)

    def test_adapter_revalidates_manifest_before_loading_each_batch(self):
        self.build()
        ADAPTER.configure(self.config)
        ADAPTER.load_batch(1)
        path = self.plan_dir / 'batches' / 'batch_0001.json'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            ADAPTER.load_batch(1)

    def test_adapter_rejects_unknown_and_boolean_batch_ids(self):
        self.build()
        ADAPTER.configure(self.config)
        for identifier in (0, -1, 9999, True, 1.5, '1.5', 'invalid'):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    ADAPTER.load_batch(identifier)

    def capture_inventory(self, kind='source', pairs=None, returncode=0, mutate=None, name=None):
        folder = self.root / 'captures'
        folder.mkdir(exist_ok=True)
        output = folder / (name or (kind + ('.txt' if kind == 'source' else '.json')))
        source_rows = self.rows if pairs is None else self.source(pairs) if kind == 'source' else None
        if kind == 'source':
            raw = source_text([(key, row['display_size']) for key, row in source_rows.items()]).encode('utf-8')
        else:
            target_pairs = [(key, int(Decimal(row['nominal_bytes']))) for key, row in self.rows.items()] if pairs is None else pairs
            raw = json.dumps(dict(data=[dict(name=key, size=size, md5=None)
                                       for key, size in target_pairs])).encode('utf-8')

        def native_run(command, stdout, stderr, env):
            self.assertEqual(command, PLANNER.capture_command(self.config, kind))
            self.assertEqual(env['LC_ALL'], self.config['locale'])
            stdout.write(raw)
            if mutate is not None:
                mutate()
            return SimpleNamespace(returncode=returncode)

        with mock.patch.object(PLANNER.subprocess, 'run', side_effect=native_run) as child:
            if returncode:
                with self.assertRaises(ValueError):
                    PLANNER.capture(self.config, kind, output)
                record = json.loads(Path(str(output) + '.capture.json').read_text(encoding='utf-8'))
            else:
                record = PLANNER.capture(self.config, kind, output)
        self.assertEqual(child.call_count, 1)
        return output, record

    def test_full_source_and_target_capture_sidecars_bind_readonly_commands(self):
        for kind in ('source', 'target'):
            with self.subTest(kind=kind):
                output, record = self.capture_inventory(kind)
                rows, proof = PLANNER.load_capture(self.config, output, kind)
                self.assertEqual(set(rows), set(self.rows))
                self.assertTrue(proof['full_scope_verified'])
                self.assertEqual(proof['status'], 'captured')
                self.assertEqual(record['output_sha256'], sha(output.read_bytes()))
                self.assertEqual(Path(str(output) + '.exit').read_text().strip(), '0')
                self.assertEqual(record['command'][0], self.config['source']['mv2_binary']
                                 if kind == 'source' else self.config['target']['oci_binary'])
                if kind == 'target':
                    self.assertIn('--all', record['command'])
                    self.assertNotIn('--prefix', record['command'])
                    self.assertIn(self.config['target']['endpoint'], record['command'])

    def test_nonzero_native_capture_keeps_valid_output_but_rejects_load(self):
        output, record = self.capture_inventory(returncode=3)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['exit_code'], 3)
        self.assertEqual(Path(str(output) + '.exit').read_text().strip(), '1')
        self.assertEqual(set(INV.parse_source(output.read_bytes())), set(self.rows))
        with self.assertRaises(ValueError):
            PLANNER.load_capture(self.config, output, 'source')

    def test_capture_output_hash_scope_and_file_identity_drift_are_rejected(self):
        output, unused_record = self.capture_inventory()
        original = output.read_bytes()
        output.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            PLANNER.load_capture(self.config, output, 'source')
        output.write_bytes(original)
        changed = copy.deepcopy(self.config)
        changed['source']['db_unique_name'] = 'OTHER_DB'
        with self.assertRaises(ValueError):
            PLANNER.load_capture(changed, output, 'source')
        Path(self.config['source']['opcfile']).write_bytes(b'CHANGED SYNTHETIC CONFIG\n')
        with self.assertRaises(ValueError):
            PLANNER.load_capture(self.config, output, 'source')

    def test_tool_identity_change_during_capture_marks_it_failed(self):
        output = self.root / 'captures' / 'changed.txt'
        with self.assertRaises(ValueError):
            self.capture_inventory(mutate=lambda: Path(self.config['source']['opcfile']).write_bytes(
                b'CHANGED DURING SYNTHETIC CAPTURE\n'), name='changed.txt')
        record = json.loads(Path(str(output) + '.capture.json').read_text(encoding='utf-8'))
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(Path(str(output) + '.exit').read_text().strip(), '1')
        with self.assertRaises(ValueError):
            PLANNER.load_capture(self.config, output, 'source')

    def test_external_inventory_import_requires_explicit_unverified_scope(self):
        path = self.root / 'external_source.txt'
        path.write_text(source_text([(name, row['display_size']) for name, row in self.rows.items()]), encoding='utf-8')
        with self.assertRaises(ValueError):
            PLANNER.load_capture(self.config, path, 'source')
        rows, proof = PLANNER.load_capture(self.config, path, 'source', allow_external=True)
        self.assertEqual(set(rows), set(self.rows))
        self.assertFalse(proof['full_scope_verified'])
        self.assertEqual(proof['status'], 'external_unverified_scope')

    def test_reconcile_preserves_original_gaps_and_fresh_source_denominator(self):
        self.build()
        original_files = {str(path.relative_to(self.plan_dir)): path.read_bytes()
                          for path in self.plan_dir.rglob('*') if path.is_file()}
        missing = sorted(self.rows)[0]
        new_name = 'file_chunk/new_group/new_object'
        current_pairs = [(name, row['display_size']) for name, row in self.rows.items() if name != missing]
        current_pairs.append((new_name, '5 B'))
        current = self.source(current_pairs)
        source_path = self.root / 'fresh_external_source.txt'
        source_path.write_text(source_text(current_pairs), encoding='utf-8')
        for index, old_target_present in enumerate((False, True)):
            target_pairs = [(name, int(Decimal(row['nominal_bytes']))) for name, row in current.items()]
            if old_target_present:
                target_pairs.append((missing, int(Decimal(self.rows[missing]['nominal_bytes']))))
            target_path = self.root / ('fresh_external_target_' + str(index) + '.json')
            target_path.write_text(json.dumps(dict(data=[dict(name=name, size=size, md5=None)
                                                         for name, size in target_pairs])), encoding='utf-8')
            report = PLANNER.reconcile(self.config, source_path, target_path,
                                       self.root / ('reconciled_' + str(index)),
                                       original_plan=self.plan_dir, allow_external=True)
            history = report['historical_accounting']
            self.assertEqual(history['original_source_objects'], len(self.rows))
            self.assertEqual(history['absent_from_fresh_source_objects'], 1)
            self.assertEqual(history['new_source_names'], [new_name])
            self.assertFalse(history['deletion_status_verified'])
            self.assertEqual(history['missing_source_objects'][0]['classification'],
                             'target_present_unverified' if old_target_present else 'target_unknown')
            self.assertEqual(report['comparison']['source_after_objects'], len(current))
            self.assertEqual(report['comparison']['missing_target_names'], [])
            self.assertFalse(report['full_scope_verified'])
            self.assertEqual(report['status'], 'review_required')
            self.assertFalse(report['source_content_verified'])
            self.assertFalse(report['restore_verified'])
        self.assertEqual(original_files, {str(path.relative_to(self.plan_dir)): path.read_bytes()
                                        for path in self.plan_dir.rglob('*') if path.is_file()})

    def test_old_gap_absent_both_requires_both_verified_full_captures(self):
        self.build()
        missing = sorted(self.rows)[0]
        remaining = [(key, row['display_size']) for key, row in self.rows.items() if key != missing]
        source_path, unused_source = self.capture_inventory('source', remaining)
        target_path, unused_target = self.capture_inventory('target', [(key, int(Decimal(self.rows[key]['nominal_bytes']))) for key, unused in remaining])
        report = PLANNER.reconcile(self.config, source_path, target_path, self.root / 'verified_old_gaps', original_plan=self.plan_dir)
        self.assertTrue(report['full_scope_verified'])
        self.assertEqual(report['historical_accounting']['missing_source_objects'][0]['classification'], 'absent_both')

    def test_reconcile_reports_target_size_mismatch_with_verified_capture_scope(self):
        source_path, unused_source = self.capture_inventory('source')
        pairs = [(name, int(Decimal(row['nominal_bytes']))) for name, row in self.rows.items()]
        changed_name, size = pairs[0]
        pairs[0] = (changed_name, size + 1)
        target_path, unused_target = self.capture_inventory('target', pairs=pairs)
        report = PLANNER.reconcile(self.config, source_path, target_path, self.root / 'mismatch_report')
        self.assertTrue(report['full_scope_verified'])
        self.assertEqual(report['status'], 'review_required')
        self.assertEqual([row['name'] for row in report['comparison']['size_mismatches']], [changed_name])
        self.assertFalse(report['source_content_verified'])

    def ordered_capture_inputs(self):
        before, before_record = self.capture_inventory('source', name='source_before.txt')
        target, target_record = self.capture_inventory('target')
        after, after_record = self.capture_inventory('source', name='source_after.txt')
        records = [(before, before_record, '01:00:00', '01:01:00'),
                   (target, target_record, '01:01:00', '01:02:00'),
                   (after, after_record, '01:02:00', '01:03:00')]
        for path, record, started, finished in records:
            record['started_utc'] = '2026-10-26T' + started + '.000000+00:00'
            record['finished_utc'] = '2026-10-26T' + finished + '.000000+00:00'
            Path(str(path) + '.capture.json').write_text(json.dumps(record), encoding='utf-8')
        return before, target, after, records

    def test_three_verified_captures_must_bracket_target_listing(self):
        before, target, after, unused_records = self.ordered_capture_inputs()
        report = PLANNER.reconcile(self.config, after, target, self.root / 'ordered_report',
                                   source_before=before)
        self.assertTrue(report['comparison']['capture_order_verified'])
        self.assertTrue(report['full_scope_verified'])
        self.assertTrue(report['comparison']['two_source_snapshots_compared'])
        self.assertEqual(report['status'], 'inventory_coverage_complete')

    def test_overlapping_capture_order_and_invalid_utc_date_are_rejected(self):
        before, target, after, records = self.ordered_capture_inputs()
        for index, change in enumerate((
                dict(finished_utc='2026-10-26T01:02:30.000000+00:00'),
                dict(started_utc='2026-02-30T01:00:00.000000+00:00'))):
            with self.subTest(change=change):
                for path, record, unused_started, unused_finished in records:
                    value = dict(record, **change) if path == before else record
                    Path(str(path) + '.capture.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    PLANNER.reconcile(self.config, after, target,
                                       self.root / ('bad_order_' + str(index)), source_before=before)

    def test_external_before_snapshot_prevents_verified_scope_and_order_claim(self):
        before, target, after, unused_records = self.ordered_capture_inputs()
        external = self.root / 'external_before.txt'
        external.write_bytes(before.read_bytes())
        report = PLANNER.reconcile(self.config, after, target, self.root / 'external_before_report',
                                   source_before=external, allow_external=True)
        self.assertFalse(report['comparison']['capture_order_verified'])
        self.assertFalse(report['full_scope_verified'])
        self.assertTrue(report['comparison']['two_source_snapshots_compared'])
        self.assertEqual(report['status'], 'review_required')


if __name__ == '__main__':
    unittest.main()
