"""生成安装 ZIP，只包含明确列出的运行文件。"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "manifest.json", "__init__.py", "plugin.py", "settings.py", "sources.py",
    "jobs.py", "state.py", "README.md",
)


def build(output_dir=None):
    manifest = json.loads((ROOT / "manifest.json").read_text())
    output_dir = Path(output_dir) if output_dir else ROOT / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{manifest['plugin_id']}-{manifest['version']}.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in RUNTIME_FILES:
            archive.write(ROOT / name, name)
    return output


if __name__ == "__main__":
    print(build())
