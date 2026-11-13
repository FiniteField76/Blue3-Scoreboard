"""Shared-client requests against the real HTTP listener and SQLite store."""
import concurrent.futures
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import AppServer, ScoreStore, technical_stats
from server import EntryServer, load_access_config


class SharedServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = ScoreStore(self.path / 'scoreboard.sqlite3')
        self.store.create_player({'name': '蓝白碗'})
        self.season = self.store.create_competition({'name': 'Season 14', 'note': '共享录分测试'})
        self.config = load_access_config(self.path)
        self.server = EntryServer(('127.0.0.1', 0), self.store, **{
            'access_code': self.config['access_code'], 'server_id': self.config['server_id']})
        self.admin = AppServer(('127.0.0.1', 0), self.store)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.server, self.admin)]
        for thread in self.threads:
            thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        for server in (self.server, self.admin):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, method='GET', data=None, code=None):
        headers = {'Authorization': 'Bearer ' + (self.config['access_code'] if code is None else code)}
        if data is not None:
            data = json.dumps(data, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            result = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            result = error
        with result:
            return result.status, json.load(result)

    def payload(self, key='entry-request-001'):
        return {'server_id': self.config['server_id'], 'competition_id': self.season,
                'played_at': '2026-11-13T19:30', 'note': '四人录分', 'request_key': key,
                'entries': [{'player_id': pid, 'base': base, 'battle_count': count}
                            for pid, base, count in [(1, '100.25', 8), (2, '-50', 8), (3, '-30.25', 6), (4, '-20', 6)]]}

    def test_catalog_and_persistent_access_code(self):
        self.assertEqual(load_access_config(self.path), self.config)
        self.store.update_settings({'rate': '5'})
        self.store.update_player(2, {'active': False})
        status, body = self.request('/api/entry/options')
        self.assertEqual(status, 200)
        self.assertEqual(body['rate'], '1')
        self.assertEqual([p['id'] for p in body['players']], [1, 3, 4])
        self.assertTrue(any(c['id'] == self.season for c in body['competitions']))
        self.assertNotIn('matches', body)
        self.assertNotIn('token', body)

    def test_remote_save_is_visible_in_existing_admin_and_statistics(self):
        self.store.update_settings({'rate': '5'})
        status, body = self.request('/api/entry/matches', 'POST', self.payload())
        self.assertEqual(status, 201, body)
        with urllib.request.urlopen('http://127.0.0.1:' + str(self.admin.server_port) + '/api/state') as response:
            state = json.load(response)
        match = state['matches'][0]
        self.assertEqual(match['id'], body['id'])
        self.assertEqual(match['competition_id'], self.season)
        self.assertEqual(match['rate'], '1')
        self.assertEqual(match['scoring_mode'], 'multi')
        self.assertEqual([e['total_units'] for e in match['entries']], [1002500, -500000, -302500, -200000])
        self.assertEqual([e['battle_count'] for e in match['entries']], [8, 8, 6, 6])
        stats = technical_stats(state, state['matches'])
        self.assertEqual(next(p['matches'] for p in stats['players'] if p['id'] == 1), 8)

    def test_retry_and_two_clients_do_not_duplicate_or_lose_records(self):
        payload = self.payload()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(lambda _: self.request('/api/entry/matches', 'POST', payload), range(5)))
        self.assertTrue(all(status == 201 for status, _ in replies), replies)
        self.assertEqual(len({body['id'] for _, body in replies}), 1)
        self.assertEqual(len(self.store.state()['matches']), 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda i: self.request('/api/entry/matches', 'POST', self.payload(f'client-{i}-round-001')), range(4)))
        self.assertTrue(all(status == 201 for status, _ in replies), replies)
        self.assertEqual(len(self.store.state()['matches']), 5)
        self.assertEqual(len(ScoreStore(self.store.path).state()['matches']), 5)

    def test_invalid_rounds_leave_database_unchanged(self):
        changes = [lambda p: p['entries'][0].update(base='100.24'),
                   lambda p: p['entries'][0].update(battle_count='1.5'),
                   lambda p: p['entries'][0].update(battle_count=-1),
                   lambda p: p['entries'][0].update(player_id=2),
                   lambda p: p['entries'][0].update(player_id=9999),
                   lambda p: p.update(competition_id=9999), lambda p: p.update(rate='0'),
                   lambda p: p.update(server_id='wrong-server')]
        for change in changes:
            payload = self.payload()
            change(payload)
            with self.subTest(payload=payload):
                self.assertIn(self.request('/api/entry/matches', 'POST', payload)[0], (400, 409))
                self.assertEqual(self.store.state()['matches'], [])

    def test_entry_api_only_allows_existing_players_and_new_multi_scores(self):
        payload = self.payload()
        payload.update(scoring_mode='points', record_kind='counts')
        payload['entries'][0].update(extra='900', chip=50)
        self.assertEqual(self.request('/api/entry/matches', 'POST', payload)[0], 201)
        entry = self.store.state()['matches'][0]
        self.assertEqual(entry['record_kind'], 'score')
        self.assertEqual(entry['entries'][0]['extra_cents'], 0)
        payload['entries'][0].pop('player_id')
        payload['entries'][0]['player_name'] = '不应新建'
        self.assertEqual(self.request('/api/entry/matches', 'POST', payload)[0], 400)
        self.assertEqual(len(self.store.state()['players']), 4)
        for route in ('/api/state', '/api/backup', '/api/players', '/api/matches/1'):
            self.assertEqual(self.request(route)[0], 404)
        self.assertEqual(self.request('/api/entry/options', code='wrong')[0], 401)
        self.assertEqual(self.request('/api/entry/matches', 'DELETE', {})[0], 404)

    def test_catalog_creation_is_shared_and_uses_existing_validation(self):
        for kind, name, extras in [('players', '远程新选手', {}),
                                  ('competitions', '远程新赛季', {'note': '赛季说明'})]:
            payload = {'server_id': self.config['server_id'], 'name': name, **extras}
            status, body = self.request('/api/entry/' + kind, 'POST', payload)
            self.assertEqual(status, 201, body)
            self.assertTrue(body['catalog']['capabilities']['create_' + kind])
            item = next(item for item in body['catalog'][kind] if item['id'] == body['id'])
            self.assertEqual(item['name'], name)
            self.assertIn(body['id'], [item['id'] for item in self.store.state()[kind]])
            self.assertEqual(self.request('/api/entry/' + kind, 'POST', payload)[0], 409)
        self.assertEqual(self.store.state()['matches'], [])

    def test_catalog_creation_rejects_unauthorized_wrong_server_and_invalid_names(self):
        before = {kind: len(self.store.state()[kind]) for kind in ('players', 'competitions')}
        for kind, limit in [('players', 24), ('competitions', 60)]:
            path = '/api/entry/' + kind
            payload = {'server_id': self.config['server_id'], 'name': 'Only authenticated'}
            self.assertEqual(self.request(path, 'POST', payload, code='wrong')[0], 401)
            self.assertEqual(self.request(path, 'POST', {**payload, 'server_id': 'wrong'})[0], 409)
            self.assertEqual(self.request(path, 'POST', [payload])[0], 400)
            for name in ['', '  ', 'line\nbreak', 'x' * (limit + 1), 12, None]:
                with self.subTest(kind=kind, name=name):
                    self.assertEqual(self.request(path, 'POST', {**payload, 'name': name})[0], 400)
            for method in ('GET', 'PATCH', 'DELETE'):
                self.assertEqual(self.request(path, method, payload if method != 'GET' else None)[0], 404)
            self.assertEqual(len(self.store.state()[kind]), before[kind])

    def test_concurrent_catalog_creation_cannot_duplicate_a_name(self):
        for kind in ('players', 'competitions'):
            payload = {'server_id': self.config['server_id'], 'name': ' Concurrent '}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(lambda _: self.request('/api/entry/' + kind, 'POST', payload), range(4)))
            self.assertEqual(sorted(status for status, _ in responses), [201, 409, 409, 409])
            self.assertEqual(sum(row['name'] == 'Concurrent' for row in self.store.state()[kind]), 1)
            self.assertEqual(self.request('/api/entry/' + kind, 'POST', {**payload, 'name': 'CONCURRENT'})[0], 409)

    def test_optional_rate_and_default_count(self):
        payload = self.payload()
        payload['rate'] = '1.25'
        payload['entries'][0].pop('battle_count')
        self.assertEqual(self.request('/api/entry/matches', 'POST', payload)[0], 201)
        entry = self.store.state()['matches'][0]['entries'][0]
        self.assertEqual(entry['total_units'], 1253125)
        self.assertEqual(entry['battle_count'], 1)


if __name__ == '__main__':
    unittest.main()
