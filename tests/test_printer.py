import pytest

from kobra.printer import duration
from kobra.printer import lower_bed
from kobra.printer import preflight
from kobra.printer import summary

MMU = {
    "gate": 1,
    "gate_status": [1, 1, 0, 1],
    "gate_material": ["PLA", "PLA+", "", ""],
    "gate_color": ["800080FF", "3D54AAFF", "000000FF", "000000FF"],
}


def status(state="standby", nozzle=30.0, target=0, filename="", message=""):
    return {
        "print_stats": {
            "state": state,
            "filename": filename,
            "message": message,
        },
        "extruder": {"temperature": nozzle, "target": target},
    }


def test_preflight_passes_a_cold_idle_printer_with_loaded_slots():
    assert preflight("ready", status(), MMU, [0, 1]) == []


def test_preflight_lists_every_problem():
    problems = preflight("ready", status("paused", 210), MMU, [1, 2, 3])
    assert problems[0] == "already paused"
    assert problems[1].startswith("nozzle at 210 °C")
    assert problems[2] == "slot 3 is empty"
    assert problems[3].startswith("slot 4 has no material")
    assert "kobra slot 4 PLA" in problems[3]
    assert len(problems) == 4


@pytest.mark.parametrize(
    "state, tools, error",
    [
        ("shutdown", [0], "printer is shutdown, not ready"),
        ("ready", [], "no T<n> in the G-code to check the slot against"),
    ],
)
def test_preflight_refuses_a_bad_state_or_unknown_slot(state, tools, error):
    assert preflight(state, status(), MMU, tools) == [error]


def toolhead(z, homed="xyz"):
    return {
        "position": [10, 20, z, 0],
        "axis_maximum": [265, 277, 253, 0],
        "homed_axes": homed,
    }


def test_lower_bed_homed_moves_relative():
    assert lower_bed(toolhead(5), 100) == ["G91", "G1 Z100.00 F600", "G90"]


def test_lower_bed_unhomed_sets_the_position_first():
    assert lower_bed(toolhead(5.123, "xy"), 100)[0] == (
        "SET_KINEMATIC_POSITION Z=5.12"
    )


def test_lower_bed_stops_at_the_axis_limit():
    assert lower_bed(toolhead(200), 100)[1] == "G1 Z53.00 F600"
    assert lower_bed(toolhead(253), 100) == []


@pytest.mark.parametrize(
    "seconds, expected", [(14040, "3h54m"), (59 * 60, "59m"), (20, "0m")]
)
def test_duration(seconds, expected):
    assert duration(seconds) == expected


def test_summary_while_printing():
    printing = status("printing", 205.4, 205, "robot.gcode") | {
        "heater_bed": {"temperature": 65, "target": 65},
        "virtual_sdcard": {
            "progress": 0.4,
            "current_layer": 19,
            "total_layer": 160,
            "remain_time": 14040,
        },
    }
    dryer = {
        "dryer_status": "drying",
        "dryer_temp": 44,
        "dryer_target_temp": 45,
    }
    assert summary(printing, MMU, dryer) == (
        "printing robot.gcode: 40%, layer 19/160, 3h54m left\n"
        "nozzle 205/205 °C, bed 65/65 °C\n"
        "slot 1: PLA #800080\n"
        "slot 2: PLA+ #3D54AA (active)\n"
        "slot 3: empty\n"
        "slot 4: loaded, no material set\n"
        "dryer drying, 44/45 °C"
    )


def test_summary_when_idle_shows_the_message_and_skips_progress():
    idle = status("error", message="Filament is stuck") | {
        "heater_bed": {"temperature": 25, "target": 0},
        "virtual_sdcard": {},
    }
    lines = summary(idle, MMU, {"dryer_status": "stop"}).splitlines()
    assert lines[:2] == ["error", "message: Filament is stuck"]
    assert lines[-1] == "dryer off"
