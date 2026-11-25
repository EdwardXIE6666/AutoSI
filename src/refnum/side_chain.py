"""功能5 — JUMP 特例（不恢复 SideChain 全管线）。

1. 路径上接 JUMP 网的位号标黄（is_jump_attach）
2. **JUMP 单独成行**：焊盘上「仅 JUMP 远端」的 RLCT 主链不画 → 合成
   ``Path(band=JUMP(网名))``，手册/预览独立一行；贴靠共享焊盘宿主

侧支 multiport / demote 主链 **不** 在此恢复。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from refnum.graph import (
    build_net_device_maps,
    is_gnd_net,
    is_jump_isolation_net,
    is_rlct,
    signal_infos_of,
)

# Path 仅 duck typing，避免循环 import
Path = Any


def device_touches_jump_net(ref: str, d2n: dict) -> bool:
    """器件是否接在 JUMP 隔离网上。"""
    for n in d2n.get(str(ref), []) or []:
        if is_jump_isolation_net(str(n)):
            return True
    return False


def mark_jump_attach_on_paths(
    paths: list,
    schematic_res,
    signal_infos=None,
) -> set[str]:
    """路径上接 JUMP 网的位号标黄（is_jump_attach）。

    与 Co-pad/Co-lay 同黄；不覆盖已有 is_copad（标签优先）。
    """
    if not paths or schematic_res is None:
        return set()
    sig = (
        getattr(schematic_res, "signal_infos_raw", None)
        or signal_infos_of(schematic_res, signal_infos)
    )
    _n2d, d2n = build_net_device_maps(sig)
    marked: set[str] = set()
    for path in paths:
        for dev in path.devices or []:
            if not dev or not dev.ref:
                continue
            if not device_touches_jump_net(dev.ref, d2n):
                continue
            dev.is_jump_attach = True
            marked.add(str(dev.ref))
    return marked


def mark_jump_attach_on_devices(
    devices: list,
    schematic_res,
    signal_infos=None,
) -> int:
    """给器件列表打 is_jump_attach。"""
    if not devices or schematic_res is None:
        return 0
    sig = (
        getattr(schematic_res, "signal_infos_raw", None)
        or signal_infos_of(schematic_res, signal_infos)
    )
    _n2d, d2n = build_net_device_maps(sig)
    n = 0
    for dev in devices:
        if not dev or not getattr(dev, "ref", None):
            continue
        if device_touches_jump_net(dev.ref, d2n):
            dev.is_jump_attach = True
            n += 1
    return n


def _jump_band_label(jump_net: str) -> str:
    """A 列：JUMP(完整网名)。"""
    n = str(jump_net or "").strip()
    return f"JUMP({n})" if n else "JUMP"


def _device_with_path_flags(ref: str, paths: list, schematic_res):
    """make_device 并合并已有 path 上同 ref 的 COLAY/COPAD/干路标记。"""
    from refnum.path import make_device

    d = make_device(ref, schematic_res)
    rs = str(ref or "").strip()
    for path in paths or []:
        for dev in getattr(path, "devices", None) or []:
            if str(getattr(dev, "ref", "") or "").strip() != rs:
                continue
            if getattr(dev, "is_colay", False):
                d.is_colay = True
                if getattr(dev, "colay_label", ""):
                    d.colay_label = dev.colay_label
            if getattr(dev, "is_colay_trunk", False):
                d.is_colay_trunk = True
            if getattr(dev, "is_copad", False):
                d.is_copad = True
                if getattr(dev, "copad_label", ""):
                    d.copad_label = dev.copad_label
            break
    return d


def _pick_pad_anchor(
    peers: list[str],
    occupied: set[str],
    *,
    schematic_res=None,
    dev_to_nets: Optional[dict] = None,
) -> Optional[str]:
    """选本页已上主链的焊盘**干路**共位件，供 JUMP 行贴宿主。

    公共锚须是焊盘干路/串联件，不得用同盘并位或 JUMP 附着件。
    优先：非 shunt 串联/干路 > 滤波/IC > 位号序。
    """
    from refnum.graph import is_shunt_device

    on = [p for p in peers if p in occupied]
    if not on:
        return None
    d2n = dev_to_nets or {}

    def _key(r: str) -> tuple:
        ru = str(r).upper()
        shunt = False
        if schematic_res is not None and d2n:
            try:
                shunt = bool(is_shunt_device(r, schematic_res, d2n))
            except Exception:
                shunt = False
        # shunt 垫底：并位不是 JUMP/主链公共干路
        if ru.startswith(("FIL", "LPF", "BPF", "DIP", "DUP")):
            tier = 0
        elif ru.startswith("U"):
            tier = 1
        elif not shunt:
            tier = 2  # 串联 RLCT / 干路 C
        else:
            tier = 5  # 并位
        return (tier, ru)

    return sorted(on, key=_key)[0]


def synthesize_jump_stub_paths(
    paths: list,
    schematic_res,
    signal_infos=None,
) -> list:
    """JUMP 单独成行（特例，非 SideChain 管线）。

    规则（与 copad P2b 一致）：
      某焊盘网上的 RLCT，非焊盘侧其它网**全部**为 JUMP → 不得当主链串联腿；
      若尚未出现在该 ANT 页任何 path 上 → 合成
      ``Path(band=JUMP(网名), devices=[焊盘锚?, stub…])``。

    焊盘锚仅用于行贴靠宿主，黄标只打在 JUMP 附着 stub 上。
    """
    if not paths or schematic_res is None:
        return []
    from refnum.copad import is_jump_only_far_rlct
    from refnum.path import Path as PathCls
    from refnum.path import make_device

    sig = (
        getattr(schematic_res, "signal_infos_raw", None)
        or signal_infos_of(schematic_res, signal_infos)
    )
    n2d, d2n = build_net_device_maps(sig)

    # ant → 已上图位号；ant → 触及的非 JUMP 焊盘网
    occupied: dict[str, set[str]] = defaultdict(set)
    pad_nets: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        kind = str(getattr(path, "kind", "BAND") or "BAND").upper()
        if kind in ("TRX_PA_LNA", "GPS"):
            continue
        ant = str(getattr(path, "ant_num", "") or "").strip()
        if not ant:
            continue
        # 已是 JUMP 行则跳过（防重复合成）
        band = str(getattr(path, "band", "") or "")
        if band.upper().startswith("JUMP(") or band.upper() == "JUMP":
            for dev in path.devices or []:
                if getattr(dev, "ref", None):
                    occupied[ant].add(str(dev.ref))
            continue
        for dev in path.devices or []:
            ref = str(getattr(dev, "ref", "") or "").strip()
            if not ref:
                continue
            occupied[ant].add(ref)
            for n in d2n.get(ref, []) or []:
                ns = str(n)
                if not ns or is_gnd_net(ns) or is_jump_isolation_net(ns):
                    continue
                pad_nets[ant].add(ns)

    # (ant, jump_net, pad_net) → stub refs
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for ant, nets in pad_nets.items():
        occ = occupied.get(ant) or set()
        for pad_net in nets:
            for ref in n2d.get(pad_net, []) or []:
                rs = str(ref)
                if not rs or rs in occ:
                    continue
                if not is_rlct(rs, schematic_res):
                    continue
                if not is_jump_only_far_rlct(rs, pad_net, d2n):
                    continue
                jump_nets = [
                    str(n)
                    for n in (d2n.get(rs) or [])
                    if n and not is_gnd_net(n) and is_jump_isolation_net(str(n))
                ]
                if not jump_nets:
                    continue
                # 稳定：多 JUMP 网取排序后第一个作标签（通常一网）
                jnet = sorted(jump_nets)[0]
                groups[(ant, jnet, pad_net)].add(rs)

    out: list = []
    for (ant, jnet, pad_net), stubs in sorted(
        groups.items(), key=lambda x: (x[0][0], x[0][1], x[0][2]),
    ):
        if not stubs:
            continue
        occ = occupied.get(ant) or set()
        peers = [str(p) for p in (n2d.get(pad_net) or []) if p]
        # 不含 JUMP stub 自身，从主链共位里挑干路锚
        peers_no_stub = [p for p in peers if p not in stubs]
        anchor = _pick_pad_anchor(
            peers_no_stub,
            occ,
            schematic_res=schematic_res,
            dev_to_nets=d2n,
        )
        devices = []
        if anchor:
            # 继承主链上的 COLAY/COPAD 标记，否则 is_colay 公共 RLC 竖并特例失效
            devices.append(
                _device_with_path_flags(anchor, paths, schematic_res)
            )
        for rs in sorted(stubs):
            d = make_device(rs, schematic_res)
            d.is_jump_attach = True
            devices.append(d)
        if not devices:
            continue
        out.append(
            PathCls(
                ant_num=ant,
                devices=devices,
                band=_jump_band_label(jnet),
                direction="RX",
                kind="BAND",
            )
        )
        # 同 ant 后续组勿再抢已输出 stub
        occupied[ant].update(stubs)

    return out
