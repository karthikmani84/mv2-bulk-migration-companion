"""Pure inventory reconciliation for captured MV2 and OCI output. Python 3.6+.

This module performs no file, process, network, or database operations. The
caller must preserve capture provenance, successful exit markers and timestamps.
Source sizes are rounded displays; object presence and bounds are not content
or restore verification.
"""
import base64
import binascii
import json
import re
from decimal import Decimal, localcontext


UNITS = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2,
         'GB': 1024 ** 3, 'TB': 1024 ** 4}
SIZE = re.compile(r'([0-9]+(?:\.[0-9]{1,2})?)\s+(B|KB|MB|GB|TB)\Z')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PROGRESS = re.compile(r' *\(Approx progress bar\) \[ *([0-9]{1,3})%\]\[[= ]*\] *\Z')
TOTAL = re.compile(r'\bTotal\s+Files\s*:\s*([^\s\u2502]+)', re.I)
COUNT = re.compile(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\Z')
NATIVE_ERROR = re.compile(r'\b(?:ERROR|FATAL)\b|Traceback\s*(?:\(|:)', re.I)


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _text(raw):
    if isinstance(raw, bytes):
        try:
            return raw.decode('utf-8-sig')
        except UnicodeError as exc:
            raise ValueError('Capture is not valid UTF-8: ' + str(exc))
    _require(isinstance(raw, str), 'Capture must be UTF-8 bytes or text')
    return raw[1:] if raw.startswith('\ufeff') else raw


def _name(value):
    _require(isinstance(value, str) and value, 'Object name must be a nonempty string')
    _require(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
             'Object name contains an unsafe control character')
    return value


def _size_row(name, display_size):
    _name(name)
    _require(isinstance(display_size, str), 'Missing displayed source size')
    match = SIZE.fullmatch(display_size)
    _require(match is not None, 'Unrecognized displayed source size: ' + repr(display_size))
    value, unit = match.groups()
    whole, unused_dot, fraction = value.partition('.')
    scale = 10 ** len(fraction)
    numerator = (int(whole) * scale + int(fraction or '0')) * UNITS[unit]
    # Construct exact decimals without context-dependent multiplication. The
    # native display has at most two decimal places, so bounds can use integer
    # centibytes even for sizes exceeding Decimal's default precision.
    nominal = Decimal((0, tuple(int(c) for c in str(numerator)), -len(fraction)))
    centibytes = numerator * (100 // scale)
    if unit == 'B':
        _require(centibytes % 100 == 0, 'Fractional byte size is invalid')
        lower = upper = centibytes // 100
    else:
        lower = max(0, (centibytes - UNITS[unit]) // 100)
        upper = (centibytes + UNITS[unit] + 99) // 100
    return dict(name=name, display_size=display_size, nominal_bytes=str(nominal),
                lower_bytes=lower, upper_bytes=upper)


def parse_source(raw):
    """Parse one complete, nonempty MV2 Unicode inventory and its row count."""
    text = ANSI.sub('', _text(raw))
    # Preserve original capture bytes; only this parser view handles display
    # formatting. Bare CR is accepted below solely for native progress records.
    text = text.replace('\r\n', '\n')
    _require(not any(ord(c) < 32 and c not in ('\n', '\t', '\r') or 127 <= ord(c) <= 159
                     for c in text), 'Source capture contains an unsafe control character')
    rows = {}
    header_line = None
    summary = []
    last_row_line = None
    for number, line in enumerate(text.split('\n'), 1):
        if '\r' in line:
            progress = [part for part in line.split('\r') if part.strip(' ')]
            matches = [PROGRESS.fullmatch(part) for part in progress]
            _require(header_line is None and '\u2502' not in line and bool(progress)
                     and all(match is not None and 0 <= int(match.group(1)) <= 100 for match in matches),
                     'Unexpected carriage return outside native pre-table progress display')
            continue
        cells = line.split('\u2502')
        if len(cells) == 4:
            _require(not cells[0].strip() and not cells[3].strip(),
                     'Unexpected text around source table cells')
            _require('\t' not in cells[1] and '\t' not in cells[2],
                     'Control character inside a source table cell')
            name, displayed = cells[1].strip(' '), cells[2].strip(' ')
            header = (re.sub(r'\s+', '', name).lower(), re.sub(r'\s+', '', displayed).lower())
            if header == ('filename', 'filesize'):
                _require(header_line is None and not rows, 'Multiple source tables or headers')
                header_line = number
                continue
            _require(header_line is not None, 'Source object row appears before the table header')
            _require(not summary, 'Source table continues after Total Files summary')
            row = _size_row(name, displayed)
            _require(name not in rows, 'Duplicate source object name: ' + name)
            rows[name] = row
            last_row_line = number
        else:
            _require(not NATIVE_ERROR.search(line), 'Native source error or traceback in capture')
            if header_line is not None and '\u2502' in line:
                _require(all(c.isspace() or c in '-=+' or 0x2500 <= ord(c) <= 0x259f for c in line),
                         'Malformed or truncated source table row')
            for match in TOTAL.finditer(line):
                value = match.group(1)
                _require(COUNT.fullmatch(value) is not None, 'Malformed Total Files count')
                summary.append((number, int(value.replace(',', ''))))
    _require(header_line is not None, 'Missing MV2 File Name / File Size table header')
    _require(len(summary) == 1, 'Source capture must contain exactly one Total Files summary')
    _require(bool(rows), 'Empty source inventory cannot establish migration completeness')
    _require(summary[0][0] > last_row_line and summary[0][1] == len(rows),
             'Total Files differs from the complete parsed table')
    return rows


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError('Nonstandard JSON numeric constant: ' + value)


def _md5(value):
    if value is None:
        return None
    _require(isinstance(value, str), 'Target MD5 must be canonical base64 or null')
    try:
        encoded = value.encode('ascii')
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        raise ValueError('Malformed target MD5')
    _require(len(decoded) == 16 and base64.b64encode(decoded).decode('ascii') == value,
             'Target MD5 is not canonical whole-file MD5 base64')
    return value


def _target_row(row):
    _require(isinstance(row, dict), 'Invalid target object record')
    name = _name(row.get('name'))
    size = row.get('size')
    _require(type(size) is int and size >= 0, 'Target size must be a nonnegative integer')
    return dict(name=name, size=size, md5=_md5(row.get('md5')))


def parse_target(raw):
    """Parse a complete raw OCI --all object-list response, never a wrapper."""
    try:
        data = json.loads(_text(raw), object_pairs_hook=_unique_json, parse_constant=_bad_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid target JSON: ' + str(exc))
    _require(isinstance(data, dict), 'Target inventory must be an OCI JSON object')
    _require('scope' not in data and 'inventory' not in data,
             'Target scope wrappers are not raw full OCI inventory output')
    if data == {'prefixes': []}:
        return {}
    _require(isinstance(data.get('data'), list), 'Target inventory lacks an OCI data list')
    _require(data.get('prefixes') in (None, []), 'Common-prefix/delimiter target listing is incomplete')
    for key, value in data.items():
        normalized = key.lower().replace('_', '-')
        if 'next' in normalized and ('page' in normalized or 'start' in normalized):
            _require(value is None or value == '', 'Target pagination remains; complete --all output is required')
    rows = {}
    for original in data['data']:
        row = _target_row(original)
        _require(row['name'] not in rows, 'Duplicate target object name: ' + row['name'])
        rows[row['name']] = row
    return rows


def _source_map(rows):
    _require(isinstance(rows, dict) and rows, 'Parsed source inventory must be a nonempty dictionary')
    for name, row in rows.items():
        _require(isinstance(row, dict) and row.get('name') == name, 'Source mapping/name differs')
        expected = _size_row(name, row.get('display_size'))
        _require(all(row.get(key) == value and (key not in ('lower_bytes', 'upper_bytes') or type(row.get(key)) is int)
                     for key, value in expected.items()), 'Parsed source size bounds are inconsistent')
    return rows


def _target_map(rows):
    _require(isinstance(rows, dict), 'Parsed target inventory must be a dictionary')
    normalized = {}
    for name, row in rows.items():
        _require(isinstance(row, dict) and row.get('name') == name, 'Target mapping/name differs')
        normalized[name] = _target_row(row)
    return normalized


def _sum_nominal_bytes(rows):
    values = [Decimal(row['nominal_bytes']) for row in rows.values()]
    with localcontext() as context:
        context.prec = max(len(value.as_tuple().digits) for value in values) + len(str(len(values))) + 5
        return str(sum(values, Decimal(0)))


def compare_inventory(before, after, target):
    """Compare literal current names and rounded source-size bounds, without I/O."""
    _source_map(before)
    _source_map(after)
    target = _target_map(target)
    old, current, present = set(before), set(after), set(target)
    changes = dict(added_names=sorted(current - old), removed_names=sorted(old - current),
                   changed_sizes=[dict(name=name, before_display_size=before[name]['display_size'],
                                       after_display_size=after[name]['display_size'])
                                  for name in sorted(old & current)
                                  if before[name]['display_size'] != after[name]['display_size']])
    missing = sorted(current - present)
    overlap = current & present
    mismatches = [dict(name=name, source_display_size=after[name]['display_size'],
                       lower_bytes=after[name]['lower_bytes'], upper_bytes=after[name]['upper_bytes'],
                       target_size=target[name]['size'])
                  for name in sorted(overlap)
                  if not after[name]['lower_bytes'] <= target[name]['size'] <= after[name]['upper_bytes']]
    stable = not any(changes.values())
    return dict(source_before_objects=len(before), source_after_objects=len(after), target_objects=len(target),
                source_changes=changes, source_stable=stable, missing_target_names=missing,
                size_mismatches=mismatches, target_extra_names=sorted(present - current),
                missing_target_md5_names=sorted(name for name in overlap if target[name]['md5'] is None),
                source_after_present_objects=len(overlap),
                source_before_nominal_bytes=_sum_nominal_bytes(before),
                source_after_nominal_bytes=_sum_nominal_bytes(after),
                source_after_target_overlap_bytes=sum(target[name]['size'] for name in overlap),
                target_total_bytes=sum(x['size'] for x in target.values()),
                all_source_names_present=not missing,
                inventory_name_and_size_complete=stable and not missing and not mismatches,
                source_content_verified=False, restore_verified=False,
                size_basis='Source sizes are rounded binary-unit displays; target sizes are exact integer bytes.',
                cutoff_basis='Two non-atomic source listings and one target listing; caller must bind capture times, full scope and successful exits.',
                checksum_basis='Target MD5 availability is reported; matching names and size bounds do not establish current source content equality.')
