import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from zipfile import ZipFile

import publisher


SHA = 'a' * 40
DATABASE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


def package(**extra):
    config = {'assets': {'directory': './public', 'binding': 'ASSETS', 'run_worker_first': ['/api/*']},
              'compatibility_date': '2026-01-01', 'compatibility_flags': [], 'keep_vars': True,
              'main': 'worker.js', 'name': 'example', 'triggers': {},
              'd1_databases': [{'binding': 'DB', 'database_id': DATABASE}]}
    config.update(extra)
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        for name in ['worker.js', 'public/index.html', 'public/boot.js']:
            archive.writestr(name, '// fixture')
        archive.writestr('wrangler.json', json.dumps(config))
    return stream.getvalue()


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class PublisherTests(TestCase):
    def setUp(self):
        self.workspace = TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name)
        self.run = {'id': 5, 'head_branch': 'main', 'path': '.github/workflows/release.yml',
                    'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
                    'head_sha': SHA, 'run_attempt': 2}
        self.jobs = {'jobs': [{'name': 'verify', 'conclusion': 'success',
                               'steps': [{'name': publisher.VERIFIED_STEP, 'conclusion': 'success'}]}]}
        self.artifacts = {'artifacts': [{'id': 9, 'name': 'verified-handoff-5-2', 'expired': False,
                                         'digest': digest(package())}]}

    def read(self, path):
        if '/jobs?' in path:
            return self.jobs
        if '/artifacts?' in path:
            return self.artifacts
        return self.run

    def test_only_exact_attempt_of_approved_run_is_selected(self):
        selected = publisher.select_artifact(self.read, 'example/private', SHA, '5')
        self.assertEqual(selected['id'], 9)
        self.run['run_attempt'] = 3
        with self.assertRaisesRegex(publisher.Rejected, 'missing or expired'):
            publisher.select_artifact(self.read, 'example/private', SHA, '5')

    def test_rejects_wrong_controller_commit_and_skipped_validation(self):
        with self.assertRaisesRegex(publisher.Rejected, 'unapproved'):
            publisher.select_artifact(self.read, 'example/private', 'b' * 40, '5')
        self.jobs['jobs'][0]['steps'][0]['conclusion'] = 'skipped'
        with self.assertRaisesRegex(publisher.Rejected, 'verification did not produce'):
            publisher.select_artifact(self.read, 'example/private', SHA, '5')

    def test_rejects_changed_archive_and_parent_paths(self):
        with self.assertRaisesRegex(publisher.Rejected, 'digest differs'):
            publisher.unpack_verified(package(), 'sha256:' + '0' * 64, self.directory)
        stream = io.BytesIO()
        with ZipFile(stream, 'w') as archive:
            archive.writestr('public/../outside', 'fixture')
        data = stream.getvalue()
        with self.assertRaisesRegex(publisher.Rejected, 'Unexpected artifact entry'):
            publisher.unpack_verified(data, digest(data), self.directory)

    def test_checks_exact_worker_and_database(self):
        data = package()
        publisher.unpack_verified(data, digest(data), self.directory)
        publisher.check_worker(self.directory, 'example', DATABASE)
        with self.assertRaisesRegex(publisher.Rejected, 'Worker target'):
            publisher.check_worker(self.directory, 'different', DATABASE)
        with self.assertRaisesRegex(publisher.Rejected, 'D1 binding'):
            publisher.check_worker(self.directory, 'example', 'bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee')

    def test_operational_entry_hands_same_worker_to_uploader_only_after_verification(self):
        data = package()
        env = {'HANDOFF_ACTIONS_READ': 'source-fixture', 'SOURCE_REPOSITORY': 'example/private',
               'SOURCE_WORKFLOW_SHA': SHA, 'SOURCE_RUN_ID': '5', 'WORKER_NAME': 'example',
               'WORKER_DATABASE_ID': DATABASE, 'PATH': 'fixture', 'WRANGLER_BIN': '/test/wrangler.js',
               'CLOUDFLARE_ACCOUNT_ID': 'account-fixture', 'CLOUDFLARE_API_TOKEN': 'upload-fixture'}

        def fetch(path, token, download=False):
            self.assertEqual(token, 'source-fixture')
            return data if download else self.read(path)

        def upload(args, cwd, env, **options):
            self.assertIn('--no-bundle', args)
            self.assertEqual((cwd / 'worker.js').read_text(), '// fixture')
            self.assertNotIn('HANDOFF_ACTIONS_READ', env)
            self.assertEqual(env['CLOUDFLARE_API_TOKEN'], 'upload-fixture')
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(publisher, 'github', side_effect=fetch), \
                mock.patch.object(publisher.subprocess, 'run', side_effect=upload) as uploader:
            publisher.run()
            uploader.assert_not_called()
            os.environ['PUBLISH'] = 'true'
            publisher.run()
            uploader.assert_called_once()
            data = package(name='wrong-worker')
            self.artifacts['artifacts'][0]['digest'] = digest(data)
            with self.assertRaises(publisher.Rejected):
                publisher.run()
            uploader.assert_called_once()
