"""Known protobuf/signature vectors and a fake HTTPS peer; never calls Alibaba Cloud."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import ssl
import sys
import types
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_sls_wire as wire

CREDENTIALS = dict(access_key_id='onepve-test-id', access_key_secret='onepve-test-secret', security_token='onepve-test-token')
LOGS = [dict(time=1, contents={'k': 'v'})]
PROJECT = dict(owner='1234567890123456', projectName='onepve-test', region='cn-hangzhou', status='Normal')
STORE = dict(logstoreName='audit-test', mode='standard', ttl=3, shardCount=1, autoSplit=False, enable_tracking=False)


def response(value=b'', status=200, request_id='test-request-123'):
    payload = value if isinstance(value, bytes) else json.dumps(value).encode()
    return types.SimpleNamespace(status=status, read=lambda size: payload[:size], getheader=lambda key: request_id)


def client(reserve=None, **changes):
    values = dict(endpoint='cn-hangzhou.log.aliyuncs.com', project='onepve-test', logstore='audit-test',
                  credentials=CREDENTIALS, reserve=reserve or mock.Mock())
    values.update(changes)
    return wire.SlsClient(**values)


class SlsWireTests(unittest.TestCase):
    def test_protobuf_uses_official_loggroup_field_numbers(self):
        # log_logs.proto: group.Logs=1, Log.Time=1, Log.Contents=2, Content.Key/Value=1/2.
        self.assertEqual(wire.encode_logs(LOGS).hex(), '0a0a080112060a016b120176')
        utf8 = wire.encode_logs([dict(time=128, contents={'姓名': '测试'})])
        self.assertIn(b'\x08\x80\x01', utf8)
        self.assertIn('姓名'.encode(), utf8); self.assertIn('测试'.encode(), utf8)
        self.assertEqual(len(wire.encode_logs(LOGS * 100)), 1200)

    def test_invalid_or_oversized_logs_never_reserve_or_connect(self):
        invalid = [[], LOGS * 101, [{'time': True, 'contents': {'k': 'v'}}],
                   [{'time': -1, 'contents': {'k': 'v'}}], [{'time': 2**32, 'contents': {'k': 'v'}}],
                   [{'time': 1, 'contents': {}}], [{'time': 1, 'contents': {'k': 1}}],
                   [{'time': 1, 'contents': {'k': '\ud800'}}],
                   [{'time': 1, 'contents': {'k': 'v'}, 'credential': 'never-upload'}],
                   [{'time': 1, 'contents': {str(i): 'x' * 4096 for i in range(17)}}]]
        for logs in invalid:
            reserve = mock.Mock()
            with self.subTest(logs=str(logs)[:40]), mock.patch.object(wire.http.client, 'HTTPSConnection') as connect:
                with self.assertRaises(wire.SlsError): client(reserve).put_logs(logs)
                reserve.assert_not_called(); connect.assert_not_called()
        with self.assertRaises(wire.BatchTooLarge):
            wire.encode_logs([dict(time=1, contents={str(i): 'x' * 4096 for i in range(17)})])

    def test_v1_signature_matches_documented_canonical_form_golden(self):
        headers = {'Content-Type': 'application/json', 'Date': 'Tue, 23 Aug 2022 12:12:03 GMT',
                   'x-log-apiversion': '0.6.0', 'x-log-bodyrawsize': '0', 'x-log-signaturemethod': 'hmac-sha1'}
        wire._sign('POST', '/logstores/test-logstore/shards/0?action=split', b'{"hello": "world"}',
                   headers, 'testAccessKeyId', 'onepve-test-secret')
        self.assertEqual(headers['Content-MD5'], '49DFDD54B01CBCD2D2AB5E9E5EE6B9B9')
        self.assertEqual(headers['Authorization'], 'LOG testAccessKeyId:ZwEuvB6gTgEAbcQ2f5mm7ceBUXE=')

    def test_sts_token_is_signed_and_never_part_of_error_text(self):
        base = {'Date': 'Tue, 23 Aug 2022 12:12:03 GMT', 'x-log-apiversion': '0.6.0'}
        signed = dict(base, **{'x-acs-security-token': CREDENTIALS['security_token']})
        wire._sign('GET', '/', b'', base, CREDENTIALS['access_key_id'], CREDENTIALS['access_key_secret'])
        wire._sign('GET', '/', b'', signed, CREDENTIALS['access_key_id'], CREDENTIALS['access_key_secret'])
        self.assertNotEqual(base['Authorization'], signed['Authorization'])
        with mock.patch.object(wire.http.client, 'HTTPSConnection', side_effect=OSError(str(CREDENTIALS))):
            with self.assertRaises(wire.SlsError) as error: client().put_logs(LOGS)
        for secret in CREDENTIALS.values(): self.assertNotIn(secret, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_endpoint_names_and_header_values_reject_injection(self):
        bad = [dict(endpoint=x) for x in ['https://cn-hangzhou.log.aliyuncs.com', 'localhost', '127.0.0.1',
               'cn-hangzhou.log.aliyuncs.com:443', 'cn-hangzhou.log.aliyuncs.com/anything',
               'log-global.aliyuncs.com', 'cn-hangzhou.log.aliyuncs.com.evil', 'us-east-1.log.aliyuncs.com',
               'cn-hangzhou-intranet.log.aliyuncs.com']]
        bad += [dict(project=x) for x in ['ab', 'my.project', 'my-project\r\nHost: evil', '-project', 'bad_name']]
        bad += [dict(logstore=x) for x in ['bad/name', 'a?bad', 'Bad-Case']]
        bad += [dict(credentials=dict(CREDENTIALS, security_token='token\r\nInjected: value'))]
        for changes in bad:
            with self.subTest(changes=changes), mock.patch.object(wire.http.client, 'HTTPSConnection') as connect:
                with self.assertRaises(wire.SlsError): client(**changes)
                connect.assert_not_called()

    def test_put_reserves_raw_bytes_before_one_deflated_https_request(self):
        events = []
        reserve = lambda **values: events.append(('reserve', values))
        connection = mock.Mock()
        connection.request.side_effect = lambda *args, **kwargs: events.append(('request', args, kwargs))
        connection.getresponse.return_value = response()
        with mock.patch.object(wire.http.client, 'HTTPSConnection', return_value=connection) as connect:
            result = client(reserve).put_logs(LOGS)
        self.assertEqual(events[1][1], ('POST', '/logstores/audit-test/shards/lb'))
        kwargs = events[1][2]
        self.assertEqual(zlib.decompress(kwargs['body']), wire.encode_logs(LOGS))
        self.assertGreater(len(kwargs['body']), 12)  # Tiny protobuf grows under deflate.
        self.assertEqual(events[0], ('reserve', {'raw_bytes': len(kwargs['body']), 'request_count': 1}))
        self.assertEqual(kwargs['headers']['Content-MD5'], hashlib.md5(kwargs['body']).hexdigest().upper())
        self.assertEqual(kwargs['headers']['x-log-bodyrawsize'], '12')
        self.assertEqual(kwargs['headers']['x-log-compresstype'], 'deflate')
        self.assertEqual(result, dict(request_id='test-request-123', raw_bytes=12, rows=1))
        context = connect.call_args.kwargs['context']
        self.assertTrue(context.check_hostname); self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(connect.call_args.args, ('onepve-test.cn-hangzhou.log.aliyuncs.com',))
        connection.close.assert_called_once()

    def test_budget_refusal_happens_before_connect(self):
        reserve = mock.Mock(side_effect=RuntimeError('budget unavailable'))
        with mock.patch.object(wire.http.client, 'HTTPSConnection') as connect:
            with self.assertRaisesRegex(RuntimeError, 'budget unavailable'): client(reserve).put_logs(LOGS)
        reserve.assert_called_once(); connect.assert_not_called()

    def test_timeout_redirect_and_server_error_do_not_retry_or_refund(self):
        for failure in [socket.timeout('timeout ' + CREDENTIALS['access_key_secret']), 301, 307, 403, 500, 503]:
            reserve = mock.Mock(); connection = mock.Mock()
            if isinstance(failure, Exception): connection.request.side_effect = failure
            else: connection.getresponse.return_value = response({'errorCode': 'Unauthorized', 'errorMessage': str(CREDENTIALS)}, status=failure)
            with self.subTest(failure=type(failure).__name__), mock.patch.object(wire.http.client, 'HTTPSConnection', return_value=connection):
                with self.assertRaises(wire.SlsError) as caught: client(reserve).put_logs(LOGS)
            self.assertEqual(connection.request.call_count, 1); self.assertEqual(reserve.call_count, 1)
            for secret in CREDENTIALS.values(): self.assertNotIn(secret, str(caught.exception))

    def test_response_is_bounded(self):
        reserve = mock.Mock(); connection = mock.Mock()
        connection.getresponse.return_value = response(b'x' * (wire.MAX_RESPONSE_BYTES + 1))
        with mock.patch.object(wire.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(wire.SlsError, '1 MiB'): client(reserve).put_logs(LOGS)
        reserve.assert_called_once(); connection.request.assert_called_once()

    def preflight_responses(self, project=None, store=None, metering=None, shards=None, index=None):
        return [response(PROJECT if project is None else project), response(STORE if store is None else store),
                response({'meteringMode': 'ChargeByFunction'} if metering is None else metering),
                response([{'shardID': 0, 'status': 'readwrite'}] if shards is None else shards),
                index or response({'errorCode': 'IndexConfigNotExist'}, status=404)]

    def preflight(self, **changes):
        reserve = mock.Mock(); connection = mock.Mock()
        connection.getresponse.side_effect = self.preflight_responses(**changes)
        with mock.patch.object(wire.http.client, 'HTTPSConnection', return_value=connection):
            result = client(reserve).preflight()
        return result, reserve, connection

    def test_preflight_is_five_readonly_requests_not_free_quota_proof(self):
        result, reserve, connection = self.preflight()
        self.assertEqual(result['project_owner'], PROJECT['owner']); self.assertFalse(result['free_quota_verified'])
        self.assertEqual(reserve.call_count, 5)
        self.assertEqual([call.args[:2] for call in connection.request.call_args_list],
                         [('GET', '/'), ('GET', '/logstores/audit-test'), ('GET', '/logstores/audit-test/meteringmode'),
                          ('GET', '/logstores/audit-test/shards'), ('GET', '/logstores/audit-test/index')])
        for call in reserve.call_args_list: self.assertEqual(call.kwargs, dict(raw_bytes=0, request_count=1))

    def test_preflight_refuses_costly_unknown_or_unverifiable_metadata(self):
        stores = [{'ttl': 30}, {'ttl': True}, {'shardCount': 2}, {'shardCount': True}, {'autoSplit': True},
                  {'enable_tracking': True}, {'mode': 'query'}, {'mode': 'lite'}, {'mode': None},
                  {'processorId': 'ingest-processor'}, {'processorId': {}}]
        cases = [dict(store=dict(STORE, **change)) for change in stores]
        cases += [dict(project=dict(PROJECT, **change)) for change in [{'owner': ''}, {'owner': 1234567890123456},
                  {'region': 'cn-beijing'}, {'projectName': 'different-project'}, {'status': 'Deleting'}]]
        cases += [dict(metering={'meteringMode': 'ChargeByDataIngest'}), dict(metering={}),
                  dict(shards=[{'status': 'readwrite'}, {'status': 'readwrite'}]), dict(shards=[{'status': {}}]),
                  dict(shards=[]), dict(index=response({})), dict(index=response({'errorCode': 'LogStoreNotExist'}, status=404)),
                  dict(index=response({'errorCode': 'IndexConfigNotExist'}, status=403))]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(wire.SlsError): self.preflight(**changes)

    def test_bad_json_or_wrong_response_shape_is_sanitized(self):
        for payload in [b'bad secret ' + CREDENTIALS['access_key_secret'].encode(), b'[]', b'null']:
            connection = mock.Mock(); connection.getresponse.return_value = response(payload)
            with self.subTest(payload=payload), mock.patch.object(wire.http.client, 'HTTPSConnection', return_value=connection):
                with self.assertRaises(wire.SlsError) as error: client().preflight()
            self.assertNotIn(CREDENTIALS['access_key_secret'], str(error.exception))

    def test_preflight_refuses_custom_or_unknown_kms_encryption(self):
        for encryption in [None, {}, {'enable': False}, {'enable': False, 'encrypt_type': 'default', 'user_cmk_info': {}}]:
            with self.subTest(allowed=encryption):self.preflight(store=dict(STORE, encrypt_conf=encryption))
        for encryption in [False, [], {'enable': True}, {'enable': True, 'encrypt_type': 'default'},
                           {'enable': False, 'user_cmk_info': {'cmk_key_id': 'not-uploaded'}},
                           {'encrypt_enable': False}, {'enable': False, 'unknown_option': True},
                           {'enable': False, 'encrypt_type': 'user'}]:
            with self.subTest(refused=encryption), self.assertRaises(wire.SlsError):
                self.preflight(store=dict(STORE, encrypt_conf=encryption))


if __name__ == '__main__': unittest.main()
