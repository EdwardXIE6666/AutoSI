"""Parse, validate, and group the net-info CSV: net,component,start_freq,end_freq."""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from rf.segments import is_fbrx_siwave_name, safe_component_name

_FREQ_RE = re.compile(r"^\d+(\.\d+)?\s*(Hz|kHz|MHz|GHz|THz)$", re.IGNORECASE)


class NetlistError(ValueError):
    """Raised when the net-info file is malformed."""


@dataclass(frozen=True)
class NetInfo:
    name: str
    component: str
    start_freq: str
    end_freq: str


def _validate_freq(value: str, line_no: int) -> None:
    if not _FREQ_RE.match(value):
        raise NetlistError(
            f"line {line_no}: invalid frequency {value!r} "
            f"(expected e.g. '1GHz', '100MHz')"
        )


def parse_netlist(text: str) -> list[NetInfo]:
    nets: list[NetInfo] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            raise NetlistError(
                f"line {line_no}: expected 4 fields "
                f"(net,component,start,end), got {len(parts)}: {line!r}"
            )
        name, component, start, end = parts
        if not name or not component:
            raise NetlistError(f"line {line_no}: empty net or component name")
        _validate_freq(start, line_no)
        _validate_freq(end, line_no)
        if is_fbrx_siwave_name(name) or is_fbrx_siwave_name(component):
            continue
        nets.append(NetInfo(name, safe_component_name(component), start, end))
    if not nets:
        raise NetlistError("net-info file contains no valid rows")
    return nets


def group_by_component(nets: list[NetInfo]) -> "OrderedDict[str, list[NetInfo]]":
    grouped: "OrderedDict[str, list[NetInfo]]" = OrderedDict()
    for net in nets:
        grouped.setdefault(net.component, []).append(net)
    return grouped


def decode_text(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("gbk", errors="replace")


def skip_series_nc_path(nets_path) -> str:
    p = Path(nets_path)
    name = p.name.replace(
        "Network_Frequency_Band_Information_for_SIwave.txt",
        "skip_series_nc.txt",
    )
    if name == p.name:
        name = p.stem + "+skip_series_nc.txt"
    return str(p.with_name(name))


def load_skip_series_nc(nets_path) -> frozenset:
    """功能1 写在网络表旁边的串联不贴名单。没有文件就当没有。"""
    path = Path(skip_series_nc_path(nets_path))
    if not path.is_file():
        alt = path.parent / "skip_series_nc.txt"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return frozenset()
    refs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            refs.append(s)
    return frozenset(refs)


def is_schematic_text(path: str | Path) -> bool:
    try:
        text = decode_text(path)
    except OSError:
        return False
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first.upper().startswith("*PADS-LOGIC")


def is_netlist_text(path: str | Path) -> bool:
    if is_schematic_text(path):
        return False
    try:
        parse_netlist(decode_text(path))
    except (OSError, NetlistError):
        return False
    return True


def read_netlist(path: str | Path) -> "OrderedDict[str, list[NetInfo]]":
    text = decode_text(path)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first.upper().startswith("*PADS-LOGIC"):
        raise NetlistError(
            f"{Path(path).name} 是原理图，不是网络频段表。"
            "请先运行功能1，再选择它生成的网络表。"
        )
    return group_by_component(parse_netlist(text))
