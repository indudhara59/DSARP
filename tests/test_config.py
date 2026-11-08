from pathlib import Path

import pytest

from dsarp.config import ConfigError, load_config, parse_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_sample_config_resolves_paths_against_data_root():
    cfg = load_config(FIXTURES / "sample.yaml", data_root=FIXTURES)
    assert cfg.name == "demo"
    assert cfg.commit == "0123456789abcdef0123456789abcdef01234567"
    assert cfg.detector_tools == ["arcan", "designite"]
    assert cfg.detectors[0].path == FIXTURES / "sample" / "arcan"


def test_data_root_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DSARP_DATA", str(tmp_path))
    cfg = load_config(FIXTURES / "sample.yaml")
    assert cfg.detectors[0].path == tmp_path / "sample" / "arcan"


@pytest.mark.parametrize("raw,message", [
    ({"repo_url": "u", "detectors": [{"tool": "arcan", "path": "x"}]}, "name"),
    ({"name": "n", "repo_url": "u", "detectors": []}, "non-empty"),
    ({"name": "n", "repo_url": "u", "detectors": [{"tool": "sonar", "path": "x"}]}, "tool"),
    ({"name": "n", "repo_url": "u", "detectors": [{"tool": "arcan"}]}, "path"),
    ({"name": "n", "repo_url": "u", "language": "python", "detectors": [{"tool": "arcan", "path": "x"}]}, "java"),
    ({"name": "n", "repo_url": "u", "build": "ant", "detectors": [{"tool": "arcan", "path": "x"}]}, "build"),
])
def test_invalid_configs_are_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw, Path("."))


def test_commit_is_optional():
    cfg = parse_config({"name": "n", "repo_url": "u", "detectors": [{"tool": "arcan", "path": "x"}]}, Path("."))
    assert cfg.commit is None
