from __future__ import annotations
import re
from typing import Iterable

_STD_ANT = re.compile(r"^ANT\d+$", re.I)
_NEAR_ANT = re.compile(r"^ANT[_-]\d+$", re.I)
_STD_LGA = re.compile(r"^J\d", re.I)
pref="C"+"ONN"
_NEAR_LGA = re.compile("^"+pref+r"\d", re.I)

def is_standard_ant_port(name):
    return bool(_STD_ANT.match(str(name or "").strip()))

def is_standard_lga_ref(ref):
    return bool(_STD_LGA.match(str(ref or "").strip()))

def _collect(schematic_res):
    nets, refs = set(), set()
    sig = None
    for attr in ("signal_infos_raw", "all_signal_infos_raw", "signal_infos"):
        sig = getattr(schematic_res, attr, None)
        if sig: break
    for si in sig or []:
        if not si: continue
        net = str(si[0] or "").strip()
        if net: nets.add(net)
        for d in si[1] or []:
            ref = str(d or "").strip()
            if ref: refs.add(ref)
    for r in getattr(schematic_res, "part_infos", {}) or {}:
        ref = str(r or "").strip()
        if ref: refs.add(ref)
    return nets, refs

def scan_schematic_naming(schematic_res=None, lga_ref="", *, nets=None, refs=None):
    if schematic_res is not None:
        found_nets, found_refs = _collect(schematic_res)
    else:
        found_nets, found_refs = set(), set()
    if nets is not None: found_nets.update(str(x).strip() for x in nets if x)
    if refs is not None: found_refs.update(str(x).strip() for x in refs if x)
    bad_ants = sorted({x for x in found_nets if _NEAR_ANT.match(x)}, key=str.upper)
    bad_lga = sorted({x for x in found_refs if _NEAR_LGA.match(x)}, key=str.upper)
    cfg = str(lga_ref or "").strip()
    if cfg and (not is_standard_lga_ref(cfg)) and cfg.upper() not in {x.upper() for x in bad_lga}:
        bad_lga.append(cfg)
    msgs = []
    if bad_ants:
        shown = ", ".join(bad_ants[:12])
        msgs.append("天线口须命名为 ANT 加数字（如 ANT1），不能用下划线或横杠。发现："+shown+"。请改原理图后再跑。")
    if bad_lga:
        shown = ", ".join(bad_lga[:12])
        msgs.append("LGA/座子须命名为 J 加数字（如 J3503）。发现："+shown+"。请改原理图后再跑。")
    return msgs

def report_schematic_naming(schematic_res, lga_ref=""):
    msgs = scan_schematic_naming(schematic_res, lga_ref)
    if not msgs: return []
    try:
        from app.LogTool import LogStyle, add_issue
    except Exception:
        LogStyle = None
        add_issue = None
    for i, msg in enumerate(msgs):
        if LogStyle is not None: print(LogStyle.WARNING + msg + LogStyle.ENDC)
        else: print(msg)
        if add_issue is not None: add_issue("WARNING", msg, key="schematic_naming_"+str(i))
    return msgs
