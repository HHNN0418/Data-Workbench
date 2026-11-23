"""Read-only Gluetun status. Control credentials never leave the local process."""
from concurrent.futures import ThreadPoolExecutor
import copy
import ipaddress
import json
from pathlib import Path
import threading
import time
import tomllib
from urllib.request import ProxyHandler, Request, build_opener

from .proxy_worker import NoRedirect
from .secret_files import read_secret

CONTROL_FILE=Path(__file__).resolve().parent.parent/'data/secrets/gluetun-control.toml'
NODES=(('docker-au-a','au1012.nordvpn.com',18001),('docker-au-b','au753.nordvpn.com',18002))
READ_ROUTES=frozenset(('GET /v1/vpn/status','GET /v1/publicip/ip'))
_lock=threading.Lock()
_cache={}


def _key(path):
    config=tomllib.loads(read_secret(path))
    for role in config.get('roles',[]):
        if role.get('auth')=='apikey' and set(role.get('routes',[]))==READ_ROUTES:
            key=role.get('apikey','')
            if isinstance(key,str) and len(key)>=22:
                return key
    raise ValueError('Read-only control role missing')


def _node_status(node,key):
    worker,hostname,port=node
    result={'worker':worker,'hostname':hostname,'vpn_status':'unavailable','public_ip':None,'reachable':False}
    opener=build_opener(ProxyHandler({}),NoRedirect())
    for path,field in (('/v1/vpn/status','vpn_status'),('/v1/publicip/ip','public_ip')):
        try:
            request=Request(f'http://127.0.0.1:{port}'+path,headers={'X-API-Key':key,'Accept':'application/json'},method='GET')
            with opener.open(request,timeout=2) as response:
                raw=response.read(4097)
                if len(raw)>4096:
                    raise ValueError('Control response too large')
                payload=json.loads(raw)
            if field=='vpn_status':
                value=payload.get('status')
                if value not in ('running','stopped','starting','stopping'):
                    raise ValueError('Unknown VPN state')
                result[field]=value
                result['reachable']=True
            else:
                value=payload.get('public_ip')
                result[field]=str(ipaddress.ip_address(value)) if value else None
        except Exception as exc:
            # Exception messages can contain URLs or headers; expose type only.
            result[field+'_error']=type(exc).__name__
    return result


def get_proxy_status(control_file=CONTROL_FILE, *, refresh=False):
    """Cached for ten seconds; each endpoint is GET-only and loopback-only."""
    cache_key=str(control_file)
    with _lock:
        cached=_cache.get(cache_key)
        if cached and not refresh and time.monotonic()-cached[0]<10:
            return copy.deepcopy(cached[1])
        try:
            key=_key(control_file)
        except (OSError,ValueError,tomllib.TOMLDecodeError):
            return {'configured':False,'nodes':[],'distinct_exits':None}
        with ThreadPoolExecutor(max_workers=2) as pool:
            nodes=list(pool.map(lambda node:_node_status(node,key),NODES))
        ips=[node['public_ip'] for node in nodes]
        distinct=len(set(ips))==len(ips) if all(ips) else None
        value={'configured':True,'nodes':nodes,'distinct_exits':distinct,'checked_at_epoch':time.time()}
        _cache[cache_key]=(time.monotonic(),value)
        return copy.deepcopy(value)


if __name__=='__main__':
    print(json.dumps(get_proxy_status(),ensure_ascii=False))
