"""Correctness checks for cached Git paths."""
from http.client import HTTPConnection, HTTPMessage
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import main


class CachedGitTests(unittest.TestCase):
    def setUp(self):
        self.repo = main.Repository({'default_branch': 'main'})

    def commit(self, text):
        blob = self.repo.object('blob', text)
        return self.repo.commit({'data': blob}, 'test', 'main',
                                '2026-01-01T00:00:00Z', ['data'])

    def fetch(self, tip):
        body = b''.join(main.pktline(line + '\n') for line in
                        ('command=fetch', 'want ' + tip, 'deepen 1', 'done')) + b'0000'
        return main.git_upload_pack(self.repo, body, {})

    def test_shallow_cache_and_ref_changes(self):
        first = self.commit(b'first')
        tip = self.commit(b'second')
        with patch.object(main, 'write_pack', wraps=main.write_pack) as writer:
            response = self.fetch(tip)
            self.assertEqual(response, self.fetch(tip))
            self.assertEqual(writer.call_count, 1)
            cached = self.repo.shallow_pack_cache
            objects = main.parse_pack(cached[3], {})
            self.assertIn(tip, objects)
            self.assertNotIn(first, objects)
            self.assertEqual(len(objects), 3)
            new_tip = self.commit(b'third')
            self.assertNotEqual(response, self.fetch(new_tip))
            self.assertEqual(writer.call_count, 2)
            self.repo.refs['other'] = first
            self.fetch(new_tip)
            self.assertEqual(writer.call_count, 3)
            del self.repo.refs['other']
            self.fetch(new_tip)
            self.assertEqual(writer.call_count, 4)

    def test_incremental_validation_still_rejects_missing_objects(self):
        source = main.Repository({'default_branch': 'main'})
        tree = source.tree({})
        tip = source.object('commit', f'tree {tree}\n\nfirst\n'.encode())

        def push(old, new):
            return (main.pktline(f'{old} {new} refs/heads/main\0report-status\n') +
                    b'0000' + main.write_pack(source, set(source.objects)))

        main.git_receive_pack(self.repo, push(main.ZERO, tip))
        self.assertIn(tip, self.repo.validated_objects)
        before = dict(self.repo.objects)
        bad = source.object('commit',
                            f'tree {tree}\nparent {tip}\nparent {"f" * 40}\n\nbad\n'.encode())
        with self.assertRaisesRegex(ValueError, 'Missing object'):
            main.git_receive_pack(self.repo, push(tip, bad))
        self.assertEqual(self.repo.objects, before)
        self.assertEqual(self.repo.refs['main'], tip)
        self.assertNotIn(bad, self.repo.validated_objects)

    def test_wire_cache_tracks_head_and_objects_without_ref_changes(self):
        tip = self.commit(b'first')
        body = main.pktline('command=ls-refs\n') + main.pktline('symrefs\n') + b'0000'
        first = main.git_upload_pack(self.repo, body, {})
        with patch.object(main, '_git_upload_pack', wraps=main._git_upload_pack) as upload:
            self.assertIs(first, main.git_upload_pack(self.repo, body, {}))
            self.assertEqual(upload.call_count, 0)
            self.repo.metadata['default_branch'] = 'other'
            self.assertNotEqual(first, main.git_upload_pack(self.repo, body, {}))
            self.assertEqual(upload.call_count, 1)
            # A rejected push may publish immutable objects without moving refs.
            self.repo.object('blob', b'new object')
            main.git_upload_pack(self.repo, body, {})
            self.assertEqual(upload.call_count, 2)
        self.assertIn(tip.encode(), first)


class CachedHTTPTests(unittest.TestCase):
    def setUp(self):
        class QuietHandler(main.Handler):
            def log_message(self, *args):
                pass

        self.server = main.ForgeServer(('127.0.0.1', 0), QuietHandler)
        self.server.store = main.Store('')
        self.server.token = 'cache-test'
        # Stable advertised URLs make independent snapshot replays comparable.
        self.server.base_url = 'http://forge.test'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.path = '/api/v3/repos/' + main.LOGIN + '/cache-test'
        self.http('POST', '/api/v3/user/repos', {'name': 'cache-test'}, 201)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def http(self, method, path, payload=None, status=200, authorized=True):
        headers = {'Content-Type': 'application/json'}
        if authorized:
            headers['Authorization'] = 'Bearer cache-test'
        self.connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = self.connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, status, body)
        return json.loads(body) if body else None, dict(response.getheaders())

    def test_issue_cache_mutations_headers_auth_and_mcp(self):
        issue, _ = self.http('POST', self.path + '/issues', {'title': 'first'}, 201)
        repo = self.server.store.repos[main.repo_key(main.LOGIN, 'cache-test')]
        cached = repo.issue_response_cache['1'][1]
        ids = set()
        for _ in range(3):
            result, headers = self.http('GET', self.path + '/issues/1')
            self.assertEqual(result, issue)
            self.assertIs(repo.issue_response_cache['1'][1], cached)
            ids.add(headers['X-GitHub-Request-Id'])
        self.assertEqual(len(ids), 3)
        self.http('PATCH', self.path + '/issues/1', {'title': 'changed'})
        updated, _ = self.http('GET', self.path + '/issues/1')
        self.assertEqual(updated['title'], 'changed')
        self.http('POST', self.path + '/issues/1/comments', {'body': 'comment'}, 201)
        updated, _ = self.http('GET', self.path + '/issues/1')
        self.assertEqual(updated['comments'], 1)
        identifier = main.gql_id('Issue', issue['id'])
        result, _ = self.http('POST', '/api/graphql', {'query':
            'mutation { closeIssue(input: {issueId: "' + identifier + '"}) { issue { state } } }'})
        self.assertNotIn('errors', result)
        updated, _ = self.http('GET', self.path + '/issues/1')
        self.assertEqual(updated['state'], 'closed')
        _, headers = self.http('HEAD', self.path + '/issues/1', status=404)
        self.assertGreater(int(headers['Content-Length']), 0)
        self.http('GET', self.path + '/issues/1', status=401, authorized=False)
        result, _ = self.http('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'create_issue', 'arguments':
                       {'owner': main.LOGIN, 'repo': 'cache-test', 'title': 'from MCP'}}})
        created = json.loads(result['result']['content'][0]['text'])
        self.assertEqual(created['number'], 2)

    def test_snapshot_replay_and_restart_are_deterministic(self):
        snapshots = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            for _ in range(2):
                self.server.store = main.Store('')
                self.http('POST', '/api/v3/user/repos', {'name': 'cache-test'}, 201)
                self.http('POST', self.path + '/issues', {'title': 'replay'}, 201)
                for _ in range(3):
                    self.http('GET', self.path + '/issues/1')
                self.http('PATCH', self.path + '/issues/1', {'body': 'deterministic'})
                self.http('GET', self.path + '/issues/1')
                self.server.store.path = Path(directory) / 'state.json'
                self.server.store.save()
                snapshots.append(self.server.store.path.read_bytes())
                restored = main.Store(directory)
                self.assertEqual(restored.counter, self.server.store.counter)
                self.assertEqual(restored.next_id, self.server.store.next_id)
                self.assertEqual({name: repo.dump() for name, repo in restored.repos.items()},
                                 {name: repo.dump() for name, repo in self.server.store.repos.items()})
            self.assertEqual(snapshots[0], snapshots[1])

    def test_identical_headers_reuse_parser_and_changed_headers_are_validated(self):
        self.http('POST', self.path + '/issues', {'title': 'headers'}, 201)

        class CountingMessage(HTTPMessage):
            count = 0

            def __init__(self, *args, **kwargs):
                type(self).count += 1
                super().__init__(*args, **kwargs)

        with patch.object(self.server.RequestHandlerClass, 'MessageClass', CountingMessage):
            for _ in range(4):
                self.http('GET', self.path + '/issues/1')
            parsed = CountingMessage.count
            self.assertGreater(parsed, 0)
            for _ in range(4):
                self.http('GET', self.path + '/issues/1')
            self.assertEqual(CountingMessage.count, parsed)
            self.connection.putrequest('GET', self.path + '/issues/1')
            self.connection.putheader('Authorization', 'Bearer cache-test')
            self.connection.putheader('Content-Length', '0')
            self.connection.putheader('Content-Length', '0')
            self.connection.endheaders()
            response = self.connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn(b'Invalid Content-Length', response.read())
            self.assertGreater(CountingMessage.count, parsed)
            self.connection.close()
            self.http('GET', self.path + '/issues/1', status=401, authorized=False)


if __name__ == '__main__':
    unittest.main()
