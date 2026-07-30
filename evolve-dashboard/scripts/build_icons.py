#!/usr/bin/env python3
"""Build the offline browser icon bundle from @phosphor-icons/core.

Usage:
    python3 scripts/build_icons.py /path/to/@phosphor-icons/core
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from counter_icons import COUNTER_ICONS  # noqa: E402

UI_ICONS = {
    "arrow-counter-clockwise",
    "arrow-left",
    "backspace",
    "barbell",
    "bell",
    "caret-right",
    "check-circle",
    "check-square",
    "circle",
    "flame",
    "gear-six",
    "heartbeat",
    "house",
    "house-line",
    "pencil-simple",
    "pencil-simple-line",
    "plugs-connected",
    "plus",
    "plus-circle",
    "push-pin",
    "scales",
    "sparkle",
    "square",
    "squares-four",
    "trash-simple",
    "x",
}

FILL_ICONS = {
    "barbell",
    "check-circle",
    "check-square",
    "flame",
    "gear-six",
    "house",
    "plugs-connected",
    "push-pin",
    "scales",
    "sparkle",
    "squares-four",
}


def inner_svg(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"<svg\b[^>]*>(.*)</svg>", raw)
    if not match:
        raise ValueError(f"Cannot parse {path}")
    return match.group(1)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Pass the @phosphor-icons/core package directory")
    package = Path(sys.argv[1]).resolve()
    assets = package / "assets"
    icons: dict[str, str] = {}

    for name in sorted(UI_ICONS | set(COUNTER_ICONS)):
        icons[name] = inner_svg(assets / "regular" / f"{name}.svg")
    for name in sorted(FILL_ICONS):
        icons[f"f-{name}"] = inner_svg(assets / "fill" / f"{name}-fill.svg")

    lines = [
        "// Phosphor Icons 2.1.1 (MIT) — generated and vendored for offline use.",
        "// Run scripts/build_icons.py against @phosphor-icons/core to rebuild.",
        "export default {",
    ]
    lines.extend(
        f"{json.dumps(name)}: {json.dumps(body, separators=(',', ':'))},"
        for name, body in sorted(icons.items())
    )
    lines.append("};")
    (ROOT / "app" / "www" / "icons.js").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(icons)} icons")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
