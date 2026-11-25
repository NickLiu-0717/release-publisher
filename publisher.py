import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


API = 'https://api.github.com'
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_ISOLATED_ARCHIVE = 32 * 1024 * 1024
FIXTURE_DATABASE = '00000000-0000-4000-8000-000000000000'
VERIFIED_STEP = 'Store verified handoff artifact'


class Rejected(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Rejected(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None


def github(path, token, download=False, limit=MAX_ARCHIVE):
    request = Request(API + path, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with build_opener(NoRedirect).open(request, timeout=25) as response:
            require(not download, 'Expected a GitHub artifact download redirect.')
            return json.load(response)
    except HTTPError as response:
        with response:
            require(download and response.code == 302, 'GitHub refused a source metadata or artifact request.')
            location = response.headers.get('Location', '')
    require(urlparse(location).scheme == 'https', 'Invalid artifact download destination.')
    with urlopen(location, timeout=25) as response:
        require(urlparse(response.geturl()).scheme == 'https', 'Invalid artifact download redirect.')
        data = response.read(limit + 1)
    require(len(data) <= limit, 'Artifact archive is too large.')
    return data


def select_artifact(read, repository, trusted_sha, run_id, branch='main'):
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Missing or invalid private controller repository.')
    require(re.fullmatch(r'[a-f0-9]{40}', trusted_sha), 'Missing or invalid approved controller commit.')
    require(re.fullmatch(r'[1-9][0-9]{0,19}', run_id), 'Invalid workflow run number.')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]*', branch), 'Invalid approved controller branch.')
    run_path = f'/repos/{repository}/actions/runs/{run_id}'
    run = read(run_path)
    require(str(run['id']) == run_id and run['head_branch'] == branch and
            run['path'] in ('.github/workflows/release.yml', f'.github/workflows/release.yml@refs/heads/{branch}') and
            run['event'] == 'workflow_dispatch' and run['status'] == 'completed' and
            run['conclusion'] == 'success' and run['head_sha'] == trusted_sha,
            'Source run is unsuccessful or uses an unapproved controller revision.')
    attempt = run['run_attempt']
    require(type(attempt) is int and attempt > 0, 'Invalid source run attempt.')
    jobs = read(f'{run_path}/attempts/{attempt}/jobs?per_page=100')['jobs']
    for required in ('plan', 'prepare'):
        matches = [job for job in jobs if job['name'] == required]
        require(len(matches) == 1 and matches[0]['conclusion'] == 'success',
                f'Source {required} job did not succeed.')
    verify = [job for job in jobs if job['name'] == 'verify' and job['conclusion'] == 'success' and
              any(step['name'] == VERIFIED_STEP and step['conclusion'] == 'success' for step in job['steps'])]
    require(len(verify) == 1, 'Source verification did not produce an approved handoff.')
    name = f'verified-handoff-{run_id}-{attempt}'
    artifacts = read(f'{run_path}/artifacts?per_page=100')['artifacts']
    available = [artifact for artifact in artifacts if artifact['name'] == name and artifact['expired'] is False]
    require(len(available) == 1, 'The exact verified handoff artifact is missing or expired.')
    artifact = available[0]
    require(type(artifact['id']) is int and artifact['id'] > 0 and
            re.fullmatch(r'sha256:[a-f0-9]{64}', artifact.get('digest') or ''), 'Missing GitHub artifact identity or digest.')
    return artifact


def unpack_verified(data, expected_digest, directory, extended=False, asset_dir='public', ledger=False, dtr=False, password=False, media_library=False):
    limit = MAX_ISOLATED_ARCHIVE if extended else MAX_ARCHIVE
    require(len(data) <= limit, 'Artifact archive is too large.')
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    require(digest == expected_digest, 'Artifact archive digest differs from GitHub metadata.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(len(entries) <= 250 and sum(entry.file_size for entry in entries) <= limit, 'Artifact contents are too large.')
        worker_files = {'worker.js', 'worker.js.map', 'wrangler.json'}
        if extended:
            worker_files.add('worker.mjs.map')
        files = set()
        for entry in entries:
            name = entry.filename.rstrip('/')
            parts = name.split('/')
            allowed = name in worker_files or (
                parts[0] == asset_dir and all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', part) for part in parts))
            kind = stat.S_IFMT(entry.external_attr >> 16)
            require(allowed and kind in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG), 'Unexpected artifact entry.')
            if entry.is_dir():
                require(parts[0] == asset_dir, 'Unexpected artifact directory.')
                continue
            require(name not in files, 'Duplicate artifact name.')
            files.add(name)
            destination = directory.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as output:
                output.write(archive.read(entry))
        required = {'worker.js', 'wrangler.json', f'{asset_dir}/index.html'}
        if dtr:
            required.update(f'{asset_dir}/{name}' for name in ('app.js', 'boot.js', 'apple-touch-icon.png', 'drive-save.js',
                                                               'dtr-import.js', 'gmail-send.js'))
        elif extended:
            required.update(f'{asset_dir}/{name}' for name in ('sw.js', 'manifest.webmanifest'))
            if not (ledger or password or media_library):
                required.add(f'{asset_dir}/app.mjs')
        else:
            required.add('public/boot.js')
        require(required <= files, 'Required worker files are absent.')


def check_worker(directory, worker, database):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', worker), 'Missing or invalid allowed Worker name.')
    require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', database), 'Missing or invalid allowed D1 binding.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    allowed = {'assets', 'compatibility_date', 'compatibility_flags', 'd1_databases', 'keep_vars', 'main', 'name', 'triggers'}
    require(set(config) == allowed and config['name'] == worker and config['main'] == 'worker.js' and config['keep_vars'] is True,
            'Unexpected Worker target or deploy settings.')
    bindings = config['d1_databases']
    require(len(bindings) == 1 and bindings[0]['binding'] == 'DB' and bindings[0]['database_id'] == database and
            config['assets'] == {'directory': './public', 'binding': 'ASSETS', 'run_worker_first': ['/api/*']},
            'Unexpected asset or D1 binding.')


def check_isolated_worker(directory, worker, database):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*-ci', worker) and database == FIXTURE_DATABASE,
            'Missing approved isolated Worker or D1 fixture.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    values = config['vars']
    require(set(values) == {'OWNER_EMAIL', 'APP_ORIGIN', 'GOOGLE_CLIENT_ID'} and
            values['OWNER_EMAIL'] == 'owner@example.test' and
            re.fullmatch(r'https://[a-z0-9-]+\.example\.test', values['APP_ORIGIN']) and
            values['GOOGLE_CLIENT_ID'] == 'example-inactive-client',
            'Unexpected isolated login fixtures.')
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', config['compatibility_date']), 'Invalid compatibility date.')
    require(config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': config['compatibility_date'],
        'assets': {'directory': './public', 'binding': 'ASSETS', 'run_worker_first': True},
        'workers_dev': False, 'preview_urls': False, 'vars': values,
        'd1_databases': [{'binding': 'DB', 'database_name': worker, 'database_id': database,
                          'migrations_dir': 'migrations'}]
    } and config['assets']['run_worker_first'] is True and config['workers_dev'] is False and config['preview_urls'] is False,
        'Unexpected isolated Worker target or bindings.')
    check_installation(directory)


def check_bound_worker(directory, worker, database, service=None, entrypoint=None):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', worker) and not worker.endswith('-ci') and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', database) and database != FIXTURE_DATABASE,
            'Missing approved production Worker or D1 binding.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', config['compatibility_date']), 'Invalid compatibility date.')
    assets = {'directory': './public', 'binding': 'ASSETS', 'run_worker_first': True}
    extra_bindings = {}
    if service is not None:
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', service) and
                re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', entrypoint or ''),
                'Missing approved service binding.')
        assets.update(run_worker_first=['/', '/api/*', '/auth/*'],
                      html_handling='none', not_found_handling='none')
        extra_bindings['services'] = [{'binding': 'LEDGER', 'service': service,
                                       'entrypoint': entrypoint}]
    require(config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': config['compatibility_date'],
        'assets': assets, **extra_bindings,
        'workers_dev': True, 'preview_urls': False, 'keep_vars': True,
        'd1_databases': [{'binding': 'DB', 'database_name': worker, 'database_id': database,
                          'migrations_dir': 'migrations'}]
    },
        'Unexpected production Worker target or bindings.')
    check_installation(directory)


def check_spa_worker(directory, worker, database, database_name):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', worker) and not worker.endswith('-ci') and
            re.fullmatch(r'[a-z0-9][a-z0-9-]*', database_name) and database != FIXTURE_DATABASE and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', database),
            'Missing approved Workers target or D1 binding.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    date = config.get('compatibility_date', '')
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) and config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': date,
        'compatibility_flags': ['nodejs_compat'], 'workers_dev': True, 'preview_urls': False,
        'keep_vars': True,
        'assets': {'directory': './dist', 'binding': 'ASSETS', 'run_worker_first': ['/api/*', '/auth/*'],
                   'html_handling': 'none', 'not_found_handling': 'single-page-application'},
        'triggers': {'crons': ['*/15 * * * *']},
        'd1_databases': [{'binding': 'DB', 'database_name': database_name, 'database_id': database}]
    }, 'Unexpected Workers target, assets, or D1 binding.')
    check_installation(directory, 'dist')


def check_ledger_worker(directory, worker, database):
    require(worker == 'ledger' and database != FIXTURE_DATABASE and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', database),
            'Missing approved Ledger Worker or D1 binding.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    require(config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': '2026-11-03',
        'workers_dev': True, 'preview_urls': False, 'keep_vars': True,
        'assets': {'directory': './dist', 'binding': 'ASSETS', 'run_worker_first': True,
                   'not_found_handling': 'single-page-application', 'html_handling': 'none'},
        'd1_databases': [{'binding': 'AI_USAGE', 'database_name': 'daily-ledger-db', 'database_id': database}],
        'services': [{'binding': 'SPLIT', 'service': 'together-split', 'entrypoint': 'SplitBridge'}],
        'triggers': {'crons': ['*/5 * * * *']}
    } and config['assets']['run_worker_first'] is True and config['workers_dev'] is True and
            config['preview_urls'] is False and config['keep_vars'] is True, 'Unexpected Ledger deployment settings.')
    check_installation(directory, 'dist', expected_id=None)
    require(any((directory / 'dist/assets').glob('index-*.js')) and
            (directory / 'dist/apple-touch-icon.png').is_file(), 'Required Ledger app assets are absent.')


def check_installation(directory, asset_dir='public', expected_id='/'):
    manifest = json.loads((directory / asset_dir / 'manifest.webmanifest').read_text(encoding='utf-8'))
    require([manifest.get(key) for key in ('id', 'start_url', 'scope')] == [expected_id, '/', '/'],
            'Unexpected installation identity.')


def check_media_library(directory, worker, database):
    require(worker == 'media-library' and database != FIXTURE_DATABASE and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', database),
            'Missing approved media library Worker or D1 binding.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    require(config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': '2026-10-30',
        'compatibility_flags': ['nodejs_compat'], 'workers_dev': True, 'preview_urls': False,
        'keep_vars': True, 'assets': {'directory': './public', 'binding': 'ASSETS', 'run_worker_first': True},
        'vars': {'APP_ORIGIN': f'https://{worker}.gnliu.workers.dev'},
        'd1_databases': [{'binding': 'DB', 'database_name': worker, 'database_id': database}],
    }, 'Unexpected media library deployment settings.')
    check_installation(directory)
    assets = directory / 'public'
    html = (assets / 'index.html').read_text(encoding='utf-8')
    manifest = json.loads((assets / 'manifest.webmanifest').read_text(encoding='utf-8'))
    build = re.search(r'<meta name="app-build" content="([0-9a-f]{16})">', html)
    apple = re.search(r'<link rel="apple-touch-icon" href="/(icons/app-180-[0-9a-f]{16}\.png)">', html)
    require(build and apple and manifest.get('display') == 'standalone', 'Missing media library installation assets.')
    icons = {apple.group(1)}
    for icon in manifest.get('icons', []):
        source = icon.get('src', '')
        require(re.fullmatch(r'/icons/app-(192|512)-[0-9a-f]{16}\.png', source), 'Unexpected media library icon.')
        icons.add(source[1:])
    require(len(icons) == 3 and {icon.split('-')[1] for icon in icons} == {'180', '192', '512'}, 'Incomplete media library icons.')
    for icon in icons:
        digest = hashlib.sha256((assets / icon).read_bytes()).hexdigest()[:16]
        require(icon.endswith(f'-{digest}.png'), 'Media library icon digest mismatch.')
    expected = {'index.html', 'manifest.webmanifest', 'sw.js', *icons}
    expected.update(f'_app/{build.group(1)}/{name}' for name in ('app.js', 'app.css', 'pwa-mode.js'))
    actual = {file.relative_to(assets).as_posix() for file in assets.rglob('*') if file.is_file()}
    require(actual == expected, 'Unexpected media library assets.')


def check_dtr_worker(directory, worker, database):
    require(worker == 'dtr-mobile' and database != FIXTURE_DATABASE and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', database),
            'Missing approved DTR Worker or D1 binding.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    require(config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': '2026-11-03',
        'compatibility_flags': ['nodejs_compat'], 'workers_dev': True, 'preview_urls': False,
        'keep_vars': True,
        'assets': {'directory': './dist', 'binding': 'ASSETS', 'run_worker_first': True,
                   'not_found_handling': '404-page'},
        'd1_databases': [{'binding': 'SYNC_DB', 'database_name': 'dtr-mobile-sync', 'database_id': database}],
    }, 'Unexpected DTR deployment settings.')
    assets = directory / 'dist'
    html = (assets / 'index.html').read_text(encoding='utf-8')
    require('name="apple-mobile-web-app-title" content="DTR"' in html and
            'rel="apple-touch-icon" href="/apple-touch-icon.png?' in html and
            (assets / 'apple-touch-icon.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'),
            'Unexpected DTR installation identity.')
    markers = ('fixture-dtr-folder', 'dtr-owner@example.test', 'dtr-ci.apps.googleusercontent.com', 'fixture-picker-key')
    require(not any(marker in (assets / filename).read_text(encoding='utf-8')
                    for filename in ('drive-save.js', 'dtr-import.js', 'gmail-send.js', 'index.html')
                    for marker in markers), 'Synthetic settings are present in the DTR production artifact.')


def check_password_worker(directory, worker):
    require(worker == 'gnliu-password-vault', 'Missing approved password Worker target.')
    config = json.loads((directory / 'wrangler.json').read_text(encoding='utf-8'))
    require(config == {
        'name': worker, 'main': 'worker.js', 'compatibility_date': '2026-11-20',
        'workers_dev': True, 'preview_urls': False, 'keep_vars': True,
        'assets': {'directory': './dist-worker', 'binding': 'ASSETS', 'run_worker_first': True,
                   'html_handling': 'none', 'not_found_handling': 'none'},
        'vars': {'VAULT_HOST': 'gnliu-password-vault.gnliu.workers.dev'},
    }, 'Unexpected password Worker deployment settings.')
    assets = directory / 'dist-worker'
    manifest = json.loads((assets / 'manifest.webmanifest').read_text(encoding='utf-8'))
    require(all(manifest.get(key) == '/' for key in ('id', 'scope', 'start_url')) and
            manifest.get('display') == 'standalone', 'Unexpected password installation identity.')
    html = (assets / 'index.html').read_text(encoding='utf-8')
    build = re.search(r'<meta name="vault-build" content="([0-9a-f]{16})">', html)
    icon = re.search(r'"/icons/vault-180-([0-9a-f]{16})\.png"', html)
    require(build is not None and icon is not None, 'Missing password asset versions.')
    expected = {'index.html', 'sw.js', 'manifest.webmanifest'}
    expected.update(f'_app/{build[1]}/{name}' for name in (
        'app.css', 'app.js', 'passkey.js', 'pwa-mode.js', 'storage.js', 'updates.js', 'vault.js'))
    for size in (180, 192, 512):
        expected.update((f'icons/vault-{size}.png', f'icons/vault-{size}-{icon[1]}.png'))
    actual = {path.relative_to(assets).as_posix() for path in assets.rglob('*') if path.is_file()}
    require(actual == expected, 'Unexpected password Worker assets.')


def run():
    profile = os.environ.get('HANDOFF_PROFILE', 'production')
    require(profile in ('production', 'isolated', 'bound-worker', 'routed-worker', 'routed-worker-production',
                        'spa-worker', 'spa-worker-production', 'ledger-worker-production', 'dtr-worker-production', 'password-worker-production', 'media-library-production'), 'Unknown Worker handoff profile.')
    isolated = profile == 'isolated'
    extended = profile != 'production'
    spa = profile in ('spa-worker', 'spa-worker-production')
    ledger = profile == 'ledger-worker-production'
    dtr = profile == 'dtr-worker-production'
    password = profile == 'password-worker-production'
    media_library = profile == 'media-library-production'
    publishing = os.environ.get('PUBLISH') == 'true'
    approval_output = os.environ.get('RECORD_ARTIFACT_APPROVAL') == 'true'
    require(not (publishing and approval_output), 'Artifact approval cannot deploy.')
    require(profile not in ('isolated', 'routed-worker', 'spa-worker') or not (publishing or approval_output),
            'Validation-only handoff cannot deploy.')
    token = os.environ['HANDOFF_ACTIONS_READ']
    require(bool(token), 'Private artifact read access has not been configured.')
    repository = os.environ['SOURCE_REPOSITORY']
    branch = os.environ['SOURCE_WORKFLOW_BRANCH'] if extended else 'main'
    artifact = select_artifact(lambda path: github(path, token), repository,
                               os.environ['SOURCE_WORKFLOW_SHA'], os.environ['SOURCE_RUN_ID'], branch)
    identity = f'{artifact["id"]}:{artifact["digest"]}'
    if publishing:
        require(os.environ.get('APPROVED_ARTIFACT') == identity,
                'Artifact does not match the independently approved ID and digest.')
    data = github(f'/repos/{repository}/actions/artifacts/{artifact["id"]}/zip', token, download=True,
                  limit=MAX_ISOLATED_ARCHIVE if extended else MAX_ARCHIVE)
    with tempfile.TemporaryDirectory(prefix='verified-worker-') as workspace:
        directory = Path(workspace)
        asset_dir = 'dist-worker' if password else 'dist' if spa or ledger or dtr else 'public'
        unpack_verified(data, artifact['digest'], directory, extended, asset_dir,
                        ledger=ledger, dtr=dtr, password=password, media_library=media_library)
        if isolated:
            check_isolated_worker(directory, os.environ['WORKER_NAME'], os.environ['WORKER_DATABASE_ID'])
            print('Verified the isolated private artifact and its synthetic settings.')
            return
        settings = (directory, os.environ['WORKER_NAME'], os.environ['WORKER_DATABASE_ID'])
        if media_library:
            check_media_library(*settings)
        elif password:
            check_password_worker(directory, os.environ['WORKER_NAME'])
        elif profile in ('routed-worker', 'routed-worker-production'):
            check_bound_worker(*settings, os.environ['WORKER_SERVICE'], os.environ['WORKER_ENTRYPOINT'])
        elif ledger:
            check_ledger_worker(*settings)
        elif dtr:
            check_dtr_worker(*settings)
        elif spa:
            check_spa_worker(*settings, os.environ['WORKER_DATABASE_NAME'])
        elif extended:
            check_bound_worker(*settings)
        else:
            check_worker(*settings)
        print('Verified the approved private artifact and its deployment settings.')
        if approval_output:
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as outputs:
                outputs.write(f'artifact={identity}\n')
        if not publishing:
            return
        account = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
        credential = os.environ.get('CLOUDFLARE_API_TOKEN', '')
        require(bool(account and credential), 'Protected Cloudflare credentials are unavailable.')
        env = {'PATH': os.environ['PATH'], 'CI': 'true', 'WRANGLER_SEND_METRICS': 'false',
               'XDG_CONFIG_HOME': str(directory / '.work'),
               'CLOUDFLARE_ACCOUNT_ID': account, 'CLOUDFLARE_API_TOKEN': credential}
        result = subprocess.run(['node', os.environ['WRANGLER_BIN'], 'deploy', '--config', 'wrangler.json', '--no-bundle'],
                                cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        require(result.returncode == 0, 'Cloudflare deployment did not report success; inspect Cloudflare before retrying.')
        print('Cloudflare deployment succeeded.')


if __name__ == '__main__':
    try:
        run()
    except Rejected as error:
        raise SystemExit(str(error)) from None
    except (OSError, ValueError, KeyError, TypeError, IndexError, zipfile.BadZipFile, subprocess.TimeoutExpired):
        raise SystemExit('Handoff or deployment could not be verified. No private diagnostic data was logged.') from None
