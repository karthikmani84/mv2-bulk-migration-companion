"""Offline configuration tests; no credentials or cloud access."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import migration_config as config


class Configuration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'dbhome'
        self.home.mkdir()
        for name in ('mv2', 'oci', 'opc.ora', 'wallet.sso', 'oci.conf', 'ca.pem'):
            path = self.root / name
            path.write_text('synthetic fixture\n')
            path.chmod(0o700)
        self.path = self.root / 'job.json'
        self.job = dict(format_version=1,
                        source=dict(mv2_binary='mv2', db_unique_name='SAMPLE_DB', dbhome='dbhome', opcfile='opc.ora', opcwallet='wallet.sso'),
                        target=dict(oci_binary='oci', endpoint='https://objectstorage.region.custom.example:8443/', region='custom-region-1', namespace='demo', bucket='backup', config_file='oci.conf', profile='TARGET', auth='api_key'),
                        paths=dict(plan_dir='plan', stage_root='stage', state_dir='state'),
                        limits=dict(batch_bytes=25000000000, reserve_bytes=5000000000, download_parallel=4, upload_parallel=8))

    def tearDown(self):
        self.temporary.cleanup()

    def load(self, job=None, check=True):
        self.path.write_text(json.dumps(self.job if job is None else job))
        return config.load_config(self.path, check_files=check)

    def test_normalizes_arbitrary_https_origin_and_relative_paths(self):
        job = self.load()
        self.assertEqual(job['target']['endpoint'], 'https://objectstorage.region.custom.example:8443')
        self.assertEqual(job['target']['oci_binary'], str(self.root / 'oci'))
        self.assertEqual(job['locale'], 'C.UTF-8')
        self.assertFalse(Path(job['paths']['plan_dir']).exists())

    def test_both_auth_modes_use_explicit_target_flags_even_help(self):
        for auth in ('api_key', 'instance_principal'):
            self.job['target']['auth'] = auth
            self.job['target']['cert_bundle'] = 'ca.pem'
            job = self.load()
            argv = config.target_command(job, ['os', 'object', 'bulk-upload', '--help'])
            for flag, key in (('--endpoint', 'endpoint'), ('--region', 'region'), ('--config-file', 'config_file'), ('--profile', 'profile'), ('--auth', 'auth'), ('--cert-bundle', 'cert_bundle')):
                self.assertEqual(argv[argv.index(flag) + 1], job['target'][key])
            self.assertEqual(argv[1:3], ['--cli-rc-file', '/dev/null'])

    def test_inherited_cli_overrides_are_removed(self):
        job = self.load()
        with mock.patch.dict(os.environ, dict(OCI_CLI_ENDPOINT='https://wrong.invalid', OCI_CLI_AUTH='security_token', OCI_CLI_SECURITY_TOKEN_FILE='/secret', OCI_CLI_DELEGATION_TOKEN_FILE='/secret2', OCI_CLI_CERT_BUNDLE='/wrong', OCI_CLI_EXTRA='surprise')):
            environment = config.tool_environment(job)
        expected = {'OCI_CLI_ENDPOINT', 'OCI_CLI_AUTH', 'OCI_CLI_CONFIG_FILE', 'OCI_CLI_PROFILE', 'OCI_CLI_REGION', 'OCI_CLI_RC_FILE'}
        self.assertEqual({key for key in environment if key.startswith('OCI_CLI_')}, expected)
        self.assertEqual(environment['OCI_CLI_ENDPOINT'], job['target']['endpoint'])
        self.assertEqual(environment['LC_ALL'], 'C.UTF-8')

    def test_source_only_list_get_with_explicit_paths(self):
        job = self.load()
        for verb in ('list', 'get'):
            argv = config.source_command(job, verb)
            self.assertEqual(argv[:2], [str(self.root / 'mv2'), verb])
            for flag, key in (('--dbhome', 'dbhome'), ('--opcfile', 'opcfile'), ('--opcwallet', 'opcwallet')):
                self.assertEqual(argv[argv.index(flag) + 1], job['source'][key])
            self.assertNotIn('--dbuniquename', argv)
        for verb in ('copy', 'put', 'load', 'delete'):
            with self.assertRaises(ValueError):
                config.source_command(job, verb)

    def test_bad_endpoints_rejected(self):
        for endpoint in ('http://host', 'https://u:p@host', 'https://host/path', 'https://host?q=a', 'https://host/#x', 'https://host name', 'https://host\\evil', 'https://host:99999', 'https://host\nname'):
            job = copy.deepcopy(self.job)
            job['target']['endpoint'] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.load(job)

    def test_shape_strict_numbers_auth_and_limits(self):
        mutations = [(('format_version',), True), (('limits', 'batch_bytes'), True), (('limits', 'reserve_bytes'), 0), (('limits', 'batch_bytes'), 50 * 1024 ** 3 + 1), (('limits', 'download_parallel'), 17), (('limits', 'upload_parallel'), 33), (('target', 'auth'), 'security_token')]
        for keys, value in mutations:
            job = copy.deepcopy(self.job)
            destination = job
            for key in keys[:-1]:
                destination = destination[key]
            destination[keys[-1]] = value
            with self.subTest(keys=keys, value=value), self.assertRaises(ValueError):
                self.load(job)
        job = copy.deepcopy(self.job)
        job['extra'] = True
        with self.assertRaises(ValueError):
            self.load(job)

    def test_required_files_and_duplicate_json(self):
        job = copy.deepcopy(self.job)
        del job['source']['opcfile']
        with self.assertRaises(ValueError):
            self.load(job)
        (self.root / 'wallet.sso').unlink()
        with self.assertRaises(ValueError):
            self.load()
        self.path.write_text('{"format_version":1,"format_version":1}')
        with self.assertRaises(ValueError):
            config.load_config(self.path)

    def test_separate_roots_and_credential_dbhome_protection(self):
        for section, key, value in (('paths', 'state_dir', 'stage/state'), ('source', 'dbhome', 'stage/home'), ('source', 'opcwallet', 'state/wallet'), ('paths', 'stage_root', '/tmp')):
            job = copy.deepcopy(self.job)
            job[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(job, check=False)

    def test_symlink_root_rejected_and_literal_dollar_supported(self):
        (self.root / 'stage').symlink_to(self.root / 'other', target_is_directory=True)
        with self.assertRaises(ValueError):
            self.load()
        (self.root / 'stage').unlink()
        self.job['source']['db_unique_name'] = 'DB$NAME'
        self.assertEqual(self.load()['source']['db_unique_name'], 'DB$NAME')

    def test_fingerprint_changes_when_context_changes(self):
        first = self.load()
        self.job['target']['region'] = 'another-region'
        second = self.load()
        self.assertNotEqual(config.digest(first), config.digest(second))
        before = config.file_identity(second)
        (self.root / 'wallet.sso').write_text('changed fixture')
        self.assertNotEqual(before, config.file_identity(second))


if __name__ == '__main__':
    unittest.main()
