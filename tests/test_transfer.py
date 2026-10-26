"""End-to-end fake CLI checks. No database, Oracle CLI, or network is used."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import inventory
import migration_config
import mv2_plan

FAKE = r'''
import base64,hashlib,json,os,shutil,sys,time
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='findmnt':
    print(os.environ.get('FAKE_FS','xfs'));sys.exit(0)
if name=='ps':
    print(os.environ.get('FAKE_PROCESSES',''));sys.exit(0)
job=json.loads(Path(os.environ['EXPECTED_JOB']).read_text())
with open(os.environ['CALLS'],'a') as out:
    out.write(json.dumps({'name':name,'args':args,'environment':{k:v for k,v in os.environ.items() if k.startswith('OCI_CLI_')},'locale':os.environ.get('LC_ALL')})+'\n')
def value(flag):return args[args.index(flag)+1]
if name=='source-reader':
    assert args[0] in ('list','get')
    for flag,key in (('--dbhome','dbhome'),('--opcfile','opcfile'),('--opcwallet','opcwallet')):
        assert value(flag)==job['source'][key]
    assert '--dbuniquename' not in args
else:
    assert name=='oci-target'
    assert args[:2]==['--cli-rc-file','/dev/null']
    for flag,key in (('--endpoint','endpoint'),('--region','region'),('--config-file','config_file'),('--profile','profile'),('--auth','auth')):
        assert value(flag)==job['target'][key]
    if job['target'].get('cert_bundle'):assert value('--cert-bundle')==job['target']['cert_bundle']
    expected={'OCI_CLI_RC_FILE','OCI_CLI_ENDPOINT','OCI_CLI_REGION','OCI_CLI_CONFIG_FILE','OCI_CLI_PROFILE','OCI_CLI_AUTH'}
    if job['target'].get('cert_bundle'):expected.add('OCI_CLI_CERT_BUNDLE')
    assert {k for k in os.environ if k.startswith('OCI_CLI_')}==expected
assert os.environ['LC_ALL']==job['locale']
if '--help' in args:
    if name=='source-reader':
        print('mv2bucket.bin get --stage --filter --parallel --incremental --cksum --dbhome --opcfile --opcwallet')
    else:
        print('--no-overwrite --verify-checksum --no-follow-symlinks --no-multipart --parallel-upload-count')
    sys.exit(0)
source=json.loads(Path(os.environ['SOURCE']).read_text())
target=Path(os.environ['TARGET'])
if name=='source-reader':
    prefix=value('--filter')
    chosen={k:v for k,v in source.items() if k.startswith(prefix)}
    if args[0]=='list':
        if os.environ.get('LIVE_EXTRA'):chosen['data/unplanned/piece']='extra'
        print('\u2502 File Name \u2502 File Size \u2502')
        for key,content in sorted(chosen.items()):print('\u2502 '+key+' \u2502 '+str(len(content.encode()))+' B \u2502')
        print('Total Files: '+str(len(chosen)))
        sys.exit(0)
    assert '--incremental' in args and '--cksum' in args
    assert value('--parallel')==str(job['limits']['download_parallel'])
    stage=Path(value('--stage'))
    for key,content in chosen.items():
        path=stage/key;path.parent.mkdir(parents=True,exist_ok=True)
        if os.environ.get('GET_FAILURE'):
            path.write_bytes(content.encode()[:max(1,len(content)//2)]);sys.exit(7)
        path.write_bytes(content.encode())
    if os.environ.get('GET_SLEEP'):
        Path(os.environ['SLEEP_MARKER']).write_text('started')
        time.sleep(30)
    if os.environ.get('GET_MUTATE_OPC'):Path(job['source']['opcfile']).write_text('changed during GET')
    if os.environ.get('GET_EXTRA'):(stage/'unexpected').write_text('extra')
    sys.exit(0)
index=args.index('os');args=args[index:]
assert args[:2]==['os','object']
assert value('--namespace-name')==job['target']['namespace']
assert value('--bucket-name')==job['target']['bucket']
if args[2]=='list':
    assert '--all' in args and value('--fields')=='name,size,md5'
    prefix=value('--prefix');rows=[]
    for path in sorted(target.rglob('*')):
        if path.is_file() and path.relative_to(target).as_posix().startswith(prefix):
            raw=path.read_bytes();checksum=base64.b64encode(hashlib.md5(raw).digest()).decode()
            if os.environ.get('MISSING_MD5'):checksum=None
            if os.environ.get('MULTIPART_MD5'):checksum+='-2'
            rows.append({'name':path.relative_to(target).as_posix(),'size':len(raw),'md5':checksum})
    if os.environ.get('DUPLICATE_TARGET') and rows:rows.append(dict(rows[0]))
    print(json.dumps({'data':rows}));sys.exit(0)
assert args[2]=='bulk-upload'
for flag in ('--no-overwrite','--verify-checksum','--no-follow-symlinks','--no-multipart'):assert flag in args
assert value('--object-prefix')==''
assert value('--parallel-upload-count')==str(job['limits']['upload_parallel'])
failures={};uploaded={};skipped=[]
for path in sorted(Path(value('--src-dir')).rglob('*')):
    if not path.is_file():continue
    key=path.relative_to(value('--src-dir')).as_posix();remote=target/key
    if os.environ.get('BULK_FAILURE'):failures[key]='synthetic failure';continue
    if remote.exists():skipped.append(key);continue
    remote.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(str(path),str(remote));uploaded[key]={}
print(json.dumps({'uploaded-objects':uploaded,'skipped-objects':skipped,'upload-failures':failures}))
'''


class Transfer(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('source-reader', 'oci-target', 'findmnt', 'ps'):
            path = self.bin / name
            path.write_text('#!' + sys.executable + '\n' + FAKE)
            path.chmod(0o700)
        (self.root / 'dbhome').mkdir()
        for name in ('source.ora', 'wallet.sso', 'oci.conf', 'ca.pem'):
            (self.root / name).write_text('SYNTHETIC_PRIVATE_' + name)
        self.job_path = self.root / 'job.json'
        self.job = dict(format_version=1,
                        source=dict(mv2_binary=str(self.bin / 'source-reader'), db_unique_name='EXAMPLE_DB', dbhome=str(self.root / 'dbhome'), opcfile=str(self.root / 'source.ora'), opcwallet=str(self.root / 'wallet.sso')),
                        target=dict(oci_binary=str(self.bin / 'oci-target'), endpoint='https://objectstorage.realm.custom.example', region='region-test-9', namespace='demo-namespace', bucket='demo-backup', config_file=str(self.root / 'oci.conf'), profile='CUSTOM', auth='api_key'),
                        paths=dict(plan_dir=str(self.root / 'plan'), stage_root=str(self.root / 'stage'), state_dir=str(self.root / 'state')),
                        limits=dict(batch_bytes=25000000000, reserve_bytes=5000000000, download_parallel=4, upload_parallel=8))
        self.source = {'data/example/piece/0001': 'backup payload 1234567', 'data/example/piece/metadata.xml': '<metadata>backup</metadata>'}
        self.target = self.root / 'target'
        self.target.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.calls.write_text('')
        self.source_file = self.root / 'source.json'
        self.source_file.write_text(json.dumps(self.source))
        self.expected_job = self.root / 'expected.json'
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env['PATH'], SOURCE=str(self.source_file), TARGET=str(self.target), CALLS=str(self.calls), EXPECTED_JOB=str(self.expected_job), SLEEP_MARKER=str(self.root / 'sleep.marker'), OCI_CLI_ENDPOINT='https://wrong.invalid', OCI_CLI_AUTH='security_token', OCI_CLI_SECURITY_TOKEN_FILE='/wrong/secret', OCI_CLI_EXTRA='unconfigured')
        self.build()

    def tearDown(self):
        self.temporary.cleanup()

    def build(self):
        self.job_path.write_text(json.dumps(self.job))
        job = migration_config.load_config(self.job_path)
        self.expected_job.write_text(json.dumps(job))
        rows = {name: inventory._size_row(name, str(len(value.encode())) + ' B') for name, value in self.source.items()}
        mv2_plan.build_plan(job, rows)

    def rebuild(self):
        shutil.rmtree(self.job['paths']['plan_dir'])
        self.build()

    def invoke(self, *arguments, **flags):
        environment = self.env.copy()
        environment.update(flags)
        result = subprocess.run([sys.executable, str(ROOT / 'mv2_bulk.py'), '--config', str(self.job_path)] + list(arguments), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=environment)
        return result.returncode, result.stdout

    def success(self, *arguments, **flags):
        code, output = self.invoke(*arguments, **flags)
        self.assertEqual(code, 0, output)
        return output

    def failure(self, *arguments, **flags):
        code, output = self.invoke(*arguments, **flags)
        self.assertNotEqual(code, 0, output)
        return output

    def calls_read(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def stage(self):
        return Path(self.job['paths']['stage_root']) / 'batch_0001'

    def state(self, suffix):
        return Path(self.job['paths']['state_dir']) / ('batch_0001.' + suffix + '.json')

    def target_fill(self):
        for name, content in self.source.items():
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode())

    def test_preview_is_read_only_and_explicit_context_including_help(self):
        self.assertIn('PREVIEW ONLY', self.success('preview', '1'))
        self.assertFalse(self.stage().exists())
        self.assertFalse(any(self.target.rglob('*')))
        calls = self.calls_read()
        self.assertTrue(any('--help' in call['args'] for call in calls))
        self.assertFalse(any(call['name'] == 'source-reader' and call['args'][0] == 'get' and '--help' not in call['args'] for call in calls))
        self.assertFalse(any('bulk-upload' in call['args'] and '--help' not in call['args'] for call in calls))

    def test_end_to_end_whole_md5_receipt_and_owned_cleanup(self):
        self.assertIn('VERIFIED: 2', self.success('run-range', '--start', '1', '--end', '1', '--cleanup-verified'))
        receipt = json.loads(self.state('verified').read_text())
        self.assertEqual(set(receipt['objects']), set(self.source))
        for name, content in self.source.items():
            self.assertEqual(receipt['objects'][name], dict(size=len(content), md5=base64.b64encode(hashlib.md5(content.encode()).digest()).decode()))
            self.assertEqual((self.target / name).read_text(), content)
        self.assertFalse(self.stage().exists())
        self.assertNotIn('SYNTHETIC_PRIVATE_', self.state('verified').read_text())
        binding = receipt['binding']
        self.assertEqual(binding['target']['endpoint'], self.job['target']['endpoint'])
        self.assertIn('opcwallet', binding['credential_tool_files'])
        self.assertIn('source_snapshot_sha256', binding)

    def test_instance_principal_and_custom_ca_other_endpoint(self):
        self.job['target'].update(auth='instance_principal', endpoint='https://alloy.example.invalid:8443', region='independent-region', cert_bundle=str(self.root / 'ca.pem'))
        self.rebuild()
        self.assertIn('VERIFIED', self.success('run', '1'))

    def test_verified_resume_does_not_redownload_or_reupload(self):
        self.success('run', '1')
        before = len(self.calls_read())
        self.assertIn('Previously verified receipt rechecked', self.success('run', '1'))
        calls = self.calls_read()[before:]
        self.assertTrue(calls)
        self.assertFalse(any(call['name'] == 'source-reader' for call in calls))
        self.assertFalse(any('bulk-upload' in call['args'] for call in calls))

    def test_failed_get_partial_stage_cannot_be_reused(self):
        self.assertIn('Command exited 7', self.failure('run', '1', GET_FAILURE='1'))
        self.assertFalse(self.state('downloaded').exists())
        self.assertFalse(self.state('verified').exists())
        self.assertIn('may be partial', self.failure('run', '1'))
        self.assertTrue(self.stage().exists())

    def test_successful_download_can_retry_failed_bulk_without_get(self):
        self.assertIn('clean upload-failures', self.failure('run', '1', BULK_FAILURE='1'))
        self.assertTrue(self.state('downloaded').exists())
        before = len(self.calls_read())
        self.assertIn('VERIFIED', self.success('run', '1'))
        calls = self.calls_read()[before:]
        self.assertFalse(any(call['name'] == 'source-reader' and call['args'][0] == 'get' and '--help' not in call['args'] for call in calls))

    def test_tampered_download_cache_refused(self):
        self.failure('run', '1', BULK_FAILURE='1')
        key = next(iter(self.source))
        (self.stage() / key).write_text('X' * len(self.source[key]))
        self.assertIn('Staged', self.failure('run', '1'))
        self.assertFalse(self.state('verified').exists())

    def test_source_live_extra_scope_blocks_download(self):
        self.assertIn('Live source filter drift', self.failure('run', '1', LIVE_EXTRA='1'))
        self.assertFalse(self.stage().exists())

    def test_existing_different_target_refuses_overwrite(self):
        self.target_fill()
        key = next(iter(self.source))
        (self.target / key).write_text('X' * len(self.source[key]))
        self.assertIn('Target size/MD5 mismatch', self.failure('run', '1'))
        self.assertEqual((self.target / key).read_text(), 'X' * len(self.source[key]))
        self.assertFalse(self.state('verified').exists())
        self.assertFalse(any('bulk-upload' in call['args'] and '--help' not in call['args'] for call in self.calls_read()))

    def test_missing_or_multipart_md5_never_verifies(self):
        self.target_fill()
        self.assertIn('whole-file MD5', self.failure('run', '1', MISSING_MD5='1'))
        self.assertFalse(self.state('verified').exists())
        self.assertIn('MD5', self.failure('run', '1', MULTIPART_MD5='1'))
        self.assertFalse(self.state('verified').exists())

    def test_duplicate_target_inventory_is_rejected(self):
        self.target_fill()
        self.assertIn('Duplicate target object', self.failure('preview', '1', DUPLICATE_TARGET='1'))

    def test_context_changes_during_get_stop_before_upload(self):
        self.assertIn('changed', self.failure('run', '1', GET_MUTATE_OPC='1'))
        self.assertFalse(any('bulk-upload' in call['args'] and '--help' not in call['args'] for call in self.calls_read()))

    def test_receipt_context_changed_between_sessions_refuses_cleanup(self):
        self.success('run', '1')
        (self.root / 'wallet.sso').write_text('new wallet fixture')
        self.assertIn('binding/status mismatch', self.failure('cleanup', '1'))
        self.assertTrue(self.stage().exists())

    def test_config_change_requires_new_bound_plan(self):
        self.job['target']['endpoint'] = 'https://different.example.invalid'
        self.job_path.write_text(json.dumps(self.job))
        self.expected_job.write_text(json.dumps(migration_config.load_config(self.job_path)))
        self.assertIn('different job configuration', self.failure('preview', '1'))

    def test_manifest_change_and_source_snapshot_change_refused(self):
        batch = Path(self.job['paths']['plan_dir']) / 'batches' / 'batch_0001.json'
        batch.write_text(batch.read_text() + ' ')
        self.assertIn('manifest checksum differs', self.failure('preview', '1'))
        self.rebuild()
        snapshot = Path(self.job['paths']['plan_dir']) / 'source_inventory.json'
        snapshot.write_text(snapshot.read_text() + ' ')
        self.assertIn('snapshot checksum differs', self.failure('preview', '1'))

    def test_cleanup_refuses_tampered_stage_and_owner(self):
        self.success('run', '1')
        key = next(iter(self.source))
        (self.stage() / key).write_text('X' * len(self.source[key]))
        self.assertIn('Local staged content changed', self.failure('cleanup', '1'))
        (self.stage() / key).write_text(self.source[key])
        self.state('owner').write_text('{}')
        self.assertIn('ownership marker mismatch', self.failure('cleanup', '1'))
        self.assertTrue(self.stage().exists())

    def test_symlink_stage_lock_receipt_and_logs_refused(self):
        stage_root = Path(self.job['paths']['stage_root'])
        stage_root.mkdir(mode=0o700)
        outside = self.root / 'outside'
        outside.write_text('keep')
        lock = stage_root / '.mv2_bulk.lock'
        lock.symlink_to(outside)
        self.failure('preview', '1')
        self.assertEqual(outside.read_text(), 'keep')
        lock.unlink()
        self.success('run', '1')
        self.state('verified').unlink()
        self.state('verified').symlink_to(outside)
        self.failure('run', '1')
        self.assertEqual(outside.read_text(), 'keep')
        self.state('verified').unlink()
        self.stage().rename(self.root / 'saved-stage')
        self.stage().symlink_to(self.root / 'saved-stage', target_is_directory=True)
        self.failure('run', '1')
        self.assertEqual(outside.read_text(), 'keep')

    def test_group_writable_state_and_memory_stage_refused(self):
        state = Path(self.job['paths']['state_dir'])
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        self.assertIn('not group/world writable', self.failure('preview', '1'))
        state.chmod(0o700)
        self.assertIn('disk-backed', self.failure('preview', '1', FAKE_FS='tmpfs'))

    def test_disk_reserve_and_range_confirmation_flags(self):
        self.assertIn('--cleanup-verified', self.failure('run-range', '--start', '1'))
        self.job['limits']['reserve_bytes'] = 10 ** 30
        self.rebuild()
        self.assertIn('Insufficient free staging', self.failure('preview', '1'))

    def test_signal_stops_child_group_and_lock_prevents_parallel_run(self):
        environment = dict(self.env, GET_SLEEP='1')
        command = [sys.executable, str(ROOT / 'mv2_bulk.py'), '--config', str(self.job_path), 'run', '1']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=environment)
        try:
            deadline = time.monotonic() + 15
            marker = self.root / 'sleep.marker'
            while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), 'fake GET did not begin')
            self.assertIn('holds the staging/state lock', self.failure('preview', '1'))
            process.send_signal(signal.SIGTERM)
            output = process.communicate(timeout=10)[0]
            self.assertEqual(process.returncode, 143, output)
            self.assertIn('Active child group stopped', output)
            self.assertFalse(self.state('verified').exists())
            self.assertTrue(self.stage().exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_missing_post_upload_md5_retains_stage_and_no_receipt(self):
        self.assertIn('whole-file MD5', self.failure('run', '1', MISSING_MD5='1'))
        self.assertTrue(self.stage().exists())
        self.assertFalse(self.state('verified').exists())
        self.assertTrue(any('bulk-upload' in call['args'] and '--help' not in call['args'] for call in self.calls_read()))

    def test_unplanned_get_file_blocks_upload(self):
        self.assertIn('Stage differs from the manifest', self.failure('run', '1', GET_EXTRA='1'))
        self.assertTrue((self.stage() / 'unexpected').is_file())
        self.assertFalse(self.state('verified').exists())
        self.assertFalse(any('bulk-upload' in call['args'] and '--help' not in call['args'] for call in self.calls_read()))

    def test_logs_parent_symlink_refused(self):
        state = Path(self.job['paths']['state_dir'])
        state.mkdir(mode=0o700)
        outside = self.root / 'external-logs'
        outside.mkdir()
        (state / 'logs').symlink_to(outside, target_is_directory=True)
        self.assertIn('Symbolic links', self.failure('preview', '1'))
        self.assertEqual(list(outside.iterdir()), [])

    def test_captured_plan_credential_identity_is_bound_across_startup(self):
        job = migration_config.load_config(self.job_path)
        plan_path = Path(self.job['paths']['plan_dir']) / 'plan.json'
        plan = json.loads(plan_path.read_text())
        plan['inventory_provenance'] = {'source': {'status': 'captured', 'file_identity': migration_config.file_identity(job)}}
        plan_path.write_text(json.dumps(plan))
        (self.root / 'source.ora').write_text('different source configuration fixture')
        self.assertIn('Captured plan tool/credential identity changed', self.failure('preview', '1'))


if __name__ == '__main__':
    unittest.main()
