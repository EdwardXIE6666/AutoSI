"""功能5 阶段 B — 共享 / 禁合并谓词（纯判定，无行序/投放/写表）。

硬规则（boss）：
1. 共享 = 显示序（左芯片→右）上，该器件**紧邻左邻**位号种类 ≥ 2
2. 非共享 = 左邻只有 1 种 → 全表只占 1 格
3. **例外·行首前端**：左邻恒为空时，改用**紧邻右邻**种类 ≥ 2 判共享
   （同一 LNA/PA 扇出多路；否则行首前端会被当非共享掐成一行，支路从被动件起头）
4. **Co-pad 两腿归一**：合并/分叉两腿对宿主视为同一前驱（夹心两行=一条链）。
   夹心支路几何不是 Route，不注入前驱种类。
5. RLCT / ANT 永不作 L3 对齐锚点 / L6 竖合并目标

被 rows / place / merge / unique 共用；改规则只动本文件。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Optional

from refnum.path import Device, Path, path_display_devices

_RLCT_RE = re.compile(r"^[RLCT]\d", re.I)

# 共享阈值：互异左邻至少这么多种
SHARED_MIN_LEFTS = 2
# 行首无左邻的哨兵（始终最左 → 只计 1 种）
_LEFT_NONE = ""


def is_rlct_ref(ref: str, device: Optional[Device] = None) -> bool:
    """R/L/C/T 被动件（类别语义，非项目位号名单）。"""
    if device is not None and getattr(device, "is_passive", False):
        return True
    return bool(_RLCT_RE.match(str(ref or "").strip()))


def is_ant_ref(ref: str, device: Optional[Device] = None) -> bool:
    """天线位号。"""
    if device is not None and getattr(device, "is_ant", False):
        return True
    return str(ref or "").strip().upper().startswith("ANT")


def is_non_merge_align_ref(ref: str, device: Optional[Device] = None) -> bool:
    """禁止 L3 对齐锚点 / L6 竖合并：默认 RLCT + ANT。

    例外（须与 ``is_layout_shared`` 多 path 约束配合）：
      - COLAY 焊盘第三端干路（``is_colay_trunk``）
      - COLAY 公共腿 RLC（``is_colay``：同时在 COLAY 主链与 JUMP 行）
    二者都是多支路/多行公共串联节点，须同列竖并。
    """
    if device is not None and (
        getattr(device, "is_colay_trunk", False)
        or getattr(device, "is_colay", False)
    ):
        return False
    return is_rlct_ref(ref, device) or is_ant_ref(ref, device)


def device_from_plan(plan, ref: str) -> Optional[Device]:
    """从 SheetPlan 行内取 ref 的 Device（带 is_colay_trunk 等标记）。"""
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return None
    for row in getattr(plan, "rows", None) or []:
        for d in getattr(row, "devices", None) or []:
            if str(getattr(d, "ref", "") or "").strip() == rs:
                return d
    return None


def display_ref_seq(path: Path) -> list[str]:
    """显示序位号（左 TRX → 右），行内去重保序。左邻判定与投放均用此序。"""
    refs: list[str] = []
    seen: set[str] = set()
    for d in path_display_devices(path):
        ref = str(getattr(d, "ref", "") or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def display_neighbor(row, ref: str, *, side: str = "left") -> Optional[str]:
    """显示序（左芯片→右）上 ref 的紧邻左/右邻。"""
    path = getattr(row, "path", None) if row is not None else None
    if not path:
        return None
    seq = display_ref_seq(path)
    if ref not in seq:
        return None
    i = seq.index(ref)
    if side == "right":
        return seq[i + 1] if i + 1 < len(seq) else None
    return seq[i - 1] if i > 0 else None


def _copad_leg_groups(
    copad_structures: Optional[list[Any]],
) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """Co-pad 分叉/合并对腿 → 组 id；组 id → 汇合宿主器件。

    领域（boss）：夹心视觉两行 = **一条链路**，两腿不得把宿主算成
    「左邻种类≥2」的合并器件。

    - fork 两腿 → 同一组，宿主 = ``fork.trunk_ref``
    - merge 两腿 → 同一组，宿主 = ``merge.trunk_ref`` + ``trunk_after``
    """
    leg_to_group: dict[str, str] = {}
    group_hosts: dict[str, set[str]] = defaultdict(set)

    def _add_pair(prefix: str, pair: Any, extra_hosts: tuple[str, ...] = ()) -> None:
        if pair is None:
            return
        la = str(getattr(pair, "leg_a", "") or "").strip()
        lb = str(getattr(pair, "leg_b", "") or "").strip()
        if not la or not lb:
            return
        a, b = (la, lb) if la <= lb else (lb, la)
        gid = f"{prefix}:{a}|{b}"
        leg_to_group[la] = gid
        leg_to_group[lb] = gid
        trunk = str(getattr(pair, "trunk_ref", "") or "").strip()
        if trunk:
            group_hosts[gid].add(trunk)
        for t in extra_hosts:
            tr = str(t or "").strip()
            if tr:
                group_hosts[gid].add(tr)

    for st in copad_structures or []:
        _add_pair("COPAD_FORK", getattr(st, "fork", None))
        trunk_after = tuple(getattr(st, "trunk_after", None) or ())
        _add_pair("COPAD_MERGE", getattr(st, "merge", None), trunk_after)
    return leg_to_group, {g: frozenset(hs) for g, hs in group_hosts.items()}


# 旧名
_copad_merge_leg_groups = _copad_leg_groups


def collect_left_neighbors(
    paths: list[Path],
    copad_structures: Optional[list[Any]] = None,
) -> dict[str, frozenset[str]]:
    """ref → 各 Path（Route）上出现过的紧邻前驱集合。

    例：FIL 前驱互异 → 汇合；U 前驱单一 → 单链。
    Co-pad：分叉/合并两腿对汇合宿主归一为同一逻辑前驱（夹心两行=一条链）。
    夹心支路是显示，不扫 branch 邻接；Route 前驱只来自阶段 A 的 Path。
    """
    leg_to_group, group_hosts = _copad_leg_groups(copad_structures)
    ref_to_lefts: dict[str, set[str]] = defaultdict(set)

    def _add_left(ref: str, left: str) -> None:
        gid = leg_to_group.get(left)
        if gid and ref in group_hosts.get(gid, frozenset()):
            left = gid
        ref_to_lefts[ref].add(left)

    for path in paths or []:
        seq = display_ref_seq(path)
        for i, ref in enumerate(seq):
            _add_left(ref, seq[i - 1] if i > 0 else _LEFT_NONE)
    return {ref: frozenset(lefts) for ref, lefts in ref_to_lefts.items()}


def is_pa_lna_device(d: Device) -> bool:
    """PA/LNA/LPAMiF/QPM 有源前端（不含 TRX）。"""
    if getattr(d, "is_pa_lna", False):
        return True
    if getattr(d, "is_sdr", False):
        return False
    pt = (getattr(d, "part_type", None) or "").upper()
    return any(
        k in pt
        for k in ("RF LNA", "RF PA", "LPAMI", "QPM", "LNA", " PA")
    ) or pt in {"RF LNA", "RF PA", "LPAMIF"}


def is_switch_or_coupler_device(d: Optional[Device]) -> bool:
    """第一层开关 / 耦合器（柱顶柱底按真连接钉）。不含 PA/LNA。"""
    if d is None or is_pa_lna_device(d):
        return False
    pt = (getattr(d, "part_type", None) or "").upper()
    return "SWITCH" in pt or "COUPLER" in pt


def is_frontend_structure_role(device: Optional[Device]) -> bool:
    """前端器件的**类别**判定（双重保险之一；禁止位号前缀）。

    可并射频结构件：IC / 滤波 / Coupler / 开关 / 分频等。
    排除：PA/LNA、ANT、RLCT 被动、纯 is_passive。
    """
    if device is None:
        return False
    ref = str(getattr(device, "ref", "") or "").strip()
    if is_pa_lna_device(device):
        return False
    if is_ant_ref(ref, device):
        return False
    if is_rlct_ref(ref, device) or getattr(device, "is_passive", False):
        return False
    if getattr(device, "is_ic", False) or getattr(device, "is_lpf", False):
        return True
    # SDR/TRX 不在 PA–ANT 段当「前端合并柱」主目标
    if getattr(device, "is_sdr", False):
        return False
    pt = (getattr(device, "part_type", None) or "").upper()
    if not pt or pt in {"UNKNOWN", "UNKNOWN DEVICE", "PARTNUMBER"}:
        return False
    return any(
        k in pt
        for k in (
            "COUPLER",
            "SWITCH",
            "SAW",
            "FBAR",
            "FILTER",
            "DIVIDER",
            "DIPLEX",
            "DUPLEX",
            "MULTIPLEX",
            "FREQUENCY DIVIDER",
            "ELECTRONIC SWITCH",
        )
    )


_PA_ANT_OPEN_CACHE: dict[int, tuple[int, int] | None] = {}
_FE_MERGE_REFS_CACHE: dict[int, frozenset[str]] = {}


def _pa_ant_open_interval(path: Path) -> tuple[int, int] | None:
    """显示序 last(PA/LNA) 与其后第一只 ANT 的开区间端点。按 path 身份缓存。"""
    key = id(path)
    if key in _PA_ANT_OPEN_CACHE:
        return _PA_ANT_OPEN_CACHE[key]
    devs = list(path_display_devices(path))
    if not devs:
        _PA_ANT_OPEN_CACHE[key] = None
        return None
    pa_idxs = [i for i, d in enumerate(devs) if is_pa_lna_device(d)]
    ant_idxs = [
        i for i, d in enumerate(devs)
        if is_ant_ref(str(getattr(d, "ref", "") or ""), d)
    ]
    if not pa_idxs or not ant_idxs:
        _PA_ANT_OPEN_CACHE[key] = None
        return None
    lo = max(pa_idxs)
    ant_after = [i for i in ant_idxs if i > lo]
    if not ant_after:
        _PA_ANT_OPEN_CACHE[key] = None
        return None
    interval = (lo, min(ant_after))
    _PA_ANT_OPEN_CACHE[key] = interval
    return interval


def is_on_pa_lna_to_ant_segment(
    device: Optional[Device],
    path: Optional[Path],
) -> bool:
    """前端器件的**拓扑**判定：显示序落在 last(PA/LNA) 与 ANT 开区间内。

    显示序：左 TRX → … → PA/LNA → (本区间) → ANT 右。
    """
    if device is None or path is None:
        return False
    ref = str(getattr(device, "ref", "") or "").strip()
    if not ref:
        return False
    interval = _pa_ant_open_interval(path)
    if interval is None:
        return False
    lo, hi = interval
    devs = list(path_display_devices(path))
    for i in range(lo + 1, hi):
        d = devs[i]
        if str(getattr(d, "ref", "") or "").strip() == ref:
            return True
    return False


def frontend_merge_refs_on_path(path: Optional[Path]) -> frozenset[str]:
    """path 上可并的前端器件位号（**双重判定**）。

    必须同时满足：
      1) **拓扑**：显示序 LNA/PA 与 ANT 之间（``is_on_pa_lna_to_ant_segment``）
      2) **类别**：射频结构件角色（``is_frontend_structure_role``），非 RLCT

    禁止用位号前缀（U/FIL…）单独认定。
    """
    if path is None:
        return frozenset()
    key = id(path)
    hit = _FE_MERGE_REFS_CACHE.get(key)
    if hit is not None:
        return hit
    interval = _pa_ant_open_interval(path)
    if interval is None:
        empty = frozenset()
        _FE_MERGE_REFS_CACHE[key] = empty
        return empty
    lo, hi = interval
    devs = list(path_display_devices(path))
    out: set[str] = set()
    for d in devs[lo + 1:hi]:
        ref = str(getattr(d, "ref", "") or "").strip()
        if not ref:
            continue
        if not is_frontend_structure_role(d):
            continue
        out.add(ref)
    frozen = frozenset(out)
    _FE_MERGE_REFS_CACHE[key] = frozen
    return frozen


def is_frontend_merge_device(
    device: Optional[Device],
    path: Optional[Path] = None,
) -> bool:
    """是否前端合并器件：拓扑区间 ∩ 结构角色（见 frontend_merge_refs_on_path）。

    path 缺失时无法做拓扑判定 → False。
    """
    if device is None or path is None:
        return False
    ref = str(getattr(device, "ref", "") or "").strip()
    if not ref:
        return False
    return (
        is_on_pa_lna_to_ant_segment(device, path)
        and is_frontend_structure_role(device)
    )


def _is_transceiver_device(d: Device) -> bool:
    """Transceiver / SDR 行首。"""
    if getattr(d, "is_sdr", False):
        return True
    pt = (getattr(d, "part_type", None) or "").upper()
    return "TRANSCEIVER" in pt


def _is_path_start_right_share_head(path: Path, head: Device) -> bool:
    """行首用右邻判共享的条件。

    - BAND 等：行首 PA/LNA（多路扇出）
    - GPS：行首 Transceiver（L1/L5 等同 TRX 扇出多 LNA；须竖合并，否则支路丢 TRX）
    - TRX_PA_LNA：仍不含 TRX 行首（该页 TRX 各接不同 PA，勿误合并掐支路）
    """
    if is_pa_lna_device(head):
        return True
    kind = (getattr(path, "kind", None) or "").upper()
    if kind == "GPS" and _is_transceiver_device(head):
        return True
    return False


def collect_path_start_pa_lna_right_neighbors(
    paths: list[Path],
) -> dict[str, frozenset[str]]:
    """统计「行首可右邻共享」器件的紧邻右邻集合。

    行首左邻恒为空，左邻种类不能反映多路分叉；
    用互异右邻种类判断共享（BAND 的 LNA/PA；GPS 的 Transceiver）。
    """
    ref_to_rights: dict[str, set[str]] = defaultdict(set)
    for path in paths or []:
        devs = list(path_display_devices(path))
        if not devs:
            continue
        head = devs[0]
        if not _is_path_start_right_share_head(path, head):
            continue
        href = str(getattr(head, "ref", "") or "").strip()
        if not href:
            continue
        right = ""
        if len(devs) > 1:
            right = str(getattr(devs[1], "ref", "") or "").strip()
        ref_to_rights[href].add(right)
    return {ref: frozenset(rs) for ref, rs in ref_to_rights.items()}


def count_left_neighbors(
    paths: list[Path],
    copad_structures: Optional[list[Any]] = None,
) -> dict[str, int]:
    """ref → 共享度（用于 is_shared_device）。

    - 一般器件：互异**左邻**个数（Co-pad 合并对已归一）
    - **行首 PA/LNA 或 GPS 行首 TRX**（左邻只有哨兵）：改用互异**右邻**个数
      （多路从同一前端/TRX 分出 → 共享，须竖合并；禁止 non_shared 掐成单行）

    FBRX 取样链左邻**计入**共享度（Coupler 可与主链竖并）。
    跨 FBRX/主链「不同左邻」的巨型并柱由 L6 **按左邻分组**收口，不在此掐共享。
    """
    lefts_map = collect_left_neighbors(paths, copad_structures=copad_structures)
    fe_rights = collect_path_start_pa_lna_right_neighbors(paths)
    out: dict[str, int] = {}
    for ref, lefts in lefts_map.items():
        if ref in fe_rights and (
            lefts == {_LEFT_NONE} or lefts == frozenset({_LEFT_NONE})
        ):
            rights = fe_rights.get(ref, frozenset())
            out[ref] = len(rights) if rights else 1
        else:
            out[ref] = len(lefts)
    return out


def is_shared_device(
    ref: str,
    left_neighbor_count: dict[str, int],
    *,
    min_lefts: int = SHARED_MIN_LEFTS,
) -> bool:
    """共享 ⇔ 共享度 ≥ min_lefts（行首用右邻度，其余用左邻度）。"""
    return int(left_neighbor_count.get(ref, 0) or 0) >= min_lefts


def collect_owner_adjacents(
    plan,
    ref: str,
) -> tuple[frozenset[str], frozenset[str]]:
    """owner 行上 ref 的显示序紧邻左/右集合。"""
    rs = str(ref or "").strip()
    lefts: set[str] = set()
    rights: set[str] = set()
    if not rs or plan is None:
        return frozenset(), frozenset()
    owners = set((getattr(plan, "share_map", None) or {}).get(rs) or ())
    for row in getattr(plan, "rows", None) or []:
        if owners and row.row_id not in owners:
            continue
        seq = display_ref_seq(row.path)
        if rs not in seq:
            continue
        i = seq.index(rs)
        lefts.add(seq[i - 1] if i > 0 else _LEFT_NONE)
        rights.add(seq[i + 1] if i + 1 < len(seq) else _LEFT_NONE)
    return frozenset(lefts), frozenset(rights)


def is_layout_shared(
    plan,
    ref: str,
    left_neighbor_count: dict[str, int],
    *,
    min_lefts: int = SHARED_MIN_LEFTS,
) -> bool:
    """构图用共享（§5.0.2 邻接多样性 + COLAY；**禁止 multiport 后门**）。

    判定顺序：
      1. ``is_shared_device``：左邻种类≥2（行首 PA/LNA/GPS-TRX 改用右邻）
      2. COLAY 公共 RLC（``is_colay`` / ``is_colay_trunk``）
      3. 多 path 且**右邻**种类≥2（扇出分叉；与左邻对等，不绑器件前缀）
      4. 其余（含「仅 owners≥2」的纯串联干路）→ **非共享**

    路径层多端口（自由口/Co-pad）与本谓词无关；见 ``is_multiport_anchor``。
    非共享 → 全表只占 1 格；共享 → 可同列 + 竖并。
    """
    rs = str(ref or "").strip()
    if not rs:
        return False
    # 1) 左邻 / 行首右邻多样性
    if is_shared_device(rs, left_neighbor_count, min_lefts=min_lefts):
        return True
    owners = ()
    if plan is not None:
        owners = (getattr(plan, "share_map", None) or {}).get(rs) or ()
    if len(owners) < 2:
        return False
    dev = device_from_plan(plan, rs)
    # 2) COLAY 公共 RLC：只解除「RLCT 默认单格」，**不豁免邻接多样性**。
    #    真 COLAY 公共腿 / 第三端干路上不同变体各走一腿，左右邻天然多样，
    #    仍会在步骤 1 或 3 通过；而左右邻恒为同一件的那只只是公共干上的串联
    #    匹配，不是汇合点，不得因带 COLAY 标记就无条件成柱。
    colay_rlct = (
        dev is not None
        and (
            getattr(dev, "is_colay_trunk", False)
            or getattr(dev, "is_colay", False)
        )
        and is_rlct_ref(rs, dev)
    )
    # RLCT / ANT 永不共享竖并
    if not colay_rlct and is_non_merge_align_ref(rs, dev):
        return False
    # 3) 右邻多样性（拓扑扇出；路径终点空右邻不算一种扇出）
    _lefts, rights = collect_owner_adjacents(plan, rs)
    rights = frozenset(r for r in rights if r and r != _LEFT_NONE)
    return len(rights) >= min_lefts


def should_vertical_merge(
    plan,
    ref: str,
    left_neighbor_count: dict[str, int] | None = None,
    *,
    min_lefts: int = SHARED_MIN_LEFTS,
) -> bool:
    """**唯一**「合并器件 / 可竖并 / 可当汇合点」判定（ANT 与 GPS 同一套）。

    聪明规则（boss：一条真实链路不并，禁止多谓词分叉）：
      多条**真实链路**汇入 ⇔ 邻接多样性 ``is_layout_shared``
      （左邻种类≥2 / 行首右邻扇出 / COLAY 公共点）
      且 share_map owners≥2、非 RLCT/ANT。

    RLCT 默认单格。COLAY 公共腿/干路（``is_colay`` / ``is_colay_trunk``）才并。
    邻接多样性只给前端 IC：公尾 T/C 因 COLAY 短链左邻不同，不得因此变成合并器件。

    不是合并器件：
      path 把公共干抄进多频段、但左邻恒同一串
      （C→R→C→LPF→U 类）→ 全表单格、不竖并。

    ``left_neighbor_count`` 可省略：从 ``plan.left_neighbor_count`` 取。
    **跨度高度**不在此：merge 侧树全员 + 合并列无异物可过。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return False
    if is_non_merge_align_ref(rs, device_from_plan(plan, rs)):
        return False
    owners = (getattr(plan, "share_map", None) or {}).get(rs) or ()
    if len(owners) < 2:
        return False
    lnc = left_neighbor_count
    if lnc is None:
        lnc = getattr(plan, "left_neighbor_count", None) or {}
    return is_layout_shared(
        plan, rs, lnc, min_lefts=min_lefts,
    )
