"""Run with python -I after installing a built wheel, without a checkout import."""
from importlib import resources
from pathlib import Path

import omnibci

from omnibci.application.plugin_schema import validate_parameters
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot, vendor_plugins_dir
from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.frame_pack import FramePack


def main():
    checkout = Path(__file__).resolve().parents[2]
    installed_paths = [Path(path).resolve() for path in omnibci.__path__]
    assert installed_paths and all(
        not path.is_relative_to(checkout / "omnibci") for path in installed_paths
    ), ("Wheel smoke test can import the source checkout instead of the installed package", installed_paths)
    vendor = vendor_plugins_dir()
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor, "builtin", 100)]})
    contributions = []
    for kind in ("acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector"):
        selected = catalog.list_contributions(kind)
        assert selected, f"Installed wheel contains no {kind} contributions"
        for contribution in selected:
            assert contribution.manifest_path.is_file()
            assert contribution.configuration_version == 1
            assert contribution.configuration_schema is not None
            validate_parameters(contribution.configuration_schema, {})
        contributions.extend(selected)
    assert len(contributions) == 10
    assert resources.files("omnibci.conf").joinpath("settings.toml").is_file()
    resource = resources.files("omnibci.infrastructure.plugins.paradigms.classic40SSVEP").joinpath(
        "resource", "classic40_ssvep.ofp",
    )
    # FramePack checks the complete archive manifest and verifies hashes while
    # reading every frame; no renderer or OpenGL context is created.
    with resources.as_file(resource) as path, FramePack(path) as pack:
        assert pack.idle_frame.shape == (990, 1920, 1)
        assert sum(1 for _ in pack.iter_stimulation_frames()) == 240
        assert sum(1 for _ in pack.iter_cue_frames()) == 40
    print(f"Installed wheel verified: {len(contributions)} configured contributions and 281 stimulus frames")


if __name__ == "__main__":
    main()
