"""功能5 阶段 A — GPS/GNSS 路径搭建（从 refnum_path 纯拆出，行为不变）。

公开：build_gps_paths 及 sheet 归属辅助。
由 refnum_path re-export；调用方保持 from refnum.path import ...。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from refnum.graph import (
    canonical_ant_id,
    is_ant_path_node,
    is_network,
    is_pa_lna_ref,
    is_transceiver_ref,
    make_device,
)

# Path 在 refnum_path 中先于本模块 re-export 定义；模块级可安全引用。
# reorder_matching_rlct_before_multiport 定义更晚，仅在函数内懒导入。
from refnum.path import Path  # noqa: E402


def _gps_clean_refs(raw, schematic_res) -> list[str]:
    """MAIN GPS 交替序列 [器件,网,器件,…] → 器件序；保留末端 ANT* 网名作天线端点。"""
    if not raw:
        return []
    refs: list[str] = []
    ant_end: Optional[str] = None
    for n in raw:
        if not n:
            continue
        s = str(n)
        if is_network(s, schematic_res):
            # GPS 天线网：名含 ANT 或 GNSS/GPS 且像天线口
            su = s.upper()
            if "ANT" in su or su.startswith("GPS") or "GNSS" in su:
                # 仅当更像天线终点（含 ANT）时覆盖末端
                if "ANT" in su:
                    ant_end = s
            continue
        if not refs or refs[-1] != s:
            refs.append(s)
    if ant_end and (not refs or refs[-1] != ant_end):
        refs.append(ant_end)
    return refs


def _gps_band_label(raw, refs: list[str]) -> str:
    """从路径文本猜 GPS 频段标签（通用 token，无项目位号）。"""
    text = " ".join(str(x) for x in (list(raw or []) + list(refs))).upper()
    # 长短 token：L1/L5 等需避免被其它编号误伤时优先看 GNSS_/GPS_ 前缀
    for tag in ("L1", "L2", "L5", "L6", "B1", "B2", "E1", "E5"):
        if f"GNSS_{tag}" in text or f"GNSS{tag}" in text:
            return f"GPS {tag}"
        if f"GPS_{tag}" in text or f"GPS{tag}" in text:
            return f"GPS {tag}"
        if f"_{tag}" in text or f"/{tag}" in text or f" {tag}" in text:
            # 弱匹配：仅当路径里明确出现空格/下划线分隔的 tag
            if re.search(rf"(?:^|[\s_/]){tag}(?:$|[\s_/])", text):
                return f"GPS {tag}"
    return "GPS"


def _gps_chain_ends_ant(refs: list[str]) -> bool:
    return bool(refs) and is_ant_path_node(refs[-1])


_ANT_PORT_NUM_RE = re.compile(r"^ANT(\d+)$", re.I)


def _band_sheet_ant_port_nums(level_matrixs) -> set[str]:
    """level_matrixs / reverse 已建 BAND 页的天线口号数字集合（如 {'7','10'}）。"""
    nums: set[str] = set()
    for entry in level_matrixs or []:
        if not entry:
            continue
        m = _ANT_PORT_NUM_RE.match(str(entry[0] or "").strip())
        if m:
            nums.add(str(int(m.group(1))))
    return nums


def _gps_terminal_band_ant_ports(
    gps_paths: list,
    band_ant_nums: set[str],
) -> list[str]:
    """GPS Path 终点为 ANTn 且 n 已有 BAND 页 → 这些 ANTn（ANT 口兼 GPS）。

    GPS 链路终到已有 BAND 页的 ANTn → 并入该 ANT 页，不出独立 GPS sheet。
    终到的 ANTn 尚无 BAND 页 → 仍出 GPS sheet。
    """
    hit: set[str] = set()
    for p in gps_paths or []:
        for d in reversed(getattr(p, "devices", None) or []):
            ref = str(getattr(d, "ref", "") or "").strip()
            if not ref or not is_ant_path_node(ref):
                continue
            m = _ANT_PORT_NUM_RE.match(ref.upper())
            if m and str(int(m.group(1))) in band_ant_nums:
                hit.add(canonical_ant_id(ref))
            break
    return sorted(hit)


def _should_skip_gps_sheet(
    gps_paths: list,
    band_ant_nums: set[str],
) -> tuple[bool, list[str]]:
    """ANT 口做 GPS（GPS 链路通到已有 BAND 页的 ANTn）→ 不单独出 GPS sheet。

    返回 (是否并入 ANT 页, 承载 GPS 的 ANTn 列表)。
    """
    covered = _gps_terminal_band_ant_ports(gps_paths, band_ant_nums)
    return (bool(covered), covered)


def _gps_path_home_ant(
    path,
    band_ant_nums: set[str],
    fallback_ants: list[str],
) -> str:
    """单条 GPS Path 应落入的 ANT 页（ANT 口兼 GPS）。"""
    for d in reversed(getattr(path, "devices", None) or []):
        ref = str(getattr(d, "ref", "") or "").strip().upper()
        if not ref or not is_ant_path_node(ref):
            continue
        m = _ANT_PORT_NUM_RE.match(ref)
        if m and str(int(m.group(1))) in band_ant_nums:
            return canonical_ant_id(ref)
        break
    if fallback_ants:
        return str(fallback_ants[0])
    return "GPS"


def _gps_last_index(seq: list[str], ref: str) -> int:
    for i in range(len(seq) - 1, -1, -1):
        if seq[i] == ref:
            return i
    return -1


def _gps_extend_chains_to_ant(chains: list[list[str]]) -> list[list[str]]:
    """未到 ANT 的段，按同 LNA/汇合点从已达 ANT 的兄弟链接尾。

    MAIN 常同时给出 TRX→LNA 与完整 TRX→…→ANT；短段若不接尾，unique 后
    会把器件抽到短行上，ANT 尾孤立，GPS 页失去 TRX 下的树状连通。
    """
    if not chains:
        return []
    donors = [c for c in chains if _gps_chain_ends_ant(c) and len(c) >= 2]
    if not donors:
        return [list(c) for c in chains]

    out: list[list[str]] = []
    for c in chains:
        cur = list(c)
        if len(cur) < 2 or _gps_chain_ends_ant(cur):
            out.append(cur)
            continue
        best: Optional[list[str]] = None
        best_score = (-1, -1)  # (join_pos_in_cur, suffix_len)
        for d in donors:
            # 从短链右端往左找可接合点（优先 LNA/末端）
            for i in range(len(cur) - 1, -1, -1):
                j = _gps_last_index(d, cur[i])
                if j < 0 or j >= len(d) - 1:
                    continue
                suffix = d[j + 1 :]
                if not suffix or not _gps_chain_ends_ant(d):
                    continue
                # 接合点后不得与短链已有后续冲突（短链已在 i 结束）
                score = (i, len(suffix))
                if score > best_score:
                    best_score = score
                    best = cur[: i + 1] + list(suffix)
                break
        out.append(best if best is not None else cur)
    return out


def _gps_drop_dominated_chains(chains: list[list[str]]) -> list[list[str]]:
    """去掉被更长链覆盖的纯前缀/真子集短 stub（保留并行分叉）。"""
    if len(chains) <= 1:
        return [list(c) for c in chains if len(c) >= 2]

    tuples = [tuple(c) for c in chains if len(c) >= 2]
    # 去重保序
    uniq: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for t in tuples:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)

    keep: list[tuple[str, ...]] = []
    for i, a in enumerate(uniq):
        dominated = False
        sa = set(a)
        for j, b in enumerate(uniq):
            if i == j:
                continue
            # 真前缀：完整被更长路径前缀吞掉
            if len(b) > len(a) and b[: len(a)] == a:
                dominated = True
                break
            # 无 ANT 的短链，器件集真含于已达 ANT 的长链 → stub
            if (
                not _gps_chain_ends_ant(list(a))
                and _gps_chain_ends_ant(list(b))
                and sa < set(b)
                and a[0] == b[0]
            ):
                dominated = True
                break
        if not dominated:
            keep.append(a)
    return [list(t) for t in keep]


def _gps_try_merge_entry_forks(
    a: list[str], b: list[str]
) -> Optional[tuple[list[str], set[str]]]:
    """同首同尾：TRX 后短分叉、在首个公共件汇合且后续全同 → 合并为一条。

    同 TRX 起点、同 ANT 终点：短入口 C/L 对腿在首个公共件汇合且后续全同 → 并成一行。
    分叉段过长（真并行干路，如 L1 vs L5）不合并。
    返回 (合并链, 入口分叉位号集)。
    """
    if len(a) < 3 or len(b) < 3:
        return None
    if a[0] != b[0] or a[-1] != b[-1]:
        return None
    set_b = set(b)
    join_ia = join_ib = -1
    for ia, ref in enumerate(a[1:], start=1):
        if ref not in set_b:
            continue
        ib = b.index(ref)
        if a[ia:] == b[ib:]:
            join_ia, join_ib = ia, ib
            break
    if join_ia < 1 or join_ib < 1:
        return None
    fork_a = a[1:join_ia]
    fork_b = b[1:join_ib]
    if not fork_a or not fork_b:
        return None
    # 仅短入口分叉（对腿/焊盘级）；长段是真频段分叉
    if len(fork_a) > 3 or len(fork_b) > 3:
        return None
    # 分叉含 U* 有源前端（LNA 等）→ L1/L5 真干路，不得并成一行
    if any(str(r).upper().startswith("U") for r in fork_a + fork_b):
        return None
    # 去重保序拼接分叉件
    forks: list[str] = []
    seen: set[str] = set()
    for r in fork_a + fork_b:
        if r not in seen:
            seen.add(r)
            forks.append(r)
    return [a[0]] + forks + list(a[join_ia:]), set(fork_a) | set(fork_b)


def _gps_merge_parallel_entry_forks(
    chains: list[list[str]],
) -> tuple[list[list[str]], set[str]]:
    """合并同 TRX/同 ANT 的短入口双路径；返回 (链, 应对腿黄标的分叉位号)。"""
    if len(chains) <= 1:
        return [list(c) for c in chains], set()

    by_ends: dict[tuple[str, str], list[list[str]]] = defaultdict(list)
    others: list[list[str]] = []
    for c in chains:
        if len(c) >= 3 and _gps_chain_ends_ant(c):
            by_ends[(c[0], c[-1])].append(list(c))
        else:
            others.append(list(c))

    out: list[list[str]] = list(others)
    entry_legs: set[str] = set()
    for _key, grp in by_ends.items():
        remaining = sorted(grp, key=len, reverse=True)
        while remaining:
            base = remaining.pop(0)
            i = 0
            while i < len(remaining):
                got = _gps_try_merge_entry_forks(base, remaining[i])
                if got is not None:
                    base, legs = got
                    entry_legs |= legs
                    remaining.pop(i)
                else:
                    i += 1
            out.append(base)
    return out, entry_legs


def _gps_longest_common_suffix(seqs: list[list[str]]) -> list[str]:
    if not seqs:
        return []
    rev = [list(reversed(s)) for s in seqs]
    common: list[str] = []
    for tup in zip(*rev):
        if len(set(tup)) == 1:
            common.append(tup[0])
        else:
            break
    return list(reversed(common))


def _gps_primary_chain_score(
    chain: list[str],
    *,
    band: str = "",
) -> tuple:
    """主链打分：L1 频段 > 含更多中段器件 > 更长。"""
    bu = (band or "").upper()
    l1 = 1 if "L1" in bu else 0
    l5 = 1 if "L5" in bu else 0
    # 非 L5 的默认干路略优先（无 band 时靠长度）
    return (l1, -l5, len(chain), len(set(chain)))


def _gps_join_anchors_from_ant_chains(chains: list[list[str]]) -> frozenset[str]:
    """拓扑汇合点：同起点、通 ANT 的多路公共后缀**首件**（与器件名前缀无关）。

    不通 ANT 的 Co-pad 短支不产生汇合点。
    """
    by_start: dict[str, list[list[str]]] = defaultdict(list)
    for c in chains:
        if len(c) >= 2 and _gps_chain_ends_ant(c):
            by_start[str(c[0])].append(list(c))
    anchors: set[str] = set()
    for grp in by_start.values():
        if len(grp) < 2:
            continue
        suf = _gps_longest_common_suffix(grp)
        if len(suf) >= 2:
            anchors.add(suf[0])
    return frozenset(anchors)


def _gps_restrip_paths_shared_suffix(
    paths: list,
    *,
    join_anchors: Optional[frozenset[str]] = None,
) -> list:
    """并联注入后：支路裁到最后独占件；下一件仅当为**拓扑汇合点**时保留。

    join_anchors 来自通 ANT 多路 LCS 首件，禁止把主链中段 LNA 误当汇合点。
    """
    gps = [p for p in paths if (p.kind or "").upper() == "GPS"]
    if len(gps) < 2:
        return paths
    ant_paths = [
        p
        for p in gps
        if p.devices and is_ant_path_node(p.devices[-1].ref)
    ]
    if not ant_paths:
        return paths
    primary = max(
        ant_paths,
        key=lambda p: _gps_primary_chain_score(
            [d.ref for d in p.devices], band=p.band or ""
        ),
    )
    pset = {d.ref for d in primary.devices}
    # 无外部 anchors 时：用当前仍通 ANT 的链现场算（注入可能加长支路）
    anchors = set(join_anchors or ())
    if not anchors:
        ant_refs = [
            [d.ref for d in p.devices]
            for p in gps
            if p.devices and is_ant_path_node(p.devices[-1].ref)
        ]
        anchors = set(_gps_join_anchors_from_ant_chains(ant_refs))
    # 支路已截断时仍可用：与主链的 LCS（若支路被注入又挂回共享尾）
    pref = [d.ref for d in primary.devices]
    for p in gps:
        if p is primary:
            continue
        br = [d.ref for d in p.devices]
        suf = _gps_longest_common_suffix([pref, br])
        if len(suf) >= 2:
            anchors.add(suf[0])

    new_gps: list = []
    for p in gps:
        if p is primary:
            new_gps.append(p)
            continue
        refs = [d.ref for d in p.devices]
        last_excl = -1
        for i, r in enumerate(refs):
            if r not in pset:
                last_excl = i
        if last_excl < 1:
            continue
        cut = last_excl
        if (
            cut + 1 < len(refs)
            and refs[cut + 1] in pset
            and refs[cut + 1] in anchors
        ):
            cut = cut + 1
        devices = list(p.devices[: cut + 1])
        if len(devices) < 2:
            continue
        # band 只信原 path / 网名 token（_gps_band_label），禁止位号名单兜底
        band = (p.band or "").strip() or "GPS"
        if band.upper() == "GPS":
            band = _gps_band_label(
                [d.ref for d in devices],
                [d.ref for d in devices],
            )
        new_gps.append(
            Path(
                ant_num=p.ant_num,
                devices=devices,
                band=band,
                direction=p.direction,
                kind="GPS",
            )
        )
    other = [p for p in paths if (p.kind or "").upper() != "GPS"]
    return other + new_gps


def _gps_strip_shared_ant_suffix_to_primary(
    chains: list[list[str]],
    *,
    chain_bands: Optional[dict[tuple[str, ...], str]] = None,
    join_anchors: Optional[frozenset[str]] = None,
) -> list[list[str]]:
    """同 TRX 多路到 ANT：共享尾只留给主链，支路截到**拓扑汇合点**。

    汇合点 = 通 ANT 多路公共后缀首件（join_anchors）；支路保留该点以便竖并，
    其后共享尾仅主链保留。
    """
    if len(chains) <= 1:
        return [list(c) for c in chains]
    bands = chain_bands or {}
    anchors = join_anchors or _gps_join_anchors_from_ant_chains(chains)

    by_start: dict[str, list[list[str]]] = defaultdict(list)
    others: list[list[str]] = []
    for c in chains:
        if len(c) >= 2 and _gps_chain_ends_ant(c):
            by_start[c[0]].append(list(c))
        else:
            others.append(list(c))

    out: list[list[str]] = list(others)
    for _start, grp in by_start.items():
        if len(grp) < 2:
            out.extend(grp)
            continue
        suf = _gps_longest_common_suffix(grp)
        if len(suf) < 2:
            out.extend(grp)
            continue
        primary = max(
            grp,
            key=lambda c: _gps_primary_chain_score(
                c, band=bands.get(tuple(c), "")
            ),
        )
        pkey = tuple(primary)
        # 本组 LCS 首件即拓扑汇合点
        join = suf[0]
        keep = 1 if join in anchors else 0
        for c in grp:
            if tuple(c) == pkey:
                out.append(list(c))
                continue
            if len(c) >= len(suf) and c[-len(suf) :] == suf:
                stripped = c[: len(c) - len(suf) + keep]
                if len(stripped) >= 2:
                    out.append(stripped)
            else:
                out.append(list(c))
    return out


def build_gps_paths(
    gps_paths,
    schematic_res,
) -> list:
    """GPS/GNSS 路径：MAIN `GPS_ALL_PATHS`（TRX→LNA 与 LNA→ANT 段）→ 预览 Path。

    - 去掉网络名，保留器件序；末端 ANT 网名保留为天线端点
    - 能拼接则 TRX→…→LNA→…→ANT 成一条；否则保留各段
    - 短段按兄弟链接到 ANT；去掉纯前缀 stub
    - 同 TRX 短入口双路径合并为一行（入口对腿）
    - 共享 ANT 尾只留给主链，支路止于汇合前（树状 L1 干 + L5 支）
    - 单独 Sheet「GPS」，不并入蜂窝频段行
    """
    from refnum.path import reorder_matching_rlct_before_multiport

    if not gps_paths:
        return []

    segments: list[list[str]] = []
    raw_by_key: dict[tuple[str, ...], list] = {}
    for raw in gps_paths:
        if not raw:
            continue
        refs = _gps_clean_refs(raw, schematic_res)
        if len(refs) < 2:
            continue
        key = tuple(refs)
        if key in raw_by_key:
            continue
        raw_by_key[key] = list(raw)
        segments.append(refs)

    used = [False] * len(segments)
    chains: list[list[str]] = []

    # 优先：TRX→…→LNA 拼 LNA→…→ANT（同一 LNA 可有多条 ANT 尾，全用尽）
    for i, a in enumerate(segments):
        if used[i]:
            continue
        if not (
            is_transceiver_ref(a[0], schematic_res)
            and is_pa_lna_ref(a[-1], schematic_res)
        ):
            continue
        conts = [
            j
            for j, b in enumerate(segments)
            if j != i and not used[j] and b[0] == a[-1] and len(b) >= 2
        ]
        used[i] = True
        if conts:
            for j in conts:
                used[j] = True
                chains.append(a + segments[j][1:])
        else:
            chains.append(list(a))

    # 残余段（未拼上的 LNA→ANT 或其它）
    for i, a in enumerate(segments):
        if used[i]:
            continue
        used[i] = True
        chains.append(list(a))

    # 方向校正 → 接 ANT 尾 → 去 stub → 入口对腿合并 → 共享尾归主链
    oriented: list[list[str]] = []
    for refs in chains:
        if len(refs) < 2:
            continue
        if (
            is_ant_path_node(refs[0])
            and is_transceiver_ref(refs[-1], schematic_res)
        ):
            refs = list(reversed(refs))
        oriented.append(refs)
    chains = _gps_extend_chains_to_ant(oriented)
    chains = _gps_drop_dominated_chains(chains)
    chains, entry_legs = _gps_merge_parallel_entry_forks(chains)

    def _hint_and_band(refs: list[str]) -> tuple[object, str]:
        raw_hint = raw_by_key.get(tuple(refs)) or raw_by_key.get(
            tuple(reversed(refs))
        ) or refs
        if raw_hint is refs or raw_hint == refs:
            for k, raw in raw_by_key.items():
                if len(k) >= 2 and tuple(refs[: len(k)]) == k:
                    raw_hint = raw
                    break
        return raw_hint, _gps_band_label(raw_hint, refs)

    chain_bands = {tuple(c): _hint_and_band(c)[1] for c in chains if len(c) >= 2}
    # 通 ANT 多路公共后缀首件 = 树汇合点（纯拓扑）
    join_anchors = _gps_join_anchors_from_ant_chains(chains)
    build_gps_paths.last_join_anchors = join_anchors  # type: ignore[attr-defined]
    chains = _gps_strip_shared_ant_suffix_to_primary(
        chains, chain_bands=chain_bands, join_anchors=join_anchors,
    )
    chains = _gps_drop_dominated_chains(chains)

    out: list = []
    seen: set[tuple[str, ...]] = set()
    for refs in chains:
        if len(refs) < 2:
            continue
        key = tuple(refs)
        if key in seen:
            continue
        seen.add(key)
        raw_hint, band = _hint_and_band(refs)
        # 截尾后仍尽量继承未截前的 band
        if not band or band == "GPS":
            for full_key, b in chain_bands.items():
                if len(full_key) >= len(refs) and full_key[: len(refs)] == key:
                    band = b or band
                    break
        # 多点焊盘：匹配 RLCT 在 FIL 前（预览序与注入前一致）
        refs = reorder_matching_rlct_before_multiport(
            refs, schematic_res, None,
        )
        devices = [make_device(r, schematic_res) for r in refs]
        # 末端网名标成天线
        if devices and is_ant_path_node(devices[-1].ref):
            devices[-1].is_ant = True
            devices[-1].is_ic = False
        # 入口对腿黄标（与 mark_copad 叠加，不依赖是否检出完整夹心）
        for d in devices:
            if d.ref in entry_legs:
                d.is_copad = True
        out.append(
            Path(
                ant_num="GPS",
                devices=devices,
                band=band,
                direction="RX",
                kind="GPS",
            )
        )

    out.sort(
        key=lambda p: (
            # 主链（含 ANT）优先，便于 L2/unique 以 L1 为干
            0 if (p.devices and is_ant_path_node(p.devices[-1].ref)) else 1,
            p.band or "",
            -(len(p.devices)),
            p.devices[0].ref if p.devices else "",
            p.devices[-1].ref if p.devices else "",
        )
    )
    return out

