"""Explicit, realm-neutral job configuration. Python 3.6+ standard library."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit


DEFAULT_BATCH_BYTES = 25000000000
MAX_SINGLE_OBJECT_BYTES = 50 * 1024 ** 3


def require(ok, message):
    if not ok:
        raise ValueError(message)


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _bad_constant(unused):
    raise ValueError('Non-finite configuration number')


def digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()


def config_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(value, label):
    require(type(value) is str and bool(value) and value == value.strip()
            and not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value),
            'Invalid nonempty text: ' + label)
    return value


def _keys(value, required, optional, label):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            'Missing or unknown configuration fields in ' + label)


def no_symlink_components(path):
    path = Path(os.path.abspath(str(Path(path).expanduser())))
    for component in [path] + list(path.parents):
        require(not component.is_symlink(), 'Symlink in plan/stage/state path: ' + str(component))
    return path


def _path(value, base, label, protected=False):
    value = _text(value, label)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = no_symlink_components(path) if protected else path.resolve()
    return str(path)


def _origin(value):
    value = _text(value, 'target.endpoint')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('Target endpoint must be a valid HTTPS origin')
    require(parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 or character == '\\' for character in parsed.netloc)
            and (port is None or 1 <= port <= 65535), 'Target endpoint must be an HTTPS origin without credentials/path/query')
    return urlunsplit(('https', parsed.netloc.lower(), '', '', ''))


def load_config(path, check_files=True):
    location = Path(path).expanduser().resolve()
    config = json.loads(location.read_text(encoding='utf-8'), object_pairs_hook=unique_json, parse_constant=_bad_constant)
    _keys(config, ('format_version', 'source', 'target', 'paths', 'limits'), ('locale',), 'job')
    require(type(config['format_version']) is int and config['format_version'] == 1, 'Unsupported job format_version')
    source, target, paths, limits = (config[key] for key in ('source', 'target', 'paths', 'limits'))
    _keys(source, ('mv2_binary', 'db_unique_name', 'dbhome', 'opcfile', 'opcwallet'), (), 'source')
    _keys(target, ('oci_binary', 'endpoint', 'region', 'namespace', 'bucket', 'config_file', 'profile', 'auth'), ('cert_bundle',), 'target')
    _keys(paths, ('plan_dir', 'stage_root', 'state_dir'), (), 'paths')
    _keys(limits, ('batch_bytes', 'reserve_bytes', 'download_parallel', 'upload_parallel'), (), 'limits')
    result = dict(format_version=1, source={}, target={}, paths={}, limits={}, locale=config.get('locale', 'C.UTF-8'))
    _text(result['locale'], 'locale')
    require(re.fullmatch(r'[A-Za-z0-9_.@-]+', result['locale']) is not None, 'Invalid locale name')
    for key in ('mv2_binary', 'dbhome', 'opcfile', 'opcwallet'):
        result['source'][key] = _path(source[key], location.parent, 'source.' + key)
    result['source']['db_unique_name'] = _text(source['db_unique_name'], 'source.db_unique_name')
    for key in ('oci_binary', 'config_file'):
        result['target'][key] = _path(target[key], location.parent, 'target.' + key)
    for key in ('region', 'namespace', 'bucket', 'profile'):
        result['target'][key] = _text(target[key], 'target.' + key)
    result['target']['endpoint'] = _origin(target['endpoint'])
    require(target['auth'] in ('api_key', 'instance_principal') and type(target['auth']) is str,
            'Version 1 supports auth api_key or instance_principal only')
    result['target']['auth'] = target['auth']
    if target.get('cert_bundle') is not None:
        result['target']['cert_bundle'] = _path(target['cert_bundle'], location.parent, 'target.cert_bundle')
    for key in ('plan_dir', 'stage_root', 'state_dir'):
        result['paths'][key] = _path(paths[key], location.parent, 'paths.' + key, protected=True)
    roots = [Path(result['paths'][key]) for key in ('plan_dir', 'stage_root', 'state_dir')]
    forbidden = {'/', '/root', '/u01', '/tmp', '/var/tmp', '/dev/shm'}
    require(all(str(root) not in forbidden for root in roots), 'Use dedicated plan/stage/state directories')
    require(all(a != b and a not in b.parents and b not in a.parents for index, a in enumerate(roots) for b in roots[index + 1:]),
            'Plan, staging and state directories must be separate and must not contain one another')
    for key in ('batch_bytes', 'reserve_bytes', 'download_parallel', 'upload_parallel'):
        require(type(limits[key]) is int and limits[key] > 0, 'Limits must be positive integers: ' + key)
        result['limits'][key] = limits[key]
    require(limits['batch_bytes'] <= MAX_SINGLE_OBJECT_BYTES, 'Version 1 batch cap must not exceed the 50 GiB single-part limit')
    require(limits['download_parallel'] <= 16 and limits['upload_parallel'] <= 32, 'Parallel limits: download1..16, upload1..32')
    protected_files = [location] + [Path(result['source'][key]) for key in ('mv2_binary', 'opcfile', 'opcwallet')]
    protected_files += [Path(result['target'][key]) for key in ('oci_binary', 'config_file')]
    if 'cert_bundle' in result['target']:
        protected_files.append(Path(result['target']['cert_bundle']))
    for value in protected_files + [Path(result['source']['dbhome'])]:
        require(all(value != root and root not in value.parents for root in roots[1:]),
                'Tools, credentials and job config must stay outside staging and state: ' + str(value))
    if check_files:
        require(Path(result['source']['dbhome']).is_dir(), 'Source dbhome is not a directory')
        for value in protected_files[1:]:
            require(value.is_file() and os.access(str(value), os.R_OK), 'Required readable tool/credential file is missing: ' + str(value))
        for value in (result['source']['mv2_binary'], result['target']['oci_binary']):
            require(os.access(value, os.X_OK), 'Configured tool is not executable: ' + value)
        for root in roots:
            require(not root.exists() or root.is_dir(), 'A plan/stage/state path is not a directory: ' + str(root))
    return result


def source_command(config, verb):
    require(verb in ('list', 'get'), 'MV2 is used only for source LIST/GET')
    source = config['source']
    return [source['mv2_binary'], verb, '--dbhome', source['dbhome'], '--opcfile', source['opcfile'], '--opcwallet', source['opcwallet']]


def target_command(config, arguments):
    target = config['target']
    command = [target['oci_binary'], '--cli-rc-file', '/dev/null', '--endpoint', target['endpoint'],
               '--region', target['region'], '--config-file', target['config_file'], '--profile', target['profile'], '--auth', target['auth']]
    if target.get('cert_bundle'):
        command += ['--cert-bundle', target['cert_bundle']]
    return command + list(arguments)


def tool_environment(config):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('OCI_CLI_')}
    target = config['target']
    environment.update(PAGER='cat', LC_ALL=config['locale'], LANG=config['locale'], OCI_CLI_RC_FILE='/dev/null',
                       OCI_CLI_ENDPOINT=target['endpoint'], OCI_CLI_REGION=target['region'],
                       OCI_CLI_CONFIG_FILE=target['config_file'], OCI_CLI_PROFILE=target['profile'], OCI_CLI_AUTH=target['auth'])
    if target.get('cert_bundle'):
        environment['OCI_CLI_CERT_BUNDLE'] = target['cert_bundle']
    return environment


def file_identity(config):
    """Opaque content hashes; never includes credential contents."""
    files = {key: config['source'][key] for key in ('mv2_binary', 'opcfile', 'opcwallet')}
    files.update({'oci_binary': config['target']['oci_binary'], 'target_config': config['target']['config_file']})
    if config['target'].get('cert_bundle'):
        files['cert_bundle'] = config['target']['cert_bundle']
    return {key: dict(path=value, sha256=config_sha256(value)) for key, value in files.items()}
