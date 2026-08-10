from pathlib import Path

from omnibci.conf.paths import resolve_config_path


def test_resolve_config_path_converts_portable_relative_path(tmp_path: Path):
    resolved = resolve_config_path(
        "fixtures/session/train.cnt",
        base_dir=tmp_path,
    )

    assert resolved == (tmp_path / "fixtures" / "session" / "train.cnt").resolve()


def test_resolve_config_path_preserves_absolute_path(tmp_path: Path):
    absolute = tmp_path / "train.cnt"

    assert resolve_config_path(absolute) == absolute.resolve()
