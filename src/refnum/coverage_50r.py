"""功能5 子能力：50R 器件 vs 手册覆盖审计。

**不是路径搭建。** 路径搭建见 `refnum_path.py`。

职责：
1. 从 TXT NET_CLASS 50R 收集器件（**含并位/不贴**，不按 part_active 剔除——仍进清单）
2. 「存在」**仅当手册格子上真有该位号**（写表后的 device/copad/side 格；
   禁止仅凭 path/结构表推断「画过」）
3. 分层标注未覆盖原因
4. 写入独立「50R覆盖」Sheet

由预览导出编排层调用；禁止在 build_paths 内耦合本模块。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from openpyxl.styles import Alignment, Font, PatternFill

# 拓扑工具仅复用，不反向依赖本模块的导出编排
from refnum.graph import (  # noqa: E402
    build_net_device_maps,
    is_connector_ref,
    is_gnd_net,
    is_rlct,
    is_shunt_device,
    nongnd_degree,
    signal_infos_of,
)
from refnum.xlsx_style import (
    style_cell as _style_cell,
    write_blue_title as _write_blue_title,
)

# --- 本模块专用着色（蓝标题见 refnum_xlsx_style；不污染路径预览器件色板）---
_FILL_BAND = PatternFill(fill_type="solid", fgColor="FFD6DCE4")
_FILL_OK = PatternFill(fill_type="solid", fgColor="FFC6EFCE")
_FILL_MISS = PatternFill(fill_type="solid", fgColor="FFFFC7CE")
_FILL_WARN = PatternFill(fill_type="solid", fgColor="FFFFEB9C")
_FILL_NEAR = PatternFill(fill_type="solid", fgColor="FFFCE4D6")
_FILL_REG = PatternFill(fill_type="solid", fgColor="FFD9D9D9")
_FONT_BOLD = Font(bold=True)
_FONT_OK = Font(color="FF006100", bold=True)
_FONT_MISS = Font(color="FF9C0006", bold=True)
_FONT_WARN = Font(color="FF9C5700", bold=True)
_FONT_NEAR = Font(color="FFC65911", bold=True)
_FONT_REG = Font(color="FF595959", bold=True)

COV_PATH = "路径覆盖"
COV_SHUNT_GAP = "预期可挂未挂"
COV_NEAR_GAP = "邻主链未纳入"
COV_OUT_OF_SCOPE = "域外未建路径"
COV_REGISTRY_ONLY = "仅50R登记"

CAT_ORDER = (
    COV_PATH,
    COV_SHUNT_GAP,
    COV_NEAR_GAP,
    COV_OUT_OF_SCOPE,
    COV_REGISTRY_ONLY,
)


@dataclass(frozen=True)
class FiftyRCoverageRow:
    """单条 50R 网上器件的路径覆盖结果（方案一：分层语义）。"""

    ref: str
    part_type: str
    status: str  # 存在 | 不存在
    category: str = COV_PATH
    reason: str = ""
    path_hits: int = 0
    sample_paths: str = ""


def collect_50r_net_devices(schematic_res, signal_infos=None) -> list[str]:
    """TXT ``NET_CLASS 50R`` 网络上的**全部**器件位号（保序去重）。

    硬规则：直接拿 TXT 里 50R 类网的全部器件，不再用 FBRX/SRS 网名
    或 part_active 二次筛掉（否则 FBRX 名网上的 50R 器件会漏出清单）。

    - 网范围：优先 ``schematic_res.net_class_50r_nets``（profiler 从 TXT 提取）
    - 器件表：优先 ``all_signal_infos_raw``（含被主图剔除的 50R 类 FBRX/SRS 网）
    - 不含连接器（J*）；不按 part_active 剔除
    - 「存在/不存在」仍只看 refnum 上是否真有该位号
    """
    nets_50r = {
        str(n).strip()
        for n in (getattr(schematic_res, "net_class_50r_nets", None) or ())
        if str(n or "").strip()
    }
    # 全量信号优先，才能拿到名含 FBRX 但仍属 50R 类的网
    sig_tables = []
    for cand in (
        getattr(schematic_res, "all_signal_infos_raw", None),
        getattr(schematic_res, "signal_infos_raw", None),
        signal_infos,
        getattr(schematic_res, "signal_infos", None),
    ):
        if cand and cand not in sig_tables:
            sig_tables.append(cand)

    ordered: list[str] = []
    seen: set[str] = set()
    for sig in sig_tables:
        for si in sig or []:
            if not si or len(si) < 2:
                continue
            net = str(si[0] or "").strip()
            if not net or is_gnd_net(net):
                continue
            # 有 50R 类清单则只收清单内网；无清单时退回「当前表全部非地网」
            if nets_50r and net not in nets_50r:
                continue
            for raw in si[1] or []:
                ref = str(raw or "").strip()
                if not ref or ref in seen:
                    continue
                if is_connector_ref(ref):
                    continue
                seen.add(ref)
                ordered.append(ref)
        # 已有 50R 清单时，一张全量表扫完即可（避免 raw 重复）
        if nets_50r and ordered:
            break
    return ordered


def collect_path_device_set(paths) -> set[str]:
    """路径搭建产物中的全部器件位号（只读 paths，不搭建路径）。"""
    out: set[str] = set()
    for p in paths or []:
        for d in getattr(p, "devices", None) or []:
            ref = str(getattr(d, "ref", "") or "").strip()
            if ref:
                out.add(ref)
    return out


_VISIBLE_CELL_KINDS = frozenset({"device", "copad_leg"})


def collect_visible_grid_refs(grids) -> set[str]:
    """手册网格上真正写出的位号（灰格/行标签不算）。

    「存在」只认这些格子，禁止用 path.devices 代替——path 里可能有
    unique 抽走或根本没投放的贯通件，否则 others 会少整条链。
    """
    out: set[str] = set()
    for grid in grids or []:
        if grid is None:
            continue
        for cols in (getattr(grid, "cells", None) or {}).values():
            for placed in (cols or {}).values():
                if placed is None:
                    continue
                if str(getattr(placed, "kind", "") or "") not in _VISIBLE_CELL_KINDS:
                    continue
                _add_ref(out, getattr(placed, "ref", None))
    return out


def _add_ref(out: set[str], ref) -> None:
    r = str(ref or "").strip()
    if r:
        out.add(r)


def collect_refnum_covered_device_set(
    paths,
    *,
    copad_structures=None,
    colay_structures=None,
) -> set[str]:
    """手册/预览上已出现的位号（50R 覆盖与 others 的共同「存在」判定）。

    仅 path.devices 会漏 Co-pad 夹心对腿（对腿只在 structure 画出），
    导致 50R 覆盖误标「不存在」、others 重复收录。
    """
    out = set(collect_path_device_set(paths))
    for st in copad_structures or []:
        if st is None:
            continue
        for attr in (
            "fork_pad",
            "merge_pad",
            "internal_chain",
            "trunk_before",
            "branch1",
            "branch2",
            "trunk_after",
        ):
            for r in getattr(st, attr, ()) or ():
                _add_ref(out, r)
        _add_ref(out, getattr(st, "trunk_ref", None))
        for pair_attr in ("fork", "merge"):
            pair = getattr(st, pair_attr, None)
            if pair is None:
                continue
            _add_ref(out, getattr(pair, "leg_a", None))
            _add_ref(out, getattr(pair, "leg_b", None))
    for st in colay_structures or []:
        if st is None:
            continue
        for attr in (
            "branch1",
            "branch2",
            "structure_refs",
            "pad_refs",
            "legs",
            "devices",
        ):
            seq = getattr(st, attr, None)
            if not seq:
                continue
            for item in seq:
                if item is None:
                    continue
                if hasattr(item, "ref"):
                    _add_ref(out, getattr(item, "ref", None))
                else:
                    _add_ref(out, item)
        for pair_attr in ("pair", "fork", "merge"):
            pair = getattr(st, pair_attr, None)
            if pair is None:
                continue
            _add_ref(out, getattr(pair, "leg_a", None))
            _add_ref(out, getattr(pair, "leg_b", None))
            _add_ref(out, getattr(pair, "ref_a", None))
            _add_ref(out, getattr(pair, "ref_b", None))
    return out


def _coverage_style(category: str):
    if category == COV_PATH:
        return _FILL_OK, _FONT_OK
    if category == COV_SHUNT_GAP:
        return _FILL_WARN, _FONT_WARN
    if category == COV_NEAR_GAP:
        return _FILL_NEAR, _FONT_NEAR
    if category == COV_REGISTRY_ONLY:
        return _FILL_REG, _FONT_REG
    return _FILL_MISS, _FONT_MISS


def _classify_50r_miss(
    ref: str,
    schematic_res,
    covered_refs: set[str],
    net_to_devs: dict[str, list[str]],
    dev_to_nets: dict[str, list[str]],
) -> tuple[str, str]:
    """对「未进手册/预览覆盖集」的 50R 器件做通用语义分层。返回 (category, reason)。"""
    nets = [n for n in dev_to_nets.get(ref, []) if not is_gnd_net(n)]
    if not nets:
        return COV_REGISTRY_ONLY, "无非地信号网（或仅地网），仅出现在 50R 登记侧"

    neigh: set[str] = set()
    for n in nets:
        for d in net_to_devs.get(n, []):
            if d and d != ref:
                neigh.add(d)
    neigh_on = sorted(neigh & covered_refs)
    is_shunt = is_shunt_device(ref, schematic_res, dev_to_nets)
    deg = nongnd_degree(ref, dev_to_nets)
    # 与 _inject_parallel_on_path 同一并联定义，避免「覆盖黄、注入不认」双标
    if neigh_on and is_shunt:
        anchors = ",".join(neigh_on[:4])
        if len(neigh_on) > 4:
            anchors += f"…+{len(neigh_on) - 4}"
        return (
            COV_SHUNT_GAP,
            f"邻手册器件[{anchors}]且为并联/对地挂件(deg={deg})，"
            f"但未注入路径/未进夹心（多端口门禁或注入漏网）",
        )
    if neigh_on:
        anchors = ",".join(neigh_on[:4])
        if len(neigh_on) > 4:
            anchors += f"…+{len(neigh_on) - 4}"
        return (
            COV_NEAR_GAP,
            f"与手册器件[{anchors}]共网，但自身未进入 path 序列也未进 Co-pad/侧支"
            f"（多端口主链/开关/滤波器等，非简单旁路）",
        )
    sample_n = ",".join(sorted(neigh)[:4]) if neigh else "(无邻接器件)"
    if len(neigh) > 4:
        sample_n += f"…+{len(neigh) - 4}"
    return (
        COV_OUT_OF_SCOPE,
        f"邻接器件均不在当前手册覆盖集：{sample_n}；"
        f"属未建支路/域外 50R 连通块，不是单点漏检",
    )


def audit_50r_path_coverage(
    schematic_res,
    paths,
    signal_infos=None,
    *,
    copad_structures=None,
    colay_structures=None,
    covered_refs: Optional[set[str]] = None,
) -> list[FiftyRCoverageRow]:
    """50R 网上器件 vs **手册是否画出该位号**：存在/不存在 + 分层类别。

    硬规则：**手册格子上没有这个位号 = 未覆盖**（禁止同网并位推断）。
    ``covered_refs``：优先传入构图写表后的可见位号集；未传时才回退
    path∪结构表（仅预览/无手册网格时）。
    """
    sig = signal_infos_of(schematic_res, signal_infos)
    net_to_devs, dev_to_nets = build_net_device_maps(sig)
    path_only = collect_path_device_set(paths)
    if covered_refs is not None:
        covered_refs = {str(x).strip() for x in covered_refs if str(x).strip()}
    else:
        covered_refs = collect_refnum_covered_device_set(
            paths,
            copad_structures=copad_structures,
            colay_structures=colay_structures,
        )
    fifty_r_refs = collect_50r_net_devices(schematic_res, signal_infos)
    hit_paths: dict[str, list[str]] = defaultdict(list)
    for p in paths or []:
        kind = getattr(p, "kind", "BAND")
        ant_num = getattr(p, "ant_num", "")
        band = getattr(p, "band", "") or "-"
        direction = getattr(p, "direction", "")
        label = (
            f"{ant_num}:{band}:{direction}"
            if kind != "TRX_PA_LNA"
            else f"TRX_PA_LNA:{band}"
        )
        seen_on_path: set[str] = set()
        for d in getattr(p, "devices", None) or []:
            ref = str(getattr(d, "ref", "") or "").strip()
            if not ref or ref in seen_on_path:
                continue
            seen_on_path.add(ref)
            hit_paths[ref].append(label)
    # 仅在 Co-pad/侧支出现、不在 path 序列的器件
    for ref in covered_refs - path_only:
        hit_paths[ref].append("手册Co-pad/侧支")

    rows: list[FiftyRCoverageRow] = []
    for ref in fifty_r_refs:
        part_type = "Unknown Device"
        try:
            if schematic_res is not None:
                part_type = schematic_res.get_part_type(ref) or part_type
        except Exception:
            pass
        hits = hit_paths.get(ref, [])
        sample = "; ".join(hits[:5])
        if len(hits) > 5:
            sample += f"; …+{len(hits) - 5}"
        if ref in covered_refs:
            status, category, reason = (
                "存在",
                COV_PATH,
                "手册格子上有该位号（主图 device / 夹心 / 侧支）",
            )
        else:
            status = "不存在"
            category, reason = _classify_50r_miss(
                ref, schematic_res, covered_refs, net_to_devs, dev_to_nets,
            )
        rows.append(
            FiftyRCoverageRow(
                ref=ref,
                part_type=str(part_type or "")[:40],
                status=status,
                category=category,
                reason=reason,
                path_hits=len(hits),
                sample_paths=sample,
            )
        )
    return rows


def summarize_coverage(rows: list[FiftyRCoverageRow]) -> dict[str, Any]:
    """供总览/日志使用的计数摘要。"""
    cat_c = Counter(r.category for r in rows)
    n_ok = sum(1 for r in rows if r.status == "存在")
    n_miss = len(rows) - n_ok
    return {
        "total": len(rows),
        "ok": n_ok,
        "miss": n_miss,
        "by_category": dict(cat_c),
        "note": (
            f"50R覆盖: 合计{len(rows)} 存在{n_ok} 不存在{n_miss}"
            f"（可挂未挂{cat_c.get(COV_SHUNT_GAP, 0)}"
            f"/邻主链{cat_c.get(COV_NEAR_GAP, 0)}"
            f"/域外{cat_c.get(COV_OUT_OF_SCOPE, 0)}，详见「50R覆盖」页）"
            if rows
            else "50R覆盖: 未检查"
        ),
    }


def write_50r_coverage_sheet(ws, rows: list[FiftyRCoverageRow]) -> None:
    """写入独立「50R覆盖」工作表。"""
    n_total = len(rows)
    n_ok = sum(1 for r in rows if r.status == "存在")
    n_miss = n_total - n_ok
    cat_counts = Counter(r.category for r in rows)

    r = 1
    _write_blue_title(ws, r, "数量汇总（先看分层，再看存在/不存在）", 3)
    r += 1
    for c_i, h in enumerate(("指标", "数量", "说明"), 1):
        _style_cell(ws.cell(r, c_i, h), header=True)
    r += 1
    for name, val, note, fill, font in (
        ("器件数", n_total, "50R 激活器件合计", None, None),
        ("存在", n_ok, "已进入手册覆盖集（path/夹心/侧支）", _FILL_OK, _FONT_OK),
        ("不存在", n_miss, "未进手册（须再看类别）", _FILL_MISS, _FONT_MISS),
    ):
        ws.cell(r, 1, name)
        _style_cell(ws.cell(r, 1), fill=_FILL_BAND)
        cell = ws.cell(r, 2, val)
        if fill is not None:
            _style_cell(cell, fill=fill)
            cell.font = font
        else:
            _style_cell(cell)
        _style_cell(ws.cell(r, 3, note))
        r += 1
    notes = {
        COV_PATH: "正常",
        COV_SHUNT_GAP: "优先修并联注入/多端口门禁",
        COV_NEAR_GAP: "路径序列/开关支路是否应收",
        COV_OUT_OF_SCOPE: "需扩路径范围（如 GNSS/新 ANT）才合理",
        COV_REGISTRY_ONLY: "一般可忽略",
    }
    for cat in CAT_ORDER:
        cnt = cat_counts.get(cat, 0)
        fill, font = _coverage_style(cat)
        ws.cell(r, 1, cat)
        _style_cell(ws.cell(r, 1), fill=fill)
        ws.cell(r, 1).font = font
        cell = ws.cell(r, 2, cnt)
        _style_cell(cell, fill=fill)
        cell.font = font
        _style_cell(ws.cell(r, 3, notes.get(cat, "")))
        r += 1

    r += 1
    ws.cell(
        r, 1,
        f"合计 {n_total}  |  存在 {n_ok}  |  不存在 {n_miss}  |  "
        + "  ".join(f"{c}={cat_counts.get(c, 0)}" for c in CAT_ORDER),
    )
    ws.cell(r, 1).font = _FONT_BOLD
    r += 2

    _write_blue_title(ws, r, "50R 器件清单（按类别优先：缺口→域外→已覆盖）", 5)
    r += 1
    for c_i, h in enumerate(("位号", "存在/不存在", "类别", "原因", "类型"), 1):
        _style_cell(ws.cell(r, c_i, h), header=True)
    r += 1

    if not rows:
        ws.cell(r, 1, "(无 50R 网络器件可检查 — 确认原理图含 NET_CLASS 50R)")
        return

    cat_rank = {c: i for i, c in enumerate(CAT_ORDER)}
    ordered = sorted(
        rows,
        key=lambda x: (
            0 if x.category != COV_PATH else 1,
            cat_rank.get(x.category, 9),
            x.ref,
        ),
    )
    for row in ordered:
        fill, font = _coverage_style(row.category)
        _style_cell(ws.cell(r, 1, row.ref), fill=_FILL_BAND)
        st_cell = ws.cell(r, 2, row.status)
        _style_cell(st_cell, fill=fill)
        st_cell.font = font
        cat_cell = ws.cell(r, 3, row.category)
        _style_cell(cat_cell, fill=fill)
        cat_cell.font = font
        reason_cell = ws.cell(r, 4, row.reason)
        _style_cell(reason_cell, fill=fill)
        reason_cell.alignment = Alignment(
            horizontal="left", vertical="center", wrap_text=True,
        )
        _style_cell(ws.cell(r, 5, row.part_type), fill=fill)
        r += 1

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 56
    ws.column_dimensions["E"].width = 18


def attach_50r_coverage_sheet(
    wb,
    paths,
    schematic_res=None,
    signal_infos=None,
    coverage_rows: Optional[list[FiftyRCoverageRow]] = None,
    sheet_index: int = 1,
    *,
    copad_structures=None,
    colay_structures=None,
    covered_refs: Optional[set[str]] = None,
) -> list[FiftyRCoverageRow]:
    """在 Workbook 上挂「50R覆盖」页；返回审计行（供总览摘要）。

    ``covered_refs``：手册已画位号（与格子一致）；手册写出后应传入。
    """
    rows = list(coverage_rows or ())
    if not rows and schematic_res is not None:
        rows = audit_50r_path_coverage(
            schematic_res,
            paths,
            signal_infos,
            copad_structures=copad_structures,
            colay_structures=colay_structures,
            covered_refs=covered_refs,
        )
    ws = wb.create_sheet("50R覆盖", sheet_index)
    write_50r_coverage_sheet(ws, rows)
    return rows
