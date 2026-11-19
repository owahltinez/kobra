"""Talks to a Kobra S1 running Rinkhals over Moonraker and SSH."""

import dataclasses
import gzip
import json
import pathlib
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import click

GCODES = "/useremain/app/gk/gcodes"
BUSY = ("printing", "paused")
# Hot starts after a power cycle wait forever for the nozzle to reach 0 °C.
COLD = 45


@dataclasses.dataclass(frozen=True)
class Printer:
    """A printer by host name or address."""

    host: str

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict | None = None,
        timeout: float = 30,
    ) -> dict:
        """Calls Moonraker and returns its result."""
        url = f"http://{self.host}:7125{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            url,
            data=data if method == "POST" else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)["result"]
        except urllib.error.HTTPError as err:
            try:
                message = json.load(err)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = err.reason
            raise click.ClickException(f"{path}: {message}") from err
        except (urllib.error.URLError, TimeoutError) as err:
            raise click.ClickException(f"{self.host}: {err}") from err

    def query(self, *objects: str) -> dict:
        """Returns the status of printer objects such as print_stats."""
        # Querying an object the printer doesn't list wedges GoKlipper.
        query = "&".join(urllib.parse.quote(o) for o in objects)
        return self.request("GET", f"/printer/objects/query?{query}")["status"]

    def gcode(self, script: str, timeout: float = 300) -> None:
        """Runs G-code and waits for it to finish."""
        self.request(
            "POST", "/printer/gcode/script", {"script": script}, timeout=timeout
        )

    def ssh(self, command: str, stdin: bytes | None = None) -> str:
        """Runs a shell command on the printer and returns its output."""
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"root@{self.host}", command],
            input=stdin,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            error = result.stderr.decode(errors="replace").strip()
            raise click.ClickException(f"ssh {self.host}: {error}")
        return result.stdout.decode(errors="replace")

    def upload(self, path: pathlib.Path) -> None:
        """Copies G-code to the printer without starting it."""
        dest = shlex.quote(f"{GCODES}/{path.name}")
        # Moonraker uploads crawl at ~100 KB/s; gzip over SSH is ~5x faster.
        # The .part name keeps Moonraker from listing a half-written file.
        self.ssh(
            f"gzip -d > {dest}.part && mv {dest}.part {dest}",
            stdin=gzip.compress(path.read_bytes(), compresslevel=6),
        )
        click.echo(f"uploaded {path.name} to {self.host}")

    def tools(self, filename: str) -> list[int]:
        """Returns the tools a G-code file on the printer changes to."""
        path = shlex.quote(f"{GCODES}/{filename}")
        # grep exits 1 with no match and 2 for a missing file.
        out = self.ssh(f"grep -oE '^T[0-3]$' {path}; test $? -lt 2")
        return sorted({int(line[1:]) for line in out.split()})

    def snapshot(self) -> bytes:
        """Returns a webcam frame as JPEG."""
        url = f"http://{self.host}/webcam/?action=snapshot"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as err:
            raise click.ClickException(f"webcam: {err}") from err


def current() -> Printer:
    """Returns the printer named on the command line."""
    host = click.get_current_context().find_root().obj
    if not host:
        raise click.UsageError("name the printer with --host or KOBRA_HOST")
    return Printer(host)


def preflight(
    state: str, status: dict, mmu: dict, tools: list[int]
) -> list[str]:
    """Returns why a print can't start yet, or nothing."""
    problems = []
    if state != "ready":
        problems.append(f"printer is {state}, not ready")
    if status["print_stats"]["state"] in BUSY:
        problems.append(f"already {status['print_stats']['state']}")
    nozzle = status["extruder"]["temperature"]
    if nozzle > COLD:
        problems.append(
            f"nozzle at {nozzle:.0f} °C; start at {COLD} °C or below, or the"
            " start routine can wait forever"
        )
    if not tools:
        problems.append("no T<n> in the G-code to check the slot against")
    for tool in tools:
        if mmu["gate_status"][tool] != 1:
            problems.append(f"slot {tool + 1} is empty")
        elif not mmu["gate_material"][tool]:
            problems.append(
                f"slot {tool + 1} has no material, so the tool change crashes;"
                f" set it with `kobra slot {tool + 1} PLA <hex color>`"
            )
    return problems


def lower_bed(toolhead: dict, mm: float) -> list[str]:
    """Returns G-code that lowers the bed by up to MM, homed or not."""
    z = toolhead["position"][2]
    # Z grows downward on the S1: higher Z is a lower bed.
    delta = min(mm, toolhead["axis_maximum"][2] - z)
    if delta <= 0:
        return []
    # An unhomed Z refuses moves; homing it would probe onto the parts.
    unhomed = "z" not in toolhead["homed_axes"]
    return [
        *([f"SET_KINEMATIC_POSITION Z={z:.2f}"] if unhomed else []),
        "G91",
        f"G1 Z{delta:.2f} F600",
        "G90",
    ]


def duration(seconds: float) -> str:
    """Formats seconds as e.g. 3h54m."""
    minutes = round(seconds / 60)
    return (
        f"{minutes // 60}h{minutes % 60}m" if minutes >= 60 else f"{minutes}m"
    )


def summary(status: dict, mmu: dict, dryer: dict) -> str:
    """Describes the print, temperatures, ACE slots and dryer."""
    stats, card = status["print_stats"], status["virtual_sdcard"]
    line = stats["state"]
    if stats["filename"]:
        line += f" {stats['filename']}"
    if stats["state"] in BUSY:
        line += (
            f": {card['progress']:.0%}, layer"
            f" {card['current_layer']}/{card['total_layer']},"
            f" {duration(card['remain_time'])} left"
        )
    lines = [line]
    if stats["message"]:
        lines.append(f"message: {stats['message']}")
    nozzle, bed = status["extruder"], status["heater_bed"]
    lines.append(
        f"nozzle {nozzle['temperature']:.0f}/{nozzle['target']:.0f} °C,"
        f" bed {bed['temperature']:.0f}/{bed['target']:.0f} °C"
    )
    for i, loaded in enumerate(mmu["gate_status"]):
        material = mmu["gate_material"][i]
        if loaded != 1:
            text = "empty"
        elif material:
            text = f"{material} #{mmu['gate_color'][i][:6]}"
        else:
            text = "loaded, no material set"
        active = " (active)" if i == mmu["gate"] and loaded == 1 else ""
        lines.append(f"slot {i + 1}: {text}{active}")
    if dryer["dryer_status"] == "stop":
        lines.append("dryer off")
    else:
        lines.append(
            f"dryer {dryer['dryer_status']},"
            f" {dryer['dryer_temp']}/{dryer['dryer_target_temp']} °C"
        )
    return "\n".join(lines)
