import hashlib
import json
import os
import unittest
import zipfile


BOOK_NAME = 'peta1204_d8d10_32to80_shogi.epd'
EXPECTED_POSITIONS = 29_998
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPeta1204BookAsset(unittest.TestCase):

    def test_configと配布資産が一致する(self):
        config_path = os.path.join(PROJECT_ROOT, 'Config', 'config.json')
        metadata_path = os.path.join(PROJECT_ROOT, 'Books', BOOK_NAME + '.json')
        archive_path = os.path.join(PROJECT_ROOT, 'Books', 'dist', BOOK_NAME + '.zip')

        with open(config_path, encoding='utf-8') as fin:
            config = json.load(fin)
        with open(metadata_path, encoding='utf-8') as fin:
            metadata = json.load(fin)

        self.assertIn(BOOK_NAME, config['books'])
        self.assertEqual(
            metadata['source'],
            'https://raw.githubusercontent.com/keinoda/ShogiBench/'
            'shogi/Books/dist/' + BOOK_NAME + '.zip',
        )

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.namelist(), [BOOK_NAME])
            content = archive.read(BOOK_NAME)

        self.assertEqual(hashlib.sha256(content).hexdigest(), metadata['sha'])

        lines = content.decode('utf-8').splitlines()
        self.assertEqual(len(lines), EXPECTED_POSITIONS)
        self.assertEqual(len(set(lines)), EXPECTED_POSITIONS)
        self.assertTrue(all(len(line.split()) == 4 for line in lines))

        moves_played = [int(line.rsplit(' ', 1)[1]) - 1 for line in lines]
        self.assertGreaterEqual(min(moves_played), 32)
        self.assertLessEqual(max(moves_played), 80)


if __name__ == '__main__':
    unittest.main()
