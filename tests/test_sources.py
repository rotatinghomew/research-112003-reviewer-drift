"""核对业务资料的格式与可关联性。"""
import csv
from pathlib import Path
import unittest

DATA = Path(__file__).resolve().parents[1] / 'data'
TABLES = {'ratings': ('clip', 'reviewer', 'wave', 'category', 'difficulty', 'standard', 'origin', 'label', 'pack', 'known'), 'anchors': ('clip', 'standard', 'wave', 'label', 'source', 'known'), 'pack_events': ('pack', 'known', 'affected_group', 'state'), 'people': ('reviewer', 'team', 'category', 'can_countersign', 'available'), 'decisions': ('id', 'wave', 'category', 'asof', 'min_anchors', 'max_error', 'reviewer', 'scope'), 'queue': ('case', 'clip', 'category', 'author', 'second', 'decision', 'state'), 'appeals': ('case', 'known', 'scope', 'disposition')}
RELATION = ('queue', 'decision', 'decisions', 'id')


class SourceContract(unittest.TestCase):
    def read(self, table):
        with (DATA / f'{table}.csv').open(encoding='utf-8', newline='') as file:
            return list(csv.DictReader(file))

    def test_shape(self):
        for name, columns in TABLES.items():
            with self.subTest(table=name):
                rows = self.read(name)
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(tuple(row), columns)
                    self.assertNotIn(None, row.values())

    def test_references(self):
        left, left_key, right, right_key = RELATION
        self.assertLessEqual({row[left_key] for row in self.read(left)}, {row[right_key] for row in self.read(right)})

    def test_source_variation(self):
        self.assertEqual({row['state'] for row in self.read('queue') if row['decision'] == 'd1'}, {'pending', 'countersigned', 'executed'})
        self.assertTrue(any(row['state'] == 'leaked' for row in self.read('pack_events')))
