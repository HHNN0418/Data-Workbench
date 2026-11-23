import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ctn_data import proxy_status


class ProxyStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path=Path(self.temp.name)/'control.toml'
        self.path.write_text('[[roles]]\nname="test"\nroutes=["GET /v1/vpn/status","GET /v1/publicip/ip"]\nauth="apikey"\napikey="'+('a'*64)+'"\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_two_distinct_exits_cache_and_no_key_in_response(self):
        def status(node,key):
            self.assertEqual(key,'a'*64)
            return {'worker':node[0],'public_ip':'1.1.1.1' if node[2]==18001 else '8.8.8.8','vpn_status':'running'}
        with patch.object(proxy_status,'_node_status',side_effect=status) as query:
            result=proxy_status.get_proxy_status(self.path,refresh=True)
            cached=proxy_status.get_proxy_status(self.path)
        self.assertTrue(result['distinct_exits'])
        self.assertEqual(query.call_count,2)
        self.assertEqual(result,cached)
        self.assertNotIn('a'*64,json.dumps(result))

    def test_unconfigured_does_not_query(self):
        with patch.object(proxy_status,'_node_status') as query:
            result=proxy_status.get_proxy_status(self.path.with_name('missing'))
        self.assertFalse(result['configured'])
        query.assert_not_called()

    def test_readonly_role_rejects_extra_write_permission(self):
        self.path.write_text(self.path.read_text().replace('"GET /v1/vpn/status"','"PUT /v1/vpn/status"'))
        self.assertFalse(proxy_status.get_proxy_status(self.path,refresh=True)['configured'])

    def test_requests_only_two_fixed_get_routes_and_redacts_extra_fields(self):
        opener=Mock()
        def respond(request,timeout):
            self.assertEqual(request.method,'GET')
            self.assertTrue(request.full_url.startswith('http://127.0.0.1:18001/'))
            payload={'status':'running','password':'should-not-expose'} if request.full_url.endswith('/vpn/status') else {'public_ip':'1.1.1.1'}
            response=Mock()
            response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            response.read.return_value=json.dumps(payload).encode()
            return response
        opener.open.side_effect=respond
        with patch.object(proxy_status,'build_opener',return_value=opener):
            result=proxy_status._node_status(proxy_status.NODES[0],'a'*64)
        self.assertEqual(opener.open.call_count,2)
        self.assertEqual(result['public_ip'],'1.1.1.1')
        self.assertNotIn('password',json.dumps(result))


if __name__=='__main__': unittest.main()
