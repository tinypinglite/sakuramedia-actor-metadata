import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from sakuramedia_actor_metadata.plugin import PLUGIN_ID, register
from sakuramedia_actor_metadata.settings import Settings
from sakuramedia_actor_metadata.tools.package_plugin import RUNTIME_FILES, build
from src.plugins import PluginContext
from src.plugins.manifest import load_manifest_from_dict


def test_registration_and_defaults_do_not_create_data(tmp_path):
    data_dir = tmp_path / "not-created"
    registration = register(PluginContext(PLUGIN_ID, {}, data_dir))
    assert not data_dir.exists()
    assert registration.host_api_version == 6
    assert registration.jobs[0].task_key == PLUGIN_ID
    assert registration.jobs[0].default_cron == "0 5 * * *"
    assert registration.jobs[0].params_schema is None
    assert Settings().max_attempts == Settings().retry_days == 3


@pytest.mark.parametrize("config", [
    {"max_attempts": 0}, {"retry_days": 0}, {"target_fields": []},
    {"target_fields": ["cup", "cup"]}, {"target_fields": ["avatar_url"]},
    {"batch_size": 100},
])
def test_invalid_configuration_rejected(config):
    with pytest.raises(ValidationError):
        Settings.model_validate(config)

def test_package_contains_only_runtime_files(tmp_path):
    archive = build(tmp_path)
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == set(RUNTIME_FILES)
        manifest = load_manifest_from_dict(json.loads(z.read("manifest.json")))
        assert manifest.plugin_id == PLUGIN_ID
        assert manifest.host_api_version == 6
        assert manifest.release_api_url == (
            "https://api.github.com/repos/tinypinglite/sakuramedia-actor-metadata/releases/latest"
        )
        assert "beautifulsoup4>=4.12,<5" in manifest.dependencies
    assert Path(archive).stat().st_size > 0


def test_packaged_plugin_loads_with_host_loader(tmp_path):
    from src.plugins.loader import check_plugin_dir

    archive = build(tmp_path)
    destination = tmp_path / PLUGIN_ID
    with zipfile.ZipFile(archive) as z:
        z.extractall(destination)
    registration = check_plugin_dir(plugin_dir=destination)
    assert registration.plugin_id == PLUGIN_ID
    assert registration.jobs[0].manual_trigger_allowed


def test_upgrade_ignores_retired_mapping_without_exposing_it(tmp_path):
    registration = register(PluginContext(PLUGIN_ID, {"javbus_mapping": {"a": "/star/abc"}}, tmp_path))
    assert registration.jobs[0].task_key == PLUGIN_ID
    assert "javbus_mapping" not in Settings.model_json_schema()["properties"]
