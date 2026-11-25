"""功能5 阶段 B — L0 分组折叠 / L1 share_map / 组装 SheetPlan。

L2 行序见 refnum_layout_order；共享谓词见 refnum_layout_share。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from refnum.layout.model import LayoutRow, SheetPlan, is_jump_label
from refnum.layout.order import l2_order_rows, polish_row_order
from refnum.layout.share import (
    count_left_neighbors,
    frontend_merge_refs_on_path,
    is_frontend_merge_device,
    is_pa_lna_device,
    is_rlct_ref,
)
from refnum.path import (
    CoPadStructure,
    Device,
    Path,
    path_display_devices,
    sheet_sort_key,
)


def sheet_key_for_path(path: Path) -> str:
    kind = (path.kind or "BAND").upper()
    ant = str(path.ant_num or "").strip()
    if kind == "GPS":
        # ANT 口兼 GPS：ant_num 为 ANTn 时落在该 ANT 页，不单独 GPS sheet
        au = ant.upper()
        if re.match(r"^ANT\d+$", au):
            return au
        return "GPS"
    if kind == "TRX_PA_LNA":
        return "TRX_PA_LNA"
    return ant if ant else "UNKNOWN"


def _row_label(path: Path) -> str:
    """A 列标签：频段信息（网络名推导的 band），不再用裸 TX/RX 占位。

    BAND：优先 path.band（来自路径网络名）；
    仅当 band 空或等于 ANT 名时才退回 direction/ANT。
    """
    band = (path.band or "").strip()
    direction = (path.direction or "").strip().upper()
    kind = (path.kind or "BAND").upper()
    ant = str(path.ant_num or "").strip()
    if kind == "TRX_PA_LNA":
        return band or "TRX_PA_LNA"
    if kind == "GPS":
        base = band or "GPS"
        # GPS 已是 L1/L5 等；direction 多为 RX，不必再拼
        return base
    # 频段链：网络名 band 优先（含 TX/RX 已在网名里）
    if band and band.upper() != ant.upper():
        return band
    if direction:
        return direction
    return ant or "BAND"


def dedupe_devices_preserve_order(devices: list[Device]) -> list[Device]:
    """行内去重：同一 ref 只保留首次出现。"""
    out: list[Device] = []
    seen: set[str] = set()
    for dev in devices or []:
        ref = str(getattr(dev, "ref", "") or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        out.append(dev)
    return out


def public_chain_key(devices: list[Device]) -> tuple[str, ...]:
    """公共链路指纹：保序主链（去掉 is_shunt）。相同 → 可折成一行。"""
    out: list[str] = []
    for d in devices or []:
        if getattr(d, "is_shunt", False):
            continue
        ref = str(getattr(d, "ref", "") or "").strip()
        if ref:
            out.append(ref)
    return tuple(out)


def collapse_identical_device_rows(rows: list[LayoutRow]) -> list[LayoutRow]:
    """器件序列完全相同的多行 → 一行（标签 / 拼接）。"""
    if not rows:
        return []
    groups: dict[tuple[str, ...], list[LayoutRow]] = {}
    order_keys: list[tuple[str, ...]] = []
    for row in rows:
        key = tuple(
            str(getattr(d, "ref", "") or "").strip()
            for d in row.devices
            if str(getattr(d, "ref", "") or "").strip()
        )
        if key not in groups:
            groups[key] = []
            order_keys.append(key)
        groups[key].append(row)

    collapsed: list[LayoutRow] = []
    for key in order_keys:
        grp = groups[key]
        head = grp[0]
        if len(grp) == 1:
            collapsed.append(head)
            continue
        labels = []
        for r in grp:
            lb = (r.label or "").strip()
            if lb and lb not in labels:
                labels.append(lb)
        collapsed.append(
            LayoutRow(
                row_id=head.row_id,
                path=head.path,
                label=" / ".join(labels) if labels else head.label,
                devices=list(head.devices),
            )
        )
    return collapsed


def collapse_public_chain_rows(rows: list[LayoutRow]) -> list[LayoutRow]:
    """同一 public_chain_key → 一行（不是单元格竖合并）。"""
    if not rows:
        return []
    groups: dict[tuple[str, ...], list[LayoutRow]] = {}
    order_keys: list[tuple[str, ...]] = []
    for row in rows:
        key = public_chain_key(row.devices)
        if not key:
            key = (f"__empty__{row.row_id}",)
        if key not in groups:
            groups[key] = []
            order_keys.append(key)
        groups[key].append(row)

    collapsed: list[LayoutRow] = []
    for key in order_keys:
        grp = groups[key]
        if len(grp) == 1:
            collapsed.append(grp[0])
            continue
        head = max(
            grp,
            key=lambda r: (
                len(public_chain_key(r.devices)),
                len(r.devices),
                r.row_id,
            ),
        )
        labels: list[str] = []
        for r in grp:
            lb = (r.label or "").strip()
            if lb and lb not in labels:
                labels.append(lb)
        if len(labels) > 1:
            label = "公共 " + " / ".join(labels[:6])
            if len(labels) > 6:
                label += f" 等{len(labels)}条"
        else:
            label = labels[0] if labels else head.label
        collapsed.append(
            LayoutRow(
                row_id=head.row_id,
                path=head.path,
                label=label,
                devices=list(head.devices),
            )
        )
    return collapsed


def _is_gps_trx_head(dev: Device) -> bool:
    if getattr(dev, "is_sdr", False):
        return True
    pt = (getattr(dev, "part_type", None) or "").upper()
    return "TRANSCEIVER" in pt


def _gps_link_fork_ref(row: LayoutRow) -> Optional[str]:
    """共享 TRX 之后第一条私有件。只有 TRX → None。"""
    devs = [
        d
        for d in (row.devices or [])
        if str(getattr(d, "ref", "") or "").strip()
    ]
    if not devs:
        return None
    i = 1 if _is_gps_trx_head(devs[0]) else 0
    if i >= len(devs):
        return None
    return str(devs[i].ref).strip()


def _pick_gps_link_row(grp: list[LayoutRow]) -> LayoutRow:
    def _score(r: LayoutRow):
        lab = (r.label or "").upper()
        return (
            1 if _row_has_ant(r) else 0,
            len(r.devices or []),
            1 if re.search(r"L\d", lab) else 0,
        )

    return max(grp, key=_score)


def _gps_pad_like(dev: Device) -> bool:
    """夹心段：对腿或滤波。用来找短支插入点，避免两腿只隔 RLCT。"""
    if getattr(dev, "is_copad", False) or getattr(dev, "is_colay", False):
        return True
    pt = (getattr(dev, "part_type", None) or "").upper()
    ref = str(getattr(dev, "ref", "") or "").strip().upper()
    return "FILTER" in pt or "SAW" in pt or ref.startswith("FIL")


def _gps_common_prefix_len(rows: list[LayoutRow]) -> int:
    seqs = [
        [
            str(getattr(d, "ref", "") or "").strip()
            for d in (r.devices or [])
            if str(getattr(d, "ref", "") or "").strip()
        ]
        for r in rows
    ]
    if not seqs:
        return 0
    n = 0
    while all(n < len(s) for s in seqs) and len({s[n] for s in seqs}) == 1:
        n += 1
    return n


def _gps_absorb_sibling_devices(
    head: LayoutRow, grp: list[LayoutRow],
) -> list[Device]:
    """同叉短支腿并进贯通行的器件集。path 仍是 head 干路，不合并进 path。"""
    head_devs = list(head.devices or [])
    seen = {
        str(getattr(d, "ref", "") or "").strip()
        for d in head_devs
        if str(getattr(d, "ref", "") or "").strip()
    }
    extras: list[Device] = []
    for row in grp:
        if row.row_id == head.row_id:
            continue
        for d in row.devices or []:
            ref = str(getattr(d, "ref", "") or "").strip()
            if not ref or ref in seen:
                continue
            extras.append(d)
            seen.add(ref)
    if not extras:
        return head_devs
    prefix = _gps_common_prefix_len(grp)
    i = min(prefix, len(head_devs))
    while i < len(head_devs) and _gps_pad_like(head_devs[i]):
        i += 1
    # GPS 链路从共享 TRX 起；短支不得插到 TRX 左边成第三频段头。
    if head_devs and _is_gps_trx_head(head_devs[0]):
        i = max(i, 1)
    return head_devs[:i] + extras + head_devs[i:]


def _gps_copad_member_refs(st: CoPadStructure) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for p in (getattr(st, "fork", None), getattr(st, "merge", None)):
        if p is None:
            continue
        for a in (getattr(p, "leg_a", ""), getattr(p, "leg_b", "")):
            s = str(a or "").strip()
            if s and s not in seen:
                refs.append(s)
                seen.add(s)
    for seq in (getattr(st, "branch1", None), getattr(st, "branch2", None)):
        for a in seq or ():
            s = str(a or "").strip()
            if s and s not in seen:
                refs.append(s)
                seen.add(s)
    return refs


def _gps_absorb_copad_onto_rows(
    rows: list[LayoutRow],
    structures: Optional[list[CoPadStructure]],
) -> list[LayoutRow]:
    """把 copad 另一臂对腿/RLCT 插到本行已有成员之后，回流才留得住结构。

    支路中段 FIL/IC 留黄腿。夹心不是新 Route：不得把对臂 FIL 写进未经过它的 path。
    """
    if not rows or not structures:
        return rows
    for row in rows:
        seq = list(row.devices or [])
        seen = {
            str(getattr(d, "ref", "") or "").strip()
            for d in seq
            if str(getattr(d, "ref", "") or "").strip()
        }
        st_hits: list[tuple[int, CoPadStructure, list[str]]] = []
        for st in structures:
            srefs = _gps_copad_member_refs(st)
            hits = [
                i
                for i, d in enumerate(seq)
                if str(getattr(d, "ref", "") or "").strip() in set(srefs)
            ]
            if hits:
                st_hits.append((min(hits), st, srefs))
        st_hits.sort(key=lambda t: t[0])
        for _mn, _st, srefs in st_hits:
            hits = [
                i
                for i, d in enumerate(seq)
                if str(getattr(d, "ref", "") or "").strip() in set(srefs)
            ]
            if not hits:
                continue
            legs = {
                str(x or "").strip()
                for p in (getattr(_st, "fork", None), getattr(_st, "merge", None))
                if p is not None
                for x in (getattr(p, "leg_a", ""), getattr(p, "leg_b", ""))
                if str(x or "").strip()
            }
            at = max(hits) + 1
            for ref in srefs:
                if ref in seen:
                    continue
                if ref not in legs and not is_rlct_ref(ref):
                    continue
                seq.insert(at, Device(ref=ref))
                at += 1
                seen.add(ref)
        row.devices = seq
        if row.path is not None:
            row.path.devices = list(seq)
    return rows


def collapse_gps_link_rows(rows: list[LayoutRow]) -> list[LayoutRow]:
    """GPS：TRX 后第一条私有件相同 → 一条链路（L1 一条、L5 一条）。

    同叉上的焊盘短支并进该行 devices，不单独成第三频段。path 仍是贯通干路。
    只有 TRX 的残行并进最长干路。
    """
    if len(rows) <= 1:
        return list(rows)
    groups: dict[str, list[LayoutRow]] = {}
    order: list[str] = []
    trx_only: list[LayoutRow] = []
    stubs: list[LayoutRow] = []
    for row in rows:
        kind = str(getattr(getattr(row, "path", None), "kind", "") or "").upper()
        fork = _gps_link_fork_ref(row)
        if kind and kind != "GPS":
            stubs.append(row)
            continue
        if not fork:
            trx_only.append(row)
            continue
        if fork not in groups:
            groups[fork] = []
            order.append(fork)
        groups[fork].append(row)
    if not groups:
        return list(rows)
    if trx_only:
        longest = max(
            order,
            key=lambda k: max(len(r.devices or []) for r in groups[k]),
        )
        groups[longest].extend(trx_only)
    for stub in stubs:
        srefs = {
            str(getattr(d, "ref", "") or "").strip()
            for d in (stub.devices or [])
            if str(getattr(d, "ref", "") or "").strip()
        }
        best = None
        best_n = -1
        for fork in order:
            head = _pick_gps_link_row(groups[fork])
            hrefs = {
                str(getattr(d, "ref", "") or "").strip()
                for d in (head.devices or [])
                if str(getattr(d, "ref", "") or "").strip()
            }
            n = len(srefs & hrefs)
            if n > best_n:
                best_n = n
                best = fork
        if best is not None:
            groups[best].append(stub)
    out: list[LayoutRow] = []
    for fork in order:
        grp = groups[fork]
        head = _pick_gps_link_row(grp)
        gps_labels: list[str] = []
        other_labels: list[str] = []
        for r in grp:
            lb = (r.label or "").strip()
            if not lb:
                continue
            kind = str(getattr(getattr(r, "path", None), "kind", "") or "").upper()
            if kind == "GPS":
                if lb not in gps_labels:
                    gps_labels.append(lb)
            elif lb not in other_labels:
                other_labels.append(lb)
        labels = gps_labels or other_labels
        # FIL1800_… 里的 L1 不是 GPS 频段名；只认独立 L 数字。
        bandish = [
            x
            for x in labels
            if re.search(r"(?:^|[\s_])L\d+(?:$|[\s_])", x.upper())
            or re.search(r"GPS\s*L\d", x.upper())
        ]
        label = bandish[0] if bandish else (labels[0] if labels else head.label)
        out.append(
            LayoutRow(
                row_id=head.row_id,
                path=head.path,
                label=label,
                devices=_gps_absorb_sibling_devices(head, grp),
            )
        )
    return out


def l0_group_paths(paths: list[Path]) -> dict[str, list[LayoutRow]]:
    """L0：按 Sheet 分组 → 行内去重 → 同序列折叠 → 公共链折一行。"""
    buckets: dict[str, list[LayoutRow]] = {}
    counters: dict[str, int] = defaultdict(int)
    for path in paths or []:
        key = sheet_key_for_path(path)
        counters[key] += 1
        n = counters[key]
        band = (path.band or "").replace(" ", "")
        direction = (path.direction or "").upper()
        kind = (path.kind or "BAND").upper()
        row_id = f"{key}|{kind}|{band}|{direction}|{n}"
        devices = dedupe_devices_preserve_order(list(path_display_devices(path)))
        buckets.setdefault(key, []).append(
            LayoutRow(
                row_id=row_id,
                path=path,
                label=_row_label(path),
                devices=devices,
            )
        )
    for key in list(buckets.keys()):
        rows = collapse_identical_device_rows(buckets[key])
        rows = collapse_public_chain_rows(rows)
        if key == "GPS":
            rows = collapse_gps_link_rows(rows)
        buckets[key] = rows
    return buckets


def l1_share_map(rows: list[LayoutRow]) -> dict[str, frozenset[str]]:
    """L1：ref → 含该 ref 的 row_id 集合。"""
    ref_to_rows: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        seen: set[str] = set()
        for dev in row.devices:
            ref = str(getattr(dev, "ref", "") or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            ref_to_rows[ref].add(row.row_id)
    return {ref: frozenset(ids) for ref, ids in ref_to_rows.items()}


def _row_has_copad_legs(row: LayoutRow) -> bool:
    """主链行是否带 Co-pad 对腿（夹心宿主候选）。"""
    for d in row.devices or []:
        if getattr(d, "is_copad", False):
            return True
    return False


def _row_has_coupler(row: LayoutRow) -> bool:
    for d in row.devices or []:
        if "COUPLER" in str(getattr(d, "part_type", "") or "").upper():
            return True
    return False


def _row_has_ant(row: LayoutRow) -> bool:
    """行上是否有天线端（Coupler 贯通主链 vs FBRX 取样链的区分）。"""
    for d in row.devices or []:
        if getattr(d, "is_ant", False):
            return True
        ru = str(getattr(d, "ref", "") or "").upper()
        if ru.startswith("ANT"):
            return True
    return False


def _row_attach_anchors(row: LayoutRow) -> set[str]:
    """旁路可贴靠的结构锚：Coupler / 滤波类 multiport（非 PA/TRX/普通开关）。"""
    out: set[str] = set()
    for d in row.devices or []:
        ref = str(getattr(d, "ref", "") or "").strip()
        if not ref:
            continue
        ru = ref.upper()
        pt = str(getattr(d, "part_type", "") or "").upper()
        if "COUPLER" in pt:
            out.add(ref)
            continue
        if ru.startswith(("FIL", "LPF", "BPF", "DIP", "DUP")):
            out.add(ref)
    return out


def _row_device_refs(row: LayoutRow) -> set[str]:
    return {
        str(getattr(d, "ref", "") or "").strip()
        for d in (row.devices or [])
        if str(getattr(d, "ref", "") or "").strip()
    }


def is_fbrx_sample_row(
    *,
    label: str = "",
    rid: str = "",
    devices=None,
    require_no_ant: bool = True,
    max_devs: int | None = 8,
    source: str = "label",
) -> bool:
    """FBRX 取样行（单一入口；三套门用参数区分，禁止再写含 FBRX 的第三套 if）。

    - 短行 rehome：``source=label``、无 ANT、1..8 器件
    - 并柱隔离：``source=rid``、无 ANT、不卡长度；无 row 时只看 rid
    - unique 回写：``source=label_or_rid``、不否 ANT、不卡长度（行可能已被抽空）
    """
    lab_u = str(label or "").upper()
    rid_u = str(rid or "").upper()
    src = str(source or "label").strip().lower()
    if src == "label":
        hit = "FBRX" in lab_u
    elif src == "rid":
        hit = "FBRX" in rid_u
    elif src in ("label_or_rid", "either"):
        hit = "FBRX" in lab_u or "FBRX" in rid_u
    else:
        raise ValueError(f"unknown FBRX sample source: {source!r}")
    if not hit:
        return False
    if devices is None:
        devs = None
    else:
        devs = list(devices)
    if max_devs is not None:
        if not devs:
            return False
        if len(devs) > int(max_devs):
            return False
    if require_no_ant and devs is not None:
        for d in devs:
            ref = str(getattr(d, "ref", "") or "").strip().upper()
            if ref.startswith("ANT") or getattr(d, "is_ant", False):
                return False
    return True


def _is_fbrx_short_sample_row(row: LayoutRow) -> bool:
    """Coupler→TRX/开关 取样链（无 ANT 贯通），须 rehome 贴宿主块末。"""
    return is_fbrx_sample_row(
        label=getattr(row, "label", "") or "",
        devices=getattr(row, "devices", None),
        require_no_ant=True,
        max_devs=8,
        source="label",
    )


is_fbrx_short_sample_row = _is_fbrx_short_sample_row


def _row_all_refs(row: LayoutRow) -> set[str]:
    return {
        str(getattr(d, "ref", "") or "").strip()
        for d in (row.devices or [])
        if str(getattr(d, "ref", "") or "").strip()
    }


def _row_frontend_merge_refs(
    row: LayoutRow,
    cand: set[str] | None = None,
) -> set[str]:
    """本行 path 上 PA/LNA–ANT 之间的前端可并 ref；可选与 cand 求交。"""
    fe = set(frontend_merge_refs_on_path(getattr(row, "path", None)))
    if cand is not None:
        fe &= cand
    return fe


def _shared_frontend_refs(a: LayoutRow, b: LayoutRow) -> set[str]:
    """两行共有的前端合并器件：在**任一行**的 PA–ANT 区间内即可。

    FBRX 取样链常无 ANT，本身算不出区间；以前端主链 path 上的区间为准。
    """
    shared = _row_all_refs(a) & _row_all_refs(b)
    if not shared:
        return set()
    fe_a = frontend_merge_refs_on_path(getattr(a, "path", None))
    fe_b = frontend_merge_refs_on_path(getattr(b, "path", None))
    return shared & (set(fe_a) | set(fe_b))


def rehome_fbrx_sample_rows(rows: list[LayoutRow]) -> list[LayoutRow]:
    """FBRX 取样短行：贴共享 **前端合并器件** 宿主连续块边缘。

    前端定义（boss）：显示序 **LNA/PA 与 ANT 之间** 的非 RLCT 器件
    （见 ``frontend_merge_refs_on_path``）；**不是**位号前缀 U/FIL。

    合并冲突：**前端合并优先于 PA/LNA 合并**——
      禁止为保 PA 柱把 FBRX 锁在无关贯通行旁而拆掉 PA–ANT 段上的前端并柱。
    贴块后只 gather 前端 multi ref，不 gather PA/LNA multi。
    """
    if not rows:
        return rows
    short = [r for r in rows if _is_fbrx_short_sample_row(r)]
    if not short:
        return list(rows)
    from refnum.layout.order import gather_owners, holes_for_ref

    out = _attach_short_fbrx_after_frontend_host_block(rows)
    share = l1_share_map(out)
    # 全表 path 并集上的前端 ref（FBRX 自身常无 ANT 区间）
    all_fe: set[str] = set()
    for r in out:
        all_fe |= set(frontend_merge_refs_on_path(getattr(r, "path", None)))
    for fr in list(out):
        if not _is_fbrx_short_sample_row(fr):
            continue
        fr_refs = _row_all_refs(fr)
        fe_refs = sorted(fr_refs & all_fe)
        for ref in fe_refs:
            owners = share.get(ref)
            if not owners or len(owners) < 2:
                continue
            if fr.row_id not in owners:
                continue
            ids = [r.row_id for r in out]
            if holes_for_ref(ids, owners) <= 0:
                continue
            out = gather_owners(out, owners)
            share = l1_share_map(out)
    return out


def _attach_short_fbrx_subset(
    rows: list[LayoutRow],
    fbrx_to_move: list[LayoutRow],
) -> list[LayoutRow]:
    """仅移动指定 FBRX 行到前端宿主块缘（其余行相对序不变）。"""
    move_ids = {r.row_id for r in fbrx_to_move}
    if not move_ids:
        return list(rows)
    fbrx = [r for r in rows if r.row_id in move_ids]
    rest = [r for r in rows if r.row_id not in move_ids]
    if not fbrx:
        return list(rows)
    return _attach_short_fbrx_after_frontend_host_block(rest + fbrx)


def _attach_short_fbrx_after_frontend_host_block(
    rows: list[LayoutRow],
) -> list[LayoutRow]:
    """短 FBRX 贴到共享 **前端合并器件** 的宿主连续块末。

    优先级：**前端合并 > PA/LNA 合并**。
      - 宿主分 = 共享前端 ref 数（PA–ANT 区间）；无前端共位才回退任意共位
      - 块扩张只沿「共享前端 ref」邻接，禁止因共 PA 把无关贯通行扩进块
      - 只插块末：插块顶会夹进芯片侧已冻前端，拆开开关/耦合器第一层
    """
    fbrx = [r for r in rows if _is_fbrx_short_sample_row(r)]
    rest = [r for r in rows if not _is_fbrx_short_sample_row(r)]
    if not fbrx or not rest:
        return list(rows)
    out = list(rest)
    for fr in reversed(fbrx):
        fr_refs = _row_all_refs(fr)
        if not fr_refs:
            out.append(fr)
            continue

        best_fe = -1
        best_sc = 0
        best_len = -1
        best_i = -1
        for i, m in enumerate(out):
            shared_all = fr_refs & _row_all_refs(m)
            if not shared_all:
                continue
            shared_fe = _shared_frontend_refs(fr, m)
            fe = len(shared_fe)
            sc = len(shared_all)
            mlen = len(_row_all_refs(m))
            if (
                fe > best_fe
                or (fe == best_fe and sc > best_sc)
                or (fe == best_fe and sc == best_sc and mlen > best_len)
            ):
                best_fe = fe
                best_sc = sc
                best_len = mlen
                best_i = i
        if best_i < 0 or best_sc <= 0:
            out.append(fr)
            continue

        # 扩张键：有前端共位则只用前端；否则回退任意共位（仅共 PA 的退化）
        expand_keys = _shared_frontend_refs(fr, out[best_i])
        if not expand_keys:
            expand_keys = fr_refs & _row_all_refs(out[best_i])

        lo = hi = best_i
        while lo > 0 and (
            expand_keys & _row_all_refs(out[lo - 1])
            or _shared_frontend_refs(fr, out[lo - 1])
        ):
            # 仅当邻行与 FBRX 仍共享 expand_keys 中的前端/锚
            if not (expand_keys & _row_all_refs(out[lo - 1])):
                break
            lo -= 1
        while hi + 1 < len(out) and (expand_keys & _row_all_refs(out[hi + 1])):
            hi += 1

        # 取样放已连续宿主块末。插块顶会夹进芯片侧已冻前端，拆开开关/耦合器第一层。
        out.insert(hi + 1, fr)
    return out


def _rehome_side_rows(
    rows: list[LayoutRow],
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> list[LayoutRow]:
    """侧支 / FBRX 取样链 → 贴到共享结构锚 owner 块末（行分配，L2 之后）。

    流水线硬约束（设计 §5.0）：**先优化行序，再对齐，再合并**。
    目标：旁路与宿主共享结构件的 **合并包围盒尽量小**（holes=0 且 span 短）。
      - FBRX 经 Coupler 取样 → 须落在 Coupler owner 连续块内/块末
      - **禁止**沉到无关 FE 链（仅共享远端 FIL 的贯通行）下方，
        否则竖并穿过该行，视觉上像该链路也走了 Coupler

    guest 仅无 ANT 旁路；含 ANT 贯通主链不得当 guest 整表重排。
    """
    if not rows:
        return rows

    copad_mid_fils: set[str] = set()
    for st in copad_structures or []:
        for attr in ("branch1", "branch2", "internal_chain"):
            for x in getattr(st, attr, ()) or ():
                ru = str(x or "").upper()
                if ru.startswith(("FIL", "LPF", "BPF", "DIP", "DUP")):
                    copad_mid_fils.add(str(x))

    def _row_fil_anchors(r: LayoutRow) -> set[str]:
        out = _row_attach_anchors(r)
        for d in r.devices or []:
            ref = str(getattr(d, "ref", "") or "")
            ru = ref.upper()
            if ru.startswith(("FIL", "LPF", "BPF", "DIP", "DUP")):
                out.add(ref)
            if "COUPLER" in str(getattr(d, "part_type", "") or "").upper():
                out.add(ref)
        if _row_has_copad_legs(r):
            out |= copad_mid_fils
        return out

    def _is_host(r: LayoutRow) -> bool:
        if _row_has_copad_legs(r):
            return True
        # 仅「Coupler + ANT」贯通主链为宿主；FBRX 取样链虽含 Coupler 是 guest
        return _row_has_coupler(r) and _row_has_ant(r)

    def _row_refs(r: LayoutRow) -> set[str]:
        return {
            str(getattr(d, "ref", "") or "")
            for d in (r.devices or [])
            if getattr(d, "ref", None)
        }

    hosts = [r for r in rows if _is_host(r)]
    host_anchors: set[str] = set()
    for h in hosts:
        host_anchors |= _row_fil_anchors(h)

    guests: list[LayoutRow] = []
    for r in rows:
        band = str(getattr(r.path, "band", "") or "")
        # JUMP 特例行：贴宿主，不参与主链 L2 骨架序
        if is_jump_label(band):
            guests.append(r)
            continue
        if _is_host(r):
            continue
        kind = str(getattr(r.path, "kind", "BAND") or "BAND").upper()
        if kind != "BAND":
            continue
        if _row_has_copad_legs(r):
            continue
        # 含 ANT 的贯通主链必须留在 L2 序里，不得当 guest 重排
        if _row_has_ant(r):
            continue
        if not hosts:
            continue
        anchors = _row_fil_anchors(r)
        # FBRX/侧支：有 Coupler、或结构锚与宿主相交、或位号落在宿主锚上
        # （guest 上 Coupler part_type 偶发缺失时仍靠共享位号识别）
        if (
            _row_has_coupler(r)
            or (anchors & host_anchors)
            or (_row_refs(r) & host_anchors)
        ):
            guests.append(r)

    if not guests:
        return rows
    mains = [r for r in rows if r not in guests]
    # Co-pad 结构成员集：guest 贴宿主时延伸到「同结构」主链块末（非紧贴宿主一行）
    struct_member_sets: list[set[str]] = []
    for st in copad_structures or []:
        mem: set[str] = set()
        for attr in (
            "fork_pad", "merge_pad", "branch1", "branch2", "internal_chain",
        ):
            for x in getattr(st, attr, ()) or ():
                s = str(x or "").strip()
                if s:
                    mem.add(s)
        fork = getattr(st, "fork", None)
        tr = str(getattr(fork, "trunk_ref", "") or "").strip()
        if tr:
            mem.add(tr)
        if len(mem) >= 2:
            struct_member_sets.append(mem)

    def _row_struct_ids(r: LayoutRow) -> set[int]:
        refs = _row_refs(r)
        return {
            i for i, mem in enumerate(struct_member_sets) if refs & mem
        }

    def _affinity_block_end(host_mi: int) -> int:
        """从宿主起，连续延伸同 Co-pad 结构触及的主链，guest 贴该块末。"""
        end = host_mi
        host_ids = _row_struct_ids(mains[host_mi])
        if not host_ids:
            return host_mi
        while end + 1 < len(mains):
            if _row_struct_ids(mains[end + 1]) & host_ids:
                end += 1
            else:
                break
        return end

    assign: dict[int, int] = {}
    for si, s in enumerate(guests):
        s_anchors = _row_fil_anchors(s)
        srefs = _row_refs(s) | s_anchors
        # 贴到「共享结构锚」的 owner 主链块末（缩小 Coupler 合并盒）
        # 禁止只共远端 FIL 的无关 FE 链把 guest 吸到表尾
        struct_owner_mis: list[int] = []
        for mi, m in enumerate(mains):
            m_anchors = _row_fil_anchors(m)
            if s_anchors & m_anchors or s_anchors & _row_refs(m):
                if _row_has_coupler(m) or _row_has_copad_legs(m) or (
                    s_anchors & m_anchors
                ):
                    struct_owner_mis.append(mi)
        if struct_owner_mis:
            assign[si] = max(struct_owner_mis)
            continue
        # 短链（含 JUMP stub）：无结构锚时贴共位最多的主链（焊盘锚）
        best_sc2 = -1
        host_mis2: list[int] = []
        for mi, m in enumerate(mains):
            mrefs = _row_refs(m)
            sc = len(mrefs & srefs)
            if sc <= 0:
                continue
            if sc > best_sc2:
                best_sc2 = sc
                host_mis2 = [mi]
            elif sc == best_sc2:
                host_mis2.append(mi)
        if host_mis2 and best_sc2 > 0:
            # 仅短 guest：器件少且无 ANT 贯通，或 band 为 JUMP
            bu = str(getattr(s.path, "band", "") or "").upper()
            n_dev = len([d for d in (s.devices or []) if getattr(d, "ref", None)])
            short = (
                is_jump_label(bu)
                or (n_dev <= 3 and not _row_has_ant(s))
            )
            if short:
                assign[si] = max(host_mis2)

    # guest 贴「宿主亲和块」末：宿主 + 同 Co-pad 结构连续主链之后
    emit_at: dict[int, list[int]] = defaultdict(list)
    for si, host_mi in assign.items():
        emit_at[_affinity_block_end(host_mi)].append(si)

    out: list[LayoutRow] = []
    used: set[int] = set()
    for mi, m in enumerate(mains):
        out.append(m)
        for si in emit_at.get(mi, ()):
            if si not in used:
                out.append(guests[si])
                used.add(si)
    for si, s in enumerate(guests):
        if si not in used:
            out.append(s)
    return out


def build_sheet_plans(
    paths: list[Path],
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> list[SheetPlan]:
    """L0+L1+L2 → SheetPlan 列表。残差 holes 只告警。"""
    paths_by_sheet: dict[str, list[Path]] = defaultdict(list)
    for p in paths or []:
        paths_by_sheet[sheet_key_for_path(p)].append(p)

    buckets = l0_group_paths(paths)
    plans: list[SheetPlan] = []
    for key in sorted(buckets.keys(), key=sheet_sort_key):
        rows = buckets[key]
        if key == "GPS":
            rows = _gps_absorb_copad_onto_rows(rows, copad_structures)
        # Co-pad 结构参与左邻归一：同组合并对两腿对宿主只计 1 种左邻
        left_n = count_left_neighbors(
            paths_by_sheet.get(key, []),
            copad_structures=copad_structures,
        )
        share = l1_share_map(rows)
        ordered = l2_order_rows(
            rows,
            share,
            left_neighbor_count=left_n,
            copad_structures=copad_structures,
        )
        ordered = _rehome_side_rows(
            ordered, copad_structures=copad_structures,
        )
        # 侧支贴完收；取样贴块末后再收。polish 可能把取样挤开，
        # 或把无关主链冻在开关跨度里。再贴再收，末次必须停在 polish：
        # 停在贴上会让取样抢到错误宿主开关（贯通行而不是自己的耦合器）。
        share = l1_share_map(ordered)
        ordered = polish_row_order(
            ordered,
            share,
            left_neighbor_count=left_n,
            copad_structures=copad_structures,
        )
        ordered = rehome_fbrx_sample_rows(ordered)
        share = l1_share_map(ordered)
        ordered = polish_row_order(
            ordered,
            share,
            left_neighbor_count=left_n,
            copad_structures=copad_structures,
        )
        ordered = rehome_fbrx_sample_rows(ordered)
        share = l1_share_map(ordered)
        ordered = polish_row_order(
            ordered,
            share,
            left_neighbor_count=left_n,
            copad_structures=copad_structures,
        )
        ordered = rehome_fbrx_sample_rows(ordered)
        share = l1_share_map(ordered)
        ordered = polish_row_order(
            ordered,
            share,
            left_neighbor_count=left_n,
            copad_structures=copad_structures,
        )
        share = l1_share_map(ordered)
        plan = SheetPlan(
            key=key,
            rows=ordered,
            share_map=share,
            left_neighbor_count=left_n,
            copad_structures=list(copad_structures or []),
        )
        plans.append(plan)
    return plans
