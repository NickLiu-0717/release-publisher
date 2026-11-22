import hashlib
import io
import json
import os
import subprocess
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


def isolated_package(*, include_vars=True, **extra):
    config = {'name': 'example-ci', 'main': 'worker.js', 'compatibility_date': '2026-01-01',
              'assets': {'directory': './public', 'binding': 'ASSETS', 'run_worker_first': True},
              'workers_dev': False, 'preview_urls': False,
              'vars': {'OWNER_EMAIL': 'owner@example.test', 'APP_ORIGIN': 'https://worker.example.test',
                       'GOOGLE_CLIENT_ID': 'example-inactive-client'},
              'd1_databases': [{'binding': 'DB', 'database_name': 'example-ci',
                                'database_id': publisher.FIXTURE_DATABASE, 'migrations_dir': 'migrations'}]}
    if not include_vars:
        config.pop('vars')
    config.update(extra)
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        for name in ['worker.js', 'worker.mjs.map', 'public/index.html', 'public/app.mjs', 'public/sw.js']:
            archive.writestr(name, '// fixture')
        archive.writestr('public/manifest.webmanifest', json.dumps({'id': '/', 'start_url': '/', 'scope': '/'}))
        archive.writestr('wrangler.json', json.dumps(config))
    return stream.getvalue()


class PublisherTests(TestCase):
    def test_ledger_reuses_shared_environments_without_another_workers_token(self):
        workflow = (Path(__file__).parent / '.github/workflows/publish.yml').read_text(encoding='utf-8')
        production, validation = workflow.split('\n  isolated:', 1)
        self.assertIn('ledger) profile=ledger-worker-production; environment=spa-worker-production', production)
        self.assertNotIn('environment: ledger-worker-production', workflow)
        self.assertIn('environment: handoff-validation', validation)
        self.assertIn("inputs.ledger_worker && inputs.publish", production)
        self.assertIn('!inputs.publish', validation)
        self.assertIn("secrets.LEDGER_CLOUDFLARE_API_TOKEN || needs.select.outputs.profile != 'ledger-worker-production' && secrets.CLOUDFLARE_API_TOKEN", production)
        self.assertNotIn('CLOUDFLARE_API_TOKEN', validation)
        for job in (production, validation):
            self.assertIn('private-key: ${{ secrets.HANDOFF_APP_PRIVATE_KEY }}', job)

    def test_workflow_dispatch_maps_every_app_to_the_same_approval_step(self):
        workflow = (Path(__file__).parent / '.github/workflows/publish.yml').read_text(encoding='utf-8')
        selection, production = workflow.split('\n  publish:', 1)
        production = production.split('\n  isolated:', 1)[0]
        script = selection.split('        run: |\n', 1)[1].strip('\n')
        choices = {
            'fitness': ('production', 'production'),
            'calendar': ('bound-worker', 'bound-worker-production'),
            'split': ('routed-worker-production', 'routed-worker-production'),
            'fridge': ('spa-worker-production', 'spa-worker-production'),
            'ledger': ('ledger-worker-production', 'spa-worker-production'),
        }
        declared = selection.split('        options:\n', 1)[1].split('      source_run:', 1)[0]
        self.assertEqual({line.strip().removeprefix('- ') for line in declared.splitlines()},
                         {'legacy', *choices})
        self.assertNotIn('APPROVED_ARTIFACT_DIGEST', workflow)
        verify = production.split('name: Verify privately built artifact', 1)[1].split('      - if: inputs.publish', 1)[0]
        deploy = production.split('name: Deploy privately built artifact', 1)[1]
        self.assertIn("RECORD_ARTIFACT_APPROVAL: 'true'", verify)
        self.assertNotIn('CLOUDFLARE_API_TOKEN', verify)
        self.assertIn('APPROVED_ARTIFACT: ${{ steps.approval.outputs.artifact }}', deploy)
        output = self.directory / 'selection'
        env = {**os.environ, **dict.fromkeys(('LEDGER', 'SPA', 'ROUTED', 'BOUND', 'ISOLATED'), 'false'),
               'GITHUB_OUTPUT': str(output)}
        for project, (profile, environment) in choices.items():
            with self.subTest(project=project):
                output.write_text('')
                subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], check=True,
                               env={**env, 'PROJECT': project})
                self.assertEqual(output.read_text(), f'profile={profile}\nenvironment={environment}\n')
        for overrides in ({'PROJECT': 'unknown'}, {'PROJECT': 'ledger', 'SPA': 'true'}):
            output.write_text('')
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env={**env, **overrides})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), '')

    def setUp(self):
        self.workspace = TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name)
        self.run = {'id': 5, 'head_branch': 'main', 'path': '.github/workflows/release.yml',
                    'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
                    'head_sha': SHA, 'run_attempt': 2}
        self.jobs = {'jobs': [{'name': 'verify', 'conclusion': 'success',
                               'steps': [{'name': publisher.VERIFIED_STEP, 'conclusion': 'success'}]}]}
        self.jobs['jobs'].extend({'name': name, 'conclusion': 'success'} for name in ('plan', 'prepare'))
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

    def test_spa_worker_requires_exact_target_bindings_and_routing(self):
        config = {
            'name': 'example', 'main': 'worker.js', 'compatibility_date': '2026-01-01',
            'compatibility_flags': ['nodejs_compat'], 'workers_dev': True,
            'preview_urls': False, 'keep_vars': True,
            'assets': {'directory': './dist', 'binding': 'ASSETS',
                       'run_worker_first': ['/api/*', '/auth/*'],
                       'html_handling': 'none', 'not_found_handling': 'single-page-application'},
            'triggers': {'crons': ['*/15 * * * *']},
            'd1_databases': [{'binding': 'DB', 'database_name': 'example-data', 'database_id': DATABASE}]
        }
        stream = io.BytesIO()
        with ZipFile(stream, 'w') as archive:
            for path in ('worker.js', 'dist/index.html', 'dist/app.mjs', 'dist/sw.js'):
                archive.writestr(path, '// fixture')
            archive.writestr('dist/manifest.webmanifest', json.dumps({'id': '/', 'start_url': '/', 'scope': '/'}))
            archive.writestr('wrangler.json', json.dumps(config))
        data = stream.getvalue()
        publisher.unpack_verified(data, digest(data), self.directory, extended=True, asset_dir='dist')
        publisher.check_spa_worker(self.directory, 'example', DATABASE, 'example-data')
        with self.assertRaisesRegex(publisher.Rejected, 'Unexpected Workers'):
            publisher.check_spa_worker(self.directory, 'other', DATABASE, 'example-data')
        with self.assertRaisesRegex(publisher.Rejected, 'Missing approved'):
            publisher.check_spa_worker(self.directory, 'example', publisher.FIXTURE_DATABASE, 'example-data')
        config['assets']['run_worker_first'] = ['/api/*']
        (self.directory / 'wrangler.json').write_text(json.dumps(config), encoding='utf-8')
        with self.assertRaisesRegex(publisher.Rejected, 'Unexpected Workers'):
            publisher.check_spa_worker(self.directory, 'example', DATABASE, 'example-data')

    def test_ledger_handoff_rejects_changed_bindings_and_schedule(self):
        config = {
            'name': 'ledger', 'main': 'worker.js', 'compatibility_date': '2026-11-03',
            'workers_dev': True, 'preview_urls': False, 'keep_vars': True,
            'assets': {'directory': './dist', 'binding': 'ASSETS', 'run_worker_first': True,
                       'not_found_handling': 'single-page-application', 'html_handling': 'none'},
            'd1_databases': [{'binding': 'AI_USAGE', 'database_name': 'daily-ledger-db', 'database_id': DATABASE}],
            'services': [{'binding': 'SPLIT', 'service': 'together-split', 'entrypoint': 'SplitBridge'}],
            'triggers': {'crons': ['*/5 * * * *']}
        }
        stream = io.BytesIO()
        with ZipFile(stream, 'w') as archive:
            for path in ('worker.js', 'dist/index.html', 'dist/sw.js', 'dist/assets/index-fixture.js',
                         'dist/apple-touch-icon.png'):
                archive.writestr(path, '// fixture')
            archive.writestr('dist/manifest.webmanifest', json.dumps({'start_url': '/', 'scope': '/'}))
            archive.writestr('wrangler.json', json.dumps(config))
        data = stream.getvalue()
        publisher.unpack_verified(data, digest(data), self.directory, extended=True, asset_dir='dist', ledger=True)
        publisher.check_ledger_worker(self.directory, 'ledger', DATABASE)
        with self.assertRaisesRegex(publisher.Rejected, 'Missing approved Ledger'):
            publisher.check_ledger_worker(self.directory, 'ledger-ci', DATABASE)
        with self.assertRaisesRegex(publisher.Rejected, 'Unexpected Ledger'):
            publisher.check_ledger_worker(self.directory, 'ledger', 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff')
        for changed in ({'services': []}, {'vars': {'OWNER_EMAIL': 'fixture@example.test'}},
                        {'triggers': {'crons': ['*/15 * * * *']}}):
            with self.subTest(changed=changed):
                (self.directory / 'wrangler.json').write_text(json.dumps(config | changed), encoding='utf-8')
                with self.assertRaisesRegex(publisher.Rejected, 'Unexpected Ledger'):
                    publisher.check_ledger_worker(self.directory, 'ledger', DATABASE)

    def test_rejects_wrong_controller_commit_and_skipped_validation(self):
        with self.assertRaisesRegex(publisher.Rejected, 'unapproved'):
            publisher.select_artifact(self.read, 'example/private', 'b' * 40, '5')
        self.jobs['jobs'][0]['steps'][0]['conclusion'] = 'skipped'
        with self.assertRaisesRegex(publisher.Rejected, 'verification did not produce'):
            publisher.select_artifact(self.read, 'example/private', SHA, '5')

    def test_completed_run_must_include_successful_plan_and_prepare(self):
        for name in ('plan', 'prepare'):
            job = next(job for job in self.jobs['jobs'] if job['name'] == name)
            with self.subTest(job=name), mock.patch.dict(job, conclusion='skipped'):
                with self.assertRaisesRegex(publisher.Rejected, f'Source {name} job did not succeed'):
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

        def fetch(path, token, download=False, limit=publisher.MAX_ARCHIVE):
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
            output = self.directory / 'approval'
            with mock.patch.dict(os.environ, {'RECORD_ARTIFACT_APPROVAL': 'true', 'GITHUB_OUTPUT': str(output)}):
                publisher.run()
            uploader.assert_not_called()
            os.environ['PUBLISH'] = 'true'
            os.environ['APPROVED_ARTIFACT'] = output.read_text().strip().removeprefix('artifact=')
            self.assertEqual(os.environ['APPROVED_ARTIFACT'], f'9:{digest(data)}')
            publisher.run()
            uploader.assert_called_once()
            self.artifacts['artifacts'][0]['id'] = 10
            with self.assertRaisesRegex(publisher.Rejected, 'approved ID and digest'):
                publisher.run()
            self.artifacts['artifacts'][0]['id'] = 9
            data = package(name='wrong-worker')
            with self.assertRaisesRegex(publisher.Rejected, 'archive digest differs'):
                publisher.run()
            self.artifacts['artifacts'][0]['digest'] = digest(data)
            with self.assertRaisesRegex(publisher.Rejected, 'approved ID and digest'):
                publisher.run()
            os.environ['APPROVED_ARTIFACT'] = f'9:{digest(data)}'
            with self.assertRaisesRegex(publisher.Rejected, 'Worker target'):
                publisher.run()
            uploader.assert_called_once()

    def test_isolated_worker_uses_its_own_branch_archive_limit_and_settings(self):
        data = isolated_package()
        self.run['head_branch'] = 'example-isolation'
        self.artifacts['artifacts'][0]['digest'] = digest(data)
        env = {'HANDOFF_PROFILE': 'isolated', 'HANDOFF_ACTIONS_READ': 'source-fixture',
               'SOURCE_REPOSITORY': 'example/private', 'SOURCE_WORKFLOW_SHA': SHA,
               'SOURCE_WORKFLOW_BRANCH': 'example-isolation', 'SOURCE_RUN_ID': '5',
               'WORKER_NAME': 'example-ci', 'WORKER_DATABASE_ID': publisher.FIXTURE_DATABASE}

        def fetch(path, token, download=False, limit=publisher.MAX_ARCHIVE):
            if download:
                self.assertEqual(limit, 32 * 1024 * 1024)
                return data
            return self.read(path)

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(publisher, 'github', side_effect=fetch), \
                mock.patch.object(publisher.subprocess, 'run') as uploader:
            publisher.run()
            os.environ['PUBLISH'] = 'true'
            with self.assertRaisesRegex(publisher.Rejected, 'cannot deploy'):
                publisher.run()
            os.environ['PUBLISH'] = 'false'
            os.environ['SOURCE_WORKFLOW_BRANCH'] = 'main'
            with self.assertRaisesRegex(publisher.Rejected, 'unapproved'):
                publisher.run()
            uploader.assert_not_called()

    def test_isolated_worker_rejects_production_configuration_and_oversize_archives(self):
        data = isolated_package(workers_dev=True)
        publisher.unpack_verified(data, digest(data), self.directory, extended=True)
        with self.assertRaisesRegex(publisher.Rejected, 'Unexpected isolated Worker'):
            publisher.check_isolated_worker(self.directory, 'example-ci', publisher.FIXTURE_DATABASE)
        with mock.patch.object(publisher, 'MAX_ISOLATED_ARCHIVE', len(data) - 1):
            with self.assertRaisesRegex(publisher.Rejected, 'archive is too large'):
                publisher.unpack_verified(data, digest(data), self.directory, extended=True)

    def test_bound_worker_checks_existing_binding_and_preserves_vars_before_upload(self):
        fixture = isolated_package()
        with ZipFile(io.BytesIO(fixture)) as original:
            config = json.loads(original.read('wrangler.json'))
            del config['vars']
            config.update(name='example', workers_dev=True, keep_vars=True)
            config['d1_databases'][0].update(database_name='example', database_id=DATABASE)
            stream = io.BytesIO()
            with ZipFile(stream, 'w') as archive:
                for member in original.namelist():
                    archive.writestr(member, json.dumps(config) if member == 'wrangler.json' else original.read(member))
        data = stream.getvalue()
        self.run['head_branch'] = 'example-bound'
        self.artifacts['artifacts'][0]['digest'] = digest(data)
        env = {'HANDOFF_PROFILE': 'bound-worker', 'HANDOFF_ACTIONS_READ': 'source-fixture',
               'SOURCE_REPOSITORY': 'example/private', 'SOURCE_WORKFLOW_SHA': SHA,
               'SOURCE_WORKFLOW_BRANCH': 'example-bound', 'SOURCE_RUN_ID': '5',
               'WORKER_NAME': 'example', 'WORKER_DATABASE_ID': DATABASE, 'PATH': 'fixture',
               'WRANGLER_BIN': '/test/wrangler.js', 'CLOUDFLARE_ACCOUNT_ID': 'account-fixture',
               'CLOUDFLARE_API_TOKEN': 'upload-fixture'}

        def fetch(path, token, download=False, limit=publisher.MAX_ARCHIVE):
            return data if download else self.read(path)

        def upload(args, cwd, **options):
            self.assertIn('--no-bundle', args)
            self.assertEqual(json.loads((cwd / 'wrangler.json').read_text()), config)
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(publisher, 'github', side_effect=fetch), \
                mock.patch.object(publisher.subprocess, 'run', side_effect=upload) as uploader:
            publisher.run()
            uploader.assert_not_called()
            os.environ['PUBLISH'] = 'true'
            os.environ['APPROVED_ARTIFACT'] = f'9:{digest(data)}'
            publisher.run()
            uploader.assert_called_once()
            os.environ['WORKER_DATABASE_ID'] = 'bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee'
            with self.assertRaisesRegex(publisher.Rejected, 'Unexpected production Worker'):
                publisher.run()
            uploader.assert_called_once()

    def test_routed_worker_preserves_routes_and_rejects_a_different_service(self):
        config = {
            'name': 'example', 'main': 'worker.js', 'compatibility_date': '2026-01-01',
            'assets': {'directory': './public', 'binding': 'ASSETS',
                       'run_worker_first': ['/', '/api/*', '/auth/*'],
                       'html_handling': 'none', 'not_found_handling': 'none'},
            'workers_dev': True, 'preview_urls': False, 'keep_vars': True,
            'd1_databases': [{'binding': 'DB', 'database_name': 'example', 'database_id': DATABASE,
                              'migrations_dir': 'migrations'}],
            'services': [{'binding': 'LEDGER', 'service': 'linked-worker', 'entrypoint': 'LinkedBridge'}]
        }
        data = isolated_package(include_vars=False, **config)
        publisher.unpack_verified(data, digest(data), self.directory, extended=True)
        publisher.check_bound_worker(self.directory, 'example', DATABASE, 'linked-worker', 'LinkedBridge')
        with self.assertRaisesRegex(publisher.Rejected, 'target or bindings'):
            publisher.check_bound_worker(self.directory, 'example', DATABASE, 'other-worker', 'LinkedBridge')

    def test_routed_validation_rejects_publish_before_accessing_credentials(self):
        with mock.patch.dict(os.environ, {'HANDOFF_PROFILE': 'routed-worker', 'PUBLISH': 'true'}, clear=True):
            with self.assertRaisesRegex(publisher.Rejected, 'Validation-only handoff cannot deploy'):
                publisher.run()

    def test_production_profiles_pin_the_independently_verified_archive(self):
        env = {
            'HANDOFF_PROFILE': 'routed-worker-production', 'PUBLISH': 'true',
            'HANDOFF_ACTIONS_READ': 'source-fixture', 'SOURCE_REPOSITORY': 'example/private',
            'SOURCE_WORKFLOW_SHA': SHA, 'SOURCE_WORKFLOW_BRANCH': 'main', 'SOURCE_RUN_ID': '5',
        }
        for profile in ('production', 'bound-worker', 'routed-worker-production',
                        'spa-worker-production', 'ledger-worker-production'):
            env['HANDOFF_PROFILE'] = profile
            with self.subTest(profile=profile), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(publisher, 'github', side_effect=lambda path, token: self.read(path)), \
                    mock.patch.object(publisher.subprocess, 'run') as uploader:
                with self.assertRaisesRegex(publisher.Rejected, 'approved ID and digest'):
                    publisher.run()
                uploader.assert_not_called()
