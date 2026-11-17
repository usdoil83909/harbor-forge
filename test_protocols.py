"""HTTP and real-Git regression checks; all scratch files stay in this repo."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import sys
import socket
import urllib.error
import urllib.request

import main


ROOT = Path(__file__).resolve().parent


class QuietHandler(main.Handler):
    def log_message(self, *args):
        pass


def run():
    server = main.ForgeServer(('127.0.0.1', 0), QuietHandler)
    server.store = main.Store('')
    server.token = 'integration-token'
    server.base_url = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    passed = []

    def check(label):
        passed.append(label)
        print('PASS ' + label)

    def http(path, payload=None, method=None, status=200, auth=True, raw=None, response_headers=None):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + server.token
        data = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(server.base_url + path, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert response.headers.get('X-GitHub-Request-Id')
            assert response.headers.get_content_type() == 'application/json'
            if response_headers is not None:
                response_headers.update(response.headers)
            body = response.read()
            assert response.status == status, (path, response.status, body)
        return json.loads(body) if body else None, body

    def rpc(method, params=None, **kwargs):
        return http('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, **kwargs)

    def tool(name, /, **args):
        response, _ = rpc('tools/call', {'name': name, 'arguments': args})
        assert 'error' not in response, response
        return json.loads(response['result']['content'][0]['text'])

    def gql(query):
        response, _ = http('/api/graphql', {'query': query})
        assert 'errors' not in response, response
        return response

    repo_path = '/api/v3/repos/octo-user/protocol-test'
    args = {'owner': 'octo-user', 'repo': 'protocol-test'}
    try:
        response, _ = rpc('initialize', {'protocolVersion': '2026-07-28', 'capabilities': {},
                                        'clientInfo': {'name': 'test', 'version': '1'}})
        assert response['result']['protocolVersion'] == '2026-07-28'
        http('/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, status=202)
        check('MCP initialize 2026-07-28 and notifications/initialized')
        response, first = rpc('tools/list')
        _, second = rpc('tools/list')
        assert first == second
        tools = response['result']['tools']
        assert len(tools) >= 6
        assert {'create_repo', 'get_repo', 'create_issue', 'list_issues',
                'create_pull_request', 'merge_pull_request'} <= {t['name'] for t in tools}
        assert all(t['description'].strip() and t['inputSchema']['type'] == 'object' for t in tools)
        check('MCP tools/list schemas, descriptions, six tools, byte-identical response bodies')
        repo = tool('create_repo', name='protocol-test')
        rest_repo, _ = http(repo_path)
        assert repo['id'] == rest_repo['id'] == tool('get_repo', **args)['id']
        issues = [tool('create_issue', **args, title='issue-' + str(i)) for i in range(3)]
        for issue in issues:
            rest_issue, _ = http(repo_path + '/issues/' + str(issue['number']))
            assert rest_issue['id'] == issue['id']
        rest_issue, _ = http(repo_path + '/issues', {'title': 'from REST'}, status=201)
        assert {i['id'] for i in tool('list_issues', **args)} == {i['id'] for i in issues + [rest_issue]}
        http(repo_path + '/issues/' + str(issues[1]['number']), {'state': 'closed'}, method='PATCH')
        check('MCP repository/issue tools share REST store and IDs in both directions')
        for name in ('Issue', 'PullRequest', 'Repository', 'SearchType'):
            info = gql('{ __type(name: "' + name + '") { name kind fields { name type { kind name } } enumValues { name } } }')['data']['__type']
            assert info['name'] == name
            assert info['kind'] == ('ENUM' if name == 'SearchType' else 'OBJECT')
            if name == 'SearchType':
                assert 'ISSUE' in [v['name'] for v in info['enumValues']]
            else:
                assert info['fields']
        check('GraphQL Issue/PullRequest/Repository/SearchType introspection and ISSUE enum')
        prefix = 'repository(owner: "octo-user", name: "protocol-test")'
        info = gql('{ ' + prefix + ' { name databaseId issues(first: 2, orderBy: {field: CREATED_AT, direction: DESC}, states: [OPEN]) { totalCount nodes { number state createdAt } } } }')['data']['repository']
        assert info['name'] == repo['name'] and info['databaseId'] == repo['id']
        nodes = info['issues']['nodes']
        assert info['issues']['totalCount'] == 3
        assert [i['number'] for i in nodes] == [rest_issue['number'], issues[2]['number']]
        assert all(i['state'] == 'OPEN' for i in nodes)
        assert nodes[0]['createdAt'] > nodes[1]['createdAt']
        check('GraphQL repository lookup, OPEN filter, CREATED_AT DESC and first limit')
        queries = ['query First { viewer { login } }', 'query Second { ' + prefix + ' { name } }']
        expected = [{'data': {'viewer': {'login': 'octo-user'}}}, {'data': {'repository': {'name': 'protocol-test'}}}]
        assert gql(' '.join(queries)) == expected
        batch, _ = http('/api/graphql', [{'query': q} for q in queries])
        assert batch == expected
        check('GraphQL two operations and JSON batch return results in request order')
        unknown, _ = http('/api/graphql', {'query': '{ ' + prefix + ' { nonexistent } }'})
        assert unknown['errors']
        assert gql('{ repository(owner: "octo-user", name: "absent") { name } }') == {'data': {'repository': None}}
        check('GraphQL unknown field errors at HTTP 200; missing repository null without errors')
        response, _ = rpc('tools/call', {'name': 'absent', 'arguments': {}})
        assert response['error']['code'] == -32601
        for entry in tools:
            response, _ = rpc('tools/call', {'name': entry['name'], 'arguments': {}})
            assert response.get('error', {}).get('code') == -32602 or response.get('result', {}).get('isError') is True
        response, _ = http('/mcp', raw=b'not JSON', status=400)
        assert response['error']['code'] == -32700
        rpc('tools/list', auth=False, status=401)
        check('MCP unknown tool, required arguments, malformed JSON and authentication errors')

        http(repo_path, {'description': 'updated'}, method='PATCH')
        assert http(repo_path)[0]['description'] == 'updated'
        http(repo_path + '/issues', {'title': 'bad label', 'labels': ['bug']}, status=422)
        http(repo_path + '/labels', {'name': 'bug', 'color': 'ff0000'}, status=201)
        assert http(repo_path + '/issues', {'title': 'labeled', 'labels': ['bug']}, status=201)[0]['labels'][0]['name'] == 'bug'
        http('/api/v3/repos/wrong/protocol-test', status=404)
        http('/unknown', status=404)
        http('/unknown', method='TRACE', status=404)
        for raw in (b'{', b'[]', b'null', b'\xff'):
            http('/api/v3/user/repos', raw=raw, status=400)
        disposable = '/api/v3/repos/octo-user/disposable'
        http('/api/v3/user/repos', {'name': 'disposable'}, status=201)
        headers = {}
        assert len(http('/api/v3/user/repos?per_page=1&page=1', response_headers=headers)[0]) == 1
        assert 'page=2' in headers['Link'] and 'rel="next"' in headers['Link']
        assert len(http('/api/v3/user/repos?per_page=1&page=2')[0]) == 1
        file = disposable + '/contents/a.txt'
        created = http(file, {'message': 'create', 'content': 'YQ=='}, method='PUT', status=201)[0]
        http(file, {'message': 'stale', 'content': 'Yg==', 'sha': '0' * 40}, method='PUT', status=409)
        http(file, {'message': 'delete', 'sha': created['content']['sha']}, method='DELETE')
        http(file, status=404)
        http(disposable, method='DELETE', status=204)
        http(disposable, status=404)
        check('REST description, deletion, stale sha, labels, pagination, malformed JSON, owner mismatch and JSON headers')

        with tempfile.TemporaryDirectory(prefix='.protocol-test-', dir=ROOT) as directory:
            work = Path(directory)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.test',
                       GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.test')

            def git(*command):
                result = subprocess.run(['git', '-c', 'http.extraHeader=Authorization: Bearer ' + server.token,
                                         *command], cwd=work, env=env, capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, (command, result.stdout, result.stderr)
                return result.stdout.strip()

            def commit(path, text):
                (work / path).write_text(text)
                git('add', path)
                git('commit', '-m', text.strip())

            def pull(branch, via_mcp=True):
                values = dict(title=branch, head=branch, base='main')
                return tool('create_pull_request', **args, **values) if via_mcp else http(repo_path + '/pulls', values, status=201)[0]

            git('init', '-b', 'main')
            commit('shared.txt', 'original\n')
            git('remote', 'add', 'origin', repo['clone_url'])
            git('push', 'origin', 'main')
            for transport in ('MCP', 'REST'):
                git('checkout', 'main')
                branch = transport.lower() + '-feature'
                git('checkout', '-b', branch)
                commit(branch + '.txt', 'feature one\n')
                commit(branch + '.txt', 'feature two\n')
                head = git('rev-parse', 'HEAD')
                git('push', 'origin', branch)
                git('checkout', 'main')
                commit(transport.lower() + '-base.txt', 'independent base change\n')
                git('push', 'origin', 'main')
                base = git('rev-parse', 'HEAD')
                pr = pull(branch, transport == 'MCP')
                endpoint = repo_path + '/pulls/' + str(pr['number'])
                assert http(endpoint)[0]['id'] == pr['id']
                assert pr['head']['sha'] == head
                merged = tool('merge_pull_request', **args, pull_number=pr['number']) if transport == 'MCP' else http(endpoint + '/merge', {}, method='PUT')[0]
                assert merged['merged'] is True
                saved = http(endpoint)[0]
                assert saved['id'] == pr['id'] and saved['state'] == 'closed' and saved['merged'] is True
                assert saved['merge_commit_sha'] == merged['sha']
                git('fetch', 'origin')
                assert git('rev-parse', 'origin/main') == merged['sha']
                assert git('show', '-s', '--format=%P', 'origin/main') == base + ' ' + head
                assert git('rev-list', '--first-parent', '--count', base + '..origin/main') == '1'
                assert git('show', '-s', '--format=%B', 'origin/main') == 'Merge pull request #' + str(pr['number'])
                assert saved['merged_commit'] == merged['sha']
                assert git('show', 'origin/main:' + branch + '.txt') == 'feature two'
                assert git('show', 'origin/main:' + transport.lower() + '-base.txt') == 'independent base change'
                assert git('rev-parse', 'origin/' + branch) == head
                git('merge', '--ff-only', 'origin/main')
                check(transport + ' merge: pushed head, REST IDs/state, two parents, merged tree and default message')
            git('checkout', '-b', 'conflict')
            commit('shared.txt', 'head conflict\n')
            git('push', 'origin', 'conflict')
            git('checkout', 'main')
            commit('shared.txt', 'base conflict\n')
            git('push', 'origin', 'main')
            base = git('rev-parse', 'HEAD')
            pr = pull('conflict')
            endpoint = repo_path + '/pulls/' + str(pr['number'])
            conflict, _ = http(endpoint + '/merge', {}, method='PUT', status=405)
            response, _ = rpc('tools/call', {'name': 'merge_pull_request', 'arguments': dict(args, pull_number=pr['number'])})
            assert response['error']['message'] == conflict['message']
            saved = http(endpoint)[0]
            assert saved['state'] == 'open' and saved['merged'] is False
            git('fetch', 'origin')
            assert git('rev-parse', 'origin/main') == base
            check('Shared merge conflict: REST 405, MCP same error, pull and base unchanged')
            missing, _ = http(repo_path + '/pulls/99999/merge', {}, method='PUT', status=404)
            response, _ = rpc('tools/call', {'name': 'merge_pull_request', 'arguments': dict(args, pull_number=99999)})
            assert response['error']['message'] == missing['message']
            check('Unknown pull: REST 404 and MCP matching error')
            git('clone', '--depth', '1', repo['clone_url'], 'shallow')
            assert git('-C', 'shallow', 'rev-list', '--count', 'HEAD') == '1'
            check('Real git clone --depth 1')

            git('checkout', '-b', 'modes')
            commit('executable', 'executable content\n')
            git('update-index', '--chmod=+x', 'executable')
            git('commit', '-m', 'executable mode')
            git('push', 'origin', 'modes')
            pr = pull('modes', False)
            merged = http(repo_path + '/pulls/' + str(pr['number']) + '/merge',
                          {'commit_message': 'custom merge'}, method='PUT')[0]
            git('fetch', 'origin')
            assert git('show', '-s', '--format=%B', merged['sha']) == 'custom merge'
            assert git('ls-tree', merged['sha'], 'executable').startswith('100755')
            assert http(repo_path + '/pulls?state=all')[0]
            http(repo_path + '/pulls', {'title': 'missing', 'head': 'missing', 'base': 'main'}, status=422)
            check('Custom merge message, executable mode, pull listing and missing head validation')

            barrier = threading.Barrier(24)
            def concurrent(i):
                barrier.wait(timeout=10)
                if i < 20:
                    return http(repo_path + '/issues', {'title': 'parallel-' + str(i)}, status=201)[0]['number']
                git('push', 'origin', 'HEAD:refs/heads/parallel-' + str(i))
                return None
            with ThreadPoolExecutor(max_workers=24) as pool:
                results = list(pool.map(concurrent, range(24)))
            assert len(set(results[:20])) == 20
            git('fetch', 'origin')
            for i in range(20, 24):
                assert git('rev-parse', 'origin/parallel-' + str(i)) == git('rev-parse', 'HEAD')
            check('20 parallel REST mutations plus four real Git pushes without deadlocks')
            for garbage in (b'garbage\r\n\r\n', b'GET / HTTP/1.1\r\nBad\r\n\r\n'):
                with socket.create_connection(server.server_address, timeout=5) as connection:
                    connection.sendall(garbage)
                    connection.shutdown(socket.SHUT_WR)
                    assert connection.recv(8192)
            http('/octo-user/protocol-test.git/git-receive-pack', raw=b'garbage', status=400)
            http('/api/v3/user')
            check('Garbage HTTP/Git input leaves server serving')
        print(f'ALL {len(passed)} CHECK GROUPS PASSED')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def handshake():
    registered = threading.Event()
    polls = []
    ports = []

    class Environment(main.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == '/register'
            ports.append(json.loads(self.rfile.read(int(self.headers['Content-Length'])))['port'])
            self.send_response(200)
            self.end_headers()
            registered.set()

        def do_GET(self):
            assert self.path == '/done'
            polls.append(time.monotonic())
            body = json.dumps({'done': len(polls) >= 2}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    environment = main.ForgeServer(('127.0.0.1', 0), Environment)
    thread = threading.Thread(target=environment.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='.handshake-', dir=ROOT) as directory:
        process = subprocess.Popen([sys.executable, str(ROOT / 'main.py')], cwd=ROOT,
                                   env=dict(os.environ, MAKEFUN_ENV_URL='http://127.0.0.1:' + str(environment.server_port),
                                            MAKEFUN_STATE_DIR=directory, MAKEFUN_SHOP_TOKEN='handshake-token'),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert registered.wait(10)
            url = 'http://127.0.0.1:' + str(ports[0]) + '/api/v3/user/repos'
            def create(i):
                request = urllib.request.Request(url, data=json.dumps({'name': 'persist-' + str(i)}).encode(),
                                                 headers={'Authorization': 'Bearer handshake-token', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=10) as response:
                    assert response.status == 201 and response.headers.get('X-GitHub-Request-Id')
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(create, range(20)))
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, (stdout, stderr)
            assert b'Traceback' not in stderr
            assert len(polls) == 2 and 4.5 <= polls[1] - polls[0] < 8
            assert len(main.Store(directory).repos) == 20
            print('PASS port-0 register/done handshake, five-second repoll, concurrent serving/persistence and exit 0')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            environment.shutdown()
            environment.server_close()
            thread.join()


def merge_edges():
    handler = object.__new__(main.Handler)
    for label, base_changes, head_changes, conflict in [
        ('identical edits', {'a': 'same'}, {'a': 'same'}, False),
        ('head deletion', {}, {'a': None}, False),
        ('base deletion', {'a': None}, {}, False),
        ('delete/modify', {'a': None}, {'a': 'edited'}, True),
        ('add/add', {'new': 'base'}, {'new': 'head'}, True),
        ('file/directory', {'new': 'file'}, {'new/child': 'child'}, True),
    ]:
        repo = main.Repository({})
        original = {'a': repo.object('blob', b'original')}
        date = '2026-01-01T00:00:00Z'
        ancestor = repo.commit(original, 'initial', 'main', date, ['a'])
        repo.refs['head'] = ancestor
        for branch, changes in [('main', base_changes), ('head', head_changes)]:
            files = dict(original)
            for path, value in changes.items():
                if value is None:
                    files.pop(path, None)
                else:
                    files[path] = repo.object('blob', value.encode())
            repo.commit(files, branch, branch, date, list(changes))
        issue = {'head': {'ref': 'head'}, 'base': {'ref': 'main'}}
        try:
            entries, _, _, _ = handler.pull_merge_snapshot(repo, issue)
        except main.APIError as error:
            assert conflict and error.status == 405 and error.message == 'Pull Request is not mergeable', label
        else:
            assert not conflict, label
            if 'deletion' in label:
                assert 'a' not in entries, label
            if label == 'identical edits':
                assert repo.blob(entries['a'][1]) == b'same'
    print('PASS merge identical edits, deletions, delete/modify, add/add and file/directory conflicts')


if __name__ == '__main__':
    run()
    handshake()
    merge_edges()
