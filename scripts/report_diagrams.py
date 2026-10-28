"""Render reviewed architecture-under-test views as SVG using Python's XML library."""

import json
from xml.etree.ElementTree import Element, SubElement, tostring

ARCHITECTURE = [
    ("Consumers", "user1 / user2", "Agent-scoped Consumer; distinct authenticated sessions"),
    (
        "Hosted boundary",
        "Hosted endpoint + Session Files",
        "user1 and user2 upload/download through their session authorization",
    ),
    (
        "Application boundary",
        "Trusted ownership checks",
        "Caller + session + conversation + workflow; durable no-replay journal",
    ),
    (
        "Native execution",
        "Backend -> native Responses",
        "Backend credential + trusted call context; model writes Python",
    ),
    ("Managed sandbox", "Code Interpreter", "One bounded execution; input.xlsx -> result.json + chart.png"),
    (
        "Original output delivery",
        "SDK citations -> Hosted HOME",
        "Validate container binding, original bytes, hashes and lengths",
    ),
]
SEQUENCE = [
    ("user1", "Hosted", "Prepare user1 workflow / session / conversation"),
    ("user1", "Hosted", "Upload user1 synthetic input.xlsx"),
    ("Hosted", "Backend", "Verify trusted user1 / session / workflow binding"),
    ("Backend", "Registry", "Seal submitted state before native work"),
    ("Backend", "Native", "Upload original input and submit fixed prompt"),
    ("Native", "Backend", "Completed interpreter call + bound output citations"),
    ("Backend", "Native", "Retrieve original result.json and chart.png"),
    ("Backend", "Hosted", "Stage unchanged output bytes and persist receipts"),
    ("user1", "Hosted", "Download user1 outputs; verify hash / length"),
    ("user2", "Hosted", "Try user1 exact generated-file download"),
    ("Hosted", "user2", "Record actual authorization outcome; denial is expected"),
    ("user1", "Hosted", "Recheck user1 access; repeat mirrored direction for user2"),
]
NATIVE_BYPASS_SEQUENCE = [
    ("Backend", "Harness", "Return sealed lineage for the already completed fresh analysis"),
    ("Harness", "Harness", "Decrypt locally; verify workflow, session and original-byte bindings"),
    ("Harness", "Control", "Use separately approved existing operator credential"),
    ("Control", "Native", "GET exact native resource; verify metadata or original-byte hash"),
    ("Native", "Control", "Record actual positive-control HTTP outcome"),
    ("Harness", "user2", "Select user2 Consumer client and user1 resource reference"),
    ("user2", "Native", "GET user1 native input / container listing / JSON or PNG"),
    ("Native", "user2", "Record actual outcome; authorization denial is expected, not assumed"),
    ("Control", "Native", "Recheck the same resource immediately after the attempt"),
    ("Harness", "Backend", "Recheck user1 authorized Hosted output delivery"),
    ("Harness", "Harness", "Record verdict; stop on failed controls, access or ambiguity"),
    ("Harness", "Harness", "Repeat mirrored probes: user1 targets user2 resources"),
]


def _svg(width: int, height: int, title: str, description: str) -> Element:
    svg = Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "viewBox": f"0 0 {width} {height}",
            "role": "img",
            "aria-labelledby": "title desc",
        },
    )
    SubElement(svg, "title", {"id": "title"}).text = title
    SubElement(svg, "desc", {"id": "desc"}).text = description
    SubElement(svg, "rect", {"width": str(width), "height": str(height), "fill": "#f4f7fb"})
    defs = SubElement(svg, "defs")
    marker = SubElement(
        defs,
        "marker",
        {
            "id": "arrow",
            "viewBox": "0 0 10 10",
            "refX": "9",
            "refY": "5",
            "markerWidth": "6",
            "markerHeight": "6",
            "orient": "auto",
        },
    )
    SubElement(marker, "path", {"d": "M 0 0 L 10 5 L 0 10 z", "fill": "#24558a"})
    return svg


def _text(svg: Element, x: int, y: int, value: str, *, size: int = 16, bold: bool = False) -> None:
    SubElement(
        svg,
        "text",
        {
            "x": str(x),
            "y": str(y),
            "font-family": "Arial, sans-serif",
            "font-size": str(size),
            "font-weight": "bold" if bold else "normal",
            "fill": "#16324f",
        },
    ).text = value


def _line(svg: Element, x1: int, y1: int, x2: int, y2: int) -> None:
    SubElement(
        svg,
        "line",
        {
            "x1": str(x1),
            "y1": str(y1),
            "x2": str(x2),
            "y2": str(y2),
            "stroke": "#24558a",
            "stroke-width": "2",
            "marker-end": "url(#arrow)",
        },
    )


def architecture_svg() -> str:
    """Use fixed reviewed labels, never untrusted report data or external SVG resources."""
    svg = _svg(
        1100,
        850,
        "Consumer CI architecture under test",
        "Intended trust boundaries, not proof of isolation. Check recorded test verdicts.",
    )
    _text(svg, 35, 42, "Architecture under test - not a test verdict", size=24, bold=True)
    for index, (boundary, name, detail) in enumerate(ARCHITECTURE):
        y = 78 + index * 112
        SubElement(
            svg,
            "rect",
            {"x": "35", "y": str(y), "width": "1030", "height": "88", "rx": "10", "fill": "#fff", "stroke": "#7d9cbb"},
        )
        _text(svg, 55, y + 25, boundary, size=14)
        _text(svg, 55, y + 49, name, size=20, bold=True)
        _text(svg, 55, y + 73, detail, size=15)
        if index < len(ARCHITECTURE) - 1:
            _line(svg, 550, y + 90, 550, y + 110)
    _text(svg, 35, 792, "user2 access to user1 files must be denied; user1 positive controls must succeed.", bold=True)
    _text(
        svg,
        35,
        821,
        "Native file/container content isolation and privileged-user isolation are separate, untested claims.",
    )
    return tostring(svg, encoding="unicode")


def sequence_svg() -> str:
    """Draw an accessible static sequence view; expected denial is not rendered as a proven pass."""
    participants = ["user1", "user2", "Hosted", "Backend", "Registry", "Native"]
    xs = {name: 80 + index * 190 for index, name in enumerate(participants)}
    svg = _svg(
        1140,
        1010,
        "Consumer CI request and file delivery sequence under test",
        "user1 upload, backend native execution, original-byte delivery, user2 probes, then repeat mirrored.",
    )
    _text(svg, 25, 35, "Sequence under test - consult actual HTTP and assertion evidence", size=22, bold=True)
    for name, x in xs.items():
        _text(svg, x - 35, 78, name, size=15, bold=True)
        SubElement(
            svg,
            "line",
            {"x1": str(x), "x2": str(x), "y1": "95", "y2": "930", "stroke": "#8c9fb4", "stroke-dasharray": "4 4"},
        )
    for index, (sender, receiver, message) in enumerate(SEQUENCE):
        y = 135 + index * 67
        _line(svg, xs[sender], y, xs[receiver], y)
        _text(svg, 25, y + 26, f"{index + 1:02d}. {message}", size=14)
    _text(
        svg,
        25,
        975,
        "Trusted call context is not an end-user bearer token; no automatic retries or backend fallback.",
        size=15,
    )
    return tostring(svg, encoding="unicode")


def native_bypass_sequence_svg() -> str:
    """Illustrate optional harness design only, never implying the referenced run executed it."""
    participants = ["user2", "Harness", "Backend", "Control", "Native"]
    xs = {name: 80 + index * 235 for index, name in enumerate(participants)}
    svg = _svg(
        1140,
        1010,
        "Optional native bypass sequence - reference design, not execution evidence",
        "A protected harness resolves sealed lineage, brackets user2 requests for user1 resources with privileged "
        "controls, then repeats the mirrored direction. No additional model analysis is performed by these GET probes.",
    )
    _text(svg, 25, 35, "OPTIONAL native bypass - not a recorded run or a required consumer step", size=21, bold=True)
    for name, x in xs.items():
        _text(svg, x - 35, 78, name, size=15, bold=True)
        SubElement(
            svg,
            "line",
            {
                "x1": str(x),
                "x2": str(x),
                "y1": "95",
                "y2": "930",
                "stroke": "#8c9fb4",
                "stroke-dasharray": "4 4",
            },
        )
    for index, (sender, receiver, message) in enumerate(NATIVE_BYPASS_SEQUENCE):
        y = 135 + index * 67
        if sender == receiver:
            x = xs[sender]
            SubElement(
                svg,
                "path",
                {
                    "d": f"M {x} {y} h 32 v 14 h -32",
                    "fill": "none",
                    "stroke": "#24558a",
                    "stroke-width": "2",
                    "marker-end": "url(#arrow)",
                },
            )
        else:
            _line(svg, xs[sender], y, xs[receiver], y)
        _text(svg, 25, y + 34, f"{index + 1:02d}. {message}", size=14)
    _text(svg, 25, 975, "Control = privileged-control; native references and private keys are never printed.", size=15)
    return tostring(svg, encoding="unicode")


def diagram_sources() -> str:
    """Editable JSON sources accompany rendered SVG so no Node/Graphviz/Mermaid renderer is required."""
    return json.dumps(
        {
            "notice": "Architecture under test, not a proof or live topology inventory",
            "architecture": ARCHITECTURE,
            "sequence": SEQUENCE,
            "optional_native_bypass_sequence": NATIVE_BYPASS_SEQUENCE,
        },
        indent=2,
    )
