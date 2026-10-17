from contextlib import contextmanager
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import io
import zipfile
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid


HERE = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('network_armory_manager', str(HERE / 'files/armory-network'))
spec = importlib.util.spec_from_loader(loader.name, loader)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ArmoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = HERE / ('.test-armory-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.collection = self.folder / 'collection'
        self.collection.mkdir()
        self.root_patch = patch.object(manager, 'ROOT', self.collection)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.state_patch = patch.object(manager, 'STATE', self.folder / 'state/managed.json')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def entry(self, category, filename, data):
        return {'category': category, 'filename': filename, 'type': 'file',
                'url': 'https://example.test/' + filename}

    @contextmanager
    def temporary(self, **kwargs):
        folder = self.folder / ('stage-' + uuid.uuid4().hex)
        folder.mkdir()
        yield str(folder)

    def test_scripts_and_licenses_are_installed_without_execution(self):
        data = b'#!/bin/sh\nprintf test\n'
        license_data = b'Original upstream license\n'
        entries = [self.entry('linpeas', 'linpeas.sh', data), self.entry('linpeas', 'LICENSE', license_data)]
        personal = self.collection / 'personal.sh'
        personal.write_bytes(b'personal file')
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager.tempfile, 'TemporaryDirectory', self.temporary), patch.object(manager, 'fetch', side_effect=[data, license_data]):
            manager.configure(entries)
        self.assertEqual((self.collection / 'linpeas/linpeas.sh').read_bytes(), data)
        self.assertEqual((self.collection / 'linpeas/LICENSE').read_bytes(), license_data)
        self.assertEqual(personal.read_bytes(), b'personal file')
        manager.check(entries)
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager.tempfile, 'TemporaryDirectory', self.temporary), patch.object(manager, 'fetch') as fetch:
            manager.configure(entries)
        fetch.assert_not_called()

    def test_failed_download_does_not_replace_existing_files(self):
        previous = self.collection / 'lse/lse.exe'
        previous.parent.mkdir()
        previous.write_bytes(b'previous version')
        entries = [self.entry('lse', 'lse.exe', b'MZnew version'), self.entry('lse', 'LICENSE', b'license')]
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager.tempfile, 'TemporaryDirectory', self.temporary), patch.object(manager, 'fetch', side_effect=[b'MZnew version', RuntimeError('connection failed')]):
            with self.assertRaisesRegex(RuntimeError, 'connection failed'):
                manager.configure(entries)
        self.assertEqual(previous.read_bytes(), b'previous version')
        self.assertFalse(manager.STATE.exists())

    def test_resources_use_https_without_download_pins(self):
        manifest = json.loads((HERE / 'armory-network.json').read_text())
        self.assertTrue(manifest['resources'])
        for entry in manifest['resources']:
            self.assertTrue(entry['url'].startswith('https://'))
            self.assertNotIn('size', entry)
            self.assertNotIn('sha256', entry)
            for member in entry.get('members', []):
                self.assertNotIn('size', member)
                self.assertNotIn('sha256', member)

    def test_zip_destinations_are_preserved(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('wintun/bin/amd64/wintun.dll', b'MZdriver')
            archive.writestr('wintun/LICENSE.txt', b'license')
        entry = dict(category='ligolo', filename='wintun.zip', type='zip', members=[
            dict(path='wintun/bin/amd64/wintun.dll', destination='wintun.dll'),
            dict(path='wintun/LICENSE.txt', destination='wintun-license.txt')])
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager, 'fetch', return_value=buffer.getvalue()):
            manager.configure([entry])
        self.assertEqual((self.collection / 'ligolo/wintun.dll').read_bytes(), b'MZdriver')
        self.assertEqual((self.collection / 'ligolo/wintun-license.txt').read_bytes(), b'license')

    def test_changed_upstream_file_is_accepted_and_recorded_for_cleanup(self):
        data = b'MZnew upstream executable'
        entry = self.entry('sysinternals', 'procdump.exe', data)
        entry.update(size=1, sha256='obsolete')
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager, 'fetch', return_value=data):
            manager.configure([entry])
        self.assertEqual((self.collection / 'sysinternals/procdump.exe').read_bytes(), data)
        state = json.loads(manager.STATE.read_text())
        self.assertNotEqual(state[0]['sha256'], 'obsolete')
        manager.check([entry])

    def test_fetch_reads_whole_changed_file(self):
        data = b'MZlarger than the pinned size'
        response = io.BytesIO(data)
        response.url = 'https://example.test/tool.exe'
        entry = self.entry('test', 'tool.exe', data)
        entry.update(size=1, sha256='obsolete')
        with patch.object(manager.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(manager.fetch(entry), data)

    def test_zip_members_accept_changed_sizes_and_hashes(self):
        data = b'MZupdated executable'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('tool.exe', data)
        entry = dict(category='test', filename='tools.zip', type='zip',
                     members=[dict(path='tool.exe', size=1, sha256='obsolete')])
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager, 'fetch', return_value=buffer.getvalue()):
            manager.configure([entry])
        self.assertEqual((self.collection / 'test/tool.exe').read_bytes(), data)

    def test_invalid_executable_is_rejected(self):
        entry = self.entry('test', 'tool.exe', b'HTML error page')
        with patch.object(manager.os, 'geteuid', return_value=0, create=True), patch.object(manager, 'fetch', return_value=b'HTML error page'):
            with self.assertRaisesRegex(RuntimeError, 'Windows binary format'):
                manager.configure([entry])
        self.assertFalse((self.collection / 'test/tool.exe').exists())

    def test_collection_paths_cannot_escape(self):
        for relative in ['../outside', '/outside', 'lse/../../outside', 'lse\\outside']:
            with self.assertRaises(RuntimeError):
                manager.safe_path(relative)


if __name__ == '__main__':
    unittest.main()
