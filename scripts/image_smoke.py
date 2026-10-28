"""Exercise the built image without HA/network access or persistent addon data."""
import importlib
import sqlite3
import tempfile
from pathlib import Path

import dateutil
import yaml
from managed_scenes import ManagedScenes
from reliability import CommandJournal

expected = str(yaml.safe_load(Path('/config.yaml').read_text())['version'])
app = importlib.import_module('run')
assert app.ADDON_VERSION == expected, 'Bundled version fallback does not match config'

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    command = {'id': 'image-smoke', 'entity_id': 'light.fixture', 'action': 'turn_on', 'params': {}}
    journal = CommandJournal(path / 'command_journal.sqlite3')
    scenes = ManagedScenes(path / 'managed_scenes.sqlite3')
    assert journal.claim(command)
    journal.complete(command['id'], 'expired')
    scenes.save('scene.lapahub_image_smoke', {'scene_id': 'lapahub_image_smoke', 'entities': {'light.fixture': {'state': 'off'}}})
    journal.close()
    scenes.close()
    for database in path.glob('*.sqlite3'):
        with sqlite3.connect(database) as connection:
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    journal = CommandJournal(path / 'command_journal.sqlite3')
    scenes = ManagedScenes(path / 'managed_scenes.sqlite3')
    assert not journal.claim(command), 'Restored command would replay'
    assert journal.pending() == [('image-smoke', 'expired')]
    assert 'scene.lapahub_image_smoke' in scenes.active()
    journal.close()
    scenes.close()

print('Image imports, offline version metadata and both SQLite reopen checks passed; no HA service was called.')
