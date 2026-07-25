#!/bin/python3

import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PARENT     = os.path.join(os.path.dirname(__file__), os.path.pardir)
CLIENT_DIR = os.path.abspath(os.path.join(PARENT, 'Client'))


def import_client_utils():
    sys.path.insert(0, CLIENT_DIR)
    return importlib.import_module('utils')


class FakeResponse:

    status_code = 200

    def __init__(self, content=b'zip-data'):
        self.content = content
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


class PrivateSourceDownloadTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.utils = import_client_utils()

    @patch('utils.requests.post')
    def test_private_source_uses_worker_session_without_github_token(self, mock_post):
        response = FakeResponse()
        mock_post.return_value = response
        request = {
            'server' : 'https://bench.example/',
            'payload' : {
                'machine_id' : 7,
                'secret'     : 'session-secret',
                'test_id'    : 12,
                'side'       : 'dev',
            },
        }

        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'source.zip')
            self.utils.download_engine_source(
                'openbench://github/keinoda/YaneuraOu-private/%s.zip' % ('a' * 40),
                target,
                request,
            )
            with open(target, 'rb') as source:
                self.assertEqual(source.read(), b'zip-data')

        self.assertTrue(response.closed)
        self.assertEqual(
            mock_post.call_args.args[0],
            'https://bench.example/clientGetGitHubArchive/')
        self.assertEqual(mock_post.call_args.kwargs['data'], request['payload'])
        self.assertNotIn('Authorization', mock_post.call_args.kwargs)

    @patch('utils.requests.get')
    def test_public_source_keeps_direct_github_download(self, mock_get):
        response = FakeResponse(b'public-zip')
        mock_get.return_value = response

        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'source.zip')
            self.utils.download_engine_source(
                'https://github.com/keinoda/YaneuraOu/archive/main.zip',
                target,
            )
            with open(target, 'rb') as source:
                self.assertEqual(source.read(), b'public-zip')

        mock_get.assert_called_once()
        self.assertTrue(response.closed)

    def test_private_source_requires_worker_session(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(self.utils.OpenBenchFatalWorkerException):
                self.utils.download_engine_source(
                    'openbench://github/keinoda/YaneuraOu-private/%s.zip' % ('a' * 40),
                    os.path.join(root, 'source.zip'),
                )


if __name__ == '__main__':
    unittest.main()
