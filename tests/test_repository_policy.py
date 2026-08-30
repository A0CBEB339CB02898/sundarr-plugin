"""仓库初始化边界和未来 Manifest 的静态保护。"""

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_project_metadata_matches_official_plugin_repository() -> None:
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]

    assert project["name"] == "sundarr-official-plugins"
    assert project["requires-python"] == ">=3.12"


def test_repository_does_not_publish_source_plugins() -> None:
    manifest_path = ROOT / "sundarr_plugin.toml"
    if not manifest_path.exists():
        return

    with manifest_path.open("rb") as file:
        manifest = tomllib.load(file)

    assert manifest["manifest_version"] == 2
    plugins = manifest.get("plugins")
    assert isinstance(plugins, list) and plugins
    assert all(plugin.get("plugin_type") != "source" for plugin in plugins)
