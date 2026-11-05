"""Small SLS V1 client: official protobuf fields, bounded requests, no implicit retry.

Wire references: aliyun/aliyun-log-python-sdk log_logs.proto and auth.py;
https://help.aliyun.com/zh/sls/developer-reference/request-signatures/
"""
import base64
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import hmac
import http.client
import json
import re
import ssl
import zlib

MAX_RAW_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ROWS = 100


class SlsError(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class BatchTooLarge(SlsError):
    pass


def need(value, message):
    if not value:
        raise SlsError(message)


def _varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128); value >>= 7
    result.append(value)
    return bytes(result)


def _field(number, value):
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def encode_logs(logs):
    """Encode the documented LogGroup subset, never an arbitrary config object."""
    need(isinstance(logs, list) and 1 <= len(logs) <= MAX_ROWS, 'SLS batch must contain 1 to 100 logs')
    result = bytearray()
    for log in logs:
        need(isinstance(log, dict) and set(log) == {'time', 'contents'}, 'Invalid SLS log fields')
        need(type(log['time']) is int and 0 <= log['time'] < 2 ** 32, 'Invalid SLS log time')
        contents = log['contents']
        need(isinstance(contents, dict) and 1 <= len(contents) <= 64, 'Invalid SLS log contents')
        encoded = bytearray(b'\x08' + _varint(log['time']))
        for key, value in contents.items():
            need(isinstance(key, str) and isinstance(value, str), 'SLS contents must be strings')
            need(1 <= len(key) <= 256 and len(value) <= 4096, 'SLS log field exceeds bounds')
            try:
                key_bytes, value_bytes = key.encode('utf-8'), value.encode('utf-8')
            except UnicodeError:
                raise SlsError('Invalid UTF-8 in SLS log contents') from None
            need(1 <= len(key_bytes) <= 256 and len(value_bytes) <= 4096, 'SLS log field exceeds bounds')
            encoded += _field(2, _field(1, key_bytes) + _field(2, value_bytes))
        result += _field(1, encoded)
        if len(result) > MAX_RAW_BYTES:
            raise BatchTooLarge('SLS batch exceeds 64 KiB raw protobuf limit')
    return bytes(result)


def _sign(method, path, body, headers, access_key_id, access_key_secret):
    """V1 canonical form; Date only, so no special x-log-date override is needed."""
    if body:
        headers['Content-MD5'] = hashlib.md5(body, usedforsecurity=False).hexdigest().upper()
    canonical = ''.join(key.lower() + ':' + value + '\n'
                        for key, value in sorted(headers.items(), key=lambda item: item[0].lower())
                        if key.lower().startswith(('x-log-', 'x-acs-')))
    message = '\n'.join([method, headers.get('Content-MD5', ''), headers.get('Content-Type', ''),
                         headers['Date'], canonical + path])
    signature = base64.b64encode(hmac.new(access_key_secret.encode('ascii'), message.encode('utf-8'), hashlib.sha1).digest()).decode('ascii')
    headers['Authorization'] = 'LOG ' + access_key_id + ':' + signature


class SlsClient:
    def __init__(self, endpoint, project, logstore, credentials, reserve):
        need(isinstance(endpoint, str) and re.fullmatch(r'cn-[a-z0-9]+(?:-[a-z0-9]+)*\.log\.aliyuncs\.com', endpoint),
             'Use a bare mainland-China SLS endpoint ending in .log.aliyuncs.com')
        need('-intranet.' not in endpoint and '-accelerate.' not in endpoint,
             'Use the ordinary public regional SLS endpoint')
        need(isinstance(project, str) and re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', project), 'Invalid SLS project name')
        need(isinstance(logstore, str) and re.fullmatch(r'[a-z][a-z0-9_-]{1,61}[a-z0-9]', logstore), 'Invalid SLS Logstore name')
        need(isinstance(credentials, dict) and set(credentials) <= {'access_key_id', 'access_key_secret', 'security_token'},
             'Invalid SLS credentials structure')
        for key in ['access_key_id', 'access_key_secret']:
            value = credentials.get(key)
            need(isinstance(value, str) and 1 <= len(value) <= 256 and all(33 <= ord(char) <= 126 for char in value),
                 'SLS credentials contain invalid characters')
        token = credentials.get('security_token')
        need(token is None or isinstance(token, str) and 1 <= len(token) <= 16384
             and all(33 <= ord(char) <= 126 for char in token), 'Invalid SLS security token')
        need(callable(reserve), 'SLS requires an explicit budget reservation callback')
        self.endpoint, self.project, self.logstore = endpoint, project, logstore
        self.host = project + '.' + endpoint
        self._credentials, self._reserve = dict(credentials), reserve

    def _request(self, method, path, body=b'', raw_bytes=0):
        headers = {'Host': self.host, 'Date': format_datetime(datetime.now(timezone.utc), usegmt=True),
                   'x-log-apiversion': '0.6.0', 'x-log-signaturemethod': 'hmac-sha1',
                   'x-log-bodyrawsize': str(raw_bytes), 'User-Agent': 'onePVE-sls/1'}
        if body:
            headers.update({'Content-Type': 'application/x-protobuf', 'x-log-compresstype': 'deflate'})
        if self._credentials.get('security_token'):
            headers['x-acs-security-token'] = self._credentials['security_token']
        _sign(method, path, body, headers, self._credentials['access_key_id'], self._credentials['access_key_secret'])
        self._reserve(raw_bytes=max(raw_bytes, len(body)), request_count=1)
        connection = None
        try:
            connection = http.client.HTTPSConnection(self.host, timeout=15, context=ssl.create_default_context())
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            status = response.status
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            request_id = response.getheader('x-log-requestid')
        except (OSError, http.client.HTTPException, ValueError):
            raise SlsError('SLS request outcome is unknown; no automatic retry was made') from None
        finally:
            if connection is not None:
                try: connection.close()
                except OSError: pass
        need(type(status) is int, 'Invalid SLS HTTP response')
        need(len(payload) <= MAX_RESPONSE_BYTES, 'SLS response exceeds 1 MiB limit')
        if status != 200:
            code = None
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict) and parsed.get('errorCode') == 'IndexConfigNotExist':
                    code = 'IndexConfigNotExist'
            except (ValueError, UnicodeError):
                pass
            # Never expose server messages: errors can echo credentials or uploaded contents.
            raise SlsError('SLS returned HTTP ' + str(status) + '; no automatic retry was made', status=status, code=code)
        request_id = request_id if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}', request_id) else None
        return payload, request_id

    def _get(self, suffix):
        payload, _ = self._request('GET', '/logstores/' + self.logstore + suffix)
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError):
            raise SlsError('SLS returned invalid metadata JSON') from None

    def preflight(self):
        """Read metadata only. This cannot establish an account's free quota."""
        project_body, _ = self._request('GET', '/')
        try:
            project = json.loads(project_body)
        except (ValueError, UnicodeError):
            raise SlsError('SLS returned invalid project metadata') from None
        need(isinstance(project, dict) and project.get('projectName') == self.project
             and project.get('region') == self.endpoint.split('.')[0] and project.get('status') == 'Normal',
             'SLS project identity or region could not be verified')
        owner = project.get('owner')
        need(isinstance(owner, str) and re.fullmatch(r'[0-9]{16}', owner), 'SLS project account owner could not be verified')
        store = self._get('')
        need(isinstance(store, dict) and store.get('logstoreName') == self.logstore, 'SLS Logstore identity differs')
        need(store.get('mode') == 'standard', 'SLS requires the standard Logstore mode')
        need(type(store.get('ttl')) is int and store['ttl'] == 3, 'SLS retention must be exactly 3 days')
        need(type(store.get('shardCount')) is int and store['shardCount'] == 1, 'SLS configured shard count must be 1')
        need(store.get('autoSplit') is False and store.get('enable_tracking') is False,
             'SLS auto-split and WebTracking must be disabled')
        need(store.get('processorId') in (None, ''), 'SLS ingestion processors are not allowed')
        encryption = store.get('encrypt_conf')
        need(encryption is None or isinstance(encryption, dict) and (not encryption or
             encryption.get('enable') is False and encryption.get('user_cmk_info') in (None, {})
             and encryption.get('encrypt_type') in (None, 'default')
             and set(encryption) <= {'enable', 'encrypt_type', 'user_cmk_info'}),
             'SLS custom or unknown encryption configuration is refused to avoid separate KMS charges')
        mode = self._get('/meteringmode')
        need(isinstance(mode, dict) and mode.get('meteringMode') == 'ChargeByFunction',
             'SLS must use pay-by-feature billing; unknown or ingest-based billing is refused')
        shards = self._get('/shards')
        need(isinstance(shards, list) and shards and all(isinstance(item, dict) and item.get('status') in ('readwrite', 'readonly') for item in shards),
             'SLS shard metadata is unknown')
        need(sum(item['status'] == 'readwrite' for item in shards) == 1, 'SLS actual active shard count must be 1')
        try:
            self._get('/index')
        except SlsError as error:
            if error.status != 404 or error.code != 'IndexConfigNotExist':
                raise
        else:
            raise SlsError('SLS index exists; this cost-limited uploader requires no index')
        return {'status': 'PREFLIGHT_OK', 'project_owner': owner, 'metering_mode': 'ChargeByFunction',
                'retention_days': 3, 'active_shards': 1, 'index': 'absent', 'free_quota_verified': False}

    def put_logs(self, logs):
        raw = encode_logs(logs)
        body = zlib.compress(raw)
        _, request_id = self._request('POST', '/logstores/' + self.logstore + '/shards/lb', body, len(raw))
        return {'request_id': request_id, 'raw_bytes': len(raw), 'rows': len(logs)}
