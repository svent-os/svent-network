import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
META = json.loads((ROOT / 'upstream.json').read_text())
BUILD = ROOT / '.build'
SOURCE = BUILD / 'source'
ENVIRONMENT = BUILD / 'environment'
PREFIX = '/opt/' + META['package']
ENV = os.environ.copy()
for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP', 'VIRTUAL_ENV',
            'GOPRIVATE', 'GONOSUMDB', 'GONOPROXY', 'GOFLAGS'):
    ENV.pop(key, None)
ENV.update(PYTHONNOUSERSITE='1', PIP_CONFIG_FILE='/dev/null', PIP_NO_INPUT='1',
           PIP_REQUIRE_VIRTUALENV='true', PIP_DISABLE_PIP_VERSION_CHECK='1')


def run(arguments, cwd=ROOT, environment=None):
    print('[+] ' + ' '.join(map(str, arguments)), flush=True)
    subprocess.run(list(map(str, arguments)), cwd=cwd, env=environment or ENV, check=True)


def pip(*arguments):
    run([ENVIRONMENT / 'bin/python', '-I', '-m', 'pip', '--no-cache-dir', *arguments])


def smoke(arguments, cwd=ROOT, environment=None):
    result = subprocess.run(list(map(str, arguments)), cwd=cwd, env=environment or ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)
    print(result.stdout[-12000:], flush=True)
    if result.returncode not in (0, 1, 2) or not result.stdout.strip():
        raise RuntimeError('Tool startup check failed')
    if any(value in result.stdout for value in ('Traceback (most recent call last)', 'ModuleNotFoundError',
                                                'ImportError:', 'LibraryNotFoundError', 'panic:')):
        raise RuntimeError('Tool import check failed')
    if result.returncode and not re.search(r'usage|options|help', result.stdout, re.I):
        raise RuntimeError('Unexpected help error')


def source():
    archive = BUILD / 'upstream.tar.gz'
    request = urllib.request.Request(META['archive_url'], headers={'User-Agent': 'Svent-packaging'})
    with urllib.request.urlopen(request, timeout=120) as response, archive.open('wb') as output:
        if not response.url.startswith('https://'):
            raise RuntimeError('Insecure upstream redirect')
        shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != META['sha256']:
        raise RuntimeError('Upstream archive checksum mismatch')
    unpacked = BUILD / 'unpacked'
    unpacked.mkdir()
    with tarfile.open(archive) as content:
        content.extractall(unpacked, filter='data')
    entries = list(unpacked.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise RuntimeError('Unexpected source archive layout')
    entries[0].rename(SOURCE)
    for relative, replacements in META.get('source_replacements', {}).items():
        path = SOURCE / relative
        original = path.read_text()
        for old, new in replacements.items():
            if old not in original:
                raise RuntimeError('Expected source text missing: ' + relative)
            original = original.replace(old, new)
        path.write_text(original)


def notices():
    paths = [str(path) for path in ENVIRONMENT.glob('lib/python*/site-packages')]
    records = []
    for distribution in importlib.metadata.distributions(path=paths):
        licenses = {}
        for relative in distribution.files or []:
            if not any(word in str(relative).lower() for word in ('license', 'copying', 'notice')):
                continue
            path = pathlib.Path(distribution.locate_file(relative)).resolve()
            if path.is_relative_to(ENVIRONMENT.resolve()) and path.is_file() and path.stat().st_size < 200000:
                licenses[str(relative)] = path.read_text(errors='replace')
        records.append({'name': distribution.metadata['Name'], 'version': distribution.version,
                        'license': distribution.metadata.get('License'),
                        'license_expression': distribution.metadata.get('License-Expression'),
                        'license_files': licenses})
    (BUILD / 'third-party-notices.json').write_text(json.dumps(records, indent=2) + '\n')


def patch_runtime():
    directories = list(ENVIRONMENT.glob('lib/python*/site-packages'))
    if len(directories) != 1:
        raise RuntimeError('Unexpected private Python package directory')
    directory = directories[0].resolve()
    for relative, replacements in META.get('runtime_replacements', {}).items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise RuntimeError('Invalid runtime patch path')
        content = path.read_text()
        for old, new in replacements.items():
            if old not in content:
                raise RuntimeError('Runtime patch does not match: ' + relative)
            content = content.replace(old, new)
        path.write_text(content)


def build():
    clean()
    BUILD.mkdir()
    source()
    if META['kind'] == 'go':
        environment = ENV.copy()
        environment.update(CGO_ENABLED='0', GOTOOLCHAIN=META['toolchain'], GOSUMDB='sum.golang.org',
                           GOPROXY='https://proxy.golang.org,direct', GOWORK='off')
        jobs = os.environ.get('SVENT_BUILD_JOBS', '2')
        if not jobs.isdigit() or int(jobs) < 1:
            raise RuntimeError('SVENT_BUILD_JOBS must be a positive integer')
        binary_directory = BUILD / 'bin'
        binary_directory.mkdir()
        run(['go', 'mod', 'download'], cwd=SOURCE, environment=environment)
        run(['go', 'mod', 'verify'], cwd=SOURCE, environment=environment)
        for command, relative in META['commands'].items():
            run(['go', 'build', '-mod=readonly', '-p', jobs, '-trimpath', '-ldflags', META['ldflags'],
                 '-o', binary_directory / command, relative], cwd=SOURCE, environment=environment)
            smoke([binary_directory / command, *META['test_arguments']])
        run(['go', 'mod', 'vendor'], cwd=SOURCE, environment=environment)
        (SOURCE / 'rebuild.sh').write_text('#!/bin/sh\nset -eu\nCGO_ENABLED=0 GOTOOLCHAIN=' +
                                          META['toolchain'] + ' go build -mod=vendor -trimpath -ldflags "' +
                                          META['ldflags'] + '" ' + next(iter(META['commands'].values())) + '\n')
        with tarfile.open(BUILD / 'corresponding-source.tar.gz', 'w:gz') as archive:
            archive.add(SOURCE, arcname='source')
    elif META['kind'] == 'standalone':
        smoke(['/usr/bin/python3', '-I', SOURCE / 'penelope.py', '--help'])
    else:
        run(['/usr/bin/python3', '-m', 'venv', ENVIRONMENT])
        pins = META['build_pins']
        pip('install', '--upgrade', *[name + '==' + pins[name] for name in ('pip', 'setuptools', 'wheel')])
        constraints = BUILD / 'constraints.txt'
        constraints.write_text('setuptools==' + pins['setuptools'] + '\n' + '\n'.join(META.get('constraints', [])) + '\n')
        ENV['PIP_CONSTRAINT'] = str(constraints)
        pip('install', SOURCE, *META.get('requirements', []))
        for package in META.get('remove_build_packages', []):
            pip('uninstall', '--yes', package)
        patch_runtime()
        pip('check')
        for command in META['commands']:
            smoke([ENVIRONMENT / 'bin/python', '-I', ENVIRONMENT / 'bin' / command, '--help'])
        for module in META.get('import_checks', []):
            run([ENVIRONMENT / 'bin/python', '-I', '-c', 'import ' + module])
        notices()
        with (BUILD / 'requirements-installed.txt').open('w') as output:
            subprocess.run([str(ENVIRONMENT / 'bin/python'), '-I', '-m', 'pip', 'freeze', '--all'],
                           cwd=ROOT, env=ENV, stdout=output, check=True)


def install(destination):
    destination = pathlib.Path(destination).resolve()
    if destination != (ROOT / 'debian' / META['package']).resolve():
        raise RuntimeError('Unexpected staging directory')
    target = destination / PREFIX.lstrip('/')
    target.mkdir(parents=True, exist_ok=True)
    documentation = destination / 'usr/share/doc' / META['package']
    documentation.mkdir(parents=True, exist_ok=True)
    for path in SOURCE.rglob('*'):
        if path.is_file() and path.name.lower().startswith(('license', 'copying', 'notice')):
            license_path = documentation / 'licenses' / path.relative_to(SOURCE)
            license_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, license_path)
    shutil.copy2(ROOT / 'upstream.json', documentation / 'upstream.json')
    commands = destination / 'usr/bin'
    commands.mkdir(parents=True, exist_ok=True)
    if META['kind'] == 'go':
        for command in META['commands']:
            shutil.copy2(BUILD / 'bin' / command, commands / command)
            (commands / command).chmod(0o755)
        shutil.copy2(BUILD / 'corresponding-source.tar.gz', documentation / 'corresponding-source.tar.gz')
    elif META['kind'] == 'standalone':
        shutil.copy2(SOURCE / 'penelope.py', target / 'penelope.py')
        (commands / 'penelope').write_text('#!/bin/sh\nset -eu\nunset PYTHONPATH PYTHONHOME PYTHONUSERBASE PYTHONSTARTUP\nexec /usr/bin/python3 -I ' + PREFIX + '/penelope.py "$@"\n')
        (commands / 'penelope').chmod(0o755)
    else:
        shutil.copytree(ENVIRONMENT, target, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for path in (target / 'bin').iterdir():
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if data.startswith(b'#!') and b'\x00' not in data:
                    path.write_bytes(data.replace(str(ENVIRONMENT).encode(), PREFIX.encode()))
                    path.chmod(0o755)
        configuration = target / 'pyvenv.cfg'
        configuration.write_text(configuration.read_text().replace(str(ENVIRONMENT), PREFIX))
        for path in target.rglob('*.pth'):
            if str(BUILD) in path.read_text(errors='replace'):
                raise RuntimeError('Build path leaked into Python search configuration')
        for name in ('requirements-installed.txt', 'third-party-notices.json'):
            shutil.copy2(BUILD / name, target / name)
        for command in META['commands']:
            path = commands / command
            path.write_text('#!/bin/sh\nset -eu\nunset PYTHONPATH PYTHONHOME PYTHONUSERBASE PYTHONSTARTUP\nexport PYTHONNOUSERSITE=1\nexec ' + PREFIX + '/bin/python -I ' + PREFIX + '/bin/' + command + ' "$@"\n')
            path.chmod(0o755)
    shutil.copytree(ROOT / 'catalog.d', destination / 'usr/share/svent/catalog.d', dirs_exist_ok=True)


def shlibdeps():
    staging = ROOT / 'debian' / META['package']
    directories = set()
    for path in (staging / PREFIX.lstrip('/')).rglob('*'):
        if path.is_file() and '.so' in path.name:
            with path.open('rb') as stream:
                if stream.read(4) == b'\x7fELF':
                    directories.add(str(path.parent.resolve()))
    arguments = ['dh_shlibdeps']
    if directories:
        arguments += ['-l' + ':'.join(sorted(directories))]
    run([*arguments, '--', '--ignore-missing-info'])


def clean():
    if BUILD.is_symlink():
        raise RuntimeError('Build directory cannot be a symbolic link')
    if BUILD.exists():
        shutil.rmtree(BUILD)


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'build':
        build()
    elif action == 'install':
        install(sys.argv[2])
    elif action == 'shlibdeps':
        shlibdeps()
    elif action == 'clean':
        clean()
    else:
        raise RuntimeError('Unknown build action')
