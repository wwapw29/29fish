# pixel-sentinel

A small Windows screen-vision toolkit.

It watches a region of the screen, detects a target by colour and blob shape,
tracks it across frames, and triggers a mouse click when its state changes.
Built as a playground for screen-driven automation.

## What's inside

- **Colour + shape detection** — adaptive R-B threshold (follows the scene's
  colour cast), morphological cleanup, blob filtering by area and aspect ratio.
- **Tracking with re-acquire** — cheap per-frame search around the last position,
  full re-acquire on loss.
- **State machine** — cast → settle → acquire → track → trigger → recover.
- **Result verifier** — after each trigger it reads the status line at the top of
  the screen with a small ONNX OCR model and classifies the outcome as
  hit / miss. Misses are saved as annotated screenshots.
- **Qt GUI** — live preview, stats, start/stop, log pane.
- **Region overlay** — a click-through always-on-top layered window that draws
  the capture regions so you can see exactly what the program is looking at.

## Requirements

- Windows 10 2004+ (uses `SetWindowDisplayAffinity`)
- Python 3.11+
- The target application must run in **windowed / borderless** mode, not exclusive
  fullscreen — exclusive fullscreen returns black frames to GDI capture.

## Setup

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

First copy the example config, then edit it:

```bat
copy config.example.toml config.toml
```

- `[game] window_titles` — the window title to focus
- `[search] region` — normalised screen rect to scan
- `[detect]` — colour / size / aspect thresholds
- `[verify] region` — where the status line appears

`roi-overlay.bat` draws both regions on screen so you can tune them visually.

## Packaging

`build.py` produces a self-contained folder (PyInstaller + Qt + ONNX runtime).
Output goes to `release/`. Delete that folder between builds — PyInstaller
refuses to overwrite a large output tree cleanly.

## Notes

Thresholds in `config.toml` are tuned for one particular scene. Different
resolutions, UI scales or lighting will need re-tuning; that's what the overlay
and the `calibrate check` subcommand are for.

No network access, no telemetry.
