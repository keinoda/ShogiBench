import hashlib
import json
import os
import unittest
import zipfile


BOOKS = {
    'peta1204_d8d10_32to80_shogi.epd': {
        'display': 'peta1204_depth8_diff10_32to80_shogi.epd',
        'positions': 29_998,
    },
    'peta1204_d8d25_32to80_shogi.epd': {
        'display': 'peta1204_depth8_diff25_32to80_shogi.epd',
        'positions': 29_979,
    },
}
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPeta1204BookAsset(unittest.TestCase):

    def test_configと配布資産が一致する(self):
        config_path = os.path.join(PROJECT_ROOT, 'Config', 'config.json')

        with open(config_path, encoding='utf-8') as fin:
            config = json.load(fin)

        for book_name, expected in BOOKS.items():
            with self.subTest(book_name=book_name):
                metadata_path = os.path.join(PROJECT_ROOT, 'Books', book_name + '.json')
                archive_path = os.path.join(PROJECT_ROOT, 'Books', 'dist', book_name + '.zip')

                with open(metadata_path, encoding='utf-8') as fin:
                    metadata = json.load(fin)

                self.assertIn(book_name, config['books'])
                self.assertEqual(metadata['display'], expected['display'])
                self.assertEqual(
                    metadata['source'],
                    'https://raw.githubusercontent.com/keinoda/ShogiBench/'
                    'shogi/Books/dist/' + book_name + '.zip',
                )

                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual(archive.namelist(), [book_name])
                    content = archive.read(book_name)

                self.assertEqual(hashlib.sha256(content).hexdigest(), metadata['sha'])

                lines = content.decode('utf-8').splitlines()
                self.assertEqual(len(lines), expected['positions'])
                self.assertEqual(len(set(lines)), expected['positions'])
                self.assertTrue(all(len(line.split()) == 4 for line in lines))

                moves_played = [int(line.rsplit(' ', 1)[1]) - 1 for line in lines]
                self.assertGreaterEqual(min(moves_played), 32)
                self.assertLessEqual(max(moves_played), 80)


if __name__ == '__main__':
    unittest.main()
