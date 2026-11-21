# Forge performance measurements

## Full conformance runner optimization

The full runner uses its own HTTP Git client and REST client, with five warm-up
operations followed by three timed rounds per workload. These measurements are
distinct from the real-Git subprocess benchmark below. Run:

```sh
bash /root/makefun/forge_fullrun.sh
python3 /root/makefun/parse_suite.py
```

Every measured optimization stage passed all 120 conformance checks, with zero
failed checks and no failed performance workloads. Times below are p50 milliseconds.

| Stage | Push | Clone | Read | Write | Performance points |
| --- | ---: | ---: | ---: | ---: | ---: |
| Supplied baseline | 372.850 | 155.020 | 1079.890 | 153.830 | 167 |
| Serialized issue cache and combined JSON response writes | 365.329 | 122.493 | 941.549 | 152.736 | 173 |
| Framed upload cache, static v2 advertisement, creation serialization | 413.326 | 122.045 | 1083.392 | 146.465 | 172 |
| Cached paths/response headers, opt-in access logs | 344.794 | 128.069 | 802.417 | 132.627 | 177 |
| Reuse identical validated request headers | 343.676 | 110.157 | 566.436 | 135.957 | 183 |
| Bounded caches covering multiple negotiation requests | 317.548 | 104.406 | 556.304 | 132.401 | 184 |
| Larger header-cache experiment | 319.233 | 105.718 | 608.953 | 133.710 | 184 |
| Final verification after restoring smaller caches | 325.965 | 110.657 | 584.285 | 136.860 | 183 |

The larger header-cache experiment did not improve performance and was reverted.
The final run earns 50 push, 41 clone, 50 read, and 42 write points, for a total
score of 983 (600 feature + 200 agent + 183 performance). Read earns the maximum
50 points. Read, clone, and write remain above the requested latency targets;
results vary between runs on this host.

`python3 -m unittest -v test_performance` checks cache invalidation after REST,
GraphQL, and comment mutations; MCP issue creation; authentication and duplicate
Content-Length handling; unique request IDs; Git HEAD/object/ref invalidation;
and persistence across restart. Two independent operation replays produce
byte-identical persisted state, including counters, IDs, issues, and metadata.

The older standalone `python3 test_protocols.py` stops on its expectation of the
unsupported GraphQL `Issue.createdAt` field. The same failure reproduces with
the unmodified HEAD version of `main.py`; the full 120-check suite passes.

## Earlier real-client benchmark

The measurements and validation notes below predate the response-cache changes.

Run `python3 benchmark.py --rounds 3` from this directory. Use
`python3 benchmark.py --rounds 1 --profile` for separate diagnostic profiling;
profiling serializes instrumentation and its timings are not comparable.

The benchmark starts a fresh in-process server and repository per round. It uses
real Git clients for 200 sequential pushes of prepared commits (each replaces a
1 KiB file) and 50 concurrent protocol-v2 depth-1 clones of that 200-commit history.
It then uses one `http.client` connection for 2,000 issue reads and 200 issue
creations, checking that the socket is reused. Commit preparation and clone
verification are outside the timers; Git process startup is inside the timers.
Scratch directories are created and removed inside this repository to respect
the workspace restriction.

Measurements used Python 3.12 on Linux, loopback networking, and `Store('')`
(persistence disabled). Files contain repetitive text. These results do not
measure disk snapshot costs or large, incompressible clone payloads.

## Medians, seconds (three rounds per stage)

| Stage | Push | Clone | Read | Write |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 11.096360 | 0.625607 | 82.173711 | 8.222008 |
| TCP_NODELAY | 2.845253 | 0.567035 | 0.580243 | 0.065100 |
| Incremental reachability validation | 2.812540 | 0.573870 | 0.585486 | 0.063468 |
| Shallow pack cache | 2.741659 | 0.583632 | 0.586242 | 0.061888 |
| Issue snapshot fast path (final) | 2.856452 | 0.592904 | 0.536548 | 0.067384 |

Final individual rounds:

| Round | Push | Clone | Read | Write |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2.778312 | 0.549501 | 0.546345 | 0.066851 |
| 2 | 2.856452 | 0.740137 | 0.536548 | 0.069196 |
| 3 | 2.867976 | 0.592904 | 0.504393 | 0.067384 |

Nagle/delayed ACK latency dominated the baseline. Diagnostic profiling after
disabling Nagle identified reachability traversal as the largest Git cost:
300 calls took about 0.475 cumulative seconds under instrumentation. Pushes now
validate new closures against previously validated immutable objects and use a
quarantine mapping without copying the entire object database. Shallow packs
are reused for identical wants and refs, with one bounded cache entry per repo.
Ordinary issue reads reuse the existing top-level snapshot and serialize outside
the repository lock. Clone gains are small and noisy for a three-object tip.

Validation: `python3 test_protocols.py` passed all 17 protocol check groups,
the lifecycle/persistence checks, and merge conflict checks.
`python3 -m unittest -v test_performance` passed both targeted cache and malformed
push regression tests. `git diff --check` passed.

## How to reproduce

From the repository root, with Python and Git available, run:

```sh
python3 benchmark.py
```

This runs three rounds by default and prints each workload's elapsed seconds
followed by a `MEDIANS` JSON object. Each round starts its own loopback server with
persistence disabled, so no running forge, environment registration, or shop token
is needed. It measures 200 sequential pushes, 50 concurrent depth-1 clones, 2,000
issue reads, and 200 issue creations, and removes its repository-local scratch
directories afterward. Use `--rounds 1` for a shorter run; use
`--rounds 1 --profile` separately for diagnostic profiling, whose serialized
instrumentation changes the timing conditions.
