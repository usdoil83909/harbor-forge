"""Correctness checks for cached Git paths."""
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


if __name__ == '__main__':
    unittest.main()
