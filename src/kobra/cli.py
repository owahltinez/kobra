"""Slices for and drives a Kobra S1 with an ACE, running Rinkhals."""

import pathlib
import re
import time

import click

from kobra import printer
from kobra import slicing


@click.group()
@click.option(
    "--host",
    envvar="KOBRA_HOST",
    help="Printer host or address; defaults to $KOBRA_HOST.",
)
@click.pass_context
def cli(ctx, host):
    """Slices for and drives a Kobra S1 with an ACE, running Rinkhals."""
    ctx.obj = host


cli.add_command(slicing.slice_models)


def ensure_idle(p: printer.Printer) -> None:
    """Refuses to act while a print is under way."""
    state = p.query("print_stats")["print_stats"]["state"]
    if state in printer.BUSY:
        raise click.ClickException(f"printer is {state}")


def report(p: printer.Printer, snap: pathlib.Path) -> None:
    """Prints the status summary and saves a webcam snapshot."""
    status = p.query("print_stats", "virtual_sdcard", "extruder", "heater_bed")
    # The mmu_machine object query can be stale; this endpoint isn't.
    ace = p.request("GET", "/server/mmu-ace")["status"]
    click.echo(
        printer.summary(status, ace["mmu"], ace["mmu_machine"]["unit_0"])
    )
    take_snapshot(p, snap)


def take_snapshot(p: printer.Printer, path: pathlib.Path) -> None:
    """Saves a webcam frame, switching the chamber light on first."""
    power = "/machine/device_power/device"
    light = p.request("GET", power, {"device": "chamber_light"})
    if light.get("chamber_light") != "on":
        p.request("POST", power, {"device": "chamber_light", "action": "on"})
        time.sleep(2)
    path.write_bytes(p.snapshot())
    click.echo(f"snapshot: {path}")


snapshot_option = click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=pathlib.Path),
    default="snap.jpg",
    show_default=True,
    help="Where to save the webcam snapshot.",
)


@cli.command()
@snapshot_option
def status(output):
    """Shows the print, temperatures, ACE slots and dryer, with a snapshot."""
    report(printer.current(), output)


@cli.command()
@snapshot_option
def snap(output):
    """Saves a webcam snapshot, switching the chamber light on if off."""
    take_snapshot(printer.current(), output)


@cli.command()
@click.option(
    "--minutes",
    type=click.FloatRange(0, min_open=True),
    default=15,
    show_default=True,
)
@snapshot_option
def watch(minutes, output):
    """Waits while printing, up to MINUTES, then reports with a snapshot.

    Returns early once the print stops, pauses or fails.
    """
    p = printer.current()
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        state = p.query("print_stats")["print_stats"]["state"]
        if state != "printing":
            break
        time.sleep(min(20, max(0, deadline - time.monotonic())))
    report(p, output)


@cli.command()
@click.argument(
    "gcode",
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
)
def upload(gcode):
    """Copies GCODE to the printer without starting it."""
    printer.current().upload(gcode)


@cli.command()
@click.argument("filename")
def start(filename):
    """Starts FILENAME, already on the printer, after checking it can.

    Refuses a hot nozzle, a busy printer, and tools whose ACE slot is empty
    or has no material set.
    """
    p = printer.current()
    name = pathlib.PurePath(filename).name
    problems = printer.preflight(
        p.request("GET", "/printer/info")["state"],
        p.query("print_stats", "extruder"),
        p.query("mmu")["mmu"],
        p.tools(name),
    )
    if problems:
        raise click.ClickException("can't start:\n  " + "\n  ".join(problems))
    p.request("POST", "/printer/print/start", {"filename": name})
    click.echo(f"started {name}")


@cli.command()
@click.argument("name", required=False)
def skip(name):
    """Stops printing object NAME and keeps the rest; lists them without it."""
    p = printer.current()
    objects = p.query("exclude_object")["exclude_object"]
    names = [o["name"] for o in objects["objects"]]
    if name is None:
        for o in objects["objects"]:
            mark = ""
            if o["name"] in objects["excluded_objects"]:
                mark = " (skipped)"
            elif o["name"] == objects["current_object"]:
                mark = " (printing)"
            x, y = o.get("center", ("?", "?"))
            click.echo(f"{o['name']} at {x}, {y}{mark}")
        return
    if name not in names:
        raise click.BadParameter(f"no object {name}; run `kobra skip`")
    p.gcode(f"EXCLUDE_OBJECT NAME={name}")
    click.echo(f"skipped {name}")


@cli.command("lower-bed")
@click.argument("mm", type=click.FloatRange(0, min_open=True), default=100)
def lower_bed(mm):
    """Lowers the bed by MM to clear it, without homing onto the parts."""
    p = printer.current()
    ensure_idle(p)
    script = printer.lower_bed(p.query("toolhead")["toolhead"], mm)
    for line in script:
        p.gcode(line)
    click.echo("lowered the bed" if script else "the bed is already lowest")


@cli.command()
@click.argument("slot", type=click.IntRange(1, 4))
@click.option("--tries", type=click.IntRange(1), default=10, show_default=True)
def unload(slot, tries):
    """Retracts SLOT's filament back into the ACE."""
    p = printer.current()
    ensure_idle(p)
    for _ in range(tries):
        if p.query("mmu")["mmu"]["gate_status"][slot - 1] != 1:
            click.echo(f"unloaded slot {slot}")
            return
        # Each call unwinds for ~8 s.
        p.gcode(f"UNWIND_ALL_FILAMENT ID=0 INDEX={slot - 1}")
    raise click.ClickException(f"slot {slot} still loaded after {tries} tries")


@cli.command("slot")
@click.argument("slot", type=click.IntRange(1, 4))
@click.argument("material")
@click.argument("color")
@click.option("--temp", type=click.IntRange(150, 300), default=210)
def set_slot(slot, material, color, temp):
    """Sets SLOT's MATERIAL and hex COLOR, as the touchscreen does.

    A slot with no material crashes the tool change that loads it.
    """
    if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
        raise click.BadParameter(f"expected a hex color, got {color}")
    if not re.fullmatch(r"[A-Za-z0-9+ -]+", material):
        raise click.BadParameter(f"unexpected material {material!r}")
    printer.current().gcode(
        f'MMU_GATE_MAP GATE={slot - 1} MATERIAL="{material}"'
        f' COLOR="{color.lstrip("#").upper()}" TEMP={temp} QUIET=1'
    )
    click.echo(f"slot {slot}: {material} #{color.lstrip('#').upper()}")


@cli.command()
@click.option(
    "--temp", type=click.IntRange(1, 55), default=45, show_default=True
)
@click.option(
    "--minutes", type=click.IntRange(1), default=240, show_default=True
)
@click.option("--stop", is_flag=True, help="Stop drying instead.")
def dry(temp, minutes, stop):
    """Runs the ACE dryer; 45 °C suits PLA.

    Cancelling or finishing a print stops it.
    """
    p = printer.current()
    hub = "/server/filament_hub"
    if stop:
        p.request("POST", f"{hub}/stop_drying", body={"id": 0})
        click.echo("dryer stopped")
        return
    p.request(
        "POST",
        f"{hub}/start_drying",
        body={"id": 0, "temp": temp, "duration": minutes},
    )
    click.echo(f"drying at {temp} °C for {minutes} min")
