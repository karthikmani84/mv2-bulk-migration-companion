#!/usr/bin/env python3
"""Bounded MV2 downloads followed by OCI bulk upload. Python 3.6+, stdlib only.

Standalone configured edition. MV2 is used only for managed-source LIST/GET;
all target calls use explicit OCI CLI endpoint/region/authentication settings.
Only packaged code is imported; plans contain data, never executable adapters.
"""
import argparse
import base64
import datetime
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import tempfile

import migration_config
import mv2_plan_adapter as plan_adapter
import inventory


class TransferInterrupted(Exception):
    def __init__(self, number):
        self.number = number
        super().__init__('received signal ' + str(number))


def handle_signal(number, frame):
    raise TransferInterrupted(number)


def stop(message):
    raise ValueError(message)


def sha256(path):
    return hashlib.sha256(plan_adapter.read_bytes(Path(path).absolute())).hexdigest()


def md5(path):
    path = no_symlink_components(Path(path))
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            stop('Only regular staged files can be checksummed: ' + str(path))
        digest = hashlib.md5()
        while True:
            block = os.read(descriptor, 4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if signature(before) != signature(os.fstat(descriptor)) or signature(before) != signature(os.lstat(str(path))):
            stop('Staged file changed while checksumming: ' + str(path))
        return base64.b64encode(digest.digest()).decode('ascii')
    finally:
        os.close(descriptor)


def canonical_md5(value):
    if type(value) is not str:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
        return len(raw) == 16 and base64.b64encode(raw).decode('ascii') == value
    except (ValueError, TypeError):
        return False


def read_json(path, private=False):
    path = Path(path)
    no_symlink_components(path)
    if not path.is_file() or path.is_symlink():
        stop('Expected regular, non-symlink metadata: ' + str(path))
    if private:
        value = path.stat()
        if value.st_uid != os.geteuid() or value.st_mode & 0o022:
            stop('Receipt/ownership metadata must be owned by this user and not writable by others: ' + str(path))
    return json.loads(plan_adapter.read_bytes(path).decode('utf-8'), object_pairs_hook=migration_config.unique_json, parse_constant=migration_config._bad_constant)


def atomic_json(path, value):
    path = Path(path)
    no_symlink_components(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            stop('Refusing metadata symlink: ' + str(path))
        os.replace(temporary, str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def check_key(name):
    if not isinstance(name, str) or not name or name.startswith('/'):
        stop('Invalid manifest object name: ' + repr(name))
    if any(character in name for character in ('\\', '\x00', '\r', '\n', '\t')):
        stop('Unsafe manifest object name: ' + repr(name))
    if any(part in ('', '.', '..') for part in name.split('/')):
        stop('Unsafe object path components: ' + repr(name))
    if str(PurePosixPath(name)) != name:
        stop('Object name cannot be mapped exactly to a local path: ' + name)


def no_symlink_components(path):
    path = Path(os.path.abspath(str(path.expanduser())))
    for component in [path] + list(path.parents):
        if component.is_symlink():
            stop('Symbolic links are not allowed in staging/state paths: ' + str(component))
    return path


def process_conflicts(bucket):
    output = subprocess.check_output(['ps', '-eo', 'pid=,ppid=,args='], universal_newlines=True)
    processes = {}
    for line in output.splitlines():
        pieces = line.strip().split(None, 2)
        if len(pieces) == 3 and pieces[0].isdigit() and pieces[1].isdigit():
            processes[int(pieces[0])] = (int(pieces[1]), pieces[2])
    ancestors = {os.getpid()}
    current = os.getpid()
    while current in processes and processes[current][0] not in ancestors:
        current = processes[current][0]
        ancestors.add(current)
    conflicts = []
    for process_id, (parent_id, arguments) in processes.items():
        if process_id in ancestors:
            continue
        original_copy = re.search(r'mv2_batch\.py\s+copy\b', arguments)
        mv2_transfer = re.search(r'mv2bucket\.bin\s+(copy|get|load)\b', arguments)
        cli_transfer = re.search(r'\boci\s+.*\bos\s+object\s+(put|bulk-upload)\b', arguments)
        companion = re.search(r'mv2_(?:bulk|bulk_resume)[.]py\b', arguments)
        if original_copy or mv2_transfer or companion or (cli_transfer and bucket in arguments):
            conflicts.append(str(process_id) + ': ' + arguments)
    if conflicts:
        stop('Another transfer may be active. Pause its runner before continuing:\n' + '\n'.join(conflicts[:5]))


class Companion:
    def __init__(self, args):
        self.args = args
        self.job_path = Path(args.config).expanduser().resolve()
        self.job = migration_config.load_config(self.job_path)
        self.original = plan_adapter
        self.original.configure(self.job)
        self.config = self.original.config()
        self.namespace = self.job['target']['namespace']
        self.cap = self.job['limits']['batch_bytes']
        self.reserve = self.job['limits']['reserve_bytes']
        self.root = no_symlink_components(Path(self.job['paths']['stage_root']))
        self.state = no_symlink_components(Path(self.job['paths']['state_dir']))
        self.plan_dir = no_symlink_components(Path(self.job['paths']['plan_dir']))
        self.batch = None
        self.logdir = None
        self.sequence = 0
        self.locks = []
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.root, self.state):
            value = directory.stat()
            if value.st_uid != os.geteuid() or value.st_mode & 0o022:
                stop('Staging/state directories must be owned by this user and not group/world writable: ' + str(directory))
        filesystem = subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '--target', str(self.root)], universal_newlines=True).strip().lower()
        if not filesystem or filesystem in ('tmpfs', 'ramfs', 'devtmpfs'):
            stop('A disk-backed staging filesystem is required; detected: ' + filesystem)
        for lock_path in (self.root / '.mv2_bulk.lock', self.state / '.lock'):
            descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o022:
                os.close(descriptor)
                stop('Unsafe staging/state lock: ' + str(lock_path))
            stream = os.fdopen(descriptor, 'a+')
            self.locks.append(stream)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stop('Another transfer holds the staging/state lock: ' + str(lock_path))
        process_conflicts(self.config['bucket'])
        self.identity = self.current_identity()
        provenance = read_json(self.plan_dir / 'plan.json').get('inventory_provenance', {})
        for kind in ('source', 'target'):
            proof = provenance.get(kind)
            if isinstance(proof, dict) and proof.get('status') == 'captured' and proof.get('file_identity') != self.identity['credential_tool_files']:
                stop('Captured plan tool/credential identity changed before transfer; capture and build a new plan.')
        self.original.run_mv2 = self.original_mv2

    def current_identity(self):
        modules = [Path(__file__).resolve(), Path(migration_config.__file__).resolve(),
                   Path(plan_adapter.__file__).resolve(), Path(inventory.__file__).resolve()]
        return dict(format_version=1, job_config=str(self.job_path),
                    config_sha256=migration_config.config_sha256(self.job_path),
                    normalized_config_sha256=migration_config.digest(self.job),
                    plan_dir=str(self.plan_dir), plan_sha256=sha256(self.plan_dir / 'plan.json'),
                    source_snapshot_sha256=sha256(self.plan_dir / 'source_inventory.json'),
                    db=self.job['source']['db_unique_name'], bucket=self.job['target']['bucket'],
                    namespace=self.namespace, target=dict(self.job['target']), source=dict(self.job['source']),
                    stage_root=str(self.root), state_dir=str(self.state), limits=dict(self.job['limits']),
                    credential_tool_files=migration_config.file_identity(self.job),
                    packaged_modules={str(path): sha256(path) for path in modules}, cli_rc_policy='/dev/null')

    def check_context(self):
        if migration_config.load_config(self.job_path) != self.job or self.current_identity() != self.identity:
            stop('Job, source/target credential files, tools, packaged code or plan changed; transfer and cleanup refused.')
        if self.batch is not None and sha256(self.plan_dir / 'batches' / ('batch_' + self.batch['id'] + '.json')) != self.binding['batch_sha256']:
            stop('Selected manifest changed; transfer and cleanup refused.')

    def message(self, value):
        print(value, flush=True)

    def select(self, number):
        self.check_context()
        process_conflicts(self.config['bucket'])
        self.batch = self.original.load_batch(number)
        if self.batch['object_count'] != len(self.batch['objects']):
            stop('Manifest object count differs from its declared count.')
        names = [row['name'] for row in self.batch['objects']]
        if len(names) != len(set(names)):
            stop('Duplicate names in batch manifest.')
        for name in names:
            check_key(name)
        if any(row['upper_bytes'] < 0 or Decimal(row['nominal_bytes']) < 0 for row in self.batch['objects']):
            stop('Negative planned object size.')
        if not self.batch['selectors'] or not 0 <= self.batch['upper_bytes'] < self.cap:
            stop('Invalid batch selectors or estimated size.')
        for selector in self.batch['selectors']:
            if not selector or any(not name.startswith(selector) for name in names if selector in name):
                stop('Selector does not have the reviewed prefix behavior: ' + selector)
        if any(sum(name.startswith(selector) for selector in self.batch['selectors']) != 1 for name in names):
            stop('Manifest prefixes overlap or fail to cover every planned object exactly once.')
        self.expected = {row['name']: row for row in self.batch['objects']}
        self.binding = dict(self.identity, batch=self.batch['id'], names_sha256=self.batch['names_sha256'],
                            batch_sha256=sha256(self.plan_dir / 'batches' / ('batch_' + self.batch['id'] + '.json')))
        self.stage = self.root / ('batch_' + self.batch['id'])
        self.owner = self.state / ('batch_' + self.batch['id'] + '.owner.json')
        self.receipt = self.state / ('batch_' + self.batch['id'] + '.verified.json')
        self.download_receipt = self.state / ('batch_' + self.batch['id'] + '.downloaded.json')
        stamp = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')
        self.logdir = self.state / 'logs' / ('batch_' + self.batch['id']) / stamp
        no_symlink_components(self.logdir)
        self.logdir.mkdir(parents=True, exist_ok=False)
        self.message('Batch {}: {:,} objects; estimated upper size {:.3f} GB; target {} / {}'.format(
            self.batch['id'], len(names), self.batch['upper_bytes'] / 1e9, self.namespace, self.config['bucket']))
        self.message('Logs: ' + str(self.logdir))

    def run_process(self, command, output, watch_stage=False, accepted_exitcodes=(0,)):
        self.check_context()
        if command[0] == self.config['bin']:
            if len(command) < 2 or command[1] not in ('list', 'get'):
                stop('MV2 may only read the source through LIST/GET.')
            prefix = migration_config.source_command(self.job, command[1])
            if command[:len(prefix)] != prefix:
                stop('Unexpected MV2 source command; explicit configured source paths are required.')
        process_conflicts(self.config['bucket'])
        output = Path(output)
        error = output.with_name(output.name + '.stderr')
        self.message('+ ' + ' '.join(shlex.quote(value) for value in command))
        environment = migration_config.tool_environment(self.job)
        with output.open('w', encoding='utf-8') as stdout, error.open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=environment, start_new_session=True)
            last_report = time.monotonic()
            next_stage_check = 0.0
            try:
                while process.poll() is None:
                    if watch_stage and time.monotonic() >= next_stage_check:
                        free = shutil.disk_usage(str(self.root)).free
                        used = self.scan_stage(allow_missing=True)[1]
                        next_stage_check = time.monotonic() + 1.0
                        if free < self.reserve or used >= self.cap:
                            stop('Staging watchdog: free space below configured reserve or staging reached configured batch cap. Files retained.')
                        if time.monotonic() - last_report >= 30:
                            self.message('Running: staged {:.3f} GB; {:.3f} GB free.'.format(used / 1e9, free / 1e9))
                            last_report = time.monotonic()
                    time.sleep(0.1)
                result = process.wait()
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise
        if result not in accepted_exitcodes:
            stop('Command exited {}. Review {} and {}. Staging retained.'.format(result, output, error))
        return output

    def original_mv2(self, arguments, log):
        return self.run_process([self.config['bin']] + list(arguments), Path(log))

    def cli(self, arguments, label, watch=False):
        self.sequence += 1
        output = self.logdir / ('{:03d}_{}.json'.format(self.sequence, label))
        command = migration_config.target_command(self.job, list(arguments) + ['--output', 'json', '--query', '@'])
        self.run_process(command, output, watch_stage=watch)
        try:
            return json.loads(output.read_text(encoding='utf-8'), object_pairs_hook=migration_config.unique_json, parse_constant=migration_config._bad_constant)
        except (ValueError, OSError):
            stop('OCI returned no valid JSON. Review ' + str(output))

    def target_inventory(self):
        result = {}
        for selector in self.batch['selectors']:
            response = self.cli(['os', 'object', 'list', '--namespace-name', self.namespace,
                                 '--bucket-name', self.config['bucket'], '--prefix', selector,
                                 '--fields', 'name,size,md5', '--all'], 'target_list')
            parsed = inventory.parse_target(json.dumps(response).encode('utf-8'))
            for name, row in parsed.items():
                if not name.startswith(selector):
                    stop('OCI listing returned an object outside the requested literal prefix.')
                if name in self.expected:
                    if name in result:
                        stop('Duplicate expected target name across prefix inventories: ' + name)
                    result[name] = row
        return result

    def validate_cli_options(self):
        output = self.run_process(migration_config.target_command(self.job, ['os', 'object', 'bulk-upload', '--help']), self.logdir / 'oci_bulk_help.txt')
        text = output.read_text(encoding='utf-8')
        required = ('--no-overwrite', '--verify-checksum', '--no-follow-symlinks', '--no-multipart', '--parallel-upload-count')
        if any(option not in text for option in required):
            stop('Installed OCI CLI bulk-upload help does not list all required options. Review ' + str(output))
        # Do not infer undocumented GET options from COPY help.
        output = self.run_process(migration_config.source_command(self.job, 'get') + ['--help'], self.logdir / 'mv2_get_help.txt', accepted_exitcodes=(0, 1))
        text = output.read_text(encoding='utf-8')
        sections = re.findall(r"mv2bucket(?:[.]bin)?\s+(?:(?!mv2bucket(?:[.]bin)?\s+).)*", text, flags=re.I | re.S)
        get_sections = [section for section in sections if re.match(r'mv2bucket(?:\.bin)?\s+get\b', section, re.I)]
        get_help = '\n'.join(get_sections)
        required = ('--stage', '--filter', '--parallel', '--incremental', '--cksum', '--dbhome', '--opcfile', '--opcwallet')
        if not re.search(r'\bget\b', get_help, re.I) or any(option not in get_help for option in required):
            stop('GET help could not confirm the required download flags. Review ' + str(output))

    def scan_stage(self, allow_missing=False):
        files, total = {}, 0
        if not self.stage.exists():
            if allow_missing:
                return files, total
            stop('Staging directory does not exist: ' + str(self.stage))
        no_symlink_components(self.stage)
        for current, directories, names in os.walk(str(self.stage), followlinks=False):
            for directory in directories:
                if (Path(current) / directory).is_symlink():
                    stop('Symlink directory in staging: ' + str(Path(current) / directory))
            for name in names:
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    stop('Nonregular or symlink file in staging: ' + str(path))
                key = path.relative_to(self.stage).as_posix()
                check_key(key)
                size = path.stat().st_size
                files[key] = {'path': path, 'size': size}
                total += size
        return files, total

    def prepare_stage(self):
        no_symlink_components(self.stage)
        for candidate in self.root.glob('batch_*'):
            if candidate != self.stage and (candidate.is_symlink() or any(candidate.rglob('*'))):
                stop('Another batch is still staged. Verify/clean it first: ' + str(candidate))
        if self.stage.exists() and any(self.stage.rglob('*')):
            if not self.owner.is_file() or read_json(self.owner, private=True) != self.binding:
                stop('Existing stage does not have a matching ownership marker. Preserve it and review.')
        free = shutil.disk_usage(str(self.root)).free
        if free < self.batch['upper_bytes'] + self.reserve:
            stop('Need the full batch estimate plus configured reserve; only {:.3f} GB free.'.format(free / 1e9))
        self.stage.mkdir(mode=0o700, exist_ok=True)
        atomic_json(self.owner, self.binding)
        existing, total = self.scan_stage()
        if set(existing) - set(self.expected) or total >= self.cap:
            stop('Unexpected staged files or staging size exceeds the budget. Files retained.')
        if existing:
            if not self.download_receipt.is_file():
                stop('Unverified existing download files may be partial. Preserve this stage and use a NEW stage root AND state directory for a clean retry.')
            receipt = read_json(self.download_receipt, private=True)
            self.validate_receipt(receipt, expected_status='downloaded')
            if self.local_records() != receipt['objects']:
                stop('Staged bytes changed since the successful download receipt. Preserve this stage; restart clean in a NEW stage root AND state directory.')
            return receipt['objects']
        return None

    def local_records(self):
        files, total = self.scan_stage()
        missing, extra = set(self.expected) - set(files), set(files) - set(self.expected)
        if missing or extra:
            stop('Stage differs from the manifest. Missing: {}; extra: {}. No upload/cleanup.'.format(sorted(missing)[:3], sorted(extra)[:3]))
        if total >= self.cap:
            stop('Actual staging bytes must remain below the configured exclusive batch cap.')
        records = {}
        for name, item in files.items():
            planned = self.expected[name]
            match = self.original.CELL_SIZE.fullmatch(planned['display_size'])
            if not match:
                stop('Cannot validate planned display size: ' + name)
            quantum = Decimal('0.01') * self.original.UNITS[match.group(2)]
            low = max(Decimal(0), Decimal(planned['nominal_bytes']) - quantum)
            if not low <= Decimal(item['size']) <= Decimal(planned['upper_bytes']):
                stop('Staged file size differs from its source size estimate: ' + name)
            if item['size'] > migration_config.MAX_SINGLE_OBJECT_BYTES:
                stop('Object exceeds the reviewed no-multipart single-PUT limit: ' + name)
            records[name] = {'size': item['size'], 'md5': md5(item['path'])}
        return records

    def verify_target(self, records, allow_missing=False):
        target = self.target_inventory()
        missing = []
        for name, expected in records.items():
            actual = target.get(name)
            if actual is None:
                missing.append(name)
                continue
            value = actual.get('md5')
            try:
                whole_md5 = canonical_md5(value)
            except (ValueError, TypeError):
                whole_md5 = False
            if not whole_md5:
                stop('No comparable whole-file MD5 for target object. Retain staging and review multipart verification: ' + name)
            if type(actual.get('size')) is not int or actual.get('size') != expected['size'] or value != expected['md5']:
                stop('Target size/MD5 mismatch. Existing objects will NOT be overwritten; staging retained: ' + name)
        if missing and not allow_missing:
            stop('Target still has {} missing objects; staging retained. First: {}'.format(len(missing), missing[0]))
        return missing

    def preview(self):
        self.validate_cli_options()
        self.original.check_live(self.batch, self.logdir)
        target = self.target_inventory()
        present = sum(name in target for name in self.expected)
        free = shutil.disk_usage(str(self.root)).free
        self.message('PREVIEW ONLY: {} planned names already exist; {} absent in target.'.format(present, len(self.expected) - present))
        self.message('Free space: {:.3f} GB. Need estimated batch + reserve: {:.3f} GB.'.format(free / 1e9, (self.batch['upper_bytes'] + self.reserve) / 1e9))
        if free < self.batch['upper_bytes'] + self.reserve:
            stop('Insufficient free staging space.')
        self.message('RUN will download this full selected batch (local incremental), bulk upload missing target names, and verify all staged names/sizes/MD5s. No files downloaded or uploaded by PREVIEW.')

    def run(self, cleanup=False):
        if self.receipt.exists():
            receipt = read_json(self.receipt, private=True)
            self.validate_receipt(receipt)
            self.verify_target(receipt['objects'])
            self.message('Previously verified receipt rechecked against target; no download/upload needed.')
            if cleanup:
                self.cleanup(receipt)
            return
        self.validate_cli_options()
        self.original.check_live(self.batch, self.logdir)
        records = self.prepare_stage()
        if records is None:
            for index, selector in enumerate(self.batch['selectors'], 1):
                self.run_process(migration_config.source_command(self.job, 'get') + ['--stage', str(self.stage), '--filter', selector, '--parallel', str(self.job['limits']['download_parallel']),
                                  '--incremental', '--cksum'], self.logdir / ('get_{:03d}.txt'.format(index)), watch_stage=True)
            self.message('Checking exact staged file set, source-size bounds, and local MD5s...')
            records = self.local_records()
            atomic_json(self.download_receipt, {'binding': self.binding, 'status': 'downloaded', 'objects': records,
                                               'downloaded_utc': datetime.datetime.utcnow().isoformat() + 'Z'})
        missing = self.verify_target(records, allow_missing=True)
        if missing:
            response = self.cli(['os', 'object', 'bulk-upload', '--namespace-name', self.namespace,
                                 '--bucket-name', self.config['bucket'], '--src-dir', str(self.stage),
                                 '--object-prefix', '',
                                 '--parallel-upload-count', str(self.job['limits']['upload_parallel']), '--no-overwrite',
                                 '--no-follow-symlinks', '--no-multipart', '--verify-checksum'], 'bulk_upload', watch=True)
            if not isinstance(response, dict) or 'upload-failures' not in response or response['upload-failures']:
                stop('Bulk upload did not return a clean upload-failures result. Review logs; staging retained.')
        self.verify_target(records)
        receipt = {'binding': self.binding, 'status': 'verified', 'objects': records,
                   'verified_utc': datetime.datetime.utcnow().isoformat() + 'Z', 'logs': str(self.logdir)}
        atomic_json(self.receipt, receipt)
        self.message('VERIFIED: {} objects match target by exact name, byte size, and whole-file MD5. Receipt: {}'.format(len(records), self.receipt))
        if cleanup:
            self.cleanup(receipt)
        else:
            self.message('Staging retained. Use the separate cleanup command, or run-range --cleanup-verified after reviewing this pilot.')

    def validate_receipt(self, receipt, expected_status='verified'):
        if receipt.get('binding') != self.binding or receipt.get('status') != expected_status:
            stop('Receipt binding/status mismatch; no cleanup or completion permitted.')
        if set(receipt.get('objects', {})) != set(self.expected):
            stop('Receipt does not cover the exact planned file set.')
        for record in receipt['objects'].values():
            if type(record.get('size')) is not int or record['size'] < 0 or not canonical_md5(record.get('md5')):
                stop('Invalid size/checksum in receipt.')

    def cleanup(self, receipt=None):
        if receipt is None:
            if not self.receipt.is_file():
                stop('Cleanup requires a verified receipt; no cleanup performed.')
            receipt = read_json(self.receipt, private=True)
        self.validate_receipt(receipt)
        if not self.stage.exists():
            self.verify_target(receipt['objects'])
            self.message('Stage already absent; target receipt rechecked.')
            return
        if not self.owner.is_file() or read_json(self.owner, private=True) != self.binding:
            stop('Stage ownership marker mismatch; no cleanup performed.')
        files, total = self.scan_stage()
        if total >= self.cap or set(files) - set(self.expected):
            stop('Unexpected files or size in local stage; no cleanup performed.')
        for name, item in files.items():
            if {'size': item['size'], 'md5': md5(item['path'])} != receipt['objects'][name]:
                stop('Local staged content changed since verification; no cleanup performed: ' + name)
        self.verify_target(receipt['objects'])
        # Only this exact, owned, validated batch directory is removed.
        shutil.rmtree(str(self.stage))
        self.message('Removed verified local stage only: ' + str(self.stage))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='command')
    for command in ('preview', 'run', 'cleanup'):
        child = sub.add_parser(command)
        child.add_argument('batch', type=int)
    child = sub.add_parser('run-range')
    child.add_argument('--start', type=int, required=True)
    child.add_argument('--end', type=int)
    child.add_argument('--cleanup-verified', action='store_true')
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return
    os.umask(0o077)
    companion = Companion(args)
    if args.command == 'run-range':
        if not args.cleanup_verified:
            stop('A range requires --cleanup-verified; failed batches are retained.')
        plan = read_json(companion.plan_dir / 'plan.json')
        end = args.end if args.end is not None else len(plan['batches'])
        if not 1 <= args.start <= end <= len(plan['batches']):
            stop('Invalid batch range.')
        for batch in range(args.start, end + 1):
            companion.select(batch)
            companion.run(cleanup=True)
        companion.message('Selected plan completed and verified. Fresh source/destination reconciliation and restore validation remain separate.')
    else:
        companion.select(args.batch)
        getattr(companion, args.command)()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)
    try:
        main()
    except TransferInterrupted as error:
        print('STOP: {}. Active child group stopped; staging retained.'.format(error), file=sys.stderr)
        sys.exit(128 + error.number)
    except KeyboardInterrupt:
        print('STOP: interrupted. Owned staging retained; no unverified cleanup.', file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, subprocess.SubprocessError, ImportError, SyntaxError, AttributeError, KeyError) as error:
        print('STOP: ' + str(error), file=sys.stderr)
        sys.exit(1)
