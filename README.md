# Earth VR Tweaks

A Python script that increases terrain and building detail in Google Earth VR. Supports adjusting level of detail, tile cache memory, and other rendering settings.

Changes are applied in memory while the game is running. Close Earth VR and launch it normally to return to the default settings.

## How to Use

Requires Windows, 64-bit Python 3 (available as `python` in your terminal), and the Steam version of Google Earth VR.

1. Clone or download this repository into your Earth VR installation folder, so `EarthVRTweaks` is next to `Earth.exe`.
2. Edit `tweaks.json` to change the settings, or use the included values.
3. Double-click `Launch Earth VR (tweaked).bat` to start the game.

If the repository is somewhere else, run the script with the path to your game:

```sh
python earthvr_tweaks.py "D:\path\to\EarthVR\Earth.exe"
```

## Settings

The included config sets `lod_bias` to `2.0` (game default: `0.5`) and `mirth_memory_cap_mb` to `12000` (game default: `8000`). Higher LOD values load more detailed terrain and buildings at a distance, but use more memory and processing time.

Other settings are listed in `tweaks.json`. Set a value to `null` to leave it at the game default.

## Controls

These work while Earth VR has focus:

| Keys | Action |
| --- | --- |
| Ctrl+Alt+PageUp / PageDown | Increase / decrease LOD bias by 0.25 |
| Ctrl+Alt+F9 | Toggle live settings between your tweaks and game defaults |
| Ctrl+Alt+F10 | Run a LOD performance test (hold still for about 90 seconds) |
| Ctrl+Alt+F11 | Print current settings and performance stats |

Note: The script checks your `Earth.exe` and refuses to patch builds it doesn't recognize.
