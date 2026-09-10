"""插件注册；加载时不访问网络或数据库。"""

import json
from pathlib import Path

from src.plugins import PluginContext, PluginRegistration
from src.scheduler.contracts import JobDefinition

from .jobs import run
from .settings import Settings

PLUGIN_ID = "sakuramedia_actor_metadata"
VERSION = json.loads(Path(__file__).with_name("manifest.json").read_text())["version"]


def register(context: PluginContext) -> PluginRegistration:
    settings = Settings.model_validate(dict(context.settings))
    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name="SakuraMedia 女优资料补全",
        version=VERSION,
        host_api_version=6,
        jobs=(JobDefinition(
            task_key=PLUGIN_ID,
            log_name="actor-metadata",
            cli_name="actor-metadata",
            cli_help="从 JavDB / MinnanoAV 补全女优资料",
            default_cron="0 5 * * *",
            handler=lambda reporter, _params: run(context, settings, reporter),
        ),),
    )
