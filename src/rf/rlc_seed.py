# -*- coding: utf-8 -*-
"""原理图 R/L/C 初值：给 ADS 匹配件默认使能和数值。"""
from __future__ import annotations

import re

_UNIT = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(pF|nF|uF|μF|nH|uH|μH|pH|kOhm|ohm|Ω|K|R|k)\b",
    re.I,
)
# L_RF_47N_ / C_M_4P7_ / L_RF_8N2_
_COMPACT = re.compile(r"(?:^|_)(\d+)([NP])(\d*)(?:_|$)", re.I)


def _from_unit_text(text: str) -> tuple[str | None, str | None]:
    raw = str(text or "").lstrip("@").strip()
    if not raw or raw.upper() in ("NC", "DNP", "NOSTUFF"):
        return None, None
    m = _UNIT.search(raw)
    if not m:
        return None, None
    num, unit = m.group(1), m.group(2)
    u = unit.replace("μ", "u").replace("Ω", "ohm").upper()
    if u == "PF":
        return "C", f"{num} pF"
    if u == "NF":
        return "C", f"{num} nF"
    if u == "UF":
        return "C", f"{num} uF"
    if u == "NH":
        return "L", f"{num} nH"
    if u == "UH":
        return "L", f"{num} uH"
    if u == "PH":
        return "L", f"{num} pH"
    if u in ("K", "KOHM"):
        return "R", f"{num} kOhm"
    if u in ("R", "OHM"):
        return "R", f"{num} Ohm"
    return None, None


def _from_compact(text: str) -> tuple[str | None, str | None]:
    m = _COMPACT.search(str(text or ""))
    if not m:
        return None, None
    a, mark, b = m.group(1), m.group(2).upper(), m.group(3)
    num = f"{a}.{b}" if b else a
    if mark == "N":
        return "L", f"{num} nH"
    return "C", f"{num} pF"


_MATCHING_REF = re.compile(r"^[CLR]\d", re.I)
_BARE_UNSTUFFED = {"NC", "DNP", "NOSTUFF", "@"}


def schematic_handbook_value(text: str) -> str | None:
    """原理图 Value 栏原样。空的不贴。只有 @ / NC 时写成 NC。"""
    raw = str(text or "").strip().strip('"').strip()
    if not raw:
        return None
    if raw.upper() in _BARE_UNSTUFFED:
        return "NC"
    return raw


def schematic_handbook_values(part_value) -> dict[str, str]:
    """位号 → 原理图原值。只收 R/L/C，不给没写值的位号编一个数。"""
    out: dict[str, str] = {}
    for ref, text in (part_value or {}).items():
        name = str(ref or "").strip()
        if name.upper().startswith("LPF") or not _MATCHING_REF.match(name):
            continue
        shown = schematic_handbook_value(text)
        if shown:
            out[name] = shown
    return out


def schematic_rlc_seed(ref: str, *texts: str) -> tuple[str | None, str | None]:
    """(种类 C/L/R, ADS 参数字)。先读数值单位，再读料号里的 47N/4P7，最后看位号。"""
    for t in texts:
        kind, tok = _from_unit_text(t)
        if kind:
            return kind, tok
    for t in texts:
        kind, tok = _from_compact(t)
        if kind:
            return kind, tok
    ru = str(ref or "").upper()
    if len(ru) >= 2 and ru[1].isdigit():
        if ru[0] == "C":
            return "C", "999 pF"
        if ru[0] == "L":
            return "L", "999 nH"
        if ru[0] == "R":
            return "R", "0"
    return None, None
