"""HTTP and real-Git regression checks; all scratch files stay in this repo."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

import main


ROOT = Path(__file__).resolve().parent


class QuietHandler(main.Handler):
    def log_message(self, *args):
        pass


def run():
    server = main.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    server.store = main.Store('')
    server.token = 'integration-token'
    server.base_url = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    passed = []

    def check(label):
        passed.append(label)
        print('PASS ' + label)

    def http(path, payload=None, method=None, status=200, auth=True, raw=None):
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
                assert git('show', '-s', '--format=%P', 'origin/main') == base
                assert git('rev-list', '--count', base + '..origin/main') == '1'
                assert git('show', 'origin/main:' + branch + '.txt') == 'feature two'
                assert git('show', 'origin/main:' + transport.lower() + '-base.txt') == 'independent base change'
                assert git('rev-parse', 'origin/' + branch) == head
                git('merge', '--ff-only', 'origin/main')
                check(transport + ' merge: pushed head, REST IDs/state, single-parent squash on current base, fetched Git contents')
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
        print(f'ALL {len(passed)} CHECK GROUPS PASSED')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    run()
