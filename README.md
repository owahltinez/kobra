# kobra

Slices models for an Anycubic Kobra S1 with an ACE Pro, and drives the printer.
The printer must run [Rinkhals](https://github.com/jbatonnet/Rinkhals), which
provides Moonraker and SSH. Slicing uses the open-source
[OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) CLI, expected at
`/Applications/OrcaSlicer.app` (macOS).

```sh
uv tool install git+https://github.com/owahltinez/kobra
export KOBRA_HOST=192.168.1.50   # or pass --host
```

Uploads use `ssh root@$KOBRA_HOST` with key auth.

## Printing

```sh
kobra slice part.stl --slot 2 --upload   # slice, copy to the printer
kobra start part_PLA_0.2_1h3m.gcode      # checks first, then starts
kobra watch                              # waits up to 15 min, reports, saves snap.jpg
```

| Command | Does |
| --- | --- |
| `status` | Print progress, temperatures, ACE slots, dryer; saves a snapshot |
| `snap` | Saves a webcam snapshot, switching the chamber light on if off |
| `watch` | Waits while printing (`--minutes`, default 15), then reports like `status` |
| `upload FILE` | Copies G-code over SSH, gzipped (~5x faster than Moonraker) |
| `start FILE` | Starts a file on the printer once the checks below pass |
| `skip [NAME]` | Lists objects, or stops printing one and keeps the rest |
| `lower-bed [MM]` | Lowers the bed after a cancel without homing onto the parts |
| `unload SLOT` | Retracts a slot's filament into the ACE |
| `slot SLOT MATERIAL COLOR` | Sets a slot's material and color, as the screen does |
| `dry` | Runs the ACE dryer (`--temp 45 --minutes 240`, `--stop`) |

`start` refuses when the printer isn't ready or is busy, when the nozzle is
above 45 °C (after a power cycle a hot start waits forever for 0 °C), and when
a tool the G-code uses has an empty slot or one with no material set (the tool
change crashes). Pause, resume and cancel are plain Moonraker calls:
`curl -X POST http://$KOBRA_HOST:7125/printer/print/pause`.

## Slicing

```sh
kobra slice part.scad -D 'part="a"' --set wall_loops=4 --upload
```

The G-code lands next to the model as `<name>_<material>_<layer>_<time>.gcode`.
Several models go on one plate: `kobra slice a.stl b.stl --name plate`.
`--slots 4,4,1` gives each model its own ACE slot and prints them one at a
time (Orca's by-object sequence, shortest first). Between objects of different
slots the nozzle lifts above everything printed, swaps, and returns.

`--color HEIGHT:SLOT` (repeatable) swaps to another ACE slot for every layer
above that height, e.g. `--slot 4 --color 22:1 --color 50:4` for red, blue,
red bands. The swap is a bare `T<n>` after the layer change; the firmware cuts,
loads and purges. Orca's own multi-filament slicing segfaults in the CLI.

## 3MF

A `.3mf` (core spec, e.g. from the [toybox](https://owahltinez.github.io/toybox) robot designer) prints each mesh
object as its own model, named after the object. `--list` shows each object's
color and the settings in its Orca `Metadata/model_settings.config`; `--slot-for COLOR=SLOT` maps colors to
ACE slots, and uncolored objects use `--slot`:

```sh
kobra slice robot.3mf --list
kobra slice robot.3mf --slot-for purple=1 --slot-for gray=2 --upload
```

Mixed slots print by object as with `--slots`. Every object must carry the
same Orca settings; they apply like `--set`, which wins over them. The output is named after the 3MF's Title.
A `.3mf` must be the only model, and components, other units, rotated build
items and extensions are rejected.

## Profiles

`src/kobra/profiles/` holds the fully resolved Anycubic Slicer Next 1.4.1.1 presets
(`Anycubic Kobra S1 0.4 nozzle - Default`, `0.20mm User Default`,
`Anycubic PLA+ Default`), with these edits for Orca:

- `machine_start_gcode` ends with `T[initial_tool]` so the ACE loads a spool
- `retraction_distances_when_cut` is 18; Orca rejects Anycubic's 0
- `curr_bed_type` is `High Temp Plate`, so the 60 °C bed temperature applies
- `solid_infill_rotate_template` is empty, so solid layers alternate at 45°/135°
  instead of Orca's S1 default of 0°/90°
- `name` and `compatible_printers` match Orca's built-in Kobra S1 profile

Edit the JSON directly to change defaults; use `--set` for one-off changes.

## Development

```sh
uv run pytest
```
