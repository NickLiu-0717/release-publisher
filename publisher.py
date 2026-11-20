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
VERIFIED_STEP = 'Store verified handoff artifact'


class Rejected(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Rejected(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None


def github(path, token, download=False):
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
        data = response.read(MAX_ARCHIVE + 1)
    require(len(data) <= MAX_ARCHIVE, 'Artifact archive is too large.')
    return data


def select_artifact(read, repository, trusted_sha, run_id):
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Missing or invalid private controller repository.')
    require(re.fullmatch(r'[a-f0-9]{40}', trusted_sha), 'Missing or invalid approved controller commit.')
    require(re.fullmatch(r'[1-9][0-9]{0,19}', run_id), 'Invalid workflow run number.')
    run_path = f'/repos/{repository}/actions/runs/{run_id}'
    run = read(run_path)
    require(str(run['id']) == run_id and run['head_branch'] == 'main' and
            run['path'] in ('.github/workflows/release.yml', '.github/workflows/release.yml@refs/heads/main') and
            run['event'] == 'workflow_dispatch' and run['status'] == 'completed' and
            run['conclusion'] == 'success' and run['head_sha'] == trusted_sha,
            'Source run is unsuccessful or uses an unapproved controller revision.')
    attempt = run['run_attempt']
    require(type(attempt) is int and attempt > 0, 'Invalid source run attempt.')
    jobs = read(f'{run_path}/attempts/{attempt}/jobs?per_page=100')['jobs']
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


def unpack_verified(data, expected_digest, directory):
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    require(digest == expected_digest, 'Artifact archive digest differs from GitHub metadata.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(len(entries) <= 250 and sum(entry.file_size for entry in entries) <= MAX_ARCHIVE, 'Artifact contents are too large.')
        files = set()
        for entry in entries:
            name = entry.filename.rstrip('/')
            parts = name.split('/')
            allowed = name in {'worker.js', 'worker.js.map', 'wrangler.json'} or (
                parts[0] == 'public' and all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', part) for part in parts))
            kind = stat.S_IFMT(entry.external_attr >> 16)
            require(allowed and kind in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG), 'Unexpected artifact entry.')
            if entry.is_dir():
                require(parts[0] == 'public', 'Unexpected artifact directory.')
                continue
            require(name not in files, 'Duplicate artifact name.')
            files.add(name)
            destination = directory.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as output:
                output.write(archive.read(entry))
        require({'worker.js', 'wrangler.json', 'public/index.html', 'public/boot.js'} <= files, 'Required worker files are absent.')


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


def run():
    token = os.environ['HANDOFF_ACTIONS_READ']
    require(bool(token), 'Private artifact read access has not been configured.')
    repository = os.environ['SOURCE_REPOSITORY']
    artifact = select_artifact(lambda path: github(path, token), repository,
                               os.environ['SOURCE_WORKFLOW_SHA'], os.environ['SOURCE_RUN_ID'])
    data = github(f'/repos/{repository}/actions/artifacts/{artifact["id"]}/zip', token, download=True)
    with tempfile.TemporaryDirectory(prefix='verified-worker-') as workspace:
        directory = Path(workspace)
        unpack_verified(data, artifact['digest'], directory)
        check_worker(directory, os.environ['WORKER_NAME'], os.environ['WORKER_DATABASE_ID'])
        print('Verified the approved private artifact and its deployment settings.')
        if os.environ.get('PUBLISH') != 'true':
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
