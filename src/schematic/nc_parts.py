# -*- coding: utf-8 -*-
"""串联不贴件：ADS 不构图，PCB 抽取也不在这些焊盘上建口。"""


def series_nc_refdes(schematic) -> list[str]:
    """part_active 为假、且不是对地并联的位号。"""
    gnd = set()
    gnd_inf = getattr(schematic, "gnd_inf", None)
    if gnd_inf and len(gnd_inf) > 2:
        gnd = set(gnd_inf[2] or ())
    out = []
    for ref, on in (getattr(schematic, "part_active", None) or {}).items():
        if on is False and ref and ref not in gnd:
            out.append(str(ref))
    return sorted(out)


def skip_series_nc_path(nets_path) -> str:
    from siwave_extractor.netlist import skip_series_nc_path as _path
    return _path(nets_path)


def write_skip_series_nc(nets_path, schematic) -> str:
    from pathlib import Path
    from siwave_extractor.netlist import skip_series_nc_path as _path
    path = _path(nets_path)
    refs = series_nc_refdes(schematic)
    Path(path).write_text("\n".join(refs) + ("\n" if refs else ""), encoding="utf-8")
    return path


def load_skip_series_nc(nets_path) -> frozenset:
    from siwave_extractor.netlist import load_skip_series_nc as _load
    return _load(nets_path)


def port_refdes(port_name: str) -> str:
    """N77_TRX0_ANT3_C1808_1 → C1808。"""
    parts = str(port_name or "").split("_")
    return parts[-2] if len(parts) >= 2 else ""
