"""Exercise the public CLI through the real DNS/TLS/HTTP transport boundary."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
PUBLIC=[(2,1,6,'',('8.8.8.8',443))]
PRIVATE=[(2,1,6,'',('127.0.0.1',443))]

def response(status,body=b'',headers=None):
    headers={'Content-Type':'application/json',**(headers or {})}
    return ('HTTP/1.1 '+str(status)+' Test\r\n'+''.join(k+': '+str(v)+'\r\n' for k,v in headers.items())+'\r\n').encode()+body

class Socket:
    def __init__(self,data): self.data=data; self.sent=b''
    def sendall(self,data): self.sent+=data
    def makefile(self,*args): return io.BytesIO(self.data)
    def close(self): pass

class NetworkCLI(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('network_account',ROOT/'account.py'); a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a); return a

    def test_429_honors_retry_after_then_403_is_distinct(self):
        a=self.module(); sockets=[Socket(response(429,b'PRIVATE_SECRET',{'Retry-After':'2'})),Socket(response(200,json.dumps({'access_token':'PRIVATE_TOKEN','token_type':'Bearer','expires_in':30}).encode())),Socket(response(403,b'PRIVATE_SECRET https://private.example/?token=PRIVATE_TOKEN'))]
        context=mock.Mock(); context.wrap_socket.side_effect=lambda sock,server_hostname:sock
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ,{'AVITO_CLIENT_ID':'PRIVATE_ID','AVITO_CLIENT_SECRET':'PRIVATE_SECRET','RUNNER_TEMP':d}), mock.patch.object(a.socket,'getaddrinfo',return_value=PUBLIC) as dns, mock.patch.object(a.socket,'create_connection',side_effect=sockets) as connect, mock.patch.object(a.ssl,'create_default_context',return_value=context), mock.patch.object(a.time,'sleep') as sleep, mock.patch('sys.stderr',new_callable=io.StringIO) as error, mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            result=a.main(['prepare','--config',str(ROOT/'config.json'),'--source-file',str(ROOT/'fixtures/catalog.yml'),'--previous-state',d+'/missing','--out',d+'/out','--plan',d+'/private/plan.json','--public-feed-url','https://photostyle-avito.pages.dev/feed.xml'])
            self.assertEqual(result,2)
            self.assertEqual(connect.call_count,3)
            self.assertEqual(dns.call_count,3)
            sleep.assert_called_once_with(2)
            self.assertIn('provider_http_403',error.getvalue())
            for secret in ('PRIVATE_SECRET','PRIVATE_TOKEN','private.example'): self.assertNotIn(secret,output.getvalue()+error.getvalue())
            self.assertFalse(Path(d+'/out').exists())
            for call in connect.call_args_list: self.assertEqual(call.args[0],('8.8.8.8',443))

    def test_dns_tls_redirect_size_and_redaction_through_cli(self):
        token=json.dumps({'access_token':'PRIVATE_TOKEN','token_type':'Bearer','expires_in':30}).encode()
        cases=[('private',PRIVATE,response(200,token),0,'network_private_address_denied'),('mixed',PUBLIC+PRIVATE,response(200,token),0,'network_private_address_denied'),('redirect',PUBLIC,response(302,b'PRIVATE_SECRET',{'Location':'https://private.example/?token=PRIVATE_TOKEN'}),1,'provider_http_302'),('declared_size',PUBLIC,response(200,b'PRIVATE_SECRET',{'Content-Length':'129'}),1,'response_size_limit'),('stream_size',PUBLIC,response(200,b'PRIVATE_SECRET'+b'x'*129),1,'response_size_limit'),('pinned',PUBLIC,response(200,token),1,'invalid_response_or_network_or_storage_failure')]
        for label,addresses,data,count,code in cases:
            a=self.module(); context=mock.Mock(); context.wrap_socket.side_effect=lambda sock,server_hostname:sock
            with self.subTest(case=label), tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ,{'AVITO_CLIENT_ID':'PRIVATE_ID','AVITO_CLIENT_SECRET':'PRIVATE_SECRET','RUNNER_TEMP':d}), mock.patch.object(a,'MAX_BYTES',128), mock.patch.object(a.socket,'getaddrinfo',side_effect=[addresses,PRIVATE]) as dns, mock.patch.object(a.socket,'create_connection',return_value=Socket(data)) as connect, mock.patch.object(a.ssl,'create_default_context',return_value=context), mock.patch('sys.stderr',new_callable=io.StringIO) as error, mock.patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(a.main(['commit','--plan',d+'/private/missing.json']),2)
                self.assertEqual(connect.call_count,count); self.assertEqual(dns.call_count,1)
                self.assertIn(code,error.getvalue())
                for secret in ('PRIVATE_SECRET','PRIVATE_TOKEN','private.example'): self.assertNotIn(secret,error.getvalue()+output.getvalue())
                if count:
                    self.assertEqual(connect.call_args.args[0],('8.8.8.8',443))
                    self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'],'api.avito.ru')

    def test_response_has_total_deadline(self):
        a=self.module(); context=mock.Mock(); context.wrap_socket.side_effect=lambda sock,server_hostname:sock
        token=json.dumps({'access_token':'PRIVATE_TOKEN','token_type':'Bearer','expires_in':30}).encode()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ,{'AVITO_CLIENT_ID':'PRIVATE_ID','AVITO_CLIENT_SECRET':'PRIVATE_SECRET','RUNNER_TEMP':d}), mock.patch.object(a.socket,'getaddrinfo',return_value=PUBLIC), mock.patch.object(a.socket,'create_connection',return_value=Socket(response(200,token))), mock.patch.object(a.ssl,'create_default_context',return_value=context), mock.patch.object(a.time,'monotonic',side_effect=[0,46]), mock.patch('sys.stderr',new_callable=io.StringIO) as error:
            self.assertEqual(a.main(['commit','--plan',d+'/private/missing.json']),2)
            self.assertIn('response_time_limit',error.getvalue())
