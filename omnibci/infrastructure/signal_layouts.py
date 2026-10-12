"""Resolve installed MNE templates into explicit head coordinates in meters."""
from functools import lru_cache


class SignalLayoutProvider:
    def names(self) -> list[str]:
        from mne.channels import get_builtin_montages
        return get_builtin_montages()

    @lru_cache(maxsize=16)
    def get(self, name: str) -> dict:
        import mne
        from mne.channels import make_standard_montage
        if name not in self.names():
            raise ValueError(f"Unknown installed electrode template: {name}")
        montage = make_standard_montage(name)
        # MNE uses the montage fiducials to transform its native coordinate frame.
        # set_montage must succeed; arbitrary template coordinates are never relabeled.
        info = mne.create_info(montage.ch_names, sfreq=1.0, ch_types="eeg")
        info.set_montage(montage)
        resolved = info.get_montage().get_positions()
        if resolved["coord_frame"] != "head":
            raise ValueError("Electrode template cannot be resolved to head coordinates")
        return {"name": name, "positions": {key: value.tolist() for key, value in resolved["ch_pos"].items()},
                "coordinate_frame": "head", "unit": "m", "provider": f"MNE {mne.__version__}: {name}"}
