"""Slices OpenSCAD, STL or 3MF models for the Kobra S1 with OrcaSlicer."""

import json
import math
import pathlib
import re
import struct
import subprocess
import tempfile
from typing import NamedTuple
import xml.etree.ElementTree as ET
import zipfile

import click

from kobra import printer

ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
PROFILES = pathlib.Path(__file__).parent / "profiles"
KINDS = ("machine", "process", "filament")
FILAMENTS = {"pla": "filament.json", "petg": "filament-petg.json"}
CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
START_PART = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
# Orca's per-object settings; objects all carry the same ones from toybox.
OBJECT_SETTINGS = "Metadata/model_settings.config"
IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


class Part(NamedTuple):
    """A 3MF object as a binary STL, with its base material if any."""

    name: str
    color: str | None
    hex: str | None
    stl: bytes


def run(*args, stdin: bytes | None = None) -> None:
    """Runs a command quietly, surfacing its error lines on failure."""
    result = subprocess.run(args, input=stdin, check=False, capture_output=True)
    if result.returncode:
        lines = (
            (result.stdout + result.stderr)
            .decode(errors="replace")
            .splitlines()
        )
        errors = [
            line
            for line in lines
            if re.search(r"error|not in range|invalid", line, re.I)
        ]
        raise click.ClickException(
            f"{args[0]} failed:\n" + "\n".join(errors or lines[-10:])
        )


def apply_overrides(
    profiles: dict[str, dict], overrides: tuple[str, ...]
) -> None:
    """Applies key=value overrides to whichever profile defines the key."""
    for item in overrides:
        key, sep, value = item.partition("=")
        owner = next((p for p in profiles.values() if key in p), None)
        if not sep or owner is None:
            raise click.BadParameter(
                f"unknown setting: {item}", param_hint="--set"
            )
        owner[key] = (
            [value] * len(owner[key]) if isinstance(owner[key], list) else value
        )


def header(gcode: str, key: str) -> str:
    """Returns a slicer header value such as `total filament used [g]`."""
    match = re.search(rf"^; {re.escape(key)} = (.+)$", gcode, re.M)
    if not match:
        raise click.ClickException(f"no '{key}' in G-code")
    return match.group(1)


def insert_swaps(gcode: str, swaps: list[tuple[float, int]]) -> str:
    """Switches to ACE slot S before the first layer above height H.

    The swap goes right after Orca's layer change marker: the nozzle is
    retracted and outside any object, and the next move lifts before travel.
    """
    pending = sorted(swaps)
    out = []
    z = 0.0
    for line in gcode.splitlines(keepends=True):
        out.append(line)
        if line.startswith(";Z:"):
            z = float(line[3:])
        elif line.startswith("; AFTER_LAYER_CHANGE"):
            slot = None
            while pending and pending[0][0] < z - 1e-6:
                slot = pending.pop(0)[1]
            if slot is not None:
                out.append(f"T{slot - 1}\n")
    if pending:
        raise click.ClickException(f"no layer above {pending[0][0]} mm")
    return "".join(out)


def insert_object_swaps(gcode: str, slots: dict[str, int]) -> tuple[str, int]:
    """Prints each object from its ACE slot when printing one at a time.

    Objects are named by model file stem. A swap goes before the layer change
    that starts an object in a new slot: the nozzle lifts 2 mm above the
    tallest layer printed so far, swaps, and returns over the new object.
    Returns the G-code and the first object's slot.
    """
    lines = gcode.splitlines(keepends=True)
    inserts = []
    seen = set()
    current = first = None
    top = 0.0
    layer = travel = start = None
    for i, line in enumerate(lines):
        if line.startswith(";LAYER_CHANGE"):
            layer, start = i, travel
        elif line.startswith(";Z:"):
            top = max(top, float(line[3:]))
        elif line.startswith("G1 X") and " Y" in line:
            travel = line
        elif line.startswith("EXCLUDE_OBJECT_START NAME="):
            name = line.split("=", 1)[1].strip().rsplit("_id_", 1)[0]
            name = name.removesuffix(".stl")
            if name in seen:
                continue
            seen.add(name)
            if name not in slots:
                raise click.ClickException(f"no slot for {name}")
            if current is None:
                first = slots[name]
            elif slots[name] != current:
                move = re.search(r"X(\S+) Y(\S+)", start or "")
                if move is None or layer is None:
                    raise click.ClickException(f"no travel to {name}")
                x, y = move.groups()
                inserts.append(
                    (
                        layer,
                        f"G1 Z{top + 2:.2f} F600\nT{slots[name] - 1}\n"
                        f"G1 X{x} Y{y} F18000\n",
                    )
                )
            current = slots[name]
    if first is None:
        raise click.ClickException("no objects in G-code")
    for i, text in reversed(inserts):
        lines.insert(i, text)
    return "".join(lines), first


def parse_color(
    _ctx, _param, values: tuple[str, ...]
) -> list[tuple[float, int]]:
    """Parses HEIGHT:SLOT pairs such as 22:1."""
    swaps = []
    for value in values:
        height, _, slot = value.partition(":")
        try:
            swaps.append((float(height), int(slot)))
        except ValueError as err:
            raise click.BadParameter(
                f"expected HEIGHT:SLOT, got {value}"
            ) from err
        if not 1 <= swaps[-1][1] <= 4:
            raise click.BadParameter(f"slot must be 1-4, got {value}")
    return swaps


def parse_slot_for(_ctx, _param, values: tuple[str, ...]) -> dict[str, int]:
    """Parses COLOR=SLOT pairs such as purple=1."""
    slots = {}
    for value in values:
        color, _, slot = value.rpartition("=")
        if not color or slot not in ("1", "2", "3", "4"):
            raise click.BadParameter(
                f"expected COLOR=SLOT with SLOT 1-4, got {value}"
            )
        slots[color] = int(slot)
    return slots


def binary_stl(
    vertices: list[tuple[float, ...]], triangles: list[tuple[int, ...]]
) -> bytes:
    """Packs a mesh as binary STL."""
    rows = []
    for tri in triangles:
        a, b, c = (vertices[i] for i in tri)
        u, v = [b[k] - a[k] for k in range(3)], [c[k] - a[k] for k in range(3)]
        n = [
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        ]
        # Orca reads an STL without bytes >= 0x80 as ASCII; normals add signs.
        length = math.hypot(*n) or 1.0
        rows.append(
            struct.pack("<12fH", *(x / length for x in n), *a, *b, *c, 0)
        )
    return b"".join([bytes(80), struct.pack("<I", len(triangles)), *rows])


def parse_model(data: bytes) -> tuple[str, list[Part]]:
    """Returns a 3MF model's title and printed objects."""
    root = ET.fromstring(data)
    if root.tag != f"{CORE}model":
        raise click.ClickException(f"not a 3MF core model: {root.tag}")
    if root.get("unit", "millimeter") != "millimeter":
        raise click.ClickException(f"unsupported unit: {root.get('unit')}")
    if root.get("requiredextensions"):
        raise click.ClickException(
            f"unsupported extensions: {root.get('requiredextensions')}"
        )
    title = next(
        (
            m.text or ""
            for m in root.iterfind(f"{CORE}metadata")
            if m.get("name") == "Title"
        ),
        "",
    )
    resources = f"{CORE}resources/{CORE}"
    materials = {
        (group.get("id"), str(i)): (base.get("name"), base.get("displaycolor"))
        for group in root.iterfind(f"{resources}basematerials")
        for i, base in enumerate(group.iterfind(f"{CORE}base"))
    }
    objects = {o.get("id"): o for o in root.iterfind(f"{resources}object")}
    parts = []
    for item in root.iterfind(f"{CORE}build/{CORE}item"):
        obj = objects[item.attrib["objectid"]]
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", obj.get("name", ""))
        if not name or name in (p.name for p in parts):
            raise click.ClickException(
                f"object names must be unique: {obj.get('name')!r}"
            )
        mesh = obj.find(f"{CORE}mesh")
        if mesh is None or obj.get("type", "model") != "model":
            raise click.ClickException(
                f"{name}: only mesh objects of type model"
            )
        transform = item.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0").split()
        if [float(v) for v in transform[:9]] != IDENTITY:
            raise click.ClickException(f"{name}: only translated build items")
        color = None, None
        if "pid" in obj.attrib:
            key = obj.attrib["pid"], obj.get("pindex", "0")
            if key not in materials:
                raise click.ClickException(
                    f"{name}: no base material {'/'.join(key)}"
                )
            color = materials[key]
        vertices = [
            tuple(float(v.attrib[k]) for k in "xyz")
            for v in mesh.iterfind(f"{CORE}vertices/{CORE}vertex")
        ]
        triangles = [
            tuple(int(t.attrib[k]) for k in ("v1", "v2", "v3"))
            for t in mesh.iterfind(f"{CORE}triangles/{CORE}triangle")
        ]
        parts.append(Part(name, *color, binary_stl(vertices, triangles)))
    if not parts:
        raise click.ClickException("no objects to print in the 3MF build")
    return title, parts


def parse_object_settings(data: bytes) -> dict[str, str]:
    """Returns the settings every object shares in Orca's model_settings."""
    found = [
        {
            m.get("key", ""): m.get("value", "")
            for m in obj.iterfind("metadata")
            if m.get("key") not in ("name", "extruder")
        }
        for obj in ET.fromstring(data).iterfind("object")
    ]
    if any(f != found[0] for f in found):
        raise click.ClickException(
            "objects have different settings; one plate takes one set"
        )
    return found[0] if found else {}


def read_3mf(path: pathlib.Path) -> tuple[str, dict[str, str], list[Part]]:
    """Reads a 3MF's title, shared object settings and printed objects."""
    try:
        with zipfile.ZipFile(path) as package:
            rels = ET.fromstring(package.read("_rels/.rels"))
            targets = [
                r.get("Target", "") for r in rels if r.get("Type") == START_PART
            ]
            if not targets:
                raise click.ClickException(f"{path}: no 3D model part")
            title, parts = parse_model(package.read(targets[0].lstrip("/")))
            settings = (
                parse_object_settings(package.read(OBJECT_SETTINGS))
                if OBJECT_SETTINGS in package.namelist()
                else {}
            )
            return title, settings, parts
    except (
        zipfile.BadZipFile,
        ET.ParseError,
        KeyError,
        ValueError,
        IndexError,
    ) as err:
        raise click.ClickException(f"malformed 3MF {path}: {err}") from err


def object_slots(
    parts: list[Part], slot_for: dict[str, int], default: int
) -> list[int]:
    """Returns each part's ACE slot: its color's, or the default if none."""
    unmapped: dict[str, list[str]] = {}
    for part in parts:
        if part.color is not None and part.color not in slot_for:
            unmapped.setdefault(part.color, []).append(part.name)
    if unmapped:
        raise click.BadParameter(
            "no slot for "
            + "; ".join(f"{c} ({', '.join(n)})" for c, n in unmapped.items()),
            param_hint="--slot-for",
        )
    return [default if p.color is None else slot_for[p.color] for p in parts]


def print_time(gcode: str) -> str:
    """Returns the normal-mode estimate as e.g. 10h1m."""
    estimate = header(gcode, "estimated printing time (normal mode)")
    return re.sub(r"\s*\d+s$", "", estimate).replace(" ", "") or "0m"


@click.command("slice")
@click.argument(
    "models",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
)
@click.option(
    "-D",
    "defines",
    multiple=True,
    help="OpenSCAD variable, e.g. -D 'part=\"a\"'.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    help="Profile setting, e.g. --set wall_loops=4.",
)
@click.option(
    "--filament",
    type=click.Choice(FILAMENTS),
    default="pla",
    show_default=True,
)
@click.option(
    "--copies", type=click.IntRange(1), default=1, help="Copies on the plate."
)
@click.option(
    "--slot",
    type=click.IntRange(1, 4),
    default=1,
    help="ACE slot to print from.",
)
@click.option(
    "--color",
    "colors",
    multiple=True,
    callback=parse_color,
    help="Swap to an ACE slot from a height in mm, e.g. --color 22:1.",
)
@click.option(
    "--slots",
    help="ACE slot per model, e.g. 4,4,1; prints one object at a time, "
    "swapping filament between objects.",
)
@click.option(
    "--slot-for",
    multiple=True,
    callback=parse_slot_for,
    help="ACE slot for a .3mf color, e.g. --slot-for purple=1; uncolored "
    "objects use --slot. Mixed slots print one object at a time.",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    help="List a .3mf's objects, colors and settings without slicing.",
)
@click.option(
    "--name",
    help="Output base name; defaults to the first model's, or a .3mf's Title.",
)
@click.option(
    "--upload", is_flag=True, help="Upload to the printer without starting it."
)
def slice_models(
    models,
    *,
    defines,
    overrides,
    filament,
    copies,
    slot,
    colors,
    slots,
    slot_for,
    list_only,
    name,
    upload,
):
    """Slices MODELS (.scad or .stl, or a single .3mf) onto one plate.

    The G-code lands next to the first model.
    """
    model = models[0]
    target = printer.current() if upload else None
    files = {k: f"{k}.json" for k in KINDS} | {"filament": FILAMENTS[filament]}
    profiles = {k: json.loads((PROFILES / files[k]).read_text()) for k in KINDS}
    by_object = None
    parts = []
    if any(m.suffix == ".3mf" for m in models):
        if len(models) > 1 or slots:
            raise click.UsageError(
                "a .3mf must be the only model; use --slot-for, not --slots"
            )
        title, settings, parts = read_3mf(model)
        if list_only:
            click.echo("objects:")
            for part in parts:
                color = f"{part.color} {part.hex}" if part.color else None
                click.echo(f"  {part.name}: {color or 'default slot'}")
            click.echo("settings:")
            for key, value in settings.items():
                click.echo(f"  {key} = {value}")
            return
        try:
            apply_overrides(
                profiles, tuple(f"{k}={v}" for k, v in settings.items())
            )
        except click.BadParameter as err:
            raise click.ClickException(f"{model}: {err.message}") from err
        by_object = object_slots(parts, slot_for, slot)
        if len(set(by_object)) == 1:
            slot, by_object = by_object[0], None
        name = name or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    elif slot_for or list_only:
        raise click.UsageError("--slot-for and --list need a .3mf model")
    elif slots:
        by_object = [int(s) for s in slots.split(",")]
        if len(by_object) != len(models):
            raise click.BadParameter("one slot per model", param_hint="--slots")
    apply_overrides(profiles, overrides)
    if by_object:
        if copies != 1:
            raise click.BadParameter(
                "one copy when printing by object", param_hint="--copies"
            )
        profiles["process"]["print_sequence"] = "by object"
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = pathlib.Path(tmp_dir)
        stls = []
        for i, source in enumerate(models):
            stl = source
            if source.suffix == ".3mf":
                for part in parts:
                    stls.append(tmp / f"{part.name}.stl")
                    stls[-1].write_bytes(part.stl)
                continue
            if source.suffix == ".scad":
                stl = tmp / f"model{i}.stl"
                args = [f"-D{d}" for d in defines]
                run(
                    "openscad",
                    "--backend=manifold",
                    "--export-format=binstl",
                    *args,
                    "-o",
                    stl,
                    source,
                )
            stls.append(stl)
        for kind, profile in profiles.items():
            (tmp / f"{kind}.json").write_text(json.dumps(profile))
        run(
            ORCA,
            "--load-settings",
            f"{tmp}/machine.json;{tmp}/process.json",
            "--load-filaments",
            tmp / "filament.json",
            "--arrange",
            "1",
            "--allow-rotations",
            "--orient",
            "0",
            "--slice",
            "0",
            "--outputdir",
            tmp,
            *stls * copies,
        )
        if (tmp / "plate_2.gcode").exists():
            raise click.ClickException(
                f"{len(stls) * copies} models don't fit on one plate"
            )
        gcode = (tmp / "plate_1.gcode").read_text()
    if by_object:
        gcode, slot = insert_object_swaps(
            gcode, {m.stem: s for m, s in zip(stls, by_object, strict=True)}
        )
    # The ACE loads whichever spool the start G-code's tool change names.
    gcode, found = re.subn(r"^T0$", f"T{slot - 1}", gcode, count=1, flags=re.M)
    if not found:
        raise click.ClickException("no initial T0 in G-code")
    gcode = insert_swaps(gcode, colors)
    material = profiles["filament"]["filament_type"][0]
    out = model.with_name(
        f"{name or model.stem}_{material}_"
        f"{profiles['process']['layer_height']}_{print_time(gcode)}.gcode"
    )
    out.write_text(gcode)
    grams = header(gcode, "total filament used [g]")
    click.echo(f"{out} ({grams} g)")
    if target:
        target.upload(out)
