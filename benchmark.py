"""Real-client forge benchmark; all scratch data stays under this repository."""
import argparse
import cProfile
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import pstats
import statistics
import subprocess
import tempfile
import threading
import time

import main

ROOT = Path(__file__).resolve().parent


def run(profile=False):
    profiles = []
    profile_lock = threading.Lock()

    class Handler(main.Handler):
        def log_message(self, *args):
            pass

        def request(self):
            if profile:
                # Python 3.12 has a limited number of concurrent profiler slots.
                # Profile separately from timings; serialize instrumentation only.
                with profile_lock:
                    profiler = cProfile.Profile()
                    try:
                        return profiler.runcall(super().request)
                    finally:
                        profiles.append(profiler)
            return super().request()

        do_GET = do_POST = request

    server = main.ForgeServer(('127.0.0.1', 0), Handler)
    server.store = main.Store('')
    server.token = 'benchmark'
    server.base_url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=60)
    headers = {'Authorization': 'Bearer benchmark', 'Content-Type': 'application/json'}
    times = {}

    def http(method, path, payload=None):
        connection.request(method, '/api/v3/' + path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        body = response.read()
        assert response.status in (200, 201), (response.status, body)
        return json.loads(body)

    try:
        with tempfile.TemporaryDirectory(prefix='.benchmark-', dir=ROOT) as directory:
            work = Path(directory)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Bench', GIT_AUTHOR_EMAIL='bench@test',
                       GIT_COMMITTER_NAME='Bench', GIT_COMMITTER_EMAIL='bench@test')

            def git(*args):
                result = subprocess.run(['git', '-c', 'protocol.version=2', '-c',
                                         'http.extraHeader=Authorization: Bearer benchmark', *args],
                                        cwd=work, env=env, capture_output=True, timeout=60)
                assert result.returncode == 0, (args, result.stderr)
                return result.stdout.decode().strip()

            repo = http('POST', 'user/repos', {'name': 'bench'})
            path = 'repos/octo-user/bench/issues'
            http('POST', path, {'title': 'read target'})
            git('init', '-b', 'main')
            # Prepare commits outside the timed region, then push each sequentially.
            commits = []
            for i in range(200):
                (work / 'data').write_bytes((f'{i:04d}' * 256).encode())
                git('add', 'data')
                git('commit', '-qm', str(i))
                commits.append(git('rev-parse', 'HEAD'))
            start = time.perf_counter()
            for commit in commits:
                git('push', '-q', repo['clone_url'], commit + ':refs/heads/main')
            times['push'] = time.perf_counter() - start
            print(f'push: {times["push"]:.6f}s', flush=True)
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=50) as pool:
                list(pool.map(lambda i: git('clone', '-q', '--depth', '1', repo['clone_url'],
                                            str(work / f'clone-{i}')), range(50)))
            times['clone'] = time.perf_counter() - start
            print(f'clone: {times["clone"]:.6f}s', flush=True)
            for i in range(50):
                assert git('-C', f'clone-{i}', 'rev-list', '--count', 'HEAD') == '1'
                assert git('-C', f'clone-{i}', 'rev-parse', 'HEAD') == commits[-1]
            # Git work can outlast the idle timeout; reconnect once before REST timing.
            connection.close()
            http('GET', path + '/1')
            sock = connection.sock
            start = time.perf_counter()
            for _ in range(2000):
                assert http('GET', path + '/1')['number'] == 1
                assert connection.sock is sock
            times['read'] = time.perf_counter() - start
            print(f'read: {times["read"]:.6f}s', flush=True)
            start = time.perf_counter()
            for i in range(200):
                assert http('POST', path, {'title': f'issue {i}'})['number'] == i + 2
                assert connection.sock is sock
            times['write'] = time.perf_counter() - start
            print(f'write: {times["write"]:.6f}s', flush=True)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()
    if profiles:
        stats = pstats.Stats(profiles[0])
        for profiler in profiles[1:]:
            stats.add(profiler)
        stats.sort_stats('cumulative').print_stats(25)
    return times


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--profile', action='store_true')
    args = parser.parse_args()
    results = [run(args.profile) for _ in range(args.rounds)]
    print('MEDIANS ' + json.dumps({key: statistics.median(r[key] for r in results)
                                  for key in results[0]}, sort_keys=True), flush=True)
