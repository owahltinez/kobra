import json
import pathlib
import struct
import zipfile

import click
from click.testing import CliRunner
import pytest

from kobra.cli import cli
from kobra.slicing import apply_overrides
from kobra.slicing import insert_object_swaps
from kobra.slicing import insert_swaps
from kobra.slicing import object_slots
from kobra.slicing import parse_model
from kobra.slicing import Part
from kobra.slicing import print_time
from kobra.slicing import read_3mf


@pytest.mark.parametrize(
    "estimate, expected",
    [
        ("10h 1m 58s", "10h1m"),
        ("37m 5s", "37m"),
        ("1d 2h 3m 4s", "1d2h3m"),
        ("45s", "0m"),
    ],
)
def test_print_time(estimate, expected):
    assert (
        print_time(
            f"G1 X1\n; estimated printing time (normal mode) = {estimate}\n"
        )
        == expected
    )


def test_print_time_missing():
    with pytest.raises(click.ClickException):
        print_time("G1 X1\n")


def test_overrides_keep_list_shape():
    profiles = {
        "process": {"wall_loops": "3"},
        "filament": {"nozzle_temperature": ["205"]},
    }
    apply_overrides(profiles, ("wall_loops=4", "nozzle_temperature=210"))
    assert profiles == {
        "process": {"wall_loops": "4"},
        "filament": {"nozzle_temperature": ["210"]},
    }


@pytest.mark.parametrize("item", ["bogus=1", "wall_loops"])
def test_overrides_reject_unknown_or_malformed(item):
    with pytest.raises(click.BadParameter):
        apply_overrides({"process": {"wall_loops": "3"}}, (item,))


LAYERS = "".join(
    f";LAYER_CHANGE\n;Z:{z}\n; AFTER_LAYER_CHANGE {i} @ {z}mm\nG1 X{i}\n"
    for i, z in enumerate(["0.2", "0.4", "0.6"], 1)
)


def test_swaps_go_after_the_first_layer_change_above_each_height():
    assert insert_swaps(LAYERS, [(0.4, 2), (0.2, 4)]) == LAYERS.replace(
        "@ 0.4mm\n", "@ 0.4mm\nT3\n"
    ).replace("@ 0.6mm\n", "@ 0.6mm\nT1\n")


def test_swaps_between_layers_round_up():
    assert "@ 0.4mm\nT1\n" in insert_swaps(LAYERS, [(0.3, 2)])


def test_swap_above_the_top_fails():
    with pytest.raises(click.ClickException, match=r"no layer above 0\.6"):
        insert_swaps(LAYERS, [(0.6, 2)])


def obj(name, z, xy):
    return (
        f"G1 X{xy} Y{xy} Z{z + 0.4} F18000\n;LAYER_CHANGE\n;Z:0.2\n"
        f"EXCLUDE_OBJECT_START NAME={name}.stl_id_0_copy_0\nG1 X1\n"
        f";LAYER_CHANGE\n;Z:{z}\n"
        f"EXCLUDE_OBJECT_START NAME={name}.stl_id_0_copy_0\nG1 X2\n"
    )


SEQUENCE = "T0\n" + obj("arm", 10, 5) + obj("leg", 20, 6) + obj("head", 30, 7)


def test_object_swaps_go_before_each_new_color():
    gcode, first = insert_object_swaps(
        SEQUENCE, {"arm": 4, "leg": 4, "head": 1}
    )
    assert first == 4
    # Lift above the tallest print so far, swap, return over the next object.
    swap = "G1 Z22.00 F600\nT0\nG1 X7 Y7 F18000\n;LAYER_CHANGE\n;Z:0.2\n"
    assert gcode.splitlines().count("T0") == 2  # the start one and the swap
    assert swap + "EXCLUDE_OBJECT_START NAME=head" in gcode
    assert "NAME=leg" in gcode.split(swap)[0]


def test_object_swaps_need_a_slot_for_every_object():
    with pytest.raises(click.ClickException, match="no slot for head"):
        insert_object_swaps(SEQUENCE, {"arm": 4, "leg": 4})


TRIANGLE = (
    '<mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1.5" y="0" z="0"/>'
    '<vertex z="3" y="2" x="0"/></vertices>'
    '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh>'
)
META = (
    '<metadata name="Title">My Robot!</metadata>'
    '<metadata name="Designer">someone</metadata>'
)


def object_settings(*objects):
    """Orca's model_settings.config with each object's settings."""
    objs = "".join(
        f'<object id="{i}"><metadata key="name" value="o{i}"/>'
        + "".join(f'<metadata key="{k}" value="{v}"/>' for k, v in o.items())
        + "</object>"
        for i, o in enumerate(objects, 2)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><config>{objs}</config>'


def model_xml(*objects, meta=META, attrs='unit="millimeter"', rotation=""):
    """Builds a 3MF model with one build item per object's attributes."""
    objs = "".join(
        f'<object id="{i}" type="model" {a}>{TRIANGLE}</object>'
        for i, a in enumerate(objects, 2)
    )
    items = "".join(
        f'<item objectid="{i}" transform="{rotation or "1 0 0 0 1 0 0 0 1"}'
        ' 5 5 0"/>'
        for i in range(2, len(objects) + 2)
    )
    return (
        f'<model {attrs} xml:lang="en-US"'
        ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'{meta}<resources><basematerials id="1">'
        '<base name="purple" displaycolor="#7B3FA0"/>'
        '<base name="gray" displaycolor="#808080FF"/></basematerials>'
        f"{objs}</resources><build>{items}</build></model>"
    ).encode()


def write_3mf(path, model, settings=None):
    with zipfile.ZipFile(path, "w") as package:
        if settings:
            package.writestr("Metadata/model_settings.config", settings)
        package.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Target="/3D/3dmodel.model"'
            ' Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/'
            '2013/01/3dmodel"/></Relationships>',
        )
        package.writestr("3D/3dmodel.model", model)
    return path


def test_read_3mf(tmp_path):
    four = {"wall_loops": "4", "extruder": "2"}
    path = write_3mf(
        tmp_path / "x.3mf",
        model_xml('name="arm left" pid="1" pindex="1"', 'name="coupon"'),
        object_settings(four, four),
    )
    title, settings, parts = read_3mf(path)
    assert title == "My Robot!"
    assert settings == {"wall_loops": "4"}
    assert [p[:3] for p in parts] == [
        ("arm_left", "gray", "#808080FF"),
        ("coupon", None, None),
    ]
    stl = parts[0].stl
    assert len(stl) == 80 + 4 + 50
    assert struct.unpack_from("<I", stl, 80) == (1,)
    # 80-byte header and count, then the unit normal and the vertices.
    assert struct.unpack_from("<12f", stl, 84) == pytest.approx(
        (0, -4.5 / 29.25**0.5, 3 / 29.25**0.5, 0, 0, 0, 1.5, 0, 0, 0, 2, 3)
    )


def test_read_3mf_without_settings(tmp_path):
    path = write_3mf(tmp_path / "x.3mf", model_xml('name="a"'))
    assert read_3mf(path)[1] == {}


def test_read_3mf_rejects_objects_with_different_settings(tmp_path):
    path = write_3mf(
        tmp_path / "x.3mf",
        model_xml('name="a"', 'name="b"'),
        object_settings({"wall_loops": "4"}, {"wall_loops": "2"}),
    )
    with pytest.raises(click.ClickException, match="different settings"):
        read_3mf(path)


def test_read_3mf_rejects_a_non_3mf(tmp_path):
    (tmp_path / "x.3mf").write_text("solid")
    with pytest.raises(click.ClickException, match="malformed 3MF"):
        read_3mf(tmp_path / "x.3mf")


@pytest.mark.parametrize(
    "model, error",
    [
        (model_xml('name="a"', attrs='unit="inch"'), "unit: inch"),
        (
            model_xml('name="a"', attrs='requiredextensions="p"'),
            "extensions: p",
        ),
        (model_xml('name="a"', rotation="0 1 0 -1 0 0 0 0 1"), "translated"),
        (model_xml('name="a b"', 'name="a_b"'), "unique"),
        (model_xml(""), "unique"),
        (model_xml('name="a" pid="7"'), "a: no base material 7/0"),
        (model_xml('name="a" pid="1" pindex="2"'), "no base material 1/2"),
        (
            model_xml('name="a"').replace(
                TRIANGLE.encode(),
                b'<components><component objectid="2"/></components>',
            ),
            "a: only mesh objects",
        ),
        (
            model_xml('name="a"').replace(b'"model"', b'"support"'),
            "a: only mesh objects",
        ),
        (model_xml(), "no objects"),
    ],
)
def test_parse_model_rejects_unsupported(model, error):
    with pytest.raises(click.ClickException, match=error):
        parse_model(model)


PARTS = [
    Part("torso", "purple", "#7B3FA0", b""),
    Part("arm", "gray", "#808080", b""),
    Part("leg", "gray", "#808080", b""),
    Part("coupon", None, None, b""),
]


def test_object_slots_map_colors_and_default_the_rest():
    assert object_slots(PARTS, {"purple": 1, "gray": 2, "red": 3}, 4) == [
        1,
        2,
        2,
        4,
    ]


def test_object_slots_list_unmapped_colors_and_their_objects():
    with pytest.raises(click.BadParameter, match=r"gray \(arm, leg\)$"):
        object_slots(PARTS, {"purple": 1}, 4)


@pytest.fixture(name="robot")
def fixture_robot(tmp_path, monkeypatch):
    """A three-object 3MF, sliced by a fake Orca that records the process."""
    path = write_3mf(
        tmp_path / "robot.3mf",
        model_xml(
            'name="torso" pid="1" pindex="0"',
            'name="arm" pid="1" pindex="1"',
            'name="coupon"',
        ),
        object_settings(*[{"wall_loops": "4"}] * 3),
    )
    process = {}

    def fake_run(*args, stdin=None):
        del stdin
        tmp = pathlib.Path(args[args.index("--outputdir") + 1])
        process.update(json.loads((tmp / "process.json").read_text()))
        stls = [pathlib.Path(a) for a in args[args.index("--outputdir") + 2 :]]
        assert all(stl.stat().st_size == 134 for stl in stls)
        (tmp / "plate_1.gcode").write_text(
            "T0\n"
            + "".join(obj(s.stem, 10 + i, i) for i, s in enumerate(stls))
            + "; estimated printing time (normal mode) = 1m 2s\n"
            + "; total filament used [g] = 1.00\n"
        )

    monkeypatch.setattr("kobra.slicing.run", fake_run)
    return path, process


def test_list_shows_objects_colors_and_settings(robot):
    result = CliRunner().invoke(cli, ["slice", str(robot[0]), "--list"])
    assert result.output == (
        "objects:\n  torso: purple #7B3FA0\n  arm: gray #808080FF\n"
        "  coupon: default slot\nsettings:\n  wall_loops = 4\n"
    )
    assert not robot[1]


def test_mixed_slots_print_by_object(robot):
    path, process = robot
    result = CliRunner().invoke(
        cli,
        ["slice", str(path), "--slot-for", "purple=2", "--slot-for", "gray=3"],
        catch_exceptions=False,
    )
    out = path.with_name("my-robot_PLA_0.2_1m.gcode")
    assert result.output == f"{out} (1.00 g)\n"
    assert process["print_sequence"] == "by object"
    assert process["wall_loops"] == "4"
    tools = [x for x in out.read_text().splitlines() if x.startswith("T")]
    assert tools == ["T1", "T2", "T0"]


def test_single_slot_prints_by_layer_and_set_beats_the_3mf(robot):
    path, process = robot
    result = CliRunner().invoke(
        cli,
        [
            "slice",
            str(path),
            *("--slot", "3", "--slot-for", "purple=3", "--slot-for", "gray=3"),
            *("--set", "wall_loops=2", "--name", "r"),
        ],
        catch_exceptions=False,
    )
    out = path.with_name("r_PLA_0.2_1m.gcode")
    assert result.output == f"{out} (1.00 g)\n"
    assert process["print_sequence"] == "by layer"
    assert process["wall_loops"] == "2"
    assert [x for x in out.read_text().splitlines() if x.startswith("T")] == [
        "T2"
    ]


@pytest.mark.parametrize(
    "args, error",
    [
        (["--slot-for", "purple=1"], "no slot for gray (arm)"),
        (["--slot-for", "purple=5"], "SLOT 1-4"),
        (["--slots", "1,2,3"], "use --slot-for, not --slots"),
        (
            ["--slot-for", "purple=1", "--slot-for", "gray=2", "--copies", "2"],
            "--copies",
        ),
    ],
)
def test_3mf_rejects_bad_slots(robot, args, error):
    result = CliRunner().invoke(cli, ["slice", str(robot[0]), *args])
    assert result.exit_code == 2
    assert error in result.output
    assert not robot[1]


@pytest.mark.parametrize("flag", [["--list"], ["--slot-for", "gray=1"]])
def test_list_and_slot_for_need_a_3mf(robot, flag):
    stl = robot[0].with_name("a.stl")
    stl.write_text("solid")
    result = CliRunner().invoke(cli, ["slice", str(stl), *flag])
    assert result.exit_code == 2
    assert "need a .3mf" in result.output
    result = CliRunner().invoke(cli, ["slice", str(robot[0]), str(stl), *flag])
    assert result.exit_code == 2
    assert "only model" in result.output


def test_upload_needs_a_printer_before_slicing(robot, monkeypatch):
    monkeypatch.delenv("KOBRA_HOST", raising=False)
    result = CliRunner().invoke(cli, ["slice", str(robot[0]), "--upload"])
    assert result.exit_code == 2
    assert "KOBRA_HOST" in result.output
    assert not robot[1]
