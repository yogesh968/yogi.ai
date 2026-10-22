"""Audio device discovery by case-insensitive substring match, preferring a host API."""
from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd


class AudioDeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    hostapi: str
    samplerate: int
    max_channels: int


def _hostapi_name(i: int) -> str:
    return str(sd.query_hostapis(i)["name"])  # type: ignore[index]


def list_devices(kind: str) -> list[Device]:
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out: list[Device] = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0:  # type: ignore[index]
            out.append(Device(i, str(d["name"]), _hostapi_name(int(d["hostapi"])),  # type: ignore[index]
                              int(d["default_samplerate"]), int(d[key])))  # type: ignore[index]
    return out


# WDM-KS is excluded: it opens devices exclusively and is flaky with virtual cables.
_HOSTAPI_ORDER = ["Windows WASAPI", "Windows DirectSound", "MME"]


def find_device(kind: str, name_substring: str, preferred_hostapi: str) -> Device:
    """Resolve a device. Empty substring = system default for that kind (on the preferred host API if possible)."""
    candidates = [d for d in list_devices(kind) if "WDM-KS" not in d.hostapi]
    order = [preferred_hostapi] + [h for h in _HOSTAPI_ORDER if h != preferred_hostapi]

    def rank(d: Device) -> int:
        return order.index(d.hostapi) if d.hostapi in order else len(order)

    if not name_substring.strip():
        default_idx = sd.default.device[0 if kind == "input" else 1]
        if default_idx is None or default_idx < 0:
            raise AudioDeviceError(f"No default {kind} device. Plug in a mic/headphones or set TEST_{kind.upper()}_DEVICE in .env.")
        default_name = str(sd.query_devices(default_idx)["name"])  # type: ignore[index]
        # MME names are truncated to 31 chars; match by prefix to find the same device on a better host API.
        stem = default_name[:28].lower()
        same = sorted([d for d in candidates if d.name.lower().startswith(stem) or stem.startswith(d.name.lower()[:28])], key=rank)
        if same:
            return same[0]
        return next(d for d in list_devices(kind) if d.index == default_idx)

    needle = name_substring.strip().lower()
    matches = sorted([d for d in candidates if needle in d.name.lower()], key=rank)
    if not matches:
        raise AudioDeviceError(
            f"Audio {kind} device matching '{name_substring}' not found. "
            f"Run `python -m app.devices` (or `python -m sounddevice`) to see device names, then fix .env."
        )
    return matches[0]


def main() -> None:
    from ..config import get_settings
    try:
        s = get_settings()
        pref = s.audio_hostapi
    except SystemExit:
        s, pref = None, "Windows WASAPI"
    for kind in ("input", "output"):
        print(f"\n== {kind.upper()} devices ==")
        for d in list_devices(kind):
            print(f"  [{d.index:>2}] {d.name}  ({d.hostapi}, {d.samplerate} Hz, {d.max_channels} ch)")
    if s:
        print("\n== Resolved from .env ==")
        for label, kind, sub in [
            ("caller input (phone)", "input", s.caller_input_device),
            ("AI output (phone)", "output", s.ai_output_device),
            ("test input", "input", s.test_input_device),
            ("test output", "output", s.test_output_device),
        ]:
            try:
                d = find_device(kind, sub, pref)
                print(f"  OK  {label:<22} -> [{d.index}] {d.name} ({d.hostapi}, {d.samplerate} Hz)")
            except AudioDeviceError as e:
                print(f"  !!  {label:<22} -> {e}")


if __name__ == "__main__":
    main()
