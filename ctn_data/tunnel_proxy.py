"""Opt-in HTTPS tunnel bridge; the existing CTN backend stays on loopback."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import ipaddress
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


MAX_BODY_BYTES = 10 * 1024 * 1024
SAFE_METHODS = frozenset(('GET', 'HEAD', 'OPTIONS'))
HOP_HEADERS = frozenset((
    b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization',
    b'te', b'trailer', b'transfer-encoding', b'upgrade',
))
# Must match ctn_data.auth; only this bridge may set them for the backend.
BRIDGE_HEADER = b'x-ctn-bridge'
CLIENT_IP_HEADER = b'x-ctn-client-ip'


def public_origin(value: str) -> str:
    """Require one exact DNS origin, never an arbitrary tunnel-domain wildcard."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ''
        labels = hostname.split('.')
        valid = (
            value == value.strip() and not any(character.isspace() for character in value)
            and parsed.scheme == 'https'
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and parsed.path in ('', '/')
            and not parsed.query and not parsed.fragment
            and '?' not in value and '#' not in value
            and 1 < len(labels) and len(hostname) <= 253
            and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                    for label in labels)
            and not all(label.isdigit() for label in labels)
            and parsed.netloc.lower() in (hostname, hostname + ':443')
        )
    except (ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise ValueError('PublicUrl 必须是具体的 HTTPS 域名，例如 https://ctn.example.com；不允许 IP、通配符或路径')
    return 'https://' + hostname


def filtered_headers(headers):
    """Drop hop-by-hop fields, including fields nominated by Connection."""
    excluded = set(HOP_HEADERS)
    for key, value in headers:
        if key.lower() == b'connection':
            excluded.update(part.strip().lower() for part in value.split(b','))
    return [(key.lower(), value) for key, value in headers if key.lower() not in excluded]


class UpstreamResponse(StreamingResponse):
    """Close the backend even if sending response headers is interrupted."""

    def __init__(self, upstream, headers):
        self.upstream = upstream
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code)
        self.raw_headers = headers

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


def client_ip(values):
    """The single Cloudflare client address, or an explicit unknown marker."""
    if len(values) != 1:
        return b'unknown'
    try:
        return str(ipaddress.ip_address(values[0].decode('ascii').strip())).encode('ascii')
    except (UnicodeDecodeError, ValueError):
        return b'unknown'


def bridge_token(path=None):
    """Read, or create once, the secret proving a request came through this bridge."""
    from .secret_files import TUNNEL_BRIDGE_TOKEN, read_secret, write_secret
    path = Path(path or TUNNEL_BRIDGE_TOKEN)
    if not path.exists():
        write_secret(path, secrets.token_urlsafe(32))
    token = read_secret(path).strip()
    if len(token) < 32:
        raise ValueError('Tunnel bridge token is invalid')
    return token


def create_proxy(public_url: str, backend_port: int = 8765, *, transport=None, bridge_secret=None):
    """Create a bridge without importing, restarting or duplicating the backend."""
    origin = public_origin(public_url)
    authority = urlsplit(origin).netloc
    if not isinstance(backend_port, int) or isinstance(backend_port, bool) or not 1 <= backend_port <= 65535:
        raise ValueError('BackendPort 必须在 1 到 65535 之间')
    backend_authority = f'127.0.0.1:{backend_port}'
    backend_origin = 'http://' + backend_authority

    @asynccontextmanager
    async def lifespan(app):
        # Never use system HTTP proxies for the fixed loopback backend.
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     timeout=httpx.Timeout(60, connect=5),
                                     follow_redirects=False) as client:
            app.state.backend_client = client
            yield

    async def proxy(request: Request):
        def reject(message, status=403):
            return JSONResponse({'error': message}, status_code=status,
                                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

        hosts = request.headers.getlist('host')
        if len(hosts) != 1 or hosts[0].lower() not in (authority, authority + ':443'):
            return reject('公网域名未授权，请检查 PublicUrl 与 Cloudflare 公网域名')
        origins = request.headers.getlist('origin')
        allowed_origins = (origin, origin + ':443')
        if len(origins) > 1 or (origins and origins[0] not in allowed_origins):
            return reject('拒绝跨站请求')
        if request.method not in SAFE_METHODS and not origins:
            return reject('公网写请求必须携带同源 HTTPS Origin')

        lengths = request.headers.getlist('content-length')
        encodings = request.headers.getlist('transfer-encoding')
        if (len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings)
                or (lengths and not re.fullmatch(r'[0-9]+', lengths[0]))
                or (encodings and encodings[0].lower().strip() != 'chunked')):
            return reject('请求大小或传输编码无效', 400)
        if lengths and len(lengths[0]) > 8:
            return reject('请求大小不能超过 10MiB', 413)
        declared = int(lengths[0]) if lengths else None
        if declared is not None and declared > MAX_BODY_BYTES:
            return reject('请求大小不能超过 10MiB', 413)

        # Uvicorn decodes chunked input. Bound the decoded body before forwarding.
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        return reject('请求大小不能超过 10MiB', 413)
                    body.extend(chunk)
        except TimeoutError:
            return reject('读取请求超时', 408)
        except ClientDisconnect:
            return reject('客户端已断开', 400)
        if declared is not None and len(body) != declared:
            return reject('请求大小与 Content-Length 不一致', 400)

        raw_path = request.scope.get('raw_path', request.url.path.encode('utf-8'))
        if not raw_path.startswith(b'/') or raw_path.startswith(b'//'):
            return reject('请求路径无效', 400)
        query = request.scope.get('query_string', b'')
        target = raw_path + (b'?' + query if query else b'')
        url = httpx.URL(scheme='http', host='127.0.0.1', port=backend_port, raw_path=target)
        headers = []
        connecting_ips = []
        for key, value in filtered_headers(request.scope['headers']):
            if key == b'cf-connecting-ip':
                connecting_ips.append(value)
            # The backend trusts loopback proxy headers. Do not relay client claims.
            if (key in (b'host', b'origin', b'content-length', b'forwarded', b'x-real-ip',
                        b'cf-connecting-ip', b'true-client-ip', BRIDGE_HEADER, CLIENT_IP_HEADER)
                    or key.startswith(b'x-forwarded-')):
                continue
            headers.append((key, value))
        headers.append((b'host', backend_authority.encode('ascii')))
        if bridge_secret:
            # Cloudflare's edge overwrites CF-Connecting-IP; only it names the client.
            headers.append((BRIDGE_HEADER, bridge_secret.encode('ascii')))
            headers.append((CLIENT_IP_HEADER, client_ip(connecting_ips)))
        if origins:
            # Translate only AFTER validating the public browser's origin.
            headers.append((b'origin', backend_origin.encode('ascii')))

        # Construct Request directly: build_request() would merge the shared
        # client's cookie jar, leaking one visitor's session to another visitor.
        upstream_request = httpx.Request(request.method, url, headers=headers, content=bytes(body))
        try:
            upstream = await request.app.state.backend_client.send(upstream_request, stream=True)
        except httpx.RequestError:
            return reject('CTN 后端暂时不可用，请检查本机服务与 BackendPort', 502)

        response_headers = []
        for key, value in filtered_headers(upstream.headers.raw):
            if key == b'set-cookie' and not re.search(rb'(?:^|;)\s*secure\s*(?:;|$)', value, re.I):
                # CTN sees loopback HTTP; browsers must still receive Secure cookies.
                value += b'; Secure'
            elif key == b'location':
                try:
                    location = urlsplit(value.decode('latin1'))
                except ValueError:
                    location = None
                if location and location.netloc == backend_authority and location.scheme in ('', 'http', 'https'):
                    value = (origin + urlunsplit(('', '', location.path, location.query, location.fragment))).encode('latin1')
            response_headers.append((key, value))

        # Preserve duplicate Set-Cookie fields and the backend's security headers.
        return UpstreamResponse(upstream, response_headers)

    app = Starlette(routes=[Route('/{path:path}', proxy,
                                  methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])],
                    lifespan=lifespan)
    app.state.public_origin = origin
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description='CTN Tunnel HTTPS 接入层（不启动或重启 CTN 后端）')
    parser.add_argument('--public-url', required=True)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--backend-port', type=int, default=8765)
    parser.add_argument('--check', action='store_true', help='校验配置并输出规范公网地址，不启动服务')
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535 or args.port == args.backend_port:
            raise ValueError('桥接端口必须在 1 到 65535 之间，且不能与后端端口相同')
        app = create_proxy(args.public_url, args.backend_port)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check:
        print(app.state.public_origin)
        return 0
    app = create_proxy(args.public_url, args.backend_port, bridge_secret=bridge_token())
    import uvicorn
    print(f'CTN Tunnel bridge: {app.state.public_origin} -> 127.0.0.1:{args.port} -> 127.0.0.1:{args.backend_port}', flush=True)
    print(f'Cloudflare Tunnel Service URL: http://127.0.0.1:{args.port}', flush=True)
    # Validate the original Host ourselves; never trust forwarded client headers.
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
