import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ctn_data.db import initialize
from ctn_data.server import SPA_AGENT_PATH, SPA_PATHS, create_app
from tests.auth_helpers import login

ROUTES = Path(__file__).resolve().parents[1] / 'frontend' / 'src' / 'routes.tsx'
SAMPLE_AGENT = '0b9d3f9e-6f1a-4c3e-9b2a-1d2c3e4f5a6b'


def frontend_routes():
    """Full paths of the <Route> elements in routes.tsx, one element per line, nested under their parents."""
    stack, paths = [], []
    for line in ROUTES.read_text(encoding='utf-8').splitlines():
        text = line.strip()
        if text.startswith('</Route>'):
            stack.pop()
        elif re.match(r'<Route[\s/>]', text):
            match = re.search(r'\bpath="([^"]*)"', text)
            segment = match.group(1) if match else ''
            parent = stack[-1] if stack else ''
            full = segment if segment.startswith('/') else f"{parent.rstrip('/')}/{segment}" if segment else parent
            paths.append(full)
            if not text.endswith('/>'):
                stack.append(full)
    if stack:
        raise AssertionError(f'unbalanced <Route> nesting in {ROUTES}: {stack}')
    return paths


class SpaRouteTests(unittest.TestCase):
    def test_server_whitelist_matches_frontend_routes(self):
        routes = [path for path in frontend_routes() if not path.endswith('*')]
        self.assertGreater(len(routes), 20, 'routes.tsx format changed; update frontend_routes()')
        static = {path.lstrip('/') for path in routes if ':' not in path}
        for path in static:
            self.assertIn(path, SPA_PATHS, f'/{path} is a frontend route but the server would answer 404')
        for path in routes:
            if ':' in path:
                sample = re.sub(r':[A-Za-z]+', SAMPLE_AGENT, path).lstrip('/')
                self.assertTrue(SPA_AGENT_PATH.fullmatch(sample), f'{path} is a frontend route but the server would answer 404')
        self.assertEqual(SPA_PATHS - static - {'index.html'}, set(), 'server whitelist has paths that routes.tsx no longer serves')

    def test_every_frontend_route_loads_the_app(self):
        with tempfile.TemporaryDirectory() as temp:
            db, web_root = Path(temp) / 'test.sqlite3', Path(temp) / 'dist'
            initialize(db)
            web_root.mkdir()
            (web_root / 'index.html').write_text('<html>CTN frontend</html>')
            with TestClient(create_app(db, backup_dir=Path(temp) / 'backups', web_root=web_root), base_url='http://127.0.0.1:8765') as client:
                login(client)
                for path in sorted(SPA_PATHS - {''}) + ['', f'ai/agents/{SAMPLE_AGENT}', f'agents/{SAMPLE_AGENT}']:
                    response = client.get(f'/{path}')
                    self.assertEqual(response.status_code, 200, f'/{path}')
                    self.assertIn('CTN frontend', response.text)
                self.assertEqual(client.get('/no-such-page').status_code, 404)


if __name__ == '__main__':
    unittest.main()
