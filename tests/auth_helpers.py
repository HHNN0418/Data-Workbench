"""Exercise the real login contract when arranging authenticated API tests."""


def login(client, email='dashboard-fixture-one@example.com', password=None):
    session = client.get('/api/auth/session')
    assert session.status_code == 200, session.text
    token = session.json()['csrf_token']
    response = client.post('/api/auth/login', json={
        'email': email,
        'password': email.strip().lower().split('@')[0] if password is None else password,
    }, headers={'X-CTN-Token': token})
    assert response.status_code == 200, response.text
    return token


def completed_job(client, response):
    """Await a synthetic background response without changing production API semantics."""
    import time
    import httpx
    if response.status_code != 202:
        return response
    job = response.json()
    deadline = time.monotonic() + 15
    while job['state'] in ('running', 'cancelling') and time.monotonic() < deadline:
        time.sleep(.01)
        job = client.get('/api/ai/jobs/' + job['id']).json()
    if job['state'] == 'completed':
        return httpx.Response(200, json=job['result'])
    if job['state'] in ('failed', 'cancelled'):
        return httpx.Response(job.get('error_status', 499), json={'error': job['error']})
    raise AssertionError('Synthetic job did not finish')
