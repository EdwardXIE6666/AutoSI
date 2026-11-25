"""功能5 — 【阶段 A · 路径中间产物】路径搭建（BAND/TRX/GPS；COPAD/COLAY 见 refnum_copad）。

本模块产出内存 Path / CoPadStructure / CoLayStructure（阶段 A）。
手册最终 refnum（阶段 B）由 export_repair_manual 调用 refnum.layout.pipeline。

职责：
1. 频段 Path（ANT 页）：reverse_edges → ANT…→PA/LNA/QPM/LPAMiF（止于前端有源，不接 TRX）
2. TRX→PA/LNA 匹配 Path：独立 TRX_PA_LNA 页（MAIN find_LNA_PA_RFM）
3. GPS/GNSS Path：MAIN GPS_ALL_PATHS → 默认同 GPS 页；若终到已有 BAND 页的 ANTn
   （ANT 口兼 GPS），按 GPS 管线构图并入该 ANT 页（去掉 reverse 残链）
   —— 实现见 refnum_path_gps（本文件 re-export，行为不变）
4. 并联/对地 RLCT 挂到主链锚点旁
5. Co-pad 结构判定；完整成组后 orphan 焊盘 → Co-lay

图原语：refnum_graph.py；GPS：refnum_path_gps.py；COPAD/COLAY：refnum_copad.py；
预览：refnum_path_preview.py
MAIN 兼容入口：export_repair_manual(...) → A 预览 + B 手册构图（B1 起）。
产物命名见 docs/功能5总设计.md §1。
"""
from __future__ import annotations

import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from refnum.graph import (  # noqa: E402
    Device,
    build_net_device_maps,
    device_graph_from_signal_infos,
    device_has_gnd_net,
    drop_refs_keeping_series,
    canonical_ant_id,
    is_ant_path_node,
    same_ant_port,
    is_connector_ref,
    is_coupler_ref,
    is_cross_ant_isolation_net,
    is_gnd_net,
    is_jump_isolation_net,
    is_multiport_anchor,
    is_network,
    is_pa_lna_ref,
    is_plausible_comp_ref,
    is_rlc,
    is_rlct,
    is_series_rlct,
    is_shunt_device,
    is_side_chain_anchor,
    is_transceiver_ref,
    make_device,
    nongnd_degree,
    pair_key,
    rlc_kind,
    signal_infos_of,
)


BUILD_ID = "2026-08-09-fbrx-prefer-trx-v1"

_BAND_RE = re.compile(r"(B\d+|n\d+|UHB|MHB|LHB)", re.I)
_RLC_REF_RE = re.compile(r"^[RLCT]\d", re.I)


def _is_band_chip_endpoint(ref: str, schematic_res) -> bool:
    """ANT 频段链芯片侧终点：PA / LNA / QPM / LPAMiF 等前端有源。

    Transceiver 不在此列——TRX↔前端匹配在 TRX_PA_LNA 页。
    """
    return is_pa_lna_ref(ref, schematic_res)


def _is_band_rf_front_node(ref: str, schematic_res) -> bool:
    """PA/LNA 射频侧（通向 ANT）的节点：FIL/开关/多工/频段网/无源/天线。"""
    if not ref:
        return False
    if is_transceiver_ref(ref, schematic_res):
        return False
    if _is_band_chip_endpoint(ref, schematic_res):
        return False
    if is_ant_path_node(ref) or is_rlct(ref, schematic_res):
        return True
    try:
        if schematic_res.is_network(ref):
            return True
    except Exception:
        pass
    d = make_device(ref, schematic_res)
    pt = (d.part_type or "").upper()
    if any(
        k in pt
        for k in (
            "FILTER",
            "SWITCH",
            "MULTIPLEX",
            "DIVIDER",
            "COUPLER",
            "DIPLEX",
            "DUPLEX",
        )
    ):
        return True
    ru = str(ref).upper()
    return bool(ru.startswith(("FIL", "LPF", "BPF")))


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

@dataclass
class Path:
    """一条射频链路。

    kind=BAND：devices 为 ANT-first（[0] 天线，[-1] 为 PA/LNA/QPM/LPAMiF；不含 TRX）。
    kind=TRX_PA_LNA：devices 为 TRX-first（[0] Transceiver，[-1] PA/LNA）。
    kind=GPS：devices 为 TRX/LNA-first（左芯片侧 → 右天线侧；可含 LNA 段拼接）。
    """
    ant_num: str
    devices: list[Device] = field(default_factory=list)
    band: str = ""
    direction: str = "TX"  # TX / RX / TRX
    kind: str = "BAND"  # BAND | TRX_PA_LNA | GPS


# ---------------------------------------------------------------------------
# 路径搭建
# ---------------------------------------------------------------------------


def _keep_path_node(node: str, schematic_res, ant_num: str = "") -> bool:
    """路径上保留的节点：非网络器件，或天线端点（可与 ant_num 对齐）。"""
    if not node:
        return False
    if not is_network(node, schematic_res):
        return True
    if is_ant_path_node(node):
        return True
    if ant_num and str(node).upper() == str(ant_num).upper():
        return True
    return False


def _direction_from_band(band: str) -> str:
    bu = (band or "").upper()
    if "TRX" in bu:
        return "TRX"
    if "TX" in bu:
        return "TX"
    if "RX" in bu or "DRX" in bu or "PRX" in bu:
        return "RX"
    return "TX"


def _has_clear_band_token(net: str) -> bool:
    """网络名是否含明显频段语义：Bx / nxx / UHB / MHB / LHB / HB|MB|LB 段名。

    **FBRX 取样网不算蜂窝频段 token**：PA/LPAMiF 上常挂 FBRX_HB_TX0 等脚，
    若当频段标签会把 UHB_MIMO_PRX 等 ANT 主链误标成 FBRX。
    纯 Coupler→TRX 取样链的 A 列由补链逻辑用网名含 FBRX 单独命名。
    """
    n = str(net or "").strip()
    if not n or n.startswith("$$$"):
        return False
    try:
        if is_gnd_net(n):
            return False
    except Exception:
        pass
    nu = n.upper()
    # Coupler/PA 反馈取样网：不是本 ANT 射频主链频段名
    if "FBRX" in nu:
        return False
    # 电源/数字控制网排除
    if re.search(
        r"(?:^|_)(?:VDD|VCC|VBAT|GND|AGND|CLK|SPI|I2C|GPIO|EN|RST|IRQ)(?:_|$)",
        nu,
    ):
        return False
    # 明显频段：B1 / B41 / B1+B3 / n41 / UHB / MHB / LHB
    if re.search(r"(?:^|_)(B\d+(?:\+B\d+)*|N\d+|UHB|MHB|LHB)(?:_|$)", nu):
        return True
    # 段名+方向：MHB_RX5 / LB_TX0（须带方向，避免误伤纯 HB 电源别名）
    if re.search(r"(?:^|_)(HB|MB|LB)(?:_|$)", nu) and re.search(
        r"(?:^|_)(TX|RX|PRX|DRX|TRX)", nu
    ):
        return True
    return False


def _band_walk_order_from_pa_lna(
    nodes: list[str],
    schematic_res,
) -> list[str]:
    """从 LNA/PA 往天线方向的器件序。

    BAND 路径存储为 ANT-first（[0]=ANT，[-1]=PA/LNA）→ 反转后从 PA/LNA 往 ANT。
    若未识别到前端有源，仍按「末→首」当作芯片侧在末。
    """
    refs = [str(n) for n in (nodes or []) if str(n or "").strip()]
    if not refs:
        return []
    # 找到最靠芯片侧的 PA/LNA（ANT-first 时靠近列表尾）
    pa_i = None
    for i in range(len(refs) - 1, -1, -1):
        if _is_band_chip_endpoint(refs[i], schematic_res):
            pa_i = i
            break
    if pa_i is None:
        # 无明确 PA/LNA：默认末器件为起点往首走
        return list(reversed(refs))
    # 从 PA/LNA 往 ANT（下标减小）
    return list(reversed(refs[: pa_i + 1]))


def _band_from_path_nets(
    nodes: list[str],
    schematic_res,
    signal_infos=None,
) -> str:
    """从 LNA/PA 往前扫网络名，命中首个明显频段网 → 返回**完整网络名**。

    boss 规则：
      1. 起点 = 路径上的 LNA/PA（前端有源）
      2. 往天线方向：优先看「本器件→下一器件」共网；无共名网再看本器件管脚网
      3. 网名含 Bx / UHB / MHB / LHB 等明显频段语义 → 取**整段网络名**
      4. 贴到该 path 行最左列（A 列标签）
    """
    sig = signal_infos_of(schematic_res, signal_infos)
    if not sig or not nodes:
        return ""
    try:
        _net_to_devs, dev_to_nets = build_net_device_maps(sig)
    except Exception:
        return ""

    def _net_key(n: str) -> tuple:
        s = str(n or "")
        nu = s.upper()
        has_b = 1 if re.search(r"B\d+", nu) else 0
        has_uhb = 1 if re.search(r"UHB|MHB|LHB", nu) else 0
        has_dir = 1 if re.search(r"TX|RX|PRX|DRX|TRX", nu) else 0
        return (-has_b, -has_uhb, -has_dir, s.upper())

    def _first_band_net(net_names: list[str]) -> str:
        for net in sorted(
            (str(x).strip() for x in net_names if str(x).strip()),
            key=_net_key,
        ):
            if _has_clear_band_token(net):
                return net
        return ""

    walk = _band_walk_order_from_pa_lna(nodes, schematic_res)
    path_has_ant = any(is_ant_path_node(str(n)) for n in (nodes or []))
    for i, ref in enumerate(walk):
        # 1) 与下一跳共网（更贴本 path 通路）
        if i + 1 < len(walk):
            nxt = walk[i + 1]
            shared = set(dev_to_nets.get(str(ref), []) or []) & set(
                dev_to_nets.get(str(nxt), []) or []
            )
            hit = _first_band_net(list(shared))
            if hit:
                return hit
        # 2) 本器件管脚网：ANT 主链上跳过仅挂在多端口上的旁路语义
        #    （FBRX 已在 _has_clear_band_token 排除；此处再滤一次保险）
        pin_nets = list(dev_to_nets.get(str(ref), []) or [])
        if path_has_ant:
            pin_nets = [
                n for n in pin_nets if "FBRX" not in str(n).upper()
            ]
        hit = _first_band_net(pin_nets)
        if hit:
            return hit
    return ""


def _band_guess(nodes: list[str], schematic_res, signal_infos=None) -> str:
    """频段标签：LNA/PA→ANT 扫网络名；否则器件描述 token。"""
    from_net = _band_from_path_nets(nodes, schematic_res, signal_infos)
    if from_net:
        return from_net
    for node in nodes:
        try:
            desc = str(schematic_res.get_part_desc(node) or "")
        except Exception:
            desc = ""
        m = _BAND_RE.search(desc) or _BAND_RE.search(str(node))
        if m:
            return m.group(1).upper()
    return ""


def _reverse_edge_nodes(reverse_edges) -> set[str]:
    """本 ANT reverse_edges 覆盖的节点集合（合并实体/链路范围）。"""
    nodes: set[str] = set()
    for edge in reverse_edges or []:
        if not edge or len(edge) < 2:
            continue
        nodes.add(str(edge[0]))
        nodes.add(str(edge[1]))
    return nodes


def _exclusive_nodes_by_ant(
    ant_cones: dict[str, set[str]],
) -> dict[str, set[str]]:
    """仅属于某一 ANT reverse 锥的节点（他锥没有）。"""
    owner: dict[str, set[str]] = defaultdict(set)
    for ant, cone in ant_cones.items():
        for n in cone:
            owner[n].add(ant)
    return {
        ant: {n for n in cone if len(owner.get(n, ())) == 1}
        for ant, cone in ant_cones.items()
    }


def _routes_from_edges(
    ant_num: str,
    reverse_edges,
    schematic_res,
    *,
    pin_parser=None,
    exclusive_by_ant: Optional[dict[str, set[str]]] = None,
    all_ant_names: Optional[list[str]] = None,
) -> list[list[str]]:
    """枚举本 ANT 的 BAND 串联主链（ANT-first）。

    ## 对齐功能1路径搜索（find_PA_LNA_ANT / find_LNA_PA）
    在 **网表** 上做回溯 DFS，而不是在 reverse_edges 多端口塌缩图上乱串：
    - 起点：本锥内芯片侧最外前端（PA/LNA/QPM/LPAMiF）
    - 穿越开关/SAW/分频器时按 **入脚→允许出脚** 约束（与功能1相同）
    - 命中本 ANT 口则收一条路径；禁止以其它 ANT 口收尾
    - 路径节点不得含「他 ANT 独占」器件（禁止他 ANT 链路进入本 ANT）

    reverse_edges 只用于：限定本锥节点 + 识别前端，**不作跨口假拼主链**。
    """
    _ = all_ant_names
    cone = _reverse_edge_nodes(reverse_edges)
    if not cone:
        return []

    exclusive_by_ant = exclusive_by_ant or {}
    sig = signal_infos_of(schematic_res)

    # 锥内前端（PA/LNA/QPM/LPAMiF）
    frontends = sorted(n for n in cone if _is_band_chip_endpoint(n, schematic_res))
    if not frontends:
        return []

    # 用 reverse 有向边估计「芯片侧最外」前端（中段级联 LNA 不作根）
    adj: dict[str, set[str]] = defaultdict(set)
    for s, t in reverse_edges or []:
        adj[str(s)].add(str(t))

    def _reach(a: str, b: str, lim: int = 80) -> bool:
        if a == b:
            return True
        q = deque([a])
        se = {a}
        nstep = 0
        while q and nstep < lim:
            u = q.popleft()
            nstep += 1
            for v in adj.get(u, ()):
                if v in se:
                    continue
                if v == b:
                    return True
                se.add(v)
                q.append(v)
        return False

    roots: list[str] = []
    for f in frontends:
        if not (_reach(f, ant_num) or any(
            same_ant_port(x, ant_num) and _reach(f, x) for x in cone
        )):
            roots.append(f)
            continue
        if any(
            f2 != f and _reach(f2, f)
            for f2 in frontends
        ):
            continue
        roots.append(f)
    if not roots:
        roots = list(frontends)

    # —— 网表 DFS（功能1 find_PA_LNA_ANT 同构，收窄到本 ANT）——
    try:
        from rf.RfPathTracing import find_PA_LNA_ANT
    except Exception:
        find_PA_LNA_ANT = None  # type: ignore

    raw_paths: list[list[str]] = []
    used_pa_lna_search = False
    if find_PA_LNA_ANT is not None and pin_parser is not None:
        used_pa_lna_search = True
        for f in roots:
            try:
                raw_paths.extend(
                    find_PA_LNA_ANT(
                        schematic_res, f, pin_parser, home_ant=ant_num,
                    ) or []
                )
            except TypeError:
                # 旧签名无 home_ant 时回退
                try:
                    raw_paths.extend(
                        find_PA_LNA_ANT(schematic_res, f, pin_parser) or []
                    )
                except Exception as e:
                    print(f"[功能5][PATH] find_PA_LNA_ANT({f}) 失败: {e}")
            except Exception as e:
                print(f"[功能5][PATH] find_PA_LNA_ANT({f}) 失败: {e}")
    else:
        raw_paths = _band_net_dfs_to_ant(
            ant_num, roots, cone, schematic_res, pin_parser=pin_parser,
        )

    # stub 补入的天线口常仅在 reverse 锥内可达：find_PA_LNA_ANT 可能 0 条
    # 以本 ANT 收尾 → 用**同一套脚约束**在锥内网表 DFS 兜底。
    # 禁止 pin_parser=None：无脚约束时开关/分频器全通，会在整张网表上
    # 指数枚举简单路径，密互连板会卡死。
    if used_pa_lna_search and not any(
        p and same_ant_port(p[-1], ant_num) for p in raw_paths
    ):
        raw_paths = _band_net_dfs_to_ant(
            ant_num, roots, cone, schematic_res, pin_parser=pin_parser,
        )

    rows: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    for raw in raw_paths:
        if not raw:
            continue
        end = str(raw[-1])
        if not same_ant_port(end, ant_num):
            continue

        devices: list[str] = []
        for x in raw:
            xs = str(x)
            if is_network(xs, schematic_res) and not is_ant_path_node(xs):
                continue
            if is_transceiver_ref(xs, schematic_res):
                devices = []
                break
            if not devices or devices[-1] != xs:
                devices.append(xs)
        if not devices:
            continue

        # 防御：器件序上电子开关不得直接相邻（矩阵 RFI 直连；串级中间有 RLC 仍保留）
        try:
            for i in range(len(devices) - 1):
                a, b = devices[i], devices[i + 1]
                if (
                    schematic_res.is_electronic_switch(a)
                    and schematic_res.is_electronic_switch(b)
                ):
                    devices = []
                    break
        except Exception:
            pass
        if not devices:
            continue

        bad = False
        for d in devices:
            for a, ex in exclusive_by_ant.items():
                if not same_ant_port(a, ant_num) and d in ex:
                    bad = True
                    break
            if bad:
                break
        if bad:
            continue

        for d in devices:
            if is_ant_path_node(d) and not same_ant_port(d, ant_num):
                bad = True
                break
        if bad:
            continue

        if not (is_ant_path_node(devices[-1]) or same_ant_port(devices[-1], ant_num)):
            continue
        # 芯片侧：须含 PA/LNA
        if not any(_is_band_chip_endpoint(d, schematic_res) for d in devices):
            continue

        # 存 ANT-first
        if _is_band_chip_endpoint(devices[0], schematic_res) or not (
            is_ant_path_node(devices[0]) or same_ant_port(devices[0], ant_num)
        ):
            devices = list(reversed(devices))

        # 钉死芯片端：最靠芯片的 PA/LNA
        fe_i = None
        for i in range(len(devices) - 1, -1, -1):
            if _is_band_chip_endpoint(devices[i], schematic_res):
                fe_i = i
                break
        if fe_i is None:
            continue
        devices = devices[: fe_i + 1]
        # 多点网可能把纯并联 hop 进串联；此处再剥一层（build_paths 仍会 inject）
        devices = _strip_series_pure_shunts(devices, schematic_res, sig)
        if not devices or not _is_band_chip_endpoint(devices[-1], schematic_res):
            continue

        key = tuple(devices)
        if key not in seen:
            seen.add(key)
            rows.append(devices)

    return _keep_shortest_per_ic_core(rows, schematic_res)


def _ic_core_refs(devices: list[str], schematic_res) -> tuple[str, ...]:
    return tuple(
        d for d in devices
        if d
        and not is_ant_path_node(d)
        and not is_rlct(d, schematic_res)
    )


def _rlc_refs(devices: list[str], schematic_res) -> frozenset[str]:
    return frozenset(
        d for d in devices
        if d and is_rlct(d, schematic_res)
    )


def _keep_shortest_per_ic_core(
    rows: list[list[str]],
    schematic_res,
) -> list[list[str]]:
    """丢掉「同一 IC 骨干上多出来的匹配件」排列，不合并不同匹配支路。

    可丢：RLC 集合是另一条的真超集（DFS 在同一链上多绕了 C/L）。
    须留：同一开关/滤波/PA，匹配件集合对不上（B3 TX0 / B1 TX0 各有匹配）。
    """
    kept: list[list[str]] = []
    for devices in rows:
        core = _ic_core_refs(devices, schematic_res)
        rlc = _rlc_refs(devices, schematic_res)
        drop_i = None
        drop_new = False
        for i, prev in enumerate(kept):
            if _ic_core_refs(prev, schematic_res) != core:
                continue
            prev_rlc = _rlc_refs(prev, schematic_res)
            if rlc == prev_rlc:
                if len(devices) < len(prev):
                    drop_i = i
                else:
                    drop_new = True
                break
            if prev_rlc < rlc:
                drop_new = True
                break
            if rlc < prev_rlc:
                drop_i = i
                break
        if drop_new:
            continue
        if drop_i is not None:
            kept[drop_i] = devices
            continue
        kept.append(devices)
    return kept


def _band_net_dfs_to_ant(
    ant_num: str,
    roots: list[str],
    cone: set[str],
    schematic_res,
    *,
    pin_parser=None,
) -> list[list[str]]:
    """功能1 风格网表 DFS 回退（无/有 PINParser 均可）。

    返回 raw 路径：[器件, 网, 器件, …, ANT]。
    """
    try:
        from rf.RfPathTracing import (
            extract_pin_number,
            get_other_category_pins_with_exclusion,
            is_pin_in_neighbor,
            resolve_electronic_switch_neighbor_pins,
        )
        from rf.SnpFileParser import ComponentDivider, SNPFileInfo
    except Exception:
        extract_pin_number = None  # type: ignore

    sig = signal_infos_of(schematic_res, None)
    network_dict: dict[str, tuple[list, list]] = {}
    node_to_networks: dict[str, list[str]] = defaultdict(list)
    for item in sig or []:
        if not item:
            continue
        net = str(item[0])
        nodes = list(item[1]) if len(item) > 1 else []
        pins = list(item[2]) if len(item) > 2 else []
        network_dict[net] = (nodes, pins)
        for n in nodes:
            node_to_networks[str(n)].append(net)

    cone_u = {str(x) for x in cone}
    # 锥内 + 与锥内节点共网的 RLCT（reverse 可能未展开匹配件）。
    # 不得写成「出现在任一网络里的节点」——那等于整张网表。
    extra_rlct: set[str] = set()
    for n in cone_u:
        for net in node_to_networks.get(n, []):
            nodes_on_net, _pins = network_dict.get(net, ([], []))
            for x in nodes_on_net:
                xs = str(x)
                if is_rlct(xs, schematic_res):
                    extra_rlct.add(xs)
    scope_u = cone_u | extra_rlct
    paths: list[list[str]] = []
    n2d_full, d2n_shunt = build_net_device_maps(sig or [])
    # 电子开关口侧缓存：FE↔ANT 贯通，禁 FE↔FE
    from refnum.graph import (
        esw_through_hop_allowed,
        classify_esw_port_sides,
    )

    _esw_sides_cache: dict[str, tuple[set[str], set[str]]] = {}
    _dfs_steps = 0
    _dfs_budget = 50_000
    _dfs_cut = False

    def _esw_sides(sw: str) -> tuple[set[str], set[str]]:
        if sw not in _esw_sides_cache:
            _esw_sides_cache[sw] = classify_esw_port_sides(
                sw, ant_num, n2d_full, d2n_shunt, schematic_res,
            )
        return _esw_sides_cache[sw]

    def _in_scope(n: str) -> bool:
        if is_ant_path_node(n):
            return same_ant_port(n, ant_num)
        return n in scope_u

    for start in roots:
        if start not in node_to_networks:
            continue
        visited: set[str] = set()

        def dfs(cur: str, path: list[str]) -> None:
            nonlocal _dfs_steps, _dfs_cut
            if _dfs_cut:
                return
            _dfs_steps += 1
            if _dfs_steps > _dfs_budget:
                if not _dfs_cut:
                    print(
                        f"[功能5][PATH] {ant_num} 锥内 DFS 超过 {_dfs_budget} 步，截断",
                        flush=True,
                    )
                _dfs_cut = True
                return
            if is_ant_path_node(cur) and same_ant_port(cur, ant_num):
                paths.append(path + [cur])
                return
            if is_ant_path_node(cur) and not same_ant_port(cur, ant_num):
                return
            # 中途撞上其它前端有源（非起点）则停，避免串到 TRX/别的 PA
            if (
                cur != start
                and _is_band_chip_endpoint(cur, schematic_res)
                and is_transceiver_ref(cur, schematic_res)
            ):
                return
            if cur in visited:
                return
            visited.add(cur)

            last_net = path[-1] if path and path[-1] in network_dict else ""
            for net in node_to_networks.get(cur, []):
                if net == last_net:
                    continue
                # 跨 ANT：JUMP / 锥外异口网名不穿越。
                # 锥内中间网（如 N77_TRX0_ANTx）是本口 reverse 已证明的拓扑，
                # 不得因网名带别的 ANT 号否决。精确他口仍由下面 is_ant_path_node 拦住。
                if is_cross_ant_isolation_net(net, ant_num) and str(net) not in cone_u:
                    continue
                # 蜂窝 RF 主链不走 FBRX 取样网（取样由 supplement_coupler_fbrx 专补）
                if "FBRX" in str(net).upper():
                    continue
                # 仅真实天线口（ANT0/ANT2…）收尾；ANT0_MHB_xxx 等中间网继续扩展
                if is_ant_path_node(net):
                    if same_ant_port(net, ant_num):
                        paths.append(path + [cur, net])
                    continue

                neighbor_pin = None
                if extract_pin_number is not None:
                    try:
                        if schematic_res.is_electronic_switch(cur) and pin_parser is not None:
                            pin_index = (
                                extract_pin_number(cur, network_dict[last_net])
                                if last_net in network_dict else None
                            )
                            neighbor_pin = resolve_electronic_switch_neighbor_pins(
                                schematic_res, cur, pin_index, pin_parser,
                            )
                        elif schematic_res.is_frequency_divider(cur):
                            pin_index = (
                                extract_pin_number(cur, network_dict[last_net])
                                if last_net in network_dict else None
                            )
                            if pin_index is not None:
                                divider = ComponentDivider(
                                    SNPFileInfo(
                                        schematic_res.get_part_type(cur),
                                        schematic_res.get_part_name(cur),
                                    ),
                                    cur,
                                )
                                neighbor_pin = divider.get_connected_pins(pin_index)
                        elif schematic_res.is_SAWFBAR(cur):
                            pin_index = (
                                extract_pin_number(cur, network_dict[last_net])
                                if last_net in network_dict else None
                            )
                            snp = SNPFileInfo(
                                schematic_res.get_part_type(cur),
                                schematic_res.get_part_name(cur),
                            )
                            if snp.pin_info and pin_index is not None:
                                neighbor_pin = get_other_category_pins_with_exclusion(
                                    snp.pin_info, pin_index,
                                    component_name=f"{cur}",
                                )
                    except Exception:
                        neighbor_pin = None

                nodes_on_net, _pins = network_dict.get(net, ([], []))
                # 路径上上一器件（跳过网名）
                prev_dev = ""
                for x in reversed(path):
                    xs = str(x)
                    if xs in network_dict:
                        continue
                    prev_dev = xs
                    break
                # 多点网：不把纯并联当串联跳点（对地件不得夹进贯通主链）
                for neighbor in nodes_on_net:
                    neighbor = str(neighbor)
                    if neighbor == cur:
                        continue
                    if not _in_scope(neighbor) and not is_ant_path_node(neighbor):
                        continue
                    if is_shunt_device(neighbor, schematic_res, d2n_shunt):
                        continue
                    if not _multipad_series_hop_ok(
                        cur, neighbor, n2d_full, d2n_shunt, schematic_res,
                        home_ant=ant_num,
                    ):
                        continue
                    # 禁止电子开关→电子开关（DPDT/SRS 矩阵跨口），主链只走天线↔前端
                    try:
                        if (
                            schematic_res.is_electronic_switch(cur)
                            and schematic_res.is_electronic_switch(neighbor)
                        ):
                            continue
                    except Exception:
                        pass
                    # 电子开关：只允许 FE 口↔ANT 公共口贯通（禁 FE↔FE 串台）
                    try:
                        if (
                            schematic_res.is_electronic_switch(cur)
                            and prev_dev
                        ):
                            sides = _esw_sides(cur)
                            if not esw_through_hop_allowed(
                                cur,
                                prev_dev,
                                neighbor,
                                ant_num,
                                n2d_full,
                                d2n_shunt,
                                schematic_res,
                                sides=sides,
                            ):
                                continue
                    except Exception:
                        pass
                    pin_constrained = False
                    try:
                        pin_constrained = (
                            schematic_res.is_electronic_switch(cur)
                            or schematic_res.is_frequency_divider(cur)
                            or schematic_res.is_SAWFBAR(cur)
                        )
                    except Exception:
                        pin_constrained = False
                    if pin_constrained and neighbor_pin is not None:
                        try:
                            if not is_pin_in_neighbor(
                                cur, network_dict[net], neighbor_pin,
                            ):
                                continue
                        except Exception:
                            pass
                    dfs(neighbor, path + [cur, net])

            visited.discard(cur)

        dfs(start, [])

    return paths


def _finalize_band_to_frontend(
    ant_first_refs: list[str],
    schematic_res,
    signal_infos=None,
    *,
    allowed_nodes: Optional[set[str]] = None,
    required_root: Optional[str] = None,
) -> list[str]:
    """整理 ANT 频段串联主链：无 TRX；芯片端=前端有源（优先 required_root）。

    ANT-first：[0] 侧靠天线 … [-1] 前端有源。不延伸到 Transceiver。
    """
    _ = (signal_infos, allowed_nodes)
    if not ant_first_refs:
        return ant_first_refs
    refs = [
        r for r in ant_first_refs
        if not is_transceiver_ref(r, schematic_res)
    ]
    if not refs:
        return []

    # 强制芯片端为 DFS 根（若给定）
    if required_root and required_root in refs:
        for i in range(len(refs) - 1, -1, -1):
            if refs[i] == required_root:
                refs = refs[: i + 1]
                break
        if refs[-1] != required_root:
            return []
        return refs

    # 否则：最靠芯片侧的 PA/LNA
    end_i = None
    for i in range(len(refs) - 1, -1, -1):
        if _is_band_chip_endpoint(refs[i], schematic_res):
            end_i = i
            break
    if end_i is not None:
        return refs[: end_i + 1]

    # 链上尚无前端：仅允许沿 RLCT 接到最近 PA/LNA（不到 TRX、不串 FIL 树）
    if signal_infos is None:
        return []
    adj = device_graph_from_signal_infos(schematic_res, signal_infos)
    start = refs[-1]
    blocked = set(refs[:-1])
    allow = set(allowed_nodes) if allowed_nodes is not None else None
    if allow is not None:
        allow = {str(x) for x in allow}
        allow.update(str(x) for x in refs)
    q: deque[tuple[str, list[str]]] = deque([(start, [])])
    seen = {start}
    found: Optional[list[str]] = None
    while q:
        node, trail = q.popleft()
        for nxt in sorted(adj.get(node, ())):
            if nxt in seen or nxt in blocked:
                continue
            if is_transceiver_ref(nxt, schematic_res) or is_ant_path_node(nxt):
                continue
            if _is_band_chip_endpoint(nxt, schematic_res):
                found = trail + [nxt]
                q.clear()
                break
            if not is_rlct(nxt, schematic_res):
                continue
            if allow is not None and str(nxt) not in allow:
                continue
            seen.add(nxt)
            q.append((nxt, trail + [nxt]))
        if found is not None:
            break
    if found:
        return refs + found
    return []  # 禁止无前端有源的链（被动件不得当头）


def build_trx_pa_lna_paths(
    transceiver_paths,
    schematic_res,
) -> list[Path]:
    """Transceiver→PA/LNA 匹配链路，单独成 Path 行（不并入频段）。

    transceiver_paths：MAIN `find_LNA_PA_RFM` 等产出的交替序列
    [器件, 网络, 器件, 网络, …, PA/LNA]。此处只保留器件位号，顺序不改写。
    无数据或筛完为空则返回 []。
    """
    if not transceiver_paths:
        return []
    out: list[Path] = []
    seen: set[tuple[str, ...]] = set()
    for raw in transceiver_paths:
        if not raw:
            continue
        # 去掉网络名，保留器件序（与原理图 DFS 一致，不重排中间件）
        refs = [
            str(n) for n in raw
            if n and not is_network(str(n), schematic_res)
        ]
        # 去连续重复位号（个别图会 dual-visit）
        cleaned: list[str] = []
        for r in refs:
            if not cleaned or cleaned[-1] != r:
                cleaned.append(r)
        refs = cleaned
        if len(refs) < 2:
            continue
        # 仅当整段反了（末 TRX、首 PA）时反转；禁止中间乱序
        if is_transceiver_ref(refs[-1], schematic_res) and is_pa_lna_ref(
            refs[0], schematic_res
        ):
            refs = list(reversed(refs))
        if not is_transceiver_ref(refs[0], schematic_res):
            continue
        if not is_pa_lna_ref(refs[-1], schematic_res):
            continue
        # 中间不得再出现第二个 TRX（截断到首个 PA/LNA 终点）
        end_i = len(refs) - 1
        for j in range(1, len(refs)):
            if is_pa_lna_ref(refs[j], schematic_res):
                end_i = j
                break
        refs = refs[: end_i + 1]
        if len(refs) < 2:
            continue
        key = tuple(refs)
        if key in seen:
            continue
        seen.add(key)
        devices = [make_device(r, schematic_res) for r in refs]
        ep = devices[-1]
        direction = "RX" if "LNA" in (ep.part_type or "").upper() else "TX"
        out.append(
            Path(
                ant_num="TRX_PA_LNA",
                devices=devices,  # TRX-first，与原理图一致
                band=f"TRX→{ep.ref}",
                direction=direction,
                kind="TRX_PA_LNA",
            )
        )
    return out


# ---------------------------------------------------------------------------
# GPS/GNSS（实现见 refnum_path_gps；此处 re-export，保持公开 import 稳定）
# ---------------------------------------------------------------------------
from refnum.path_gps import (  # noqa: E402
    _ANT_PORT_NUM_RE,
    _band_sheet_ant_port_nums,
    _gps_path_home_ant,
    _gps_restrip_paths_shared_suffix,
    _should_skip_gps_sheet,
    build_gps_paths,
)


def _reverse_preserving_shunt_blocks(devices: list[Device]) -> list[Device]:
    """ANT-first → 左芯片→右ANT：反转主链序，但**连续并联块内部保序**。

    朴素 reverse 会把「两颗相邻并联」相对顺序也颠倒。
    连续 is_shunt 段当作原子，只反转段序列；块内序=注入时已排好的 RF 方向序。
    """
    if not devices:
        return []
    segments: list[list[Device]] = []
    i = 0
    n = len(devices)
    while i < n:
        if getattr(devices[i], "is_shunt", False):
            j = i + 1
            while j < n and getattr(devices[j], "is_shunt", False):
                j += 1
            segments.append(list(devices[i:j]))
            i = j
        else:
            segments.append([devices[i]])
            i += 1
    segments.reverse()
    out: list[Device] = []
    for seg in segments:
        out.extend(seg)
    return out


def path_display_devices(path: Path) -> list[Device]:
    """预览显示顺序：左芯片侧 → 右天线/PA。

    BAND：存储为 ANT-first，显示时反转主链；连续并联块内部不颠倒。
    TRX_PA_LNA / GPS：已是芯片在左，原样。
    """
    if path.kind in ("TRX_PA_LNA", "GPS"):
        return list(path.devices)
    return _reverse_preserving_shunt_blocks(list(path.devices))


def _shunt_leg_to_gnd(
    start_ref: str,
    entry_net: str,
    schematic_res,
    net_to_devs: dict[str, list[str]],
    dev_to_nets: dict[str, list[str]],
    *,
    main_set: set[str],
    max_hops: int = 8,
    extra_net_to_devs: Optional[dict[str, list[str]]] = None,
    extra_dev_to_nets: Optional[dict[str, list[str]]] = None,
) -> Optional[list[str]]:
    """从主链旁 RLCT 走出「纯 RLCT → 地」旁路腿（RF 侧 → 地侧序）。

    覆盖：
      - 单颗对地并联（deg=1）
      - 两颗及以上串联到地（C/L 串到地；中间只有 RLCT，无 IC/主链）
    失败：岔路、碰到主链/有源/连接器、开路 stub。

    extra_*：全量网表，仅用于补全「末端对地串」上被 RF 过滤丢掉的网。
    peer 发现仍用主网表，
    避免电源去耦整网灌入。
    """
    if not start_ref or start_ref in main_set:
        return None
    if is_connector_ref(start_ref) or is_ant_path_node(start_ref):
        return None
    if not is_rlct(start_ref, schematic_res):
        return None
    if _is_band_chip_endpoint(start_ref, schematic_res):
        return None

    def _nets_of(ref: str) -> list[str]:
        out: list[str] = []
        seen_n: set[str] = set()
        for src in (dev_to_nets, extra_dev_to_nets):
            if not src:
                continue
            for n in src.get(ref, []) or []:
                ns = str(n)
                if not ns or ns in seen_n:
                    continue
                seen_n.add(ns)
                out.append(ns)
        return out

    def _devs_on(net: str) -> list[str]:
        out: list[str] = []
        seen_d: set[str] = set()
        for src in (net_to_devs, extra_net_to_devs):
            if not src:
                continue
            for p in src.get(net, []) or []:
                ps = str(p)
                if not ps or ps in seen_d:
                    continue
                seen_d.add(ps)
                out.append(ps)
        return out

    def _terminates_gnd(ref: str) -> bool:
        """是否对地收口（含 is_parallel / GND 网 / 合并网表后 shunt）。"""
        try:
            if schematic_res.is_parallel(ref):
                return True
        except Exception:
            pass
        if device_has_gnd_net(ref, dev_to_nets):
            return True
        if extra_dev_to_nets and device_has_gnd_net(ref, extra_dev_to_nets):
            return True
        if is_shunt_device(ref, schematic_res, dev_to_nets):
            return True
        if extra_dev_to_nets and is_shunt_device(
            ref, schematic_res, {ref: _nets_of(ref)}
        ):
            return True
        return False

    # 单颗并联且无 extra 可延伸时直接返回
    if is_shunt_device(start_ref, schematic_res, dev_to_nets):
        if not extra_dev_to_nets:
            return [start_ref]
        # 有 extra：先尝试完整串；失败再退回单颗
        fallback_single = [start_ref]
    else:
        fallback_single = None

    chain: list[str] = [start_ref]
    cur = start_ref
    prev_net = str(entry_net)
    seen = {start_ref}

    for _ in range(max_hops):
        nets = [n for n in _nets_of(cur) if not is_gnd_net(n)]
        others = [n for n in nets if n != prev_net]
        if _terminates_gnd(cur) and not others:
            return chain
        if len(others) != 1:
            if _terminates_gnd(cur) and not others:
                return chain
            return fallback_single if (fallback_single and len(chain) == 1) else None
        next_net = others[0]
        # 对地腿的中间网只应连 RLCT 与地。跨进天线口网说明这条支路是那个口的
        # 天线馈电匹配（异口臂），不是本链的对地旁路：馈点连接器会被下面的
        # peer 过滤掉，只剩一只对地 RLCT，形状与真对地串腿雷同。
        # 不得把异口匹配段画进本口链路。
        if is_ant_path_node(next_net):
            return fallback_single if (fallback_single and len(chain) == 1) else None
        peers = [
            str(p)
            for p in _devs_on(next_net)
            if str(p) != cur
            and str(p) not in seen
            and not is_connector_ref(p)
            and not (
                is_network(p, schematic_res) and not is_ant_path_node(p)
            )
            and not is_gnd_net(p)
        ]
        if len(peers) != 1:
            return fallback_single if (fallback_single and len(chain) == 1) else None
        nxt = peers[0]
        if nxt in main_set:
            return None
        if not is_rlct(nxt, schematic_res):
            return None
        if _is_band_chip_endpoint(nxt, schematic_res):
            return None
        if is_ant_path_node(nxt):
            return None
        chain.append(nxt)
        seen.add(nxt)
        prev_net = next_net
        cur = nxt
        if _terminates_gnd(cur):
            return chain
    return fallback_single if (fallback_single and len(chain) == 1) else None


def _is_coupler_ref(ref: str, schematic_res) -> bool:
    """射频耦合器（Coupler）。"""
    if not ref or is_rlct(ref, schematic_res):
        return False
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    return "COUPLER" in pt


def _allows_private_shunt_pin_nets(ref: str, schematic_res) -> bool:
    """是否允许扫 multiport **终端型**私有对地脚（未用口 50Ω 等）。

    领域：Coupler / FIL·多工 常把未用 TX 口用单颗 R/C 对地；
    该脚网不与主链共网，但须进构图。

    不含 PA/LNA/TRX/开关。具体脚网再经「单 peer 且 is_shunt」收拢，
    禁止把滤波两侧匹配链（多 RLCT 私有网）整段刷进主链。
    """
    if not ref or is_rlct(ref, schematic_res):
        return False
    if is_pa_lna_ref(ref, schematic_res) or is_transceiver_ref(ref, schematic_res):
        return False
    try:
        if schematic_res is not None and schematic_res.is_electronic_switch(ref):
            return False
    except Exception:
        pass
    if _is_coupler_ref(ref, schematic_res):
        return True
    ru = str(ref or "").strip().upper()
    if ru.startswith(("FIL", "LPF", "BPF")):
        return True
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    # 仅滤波/多工/耦合语义；不含 DIVIDER/FREQUENCY 分频（多天线口易误挂）
    return any(
        k in pt
        for k in (
            "FILTER", "MULTIPLEX", "SAW", "BAW",
            "DIPLEX", "DUPLEX", "COUPLER",
        )
    )


def _rlct_reaches_fe_short(
    ref: str,
    schematic_res,
    net_to_devs: dict,
    dev_to_nets: dict,
    *,
    ban_net: str = "",
    max_depth: int = 2,
) -> bool:
    """ref 离开 ban_net 后 ≤max_depth 是否碰到 PA/LNA（主 FE 臂判定）。"""
    from collections import deque

    q: deque[tuple[str, int]] = deque()
    visited: set[str] = {str(ref)}
    for net in dev_to_nets.get(ref, []) or []:
        ns = str(net)
        if not ns or is_gnd_net(ns) or ns == str(ban_net):
            continue
        if is_jump_isolation_net(ns):
            continue
        for d in net_to_devs.get(ns, []) or []:
            ds = str(d)
            if not ds or ds in visited:
                continue
            visited.add(ds)
            q.append((ds, 1))
    guard = 0
    while q and guard < 64:
        guard += 1
        cur, depth = q.popleft()
        if depth > max_depth:
            continue
        if is_pa_lna_ref(cur, schematic_res) or is_transceiver_ref(
            cur, schematic_res,
        ):
            return True
        if not is_rlct(cur, schematic_res) and not is_shunt_device(
            cur, schematic_res, dev_to_nets,
        ):
            # 已碰到非被动，若非 FE 则停扩此枝
            continue
        for net in dev_to_nets.get(cur, []) or []:
            ns = str(net)
            if (
                not ns
                or is_gnd_net(ns)
                or ns == str(ban_net)
                or is_jump_isolation_net(ns)
            ):
                continue
            for d in net_to_devs.get(ns, []) or []:
                ds = str(d)
                if not ds or ds in visited:
                    continue
                visited.add(ds)
                q.append((ds, depth + 1))
    return False


def _allow_multipad_shunt_after_anchor(
    anchor: str,
    shunt: str,
    pad_net: str,
    main: list[str],
    schematic_res,
    net_to_devs: dict,
    dev_to_nets: dict,
    *,
    pad_series: list[str],
) -> bool:
    """多串联 RLCT 焊盘上并地件挂接门禁。

    **星型 FE/旁路盘**：
      焊盘上存在「短跳可达 PA/LNA」的 FE 臂时：
        - 本 path 含 FE 臂 → 只挂在 path 上第一颗 FE 臂后
        - 本 path 仅旁路入口+ANT、无 FE 臂 → **不挂**（并地件不得跨到另一条旁路入口）

    **Co-pad 合并盘 / 中段共网**：
      焊盘串联件**都不是** FE 臂（短跳碰不到前端）→ 不是星型旁路语义，
      按普通共网挂并位，**禁止**因「fe_arms 空」整网禁挂。
    """
    _ = shunt
    series = {str(x) for x in pad_series}
    on_path = [str(x) for x in main if str(x) in series]
    if len(on_path) < 2:
        # 单臂路过：仍允许（无主轴对可判）
        return True
    # 整盘 FE 臂（不限本 path）——有无 FE 才谈「旁路-only 禁挂」
    fe_on_pad = {
        r
        for r in series
        if _rlct_reaches_fe_short(
            r, schematic_res, net_to_devs, dev_to_nets, ban_net=pad_net,
        )
    }
    if not fe_on_pad:
        # 合并盘/中段共网：无 FE/旁路角色可分 → 允许在共网锚点后挂
        return True
    fe_on_path = [r for r in on_path if r in fe_on_pad]
    if not fe_on_path:
        # 焊盘有 FE，但本 path 未走 FE 臂（仅旁路入口）→ 不挂
        return False
    # 只挂在 path 上第一颗 FE 臂之后
    preferred = fe_on_path[0]
    for r in main:
        if str(r) in fe_on_pad:
            preferred = str(r)
            break
    return str(anchor) == preferred


def _strip_series_pure_shunts(
    refs: list[str],
    schematic_res,
    signal_infos=None,
) -> list[str]:
    """从串联主链去掉纯并联 RLCT（deg=1 对地），主链只留贯通件。

    多点网 DFS 会把对地件当成两颗贯通主链件之间的串联跳点，
    导致 FE 显示把并联塞进耦合器「前面」。剥离后由 ``_inject_parallel_on_path``
    按旁路语义重新挂回。
    """
    if not refs:
        return []
    sig = signal_infos_of(schematic_res, signal_infos)
    _, d2n = build_net_device_maps(sig or [])
    return [r for r in refs if not is_shunt_device(r, schematic_res, d2n)]


def _reorder_matching_rlct_before_multiport(
    refs: list[str],
    schematic_res,
    signal_infos=None,
) -> list[str]:
    """多点焊盘上：匹配串联 RLCT 须在 FIL/开关 multiport **之前**（FE→ANT）。

    错序：匹配 RLCT 被排到 FIL 之后、且与下游 IC 无共网。
    正序：同网上先匹配 RLCT 再滤波。

    规则：若 multiport 后方出现与其共非地网的非并联 RLCT，且该 RLCT
    与 multiport 之后的下一贯通件无共网，则把该 RLCT 整段挪到 multiport 前。
    """
    if not refs or len(refs) < 3:
        return list(refs)
    sig = signal_infos_of(schematic_res, signal_infos)
    n2d, d2n = build_net_device_maps(sig or [])
    out = list(refs)

    def _nongnd_nets(r: str) -> set[str]:
        return {
            str(n)
            for n in (d2n.get(r) or [])
            if n and not is_gnd_net(str(n))
        }

    def _share(a: str, b: str) -> bool:
        return bool(_nongnd_nets(a) & _nongnd_nets(b))

    changed = True
    guard = 0
    while changed and guard < len(out) * 3:
        guard += 1
        changed = False
        for i, r in enumerate(out):
            if not is_multiport_anchor(r, schematic_res):
                continue
            if is_ant_path_node(r):
                continue
            # 向后找可挪的匹配 RLCT
            j = i + 1
            while j < len(out):
                cand = out[j]
                if is_shunt_device(cand, schematic_res, d2n):
                    j += 1
                    continue
                if not is_rlct(cand, schematic_res):
                    break
                if not _share(r, cand):
                    j += 1
                    continue
                # cand 之后的贯通件：若 cand 与之无共网，说明 cand 不是离开 multiport 的出口
                nxt = None
                for k in range(j + 1, len(out)):
                    t = out[k]
                    if is_shunt_device(t, schematic_res, d2n):
                        continue
                    nxt = t
                    break
                if nxt is not None and _share(cand, nxt):
                    # cand 是通向 nxt 的串联 → 保持
                    j += 1
                    continue
                # 挪到 multiport 前
                item = out.pop(j)
                out.insert(i, item)
                changed = True
                break
            if changed:
                break
    return out


def _inject_parallel_on_path(
    main_refs: list[str],
    schematic_res,
    signal_infos,
    *,
    shared_multiports: Optional[set[str]] = None,
    multiport_pin_mode: str = "band",
    part_xy: Optional[dict[str, tuple[float, float]]] = None,
    ant_first: bool = True,
) -> tuple[list[str], set[str]]:
    """在串联主链上挂并联/对地旁路，紧挨共享信号网的主链器件。

    返回 (新序列, 旁路件位号集合)。主链相对顺序不变。

    旁路形态：
      - 单颗对地并联 RLCT
      - 多颗串联到地的纯 RLCT 腿（C/L 串到地；序=RF→地）

    同锚点多腿：腿首位号稳定序（同网多并联相对序不强制精排，可接受）。
    multiport_pin_mode / part_xy：兼容旧调用（part_xy 忽略）。

    **旁路落点（硬）**：
      - **多点焊盘**（同 RF 网 ≥2 颗主链器件）：
        并联夹在两主链之间。ant_first 插在更靠 ANT 的主链件**之后**
        → FE 显示：更靠芯片 — 并联 — 更靠 ANT
        （匹配 RLCT 须在 FIL 之前）。
      - **单主链 multiport 终端旁路**（仅 1 颗主链共网）：ant_first 时
        插 multiport 前 → FE 在 multiport 后。
      - **ant_first=False**（TRX/GPS）：旁路插锚点后。

    **多端口优先认领**：同网扫描时先 multiport，再串联 RLCT。
    **多端口默认只扫路径局部共网**；Coupler/FIL 另扫终端型私有对地脚。
    """
    _ = shared_multiports
    _ = multiport_pin_mode
    _ = part_xy
    if not main_refs:
        return main_refs, set()
    net_to_devs, dev_to_nets = build_net_device_maps(signal_infos or [])
    # 全量网仅用于补全对地串联链（不参与主链 peer 扫描，防电源去耦灌入）
    extra_n2d: Optional[dict[str, list[str]]] = None
    extra_d2n: Optional[dict[str, list[str]]] = None
    all_raw = getattr(schematic_res, "all_signal_infos_raw", None)
    if all_raw and all_raw is not signal_infos:
        try:
            extra_n2d, extra_d2n = build_net_device_maps(all_raw)
        except Exception:
            extra_n2d, extra_d2n = None, None
    main = list(main_refs)
    main_set = set(main)
    used = set(main_set)
    # anchor_index -> 若干旁路腿（每腿 RF→地序）
    inserts_after: dict[int, list[list[str]]] = defaultdict(list)
    inserts_before: dict[int, list[list[str]]] = defaultdict(list)

    def _main_idxs_on_net(net: str) -> list[int]:
        out_i: list[int] = []
        for i, d in enumerate(main):
            nets_d = set(str(n) for n in (dev_to_nets.get(d) or []) if n)
            if d in net_to_devs and not nets_d:
                # 偶发：网名即器件索引
                pass
            if str(net) in nets_d:
                out_i.append(i)
                continue
            # 主链件出现在 net 的节点表
            if d in (net_to_devs.get(net) or []):
                out_i.append(i)
        return out_i

    def _leg_ok(peer: str, net: str) -> Optional[list[str]]:
        if peer in used or peer in main_set:
            return None
        if is_connector_ref(peer):
            return None
        if is_network(peer, schematic_res) and not is_ant_path_node(peer):
            return None
        if _is_band_chip_endpoint(peer, schematic_res):
            return None
        leg = _shunt_leg_to_gnd(
            peer,
            net,
            schematic_res,
            net_to_devs,
            dev_to_nets,
            main_set=main_set,
            extra_net_to_devs=extra_n2d,
            extra_dev_to_nets=extra_d2n,
        )
        if not leg:
            return None
        if any(x in used or x in main_set for x in leg):
            return None
        return leg

    def _path_local_nets(anchor: str, nets: list[str]) -> list[str]:
        """与主链另一器件共网的 pin。"""
        local: list[str] = []
        for n in nets:
            if is_gnd_net(n):
                continue
            peers = set(net_to_devs.get(n, [])) - {anchor}
            if peers & main_set:
                local.append(n)
        return local

    def _private_terminator_shunt_nets(anchor: str, nets: list[str]) -> list[str]:
        """终端型私有脚（收拢）：恰好 1 个主链外 peer，且为对地并联。

        覆盖未用口 50Ω/51R、Coupler 单颗对地 R/C。
        不覆盖两侧匹配（多颗 L/C 共网）——那些应走主链共网或侧支，不得当私有旁路刷入。
        """
        out: list[str] = []
        for n in nets:
            if is_gnd_net(n):
                continue
            peers = set(net_to_devs.get(n, [])) - {anchor}
            if len(peers) != 1 or (peers & main_set):
                continue
            p = next(iter(peers))
            if is_shunt_device(p, schematic_res, dev_to_nets):
                out.append(n)
        return out

    def _place_leg(anchor_i: int, net: str, leg: list[str]) -> None:
        """按焊盘形态决定 before/after。"""
        mids = _main_idxs_on_net(net)
        if len(mids) >= 2:
            # 多点焊盘：旁路夹在主链件之间。
            # chip-left（GPS/TRX）min=FE 侧；ANT-first min=ANT 侧；
            # 都插在「更靠路径起点」的主链件之后 → 落在两主链件之间，禁止甩到 FIL 后。
            inserts_after[min(mids)].append(leg)
            return
        d = main[anchor_i]
        if (
            ant_first
            and is_multiport_anchor(d, schematic_res)
            and not is_ant_path_node(d)
            and len(mids) <= 1
        ):
            # 单主链 multiport 终端旁路：FE 显示在 multiport 后
            inserts_before[anchor_i].append(leg)
            return
        inserts_after[anchor_i].append(leg)

    def _scan_anchor(i: int, d: str) -> None:
        nets_to_scan = list(dev_to_nets.get(d, []))
        if d in net_to_devs and d not in nets_to_scan:
            nets_to_scan.append(d)
        if is_multiport_anchor(d, schematic_res):
            local = _path_local_nets(d, nets_to_scan)
            if _allows_private_shunt_pin_nets(d, schematic_res):
                # 局部共网 ∪ 终端型私有对地脚（去重保序）
                seen_n: set[str] = set()
                merged: list[str] = []
                for n in local + _private_terminator_shunt_nets(d, nets_to_scan):
                    if n not in seen_n:
                        seen_n.add(n)
                        merged.append(n)
                nets_to_scan = merged
            else:
                nets_to_scan = local
        else:
            nets_to_scan = [n for n in nets_to_scan if not is_gnd_net(n)]
        for net in nets_to_scan:
            if is_gnd_net(net):
                continue
            peers = set(net_to_devs.get(net, [])) - {d}
            # 多 RLCT 星型焊盘上的并地件：只挂在「主 FE 臂」上，
            # 并地件只挂本支路串联入口，不得跨到另一条旁路入口
            pad_series = [
                str(x)
                for x in (net_to_devs.get(net, []) or [])
                if is_rlct(str(x), schematic_res)
                and not is_shunt_device(str(x), schematic_res, dev_to_nets)
            ]
            multi_pad = len(set(pad_series)) >= 3
            for p in peers:
                if p in main_set:
                    continue
                if is_connector_ref(p):
                    continue
                if is_network(p, schematic_res) and not is_ant_path_node(p):
                    continue
                if multi_pad and is_shunt_device(
                    str(p), schematic_res, dev_to_nets,
                ):
                    if not _allow_multipad_shunt_after_anchor(
                        str(d),
                        str(p),
                        str(net),
                        main,
                        schematic_res,
                        net_to_devs,
                        dev_to_nets,
                        pad_series=pad_series,
                    ):
                        continue
                leg = _leg_ok(p, net)
                if not leg:
                    continue
                if any(x in used for x in leg):
                    continue
                _place_leg(i, str(net), leg)
                used.update(leg)

    # 两轮：先 multiport/IC，再串联 RLCT——同网旁路优先挂耦合器/开关
    for i, d in enumerate(main):
        if is_multiport_anchor(d, schematic_res):
            _scan_anchor(i, d)
    for i, d in enumerate(main):
        if not is_multiport_anchor(d, schematic_res):
            _scan_anchor(i, d)

    out: list[str] = []
    shunt_set: set[str] = set()
    for i, d in enumerate(main):
        before = sorted(
            inserts_before.get(i, []), key=lambda lg: (lg[0] if lg else ""),
        )
        after = sorted(
            inserts_after.get(i, []), key=lambda lg: (lg[0] if lg else ""),
        )
        for leg in before:
            for p in leg:
                if p not in shunt_set:
                    out.append(p)
                    shunt_set.add(p)
        out.append(d)
        for leg in after:
            for p in leg:
                if p not in shunt_set:
                    out.append(p)
                    shunt_set.add(p)
    return out, shunt_set


def _refs_to_devices(
    refs: list[str],
    schematic_res,
    shunt_set: Optional[set[str]] = None,
) -> list[Device]:
    shunt_set = shunt_set or set()
    devices: list[Device] = []
    for r in refs:
        d = make_device(r, schematic_res)
        if r in shunt_set:
            d.is_shunt = True
            if not d.is_ic:
                d.is_passive = True
        devices.append(d)
    return devices


def devices_from_refs(
    refs: list[str],
    schematic_res,
    signal_infos,
    *,
    ant_first: bool = True,
    keep_main_order: bool = False,
    **inject_kw,
) -> list[Device]:
    """并联注入后把位号序列变成 Device。

    keep_main_order：注入若打乱主链相对序，则回退为原 refs（BAND/TRX）。
    """
    cells2, shunt_set = _inject_parallel_on_path(
        list(refs), schematic_res, signal_infos, ant_first=ant_first, **inject_kw,
    )
    if keep_main_order:
        main_after = [r for r in cells2 if r not in shunt_set]
        if main_after != list(refs):
            cells2, shunt_set = list(refs), set()
    return _refs_to_devices(cells2, schematic_res, shunt_set)


def _device_peers_nongnd(
    cur: str,
    n2d: dict,
    d2n: dict,
    *,
    home_ant: str = "",
    skip_fbrx_nets: bool = False,
    schematic_res=None,
) -> list[str]:
    """cur 经非地邻接器件。

    **隔绝**：
      - 网名含 JUMP → 禁止穿越
      - 星型焊盘：异口臂（远侧整口 ≠ 本页）不得从本口 FE/ANT 臂跳过去
      - 异口 ANTx 网名不再整网封死：本口侧焊盘仍须能走含本口号的中间网；
        对侧 FIL/ANT 由星型门禁拦住

    skip_fbrx_nets：蜂窝 RF 主链/补链不走 FBRX 取样网。
    """
    peers: list[str] = []
    seen: set[str] = set()
    home = str(home_ant or "").strip() or None
    for net in d2n.get(cur, []) or []:
        ns = str(net)
        if not ns or is_gnd_net(ns):
            continue
        if is_jump_isolation_net(ns):
            continue
        if skip_fbrx_nets and "FBRX" in ns.upper():
            continue
        for d in n2d.get(ns, []) or []:
            ds = str(d)
            if not ds or ds == cur or ds in seen:
                continue
            if not is_plausible_comp_ref(ds) and not is_ant_path_node(ds):
                continue
            if schematic_res is not None and home:
                if not _multipad_series_hop_ok(
                    cur, ds, n2d, d2n, schematic_res, home_ant=str(home),
                ):
                    continue
            seen.add(ds)
            peers.append(ds)
    peers.sort()
    return peers


def _is_electronic_switch_ref(ref: str, schematic_res) -> bool:
    try:
        return bool(schematic_res is not None and schematic_res.is_electronic_switch(ref))
    except Exception:
        return False


def _is_rf_body_mid(
    ref: str,
    schematic_res,
    *,
    allow_mid_filter: bool = True,
) -> bool:
    """主路/补链共用的 RF 躯干中间件（非终点、非墙）。

    与 ``_band_net_dfs_to_ant`` 可进入类型对齐：
      RLCT、滤波/分频、**Coupler 贯通**、电子开关（调用方禁开关互连）。
    不进 TRX / 非射频杂散 IC。
    """
    if is_rlct(ref, schematic_res):
        return True
    if is_transceiver_ref(ref, schematic_res) or is_pa_lna_ref(ref, schematic_res):
        return False
    if is_ant_path_node(ref):
        return False
    if is_coupler_ref(ref, schematic_res):
        return True
    if allow_mid_filter and is_side_chain_anchor(ref, schematic_res):
        return True
    if _is_electronic_switch_ref(ref, schematic_res):
        return True
    return False


def _shortest_walk(
    start: str,
    goal_pred,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    home_ant: str = "",
    block: Optional[set[str]] = None,
    max_depth: int = 14,
    allow_mid_filter: bool = True,
    stop_at_other_fe: bool = True,
) -> list[str] | None:
    """器件 BFS 至**第一个** goal_pred（单目标最短）。"""
    walks = _walks_to_goals(
        start,
        goal_pred,
        n2d,
        d2n,
        schematic_res,
        home_ant=home_ant,
        block=block,
        max_depth=max_depth,
        allow_mid_filter=allow_mid_filter,
        stop_at_other_fe=stop_at_other_fe,
        all_goals=False,
    )
    return walks[0] if walks else None


def _multipad_series_hop_ok(
    cur: str,
    nxt: str,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    home_ant: str = "",
) -> bool:
    """焊盘上允许的串联跳。

    1. **滤波/multiport 焊盘**：共网有滤波 hub 且 ≥2 只串联 RLCT 臂
       → 臂不得互跳，须进 hub。
       普通 π（两只 L、无滤波）仍可 L→L。
    2. **星型（≥3 串联 RLCT）**：ANT→side / FE↔side 禁止。
    """
    if not cur or not nxt or cur == nxt:
        return True
    # 找共网且为多 RLCT 星型的 pad
    nets_c = {str(n) for n in (d2n.get(cur) or []) if n and not is_gnd_net(n)}
    nets_n = {str(n) for n in (d2n.get(nxt) or []) if n and not is_gnd_net(n)}
    shared = nets_c & nets_n
    if not shared:
        return True
    try:
        from refnum.copad import pad_rlct_arm_role
    except Exception:
        return True
    for net in shared:
        members = [str(x) for x in (n2d.get(net) or []) if x]
        series = [
            x
            for x in members
            if is_rlct(x, schematic_res)
            and not is_shunt_device(x, schematic_res, d2n)
        ]
        hub = [
            x
            for x in members
            if is_side_chain_anchor(x, schematic_res)
            or is_multiport_anchor(x, schematic_res)
        ]
        if (
            len(set(series)) >= 2
            and hub
            and cur in series
            and nxt in series
            and cur != nxt
        ):
            return False
        if len(set(series)) < 3:
            continue
        if cur not in series and nxt not in series:
            # 并地/中间件与 RLCT 共网：不挡
            if is_shunt_device(cur, schematic_res, d2n) or is_shunt_device(
                nxt, schematic_res, d2n,
            ):
                continue
            continue
        roles = {
            r: pad_rlct_arm_role(
                r, net, schematic_res, d2n, n2d, home_ant=home_ant,
            )
            for r in set(series)
        }
        # 把 cur/nxt 映射到最近 series 角色（自身或同网）
        rc = roles.get(cur)
        rd = roles.get(nxt)
        if rc is None or rd is None:
            continue
        # ANT→side / FE→side / side→FE：逆向或旁路抄道
        if rc == "ant" and rd == "side":
            return False
        if rc == "fe" and rd == "side":
            return False
        if rc == "side" and rd == "fe":
            return False
    return True


def _walks_to_goals(
    start: str,
    goal_pred,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    home_ant: str = "",
    block: Optional[set[str]] = None,
    max_depth: int = 14,
    allow_mid_filter: bool = True,
    stop_at_other_fe: bool = True,
    all_goals: bool = True,
    skip_fbrx_nets: bool = True,
) -> list[list[str]]:
    """器件 BFS 至 goal_pred。

    可穿越语义与主路 BAND 躯干对齐（见 ``_is_rf_body_mid``）：
      - RLCT / 滤波 multiport / **Coupler** / 电子开关
      - **禁止开关→开关**（与主路 DFS 相同）
      - **默认不走 FBRX 网**（取样开关矩阵不是蜂窝 RF 贯通）
      - **星型焊盘禁止 ANT→side 逆向**
      - 不进 Transceiver

    all_goals=True：每个首次到达的 goal 各留一条最短路径（不在第一个
    FE 处停整图——否则只走出最短支，丢掉同 FE 的其它出口）。
    all_goals=False：与旧 ``_shortest_walk`` 相同，找到第一条即停。
    """
    if not start:
        return []
    blocked = set(block or ())
    q: deque[tuple[str, list[str]]] = deque([(start, [start])])
    visited: set[str] = {start} | blocked
    guard = 0
    found: list[list[str]] = []
    seen_goals: set[str] = set()
    while q and guard < 1200:
        guard += 1
        cur, path = q.popleft()
        if len(path) > max_depth:
            continue
        for ds in _device_peers_nongnd(
            cur, n2d, d2n, home_ant=home_ant, skip_fbrx_nets=skip_fbrx_nets,
            schematic_res=schematic_res,
        ):
            if ds in visited:
                continue
            # 星型焊盘：禁止 ANT→side 逆向跳。
            # 并地中间件不改角色：用路径上上一颗串联 RLCT 判跳，
            # 避免经并地件绕过门禁。
            hop_from = cur
            if is_shunt_device(cur, schematic_res, d2n):
                for x in reversed(path):
                    if is_rlct(x, schematic_res) and not is_shunt_device(
                        x, schematic_res, d2n,
                    ):
                        hop_from = x
                        break
            if not _multipad_series_hop_ok(
                hop_from, ds, n2d, d2n, schematic_res, home_ant=home_ant,
            ):
                continue
            if goal_pred(ds):
                if ds not in seen_goals:
                    seen_goals.add(ds)
                    found.append(path + [ds])
                    if not all_goals:
                        return found
                # goal 不展开
                visited.add(ds)
                continue
            if is_ant_path_node(ds):
                continue
            if is_transceiver_ref(ds, schematic_res):
                continue
            # 纯并联不得作串联跳点（与 band DFS / find_PA_LNA_ANT 一致）
            if is_shunt_device(ds, schematic_res, d2n):
                continue
            if is_pa_lna_ref(ds, schematic_res):
                # 非 goal 的 FE：按 stop_at_other_fe
                if stop_at_other_fe:
                    continue
                visited.add(ds)
                q.append((ds, path + [ds]))
                continue
            if not _is_rf_body_mid(
                ds, schematic_res, allow_mid_filter=allow_mid_filter,
            ):
                continue
            # 开关→开关禁止（主路不变量）
            if (
                _is_electronic_switch_ref(ds, schematic_res)
                and _is_electronic_switch_ref(cur, schematic_res)
            ):
                continue
            visited.add(ds)
            q.append((ds, path + [ds]))
    return found


def _page_multiport_anchors(
    paths: list[Path],
    copad_structures: Optional[list],
    schematic_res,
    ant_num: str,
) -> set[str]:
    """本 ANT 页上的滤波 multiport：主路器件 ∪ 与主路相交的 Co-pad 夹心/支路。"""
    path_refs: set[str] = set()
    out: set[str] = set()
    for p in paths:
        if getattr(p, "kind", "BAND") != "BAND":
            continue
        if not same_ant_port(p.ant_num, ant_num):
            continue
        for d in p.devices:
            r = str(d.ref or "")
            if not r:
                continue
            path_refs.add(r)
            if is_side_chain_anchor(r, schematic_res):
                out.add(r)
    for st in copad_structures or []:
        # 归属：仅叉/并腿落在本页 path（勿用远侧支路 RLCT 误挂他结构）
        pad_legs: set[str] = set()
        members: set[str] = set()
        for pair_attr in ("fork", "merge"):
            pair = getattr(st, pair_attr, None)
            if pair is None:
                continue
            for a in (getattr(pair, "leg_a", ""), getattr(pair, "leg_b", "")):
                if a:
                    pad_legs.add(str(a))
                    members.add(str(a))
        for attr in (
            "trunk_before", "branch1", "branch2", "trunk_after",
            "internal_chain", "fork_pad", "merge_pad",
        ):
            for x in getattr(st, attr, ()) or ():
                if x:
                    members.add(str(x))
        if not (pad_legs & path_refs):
            continue
        for r in members:
            if is_side_chain_anchor(r, schematic_res):
                out.add(r)
    return out


def _copad_pad_legs_touching_mp(
    multiport: str,
    copad_structures: Optional[list],
    path_refs: set[str],
) -> set[str]:
    """若 multiport 落在本页 Co-pad 结构内，返回该结构叉/并腿（夹心对腿）。"""
    out: set[str] = set()
    mp = str(multiport)
    for st in copad_structures or []:
        pad_legs: set[str] = set()
        members: set[str] = set()
        for pair_attr in ("fork", "merge"):
            pair = getattr(st, pair_attr, None)
            if pair is None:
                continue
            for a in (getattr(pair, "leg_a", ""), getattr(pair, "leg_b", "")):
                if a:
                    pad_legs.add(str(a))
                    members.add(str(a))
        for attr in (
            "trunk_before", "branch1", "branch2", "trunk_after",
            "internal_chain", "fork_pad", "merge_pad",
        ):
            for x in getattr(st, attr, ()) or ():
                if x:
                    members.add(str(x))
        if mp not in members:
            continue
        if not (pad_legs & path_refs):
            continue
        out |= pad_legs
    return out


def _primary_ant_suffix_after_copad(
    ant_paths: list[Path],
    ant_num: str,
    pad_legs: set[str],
    multiport: str,
) -> list[str] | None:
    """主路 ANT-first 上、夹心对腿靠天线一侧的后缀（不含对腿/夹心 multiport）。

    夹心对腿靠天线一侧的干路后缀。补链只接这段，避免把夹心叉腿拉进补链主串破坏 Co-pad。
    """
    best: list[str] | None = None
    for p in ant_paths:
        refs = [str(d.ref) for d in p.devices if d.ref]
        if not refs:
            continue
        if not any(same_ant_port(r, ant_num) or is_ant_path_node(r) for r in refs):
            continue
        # ANT-first：从左扫到第一个 pad_leg 之前
        suffix: list[str] = []
        hit_pad = False
        for r in refs:
            if r in pad_legs or r == multiport:
                hit_pad = True
                break
            suffix.append(r)
        if not hit_pad and pad_legs:
            # 主路无对腿时：整条到 FE 前皆可作后缀候选（少见）
            continue
        if not suffix:
            continue
        if not any(same_ant_port(r, ant_num) or is_ant_path_node(r) for r in suffix):
            continue
        if best is None or len(suffix) > len(best):
            best = suffix
    return best


def _mp_to_ant_tail(
    multiport: str,
    ant_num: str,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    ant_paths: Optional[list] = None,
    copad_structures: Optional[list] = None,
    path_refs: Optional[set[str]] = None,
) -> list[str] | None:
    """multiport → … → ANT。

    若 multiport 是本页 Co-pad 夹心件：尾段 = [mp] + 主路夹心靠天线后缀
    （不把叉/并腿串进补链，保护 Co-pad 夹心）。
    否则网表最短走到 ANT。
    """
    ant_u = str(ant_num).upper()
    pref = set(path_refs or ())
    pad_legs = _copad_pad_legs_touching_mp(
        multiport, copad_structures, pref,
    )
    if pad_legs and ant_paths:
        # 夹心 multiport：ANT 干路只留在主 UHB 行，补链不要
        # 再抄一份后缀，否则 unique/占格会把干路吸到 B25/B66，夹心接不上干路。
        # 补链只钉 multiport + 本 ANT 口，与宿主在 FIL 列对齐，干路仍接在夹心右侧。
        return [multiport, ant_u]

    def goal(n: str) -> bool:
        return same_ant_port(n, ant_num)

    block = set(pad_legs)  # 有夹心时仍避免穿对腿乱走
    walk = _shortest_walk(
        multiport,
        goal,
        n2d,
        d2n,
        schematic_res,
        home_ant=ant_num,
        block=block or None,
        allow_mid_filter=True,
        stop_at_other_fe=True,
        max_depth=12,
    )
    if walk and goal(walk[-1]):
        return walk
    ant_net_devs: set[str] = set()
    for k, devs in (n2d or {}).items():
        if same_ant_port(k, ant_num):
            ant_net_devs.update(str(d) for d in (devs or []))

    def goal_dev(n: str) -> bool:
        return n in ant_net_devs

    walk2 = _shortest_walk(
        multiport,
        goal_dev,
        n2d,
        d2n,
        schematic_res,
        home_ant=ant_num,
        block=block or None,
        allow_mid_filter=True,
        stop_at_other_fe=True,
        max_depth=12,
    )
    if walk2:
        return walk2 + [ant_u]
    return None


def _fe_arms_from_multiport_free_ports(
    multiport: str,
    ant_tail: list[str],
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    home_ant: str,
    reserved_legs: Optional[set[str]] = None,
) -> list[list[str]]:
    """multiport 未用于去 ANT / 非 Co-pad 对腿 的 RF 口 → 到 PA/LNA 的串联。

    返回 chip-first 前缀：**不含** multiport，以 FE 开头、以贴 multiport 的腿结尾。
    例：[[FE, 腿A, FIL, 对侧腿], [FE, 腿B, FIL, 对侧腿]]

    reserved_legs：Co-pad 叉/并腿上的口不算自由口，
    避免再补一条经夹心的假 UHB 把 Co-pad 拆掉。
    """
    if not ant_tail or ant_tail[0] != multiport:
        return []
    reserved = set(reserved_legs or ())
    ant_next = ant_tail[1] if len(ant_tail) > 1 else ""
    ant_side_nets: set[str] = set()
    if ant_next:
        for n in d2n.get(multiport, []) or []:
            ns = str(n)
            if is_gnd_net(ns) or is_cross_ant_isolation_net(ns, home_ant):
                continue
            if ant_next in (n2d.get(ns, []) or []):
                ant_side_nets.add(ns)
    # 对腿共网一律保留给 Co-pad / 主 UHB
    for leg in reserved:
        for n in d2n.get(multiport, []) or []:
            ns = str(n)
            if is_gnd_net(ns) or is_cross_ant_isolation_net(ns, home_ant):
                continue
            if leg in (n2d.get(ns, []) or []):
                ant_side_nets.add(ns)

    # 自由口：非 ANT/夹心侧网上的 RLCT（禁 JUMP/异口网）
    seed_by_net: list[tuple[str, str]] = []  # (net, leg)
    for net in d2n.get(multiport, []) or []:
        ns = str(net)
        if (
            not ns
            or is_gnd_net(ns)
            or is_cross_ant_isolation_net(ns, home_ant)
            or ns in ant_side_nets
        ):
            continue
        for d in n2d.get(ns, []) or []:
            ds = str(d)
            if (
                ds
                and ds != multiport
                and ds not in reserved
                and is_plausible_comp_ref(ds)
                and is_rlct(ds, schematic_res)
            ):
                seed_by_net.append((ns, ds))

    arms: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    block_ant = set(ant_tail)

    def _expand_mid_exits(series_fe_last: list[str]) -> list[list[str]]:
        """series: leg→…→FE；展开中间 FIL 其它出口。返回 leg→…→FE 列表。"""
        if len(series_fe_last) < 3:
            return [series_fe_last]
        # 统一为 leg→FE
        out = [list(series_fe_last)]
        seen_l = {tuple(series_fe_last)}
        for i, node in enumerate(series_fe_last):
            if i == 0 or i >= len(series_fe_last) - 1:
                continue
            if not is_side_chain_anchor(node, schematic_res):
                continue
            prev, nxt = series_fe_last[i - 1], series_fe_last[i + 1]
            entry_nets = set()
            primary_nets = set()
            for n in d2n.get(node, []) or []:
                ns = str(n)
                if is_gnd_net(ns) or is_cross_ant_isolation_net(ns, home_ant):
                    continue
                devs = set(str(x) for x in (n2d.get(ns, []) or []))
                if prev in devs:
                    entry_nets.add(ns)
                if nxt in devs:
                    primary_nets.add(ns)
            for n in d2n.get(node, []) or []:
                ns = str(n)
                if (
                    not ns
                    or is_gnd_net(ns)
                    or is_cross_ant_isolation_net(ns, home_ant)
                    or ns in entry_nets
                    or ns in primary_nets
                ):
                    continue
                for d in n2d.get(ns, []) or []:
                    leg = str(d)
                    if (
                        not leg
                        or leg == node
                        or leg in series_fe_last
                        or not is_rlct(leg, schematic_res)
                    ):
                        continue
                    # leg → FE，不再穿中间 FIL
                    tail = _shortest_walk(
                        leg,
                        lambda x, _sr=schematic_res: is_pa_lna_ref(x, _sr),
                        n2d,
                        d2n,
                        schematic_res,
                        home_ant=home_ant,
                        block=set(series_fe_last[: i + 1]) | block_ant | {prev},
                        allow_mid_filter=False,
                        stop_at_other_fe=False,
                        max_depth=8,
                    )
                    if not tail or not is_pa_lna_ref(tail[-1], schematic_res):
                        continue
                    full = list(series_fe_last[: i + 1]) + list(tail)
                    key = tuple(full)
                    if key not in seen_l:
                        seen_l.add(key)
                        out.append(full)
        return out

    for _net, leg in seed_by_net:
        # 每个可达 FE 一条最短臂（勿只取全局最短，否则丢同 FE 其它出口）
        walks = _walks_to_goals(
            leg,
            lambda x, _sr=schematic_res: is_pa_lna_ref(x, _sr),
            n2d,
            d2n,
            schematic_res,
            home_ant=home_ant,
            block=block_ant | {multiport},
            allow_mid_filter=True,
            stop_at_other_fe=False,
            max_depth=16,
            all_goals=True,
        )
        for walk in walks:
            if not walk or not is_pa_lna_ref(walk[-1], schematic_res):
                continue
            for ser in _expand_mid_exits(walk):
                # ser: leg→…→FE  →  chip-first 前缀 FE→…→leg
                chip = list(reversed(ser))
                key = tuple(chip)
                if key in seen:
                    continue
                seen.add(key)
                arms.append(chip)
    return arms


def _supplement_band_via_page_multiports(
    paths: list[Path],
    copad_structures: Optional[list],
    schematic_res,
    signal_infos=None,
) -> list[Path]:
    """本页滤波 multiport 的未占用 RF 口若通向另一 PA/LNA，补 **正常 BAND 主链**。

    不是侧支：夹心 multiport 未占用 RF 口通向另一 PA/LNA 时补正常 BAND 主链。
    **保护 Co-pad**：夹心 multiport 的叉/并腿不进补链主串，
    ANT 尾接主路夹心靠天线后缀；对腿口不算自由口（不另补假 UHB）。
    """
    if schematic_res is None or not paths:
        return []
    sig = signal_infos_of(schematic_res, signal_infos)
    n2d, d2n = build_net_device_maps(sig)
    by_ant: dict[str, list[Path]] = defaultdict(list)
    for p in paths:
        if getattr(p, "kind", "BAND") == "BAND":
            by_ant[str(p.ant_num)].append(p)

    existing: set[tuple[str, tuple[str, ...]]] = set()
    for p in paths:
        if getattr(p, "kind", "") != "BAND":
            continue
        key = (
            str(p.ant_num),
            tuple(d.ref for d in p.devices if d.ref),
        )
        existing.add(key)

    out: list[Path] = []
    for ant, ant_paths in sorted(by_ant.items()):
        path_refs = {
            str(d.ref)
            for p in ant_paths
            for d in p.devices
            if d.ref
        }
        mps = _page_multiport_anchors(
            ant_paths, copad_structures, schematic_res, ant,
        )
        for mp in sorted(mps):
            pad_legs = _copad_pad_legs_touching_mp(
                mp, copad_structures, path_refs,
            )
            # 主路已占用的 multiport 邻接腿不再当自由口
            path_touch_legs: set[str] = set()
            for p in ant_paths:
                refs = [str(d.ref) for d in p.devices if d.ref]
                if mp not in refs:
                    continue
                i = refs.index(mp)
                for j in (i - 1, i + 1):
                    if 0 <= j < len(refs) and refs[j] != mp:
                        path_touch_legs.add(refs[j])
            ant_tail = _mp_to_ant_tail(
                mp, ant, n2d, d2n, schematic_res,
                ant_paths=ant_paths,
                copad_structures=copad_structures,
                path_refs=path_refs,
            )
            if not ant_tail or ant_tail[0] != mp:
                continue
            # 夹心对腿 / 主路已用腿 不得当自由口
            arms = _fe_arms_from_multiport_free_ports(
                mp, ant_tail, n2d, d2n, schematic_res,
                home_ant=ant,
                reserved_legs=pad_legs | path_touch_legs,
            )
            if not arms:
                continue
            # ant_tail: mp → … → ANT；chip 前缀: FE → … → leg
            # 全链 chip-first: FE…leg + mp + ant_tail[1:]
            # 存 ANT-first: reverse；ant_tail 不含 Co-pad 对腿
            for chip_prefix in arms:
                if not chip_prefix or not is_pa_lna_ref(
                    chip_prefix[0], schematic_res
                ):
                    continue
                full_chip = list(chip_prefix) + [mp] + list(ant_tail[1:])
                # 去重相邻
                compact: list[str] = []
                for r in full_chip:
                    if not compact or compact[-1] != r:
                        compact.append(r)
                if not any(is_ant_path_node(x) for x in compact):
                    continue
                if not any(is_pa_lna_ref(x, schematic_res) for x in compact):
                    continue
                # ANT-first
                ant_first = list(reversed(compact))
                # 钉 FE 在末
                fe_i = None
                for i in range(len(ant_first) - 1, -1, -1):
                    if is_pa_lna_ref(ant_first[i], schematic_res):
                        fe_i = i
                        break
                if fe_i is None:
                    continue
                ant_first = ant_first[: fe_i + 1]
                key = (ant, tuple(ant_first))
                if key in existing:
                    continue
                # 与已有 path 主链器件集合高度重合则跳过
                ref_set = set(ant_first)
                dup = False
                for p in ant_paths:
                    pref = {d.ref for d in p.devices if d.ref}
                    if pref and len(ref_set & pref) >= max(3, len(ref_set) - 1):
                        # 仅当 FE 也相同才算重复
                        pfe = [
                            d.ref for d in p.devices
                            if is_pa_lna_ref(d.ref, schematic_res)
                        ]
                        if pfe and pfe[-1] == ant_first[-1]:
                            dup = True
                            break
                if dup:
                    continue
                # 多点网 DFS 可能把纯并联塞进串联 → 先剥离再 inject
                main_cells = _strip_series_pure_shunts(
                    list(ant_first), schematic_res, sig,
                )
                if not main_cells or not is_pa_lna_ref(
                    main_cells[-1], schematic_res,
                ):
                    continue
                # 夹心补链：并联注入不得把主 UHB 天线干路
                # 再吸进 B25/B66，否则夹心右侧干路被抽空
                host_ant_trunk: set[str] = set()
                if pad_legs:
                    au = str(ant).upper()
                    host_ant_trunk = {
                        x
                        for x in (
                            _primary_ant_suffix_after_copad(
                                ant_paths, ant, pad_legs, mp,
                            ) or []
                        )
                        if x
                        and not is_ant_path_node(x)
                        and str(x).upper() != au
                    }
                cells2, shunt_set = _inject_parallel_on_path(
                    main_cells, schematic_res, sig, ant_first=True,
                )
                main_after = [r for r in cells2 if r not in shunt_set]
                if main_after != main_cells:
                    cells2, shunt_set = list(main_cells), set()
                # 夹心补链：去掉误吸入的主 UHB 天线干路件
                if host_ant_trunk:
                    # 干路件可能同时是本链穿多端口的必经中间件，剔除不得挖断链
                    cells2 = drop_refs_keeping_series(cells2, host_ant_trunk, d2n)
                    shunt_set = {s for s in shunt_set if s in set(cells2)}
                devices = _refs_to_devices(cells2, schematic_res, shunt_set)
                if any(d.is_sdr for d in devices):
                    continue
                band = _band_guess(main_cells, schematic_res, signal_infos=sig)
                path = Path(
                    ant_num=ant,
                    devices=devices,
                    band=band or ant,
                    direction=_direction_from_band(band),
                    kind="BAND",
                )
                out.append(path)
                existing.add(key)
                ant_paths.append(path)

    return out


def _walk_coupler_sample_to_chip(
    start: str,
    coupler: str,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    entry_net: str,
    max_depth: int = 12,
) -> list[str] | None:
    """从 Coupler 耦合口邻接件走到 TRX 或 PA/LNA（正常 FBRX 链路）。

    - 可经任意 RLCT 与 **级联电子开关**（二级 FBRX 选择：一级选耦合器口、
      二级选 TRX 输入口，是真实拓扑）
    - **一条取样链只属于一个耦合器**：中途碰到另一个 Coupler 即别的取样支路，
      不穿过去（互连的开关矩阵里会抓错取样口）。深度由 max_depth 兜底
    - **优先 TRX**（SDR 取样终点）；仅当无 TRX 可达时才落到 PA/LPAMiF
      （禁止第二级开关一步跳到 PA，丢掉开关→RLCT→TRX 真取样链）
    - 开关上禁止 **一步** 跳到 PA/LPAMiF（多 FBRX 口抢最短；须经 RLCT）
    - 返回 coupler-first 序： [coupler, …, chip]

    注：曾用「途经开关数 ≤1」收敛，但 start 不计数，于是耦合器直连开关能过、
    耦合器先过取样电阻的要计 2 次被拒。行为取决于耦合器旁第一个件是开关
    还是电阻，非设计意图。
    """
    if not start or start == coupler:
        return None
    _ = entry_net

    def _esw(ref: str) -> bool:
        try:
            return bool(schematic_res.is_electronic_switch(ref))
        except Exception:
            return False

    def _peers(cur: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for net in d2n.get(cur, []) or []:
            ns = str(net)
            if not ns or is_gnd_net(ns) or is_jump_isolation_net(ns):
                continue
            for d in n2d.get(ns, []) or []:
                ds = str(d)
                if not ds or ds == cur or ds in seen:
                    continue
                if not is_plausible_comp_ref(ds) and not is_ant_path_node(ds):
                    continue
                seen.add(ds)
                out.append(ds)
        out.sort()
        return out

    def _search(*, accept_pa: bool) -> list[str] | None:
        q: deque[tuple[str, list[str], int]] = deque([(start, [start], 0)])
        visited: set[str] = {start, coupler}
        guard = 0
        while q and guard < 600:
            guard += 1
            cur, path, n_sw = q.popleft()
            if len(path) > max_depth:
                continue
            for ds in _peers(cur):
                if ds in visited:
                    continue
                # 开关一步到 PA：始终跳过（保留经 RLCT 到 TRX / 其它脚）
                if _esw(cur) and is_pa_lna_ref(ds, schematic_res):
                    continue
                if is_transceiver_ref(ds, schematic_res):
                    return [coupler] + path + [ds]
                if accept_pa and is_pa_lna_ref(ds, schematic_res):
                    return [coupler] + path + [ds]
                if is_pa_lna_ref(ds, schematic_res):
                    # 第一趟只找 TRX：不把 PA 当途经点
                    continue
                if is_coupler_ref(ds, schematic_res):
                    # 另一个耦合器 = 别的取样支路，不得穿过去
                    continue
                if is_rlct(ds, schematic_res) or _esw(ds):
                    visited.add(ds)
                    q.append((ds, path + [ds], n_sw))
                    continue
        return None

    return _search(accept_pa=False) or _search(accept_pa=True)


def _emit_fbrx_band_path(
    *,
    ant: str,
    cpl: str,
    walk: list[str],
    ns: str,
    schematic_res,
    sig,
    existing: set[tuple[str, tuple[str, ...]]],
    ant_paths: list[Path],
    out: list[Path],
) -> None:
    """把一条 coupler…chip walk 落成 FBRX BAND Path（去重）。"""
    if not walk or len(walk) < 2:
        return
    body = list(walk[1:])
    cells_b, shunt_set = _inject_parallel_on_path(
        body, schematic_res, sig,
    )
    main_after = [r for r in cells_b if r not in shunt_set]
    if main_after != body:
        cells_b, shunt_set = body, set()
    cells2 = [cpl] + list(cells_b)
    chip = next(
        (
            r
            for r in reversed(cells2)
            if is_transceiver_ref(r, schematic_res)
            or is_pa_lna_ref(r, schematic_res)
        ),
        "",
    )
    if not chip:
        # 开关矩阵等：path 上非 Coupler 终点也算 FE 侧
        for r in reversed(cells2):
            if r != cpl and not is_coupler_ref(r, schematic_res):
                chip = r
                break
    if not chip:
        return
    compact: list[str] = []
    seen: set[str] = set()
    for r in cells2:
        if r == chip or r in seen:
            continue
        seen.add(r)
        compact.append(r)
    compact.append(chip)
    key = (ant, tuple(compact))
    if key in existing:
        return
    devices = _refs_to_devices(compact, schematic_res, shunt_set)
    band = (
        ns
        if "FBRX" in ns.upper()
        else (
            _band_guess(compact, schematic_res, signal_infos=sig) or "FBRX"
        )
    )
    path = Path(
        ant_num=ant,
        devices=devices,
        band=band,
        direction=_direction_from_band(band) or "TX",
        kind="BAND",
    )
    out.append(path)
    existing.add(key)
    ant_paths.append(path)


def _supplement_coupler_fbrx_band_paths(
    paths: list[Path],
    schematic_res,
    signal_infos=None,
) -> list[Path]:
    """Coupler 耦合口 → TRX/LPAMiF 取样链，补 **正常 BAND**。

    正向：主链已有 Coupler → 扫 FBRX 网邻接件走到 PA/TRX。
    **反向**（修 others 漏 FBRX）：主链已有 PA/LPAMiF/TRX → 经 FBRX 网发现
      尚未入 path 的 Coupler（否则取样链只出现在 others 残链里）。

    不是侧支：与主频段同等参与行分配 / 共享列 / 竖并。
    """
    if schematic_res is None or not paths:
        return []
    sig = signal_infos_of(schematic_res, signal_infos)
    n2d, d2n = build_net_device_maps(sig)
    by_ant: dict[str, list[Path]] = defaultdict(list)
    for p in paths:
        if getattr(p, "kind", "BAND") == "BAND":
            by_ant[str(p.ant_num)].append(p)

    existing: set[tuple[str, tuple[str, ...]]] = set()
    for p in paths:
        if getattr(p, "kind", "") != "BAND":
            continue
        existing.add(
            (str(p.ant_num), tuple(d.ref for d in p.devices if d.ref))
        )

    out: list[Path] = []
    # 全工程 path 位号：反向发现的 Coupler 若已在**任一** ANT 主链上，
    # 只补 FBRX 取样，禁止再灌 full RF 残臂到本 ANT
    global_path_refs: set[str] = {
        str(d.ref)
        for p in paths
        for d in (p.devices or [])
        if d.ref and getattr(p, "kind", "BAND") == "BAND"
    }
    for ant, ant_paths in sorted(by_ant.items()):
        path_refs = {
            str(d.ref) for p in ant_paths for d in p.devices if d.ref
        }
        couplers = sorted(
            r for r in path_refs if is_coupler_ref(r, schematic_res)
        )
        # —— 正向：path 上已有 Coupler ——
        for cpl in couplers:
            used_nets: set[str] = set()
            for p in ant_paths:
                refs = [str(d.ref) for d in p.devices if d.ref]
                if cpl not in refs:
                    continue
                i = refs.index(cpl)
                for j in (i - 1, i + 1):
                    if 0 <= j < len(refs):
                        nb = refs[j]
                        for net in d2n.get(cpl, []) or []:
                            ns = str(net)
                            if not ns or "FBRX" in ns.upper():
                                continue
                            if nb in (n2d.get(ns, []) or []):
                                used_nets.add(ns)
            for net in d2n.get(cpl, []) or []:
                ns = str(net)
                if (
                    not ns
                    or is_gnd_net(ns)
                    or is_jump_isolation_net(ns)
                    or ns in used_nets
                ):
                    continue
                fbrx_net = "FBRX" in ns.upper()
                peers = sorted(
                    str(d)
                    for d in (n2d.get(ns, []) or [])
                    if str(d) != cpl
                    and is_plausible_comp_ref(str(d))
                    and (
                        str(d) not in path_refs
                        or (
                            fbrx_net
                            and (
                                is_transceiver_ref(str(d), schematic_res)
                                or is_pa_lna_ref(str(d), schematic_res)
                            )
                        )
                    )
                )
                # 一个耦合口 = 一条取样链。同网多只取样 RLCT（分压/并联）
                # 各自起步会走出等长的等价链，只留最直接、序最小的一条。
                best: list[str] | None = None
                for d0 in peers:
                    walk = _walk_coupler_sample_to_chip(
                        d0, cpl, n2d, d2n, schematic_res, entry_net=ns,
                    )
                    if not walk or len(walk) < 2:
                        if is_transceiver_ref(d0, schematic_res) or is_pa_lna_ref(
                            d0, schematic_res
                        ):
                            walk = [cpl, d0]
                        else:
                            continue
                    if best is None or (len(walk), walk) < (len(best), best):
                        best = walk
                if best:
                    _emit_fbrx_band_path(
                        ant=ant,
                        cpl=cpl,
                        walk=best,
                        ns=ns,
                        schematic_res=schematic_res,
                        sig=sig,
                        existing=existing,
                        ant_paths=ant_paths,
                        out=out,
                    )

        # —— 反向：path 上非 Coupler 经 FBRX 网发现 Coupler（含开关矩阵）——
        orig_refs = set(path_refs)
        seeds = sorted(
            r for r in path_refs if not is_coupler_ref(r, schematic_res)
        )
        for seed in seeds:
            for net in d2n.get(seed, []) or []:
                ns = str(net)
                if not ns or "FBRX" not in ns.upper():
                    continue
                if is_gnd_net(ns) or is_jump_isolation_net(ns):
                    continue
                for d0 in sorted(str(d) for d in (n2d.get(ns, []) or [])):
                    if d0 == seed or not is_coupler_ref(d0, schematic_res):
                        continue
                    # 他 ANT 主链已有此 Coupler → 本 ANT 既不取样也不灌 full RF
                    if d0 in global_path_refs and d0 not in orig_refs:
                        continue
                    is_new_cpl = d0 not in orig_refs
                    walk = _walk_coupler_sample_to_chip(
                        seed, d0, n2d, d2n, schematic_res, entry_net=ns,
                    )
                    if not walk or len(walk) < 2:
                        walk = [d0, seed]
                    if walk[0] != d0:
                        if d0 in walk:
                            walk = walk[walk.index(d0) :]
                        else:
                            walk = [d0] + [x for x in walk if x != d0]
                    if seed not in walk:
                        walk = list(walk) + [seed]
                    _emit_fbrx_band_path(
                        ant=ant,
                        cpl=d0,
                        walk=walk,
                        ns=ns,
                        schematic_res=schematic_res,
                        sig=sig,
                        existing=existing,
                        ant_paths=ant_paths,
                        out=out,
                    )
                    path_refs.add(d0)
                    global_path_refs.add(d0)
                    # 本 ANT 新发现 Coupler：补完整 RF 贯通
                    if is_new_cpl:
                        _emit_full_rf_paths_for_coupler(
                            ant=ant,
                            cpl=d0,
                            n2d=n2d,
                            d2n=d2n,
                            schematic_res=schematic_res,
                            sig=sig,
                            existing=existing,
                            ant_paths=ant_paths,
                            out=out,
                            path_refs=path_refs,
                        )
                        global_path_refs.update(path_refs)

    return out


def _emit_full_rf_paths_for_coupler(
    *,
    ant: str,
    cpl: str,
    n2d: dict,
    d2n: dict,
    schematic_res,
    sig,
    existing: set[tuple[str, tuple[str, ...]]],
    ant_paths: list[Path],
    out: list[Path],
    path_refs: set[str],
) -> None:
    """新发现 Coupler 的非 FBRX 贯通：按 **两侧端口** 拼完整 BAND。

    禁止：
      - 只发 RLCT→Coupler 残臂（无 PA/LNA）
      - 只走耦合器一侧（有 PA 无对侧开关）

    做法：从耦合器每个非 FBRX、非并联 first-hop 分别 BFS；
    FE 臂与「对侧臂」（到本 ANT / 或其它 first-hop 长链）配对拼接。
    """
    if not cpl or not ant:
        return
    def _is_home_ant(r: str) -> bool:
        return same_ant_port(r, ant)

    def _is_fe(r: str) -> bool:
        return is_pa_lna_ref(r, schematic_res)

    # 耦合器 RF first-hop（跳过 FBRX / 纯并联）
    first_hops: list[str] = []
    seen_h: set[str] = set()
    for net in d2n.get(cpl, []) or []:
        ns = str(net)
        if (
            not ns
            or is_gnd_net(ns)
            or is_jump_isolation_net(ns)
            or "FBRX" in ns.upper()
        ):
            continue
        for d in n2d.get(ns, []) or []:
            ds = str(d)
            if ds == cpl or ds in seen_h:
                continue
            if is_shunt_device(ds, schematic_res, d2n):
                continue
            if not is_plausible_comp_ref(ds):
                continue
            seen_h.add(ds)
            first_hops.append(ds)
    if not first_hops:
        return

    # 每个 first-hop 向外：到 FE / 到本 ANT / 最长躯干
    fe_arms: list[list[str]] = []  # [cpl, hop, …, FE]
    ant_arms: list[list[str]] = []  # [cpl, hop, …, ANT]
    side_arms: list[list[str]] = []  # [cpl, hop, …] 无 FE 终点的长臂

    for hop in first_hops:
        block_other = set(first_hops) - {hop}
        w_fe = _walks_to_goals(
            hop,
            _is_fe,
            n2d,
            d2n,
            schematic_res,
            home_ant=ant,
            block=block_other,
            max_depth=14,
            skip_fbrx_nets=True,
            all_goals=True,
            stop_at_other_fe=False,
        )
        for w in w_fe:
            if w and w[0] == hop and _is_fe(w[-1]):
                fe_arms.append([cpl] + list(w))
        w_ant = _walks_to_goals(
            hop,
            _is_home_ant,
            n2d,
            d2n,
            schematic_res,
            home_ant=ant,
            block=block_other,
            max_depth=16,
            skip_fbrx_nets=True,
            all_goals=True,
            stop_at_other_fe=True,
        )
        for w in w_ant:
            if w and w[0] == hop and _is_home_ant(w[-1]):
                ant_arms.append([cpl] + list(w))
        # 对侧长臂：无 FE 时仍保留开关/滤波链
        if not w_fe and not w_ant:
            # 浅 BFS 收最长非 FE 链
            best = [hop]
            q: deque[tuple[str, list[str]]] = deque([(hop, [hop])])
            seen_b: set[str] = {hop, cpl} | block_other
            guard = 0
            while q and guard < 400:
                guard += 1
                cur, path = q.popleft()
                if len(path) > len(best):
                    best = path
                if len(path) > 12:
                    continue
                for ds in _device_peers_nongnd(
                    cur, n2d, d2n, home_ant=ant, skip_fbrx_nets=True,
                    schematic_res=schematic_res,
                ):
                    if ds in seen_b:
                        continue
                    if is_shunt_device(ds, schematic_res, d2n):
                        continue
                    if _is_fe(ds) or is_transceiver_ref(ds, schematic_res):
                        continue
                    if not _is_rf_body_mid(ds, schematic_res):
                        continue
                    if (
                        _is_electronic_switch_ref(ds, schematic_res)
                        and _is_electronic_switch_ref(cur, schematic_res)
                    ):
                        continue
                    seen_b.add(ds)
                    q.append((ds, path + [ds]))
            if len(best) >= 2:
                side_arms.append([cpl] + best)

    if not fe_arms:
        return

    candidates: list[list[str]] = []
    if ant_arms:
        for fa in fe_arms:
            for aa in ant_arms:
                if len(fa) < 2 or len(aa) < 2:
                    continue
                if fa[1] == aa[1]:
                    continue  # 同侧 first-hop，不是贯通
                # ant_first: ANT…cpl…FE
                candidates.append(list(reversed(aa)) + list(fa[1:]))
    else:
        # **无本口 ANT 贯通：禁止发 full RF**。
        # 用 FE↔FE/side 配对或单侧 FE 长链凑 path，会把其它天线口的开关/前端树灌进本 sheet。
        # FBRX 取样已在 _emit_fbrx_band_path 单独补出；此处只认「FE↔本口 ANT」贯通。
        _ = side_arms
        return

    for ant_first in candidates:
        compact: list[str] = []
        for r in ant_first:
            if not compact or compact[-1] != r:
                compact.append(r)
        compact = _strip_series_pure_shunts(compact, schematic_res, sig)
        if not compact or not _is_fe(compact[-1]):
            continue
        if cpl not in compact:
            continue
        if len(compact) < 3:
            continue
        # full RF 必须含本口 ANT（ant_first：ANT 在首）
        if not any(same_ant_port(r, ant) for r in compact):
            continue
        key = (ant, tuple(compact))
        if key in existing:
            continue
        ref_set = set(compact)
        dup = False
        for p in ant_paths:
            pref = {d.ref for d in p.devices if d.ref}
            if pref and len(ref_set & pref) >= max(4, len(ref_set) - 2):
                if any(_is_fe(d.ref) and d.ref == compact[-1] for d in p.devices):
                    dup = True
                    break
        if dup:
            continue
        devices = devices_from_refs(
            compact, schematic_res, sig, ant_first=True, keep_main_order=True,
        )
        if any(d.is_sdr for d in devices):
            continue
        band = _band_guess(compact, schematic_res, signal_infos=sig) or ant
        path = Path(
            ant_num=ant,
            devices=devices,
            band=band,
            direction=_direction_from_band(band) or "TX",
            kind="BAND",
        )
        out.append(path)
        existing.add(key)
        ant_paths.append(path)
        path_refs.update(r for r in compact if r)


def build_paths(
    level_matrixs: list,
    schematic_res,
    signal_infos=None,
    transceiver_paths=None,
    gps_paths=None,
    *,
    copad_pairs: Optional[list[CoPadPair]] = None,
    pin_parser=None,
) -> list[Path]:
    """搭建全部 Path：频段 BAND（止于 PA/LNA）+ TRX→PA/LNA + GPS。"""
    sig = signal_infos_of(schematic_res, signal_infos)
    paths: list[Path] = []
    # 全量 ANT 锥：独占节点（禁止他 ANT 独占件混入本 ANT 路径）
    ant_cone_map: dict[str, set[str]] = {}
    ant_edges_list: list[tuple[str, object]] = []
    for entry in level_matrixs or []:
        if not entry:
            continue
        ant_raw = str(entry[0])
        ant_num = canonical_ant_id(ant_raw) if is_ant_path_node(ant_raw) else ant_raw
        re_edges = entry[2] if len(entry) >= 3 else []
        re_edges = re_edges or []
        if re_edges:
            sample = next(iter(re_edges))
            if not (isinstance(sample, (tuple, list)) and len(sample) == 2):
                print(
                    f"[功能5][PATH] WARN {ant_num}: reverse_edges 非法，跳过 "
                    f"sample={repr(sample)[:80]}"
                )
                re_edges = []
        ant_edges_list.append((ant_num, re_edges))
        ant_cone_map[ant_num] = _reverse_edge_nodes(re_edges)
    exclusive_by_ant = _exclusive_nodes_by_ant(ant_cone_map)
    all_ant_names = sorted(ant_cone_map.keys())

    for ant_num, reverse_edges in ant_edges_list:
        cone = ant_cone_map.get(ant_num) or set()
        device_rows = _routes_from_edges(
            ant_num,
            reverse_edges,
            schematic_res,
            pin_parser=pin_parser,
            exclusive_by_ant=exclusive_by_ant,
            all_ant_names=all_ant_names,
        )
        for cells in device_rows:
            root = cells[-1] if cells else None
            cells = _finalize_band_to_frontend(
                list(cells),
                schematic_res,
                sig,
                allowed_nodes=cone or None,
                required_root=(
                    root
                    if root and _is_band_chip_endpoint(root, schematic_res)
                    else None
                ),
            )
            if not cells or not _is_band_chip_endpoint(cells[-1], schematic_res):
                continue
            if root and root in cells:
                ri = max(i for i, c in enumerate(cells) if c == root)
                cells = cells[: ri + 1]
            if not cells or not _is_band_chip_endpoint(cells[-1], schematic_res):
                continue
            # 多点网 DFS 可能把纯并联塞进串联 → 先剥离再 inject
            main_cells = _strip_series_pure_shunts(list(cells), schematic_res, sig)
            if not main_cells or not _is_band_chip_endpoint(
                main_cells[-1], schematic_res,
            ):
                continue
            # 并联/对地 RLCT：挂在主链锚点旁；主链相对顺序不变
            devices = devices_from_refs(
                main_cells, schematic_res, sig, ant_first=True, keep_main_order=True,
            )
            # 最终门禁：芯片端前端有源、含本 ANT、无 TRX
            if any(d.is_sdr for d in devices):
                continue
            if not any(
                same_ant_port(d.ref, ant_num) or is_ant_path_node(d.ref)
                for d in devices
            ):
                continue
            band = _band_guess(main_cells, schematic_res, signal_infos=sig)
            paths.append(
                Path(
                    ant_num=ant_num,
                    devices=devices,
                    band=band or ant_num,
                    direction=_direction_from_band(band),
                    kind="BAND",
                )
            )

    # Transceiver→PA/LNA 匹配：单独成行；旁路仅路径局部（mode=trx）
    trx_paths = build_trx_pa_lna_paths(transceiver_paths, schematic_res)
    enriched_trx: list[Path] = []
    for tp in trx_paths:
        refs = [d.ref for d in tp.devices]
        devices = devices_from_refs(
            refs,
            schematic_res,
            sig,
            multiport_pin_mode="trx",
            ant_first=False,
            keep_main_order=True,
        )
        enriched_trx.append(
            Path(
                ant_num=tp.ant_num,
                devices=devices,
                band=tp.band,
                direction=tp.direction,
                kind="TRX_PA_LNA",
            )
        )
    paths.extend(enriched_trx)

    # GPS/GNSS：MAIN GPS_ALL_PATHS → 默认独立 GPS 页
    # 若 GPS 终到已有 BAND 页的 ANTn（ANT 口兼 GPS）：
    #   按 GPS 管线构图，但落到 ANTn 页（kind=GPS, ant_num=ANTn），不单独 GPS sheet；
    #   并去掉该 ANT 上 reverse 的残缺 BAND 行（否则只有 LNA→ANT 短链、缺 TRX/夹心）。
    band_ant_nums = _band_sheet_ant_port_nums(level_matrixs)
    for _an in all_ant_names:
        _m = _ANT_PORT_NUM_RE.match(str(_an or "").strip())
        if _m:
            band_ant_nums.add(_m.group(1))
    gps_built = build_gps_paths(gps_paths, schematic_res)
    merge_gps_into_ant, gps_on_ants = _should_skip_gps_sheet(
        gps_built, band_ant_nums,
    )
    enriched_gps: list[Path] = []
    for gp in gps_built:
        refs = [d.ref for d in gp.devices]
        # 入口对腿（is_copad）在注入前后都保留
        entry_copad = {
            d.ref for d in gp.devices if getattr(d, "is_copad", False)
        }
        # 多点焊盘匹配 RLCT 收到 FIL 前
        refs = _reorder_matching_rlct_before_multiport(
            refs, schematic_res, sig,
        )
        devices = devices_from_refs(
            refs, schematic_res, sig, ant_first=False,
        )
        for d in devices:
            if is_ant_path_node(d.ref):
                d.is_ant = True
            if d.ref in entry_copad:
                d.is_copad = True
        home_ant = "GPS"
        if merge_gps_into_ant:
            home_ant = _gps_path_home_ant(gp, band_ant_nums, gps_on_ants)
        enriched_gps.append(
            Path(
                ant_num=home_ant,
                devices=devices,
                band=gp.band,
                direction=gp.direction,
                kind="GPS",
            )
        )
    # 并联注入可能把共享尾又挂回支路 → 再截；汇合点沿用通 ANT 拓扑 anchors
    _join = getattr(build_gps_paths, "last_join_anchors", None) or frozenset()
    enriched_gps = _gps_restrip_paths_shared_suffix(
        enriched_gps, join_anchors=frozenset(_join) if _join else None,
    )
    # restrip 后 home 仍跟 ant_num（已写入）
    if merge_gps_into_ant and gps_on_ants:
        home_set = {str(a).upper() for a in gps_on_ants}
        paths = [
            p
            for p in paths
            if not (
                (p.kind or "BAND").upper() == "BAND"
                and str(p.ant_num or "").upper() in home_set
            )
        ]
    paths.extend(enriched_gps)

    if copad_pairs is not None:
        candidates = list(copad_pairs)
    else:
        candidates = detect_copad_pairs(schematic_res, signal_infos)
    structures = build_copad_structures(
        candidates,
        signal_infos=sig,
        complete_only=True,
        schematic_res=schematic_res,
    )
    pairs = pairs_from_structures(structures)
    mark_copad_on_paths(paths, pairs)
    # 方案 A：完整 Co-pad 之后 orphan 焊盘 → Co-lay（兼容并列正常支路）
    colay_structures = build_colay_structures(
        signal_infos=sig,
        schematic_res=schematic_res,
        copad_structures=structures,
    )
    colay_pairs = pairs_from_colay_structures(colay_structures)
    mark_colay_on_paths(paths, colay_pairs)
    # Co-lay 双支路：仅补缺失腿为普通 BAND Path（含并位）。
    # **禁止** 把 colay 塞进 L4 Co-pad 夹心——会抽干路/拆主链，破坏整页 refnum。
    colay_extra = synthesize_colay_branch_paths(
        paths, colay_structures, schematic_res, signal_infos=sig,
    )
    if colay_extra:
        paths.extend(colay_extra)
        mark_colay_on_paths(paths, colay_pairs)
    # Coupler 耦合口取样 → TRX/LPAMiF：正常 BAND，参与行分配/竖并。
    # **必须在 colay 补支路之后**：Coupler 常只出现在 COLAY 腿上。
    fbrx_band = _supplement_coupler_fbrx_band_paths(
        paths, schematic_res, signal_infos=sig,
    )
    if fbrx_band:
        paths.extend(fbrx_band)
        mark_copad_on_paths(paths, pairs)
        mark_colay_on_paths(paths, colay_pairs)
    # 本页 multiport 未占用 RF 口 → 补正常 BAND（非侧支）。
    # **必须在 COLAY 之后**：四工器等常只出现在 COLAY 腿上；若放在 COLAY 前，
    # 页内尚无该 multiport 锚点，自由口（经 RLCT 到 PA 的 TX 臂）整批漏补。
    # 夹心 FIL 未占用口 → 其它频段主链；COLAY 引入的 quadplexer 自由口 → PA TX。
    extra_band = _supplement_band_via_page_multiports(
        paths, structures, schematic_res, signal_infos=sig,
    )
    if extra_band:
        paths.extend(extra_band)
        mark_copad_on_paths(paths, pairs)
        mark_colay_on_paths(paths, colay_pairs)
    # multiport/JUMP 锚进页后：再合成一次 COLAY 缺腿（宿主腿常在 multiport 后才出现）
    colay_extra2 = synthesize_colay_branch_paths(
        paths, colay_structures, schematic_res, signal_infos=sig,
    )
    if colay_extra2:
        paths.extend(colay_extra2)
        mark_colay_on_paths(paths, colay_pairs)
    # 仍未覆盖的 FE 收官支路：独立成 BAND（不依赖「已有一腿宿主」）
    from refnum.colay import emit_uncovered_colay_fe_branch_paths

    colay_fe_extra = emit_uncovered_colay_fe_branch_paths(
        paths, colay_structures, schematic_res, signal_infos=sig,
    )
    if colay_fe_extra:
        paths.extend(colay_fe_extra)
        mark_colay_on_paths(paths, colay_pairs)
    # 焊盘第三端干路 RLCT：两条支路公共串联节点，允许手册竖并
    mark_colay_trunk_on_paths(
        paths, colay_structures, schematic_res, signal_infos=sig,
    )
    # JUMP 特例：仅 JUMP 远端的焊盘附着件单独成行（不恢复 SideChain 全管线）
    from refnum.side_chain import synthesize_jump_stub_paths

    jump_stubs = synthesize_jump_stub_paths(
        paths, schematic_res, signal_infos=sig,
    )
    if jump_stubs:
        paths.extend(jump_stubs)
        # 锚点继承 COLAY 后，再扫一遍标记（含 JUMP 行上的公共 RLC）
        mark_colay_on_paths(paths, colay_pairs)
        mark_colay_trunk_on_paths(
            paths, colay_structures, schematic_res, signal_infos=sig,
        )
    mark_jump_attach_on_paths(paths, schematic_res, signal_infos=sig)
    build_paths.last_copad_pairs = tuple(pairs)  # type: ignore[attr-defined]
    build_paths.last_copad_structures = tuple(structures)  # type: ignore[attr-defined]
    build_paths.last_copad_candidates = tuple(candidates)  # type: ignore[attr-defined]
    build_paths.last_colay_pairs = tuple(colay_pairs)  # type: ignore[attr-defined]
    build_paths.last_colay_structures = tuple(colay_structures)  # type: ignore[attr-defined]
    build_paths.last_colay_sandwich = ()  # type: ignore[attr-defined]

    return paths


def group_paths_by_ant(paths: list[Path]) -> dict[str, list[Path]]:
    by: dict[str, list[Path]] = {}
    for p in paths:
        by.setdefault(p.ant_num, []).append(p)
    return by


def sheet_sort_key(name: str) -> tuple:
    """ANT / GPS / TRX_PA_LNA / 其它：预览与手册共用页序。"""
    s = str(name)
    if s.upper().startswith("ANT"):
        return (0, s)
    if s == "GPS":
        return (1, s)
    if s == "TRX_PA_LNA":
        return (2, s)
    return (3, s)


# ---------------------------------------------------------------------------
# MAIN 兼容入口（预览写出在 refnum_path_preview）
# ---------------------------------------------------------------------------

def export_path_preview(*args, **kwargs):
    """兼容 re-export：实现见 refnum_path_preview.export_path_preview。"""
    from refnum.path_preview import export_path_preview as _export
    return _export(*args, **kwargs)


def export_repair_manual(
    level_matrixs,
    filename,
    schematic_res,
    signal_infos=None,
    gps_paths=None,
    transceiver_paths=None,
    handbook_filename=None,
    write_path_preview=True,
    write_handbook=True,
    pin_parser=None,
    component_values=None,
):
    """MAIN 兼容入口：阶段 A 路径中间产物 + 阶段 B 手册构图。

    每工程固定两个落盘（无 _OK/_NEW 旁路）：
    - filename：建议 `7_refnum/中间产物/*_path_preview.xlsx`（A 路径中间产物）
    - handbook_filename：建议 `7_refnum/*_refnum.xlsx`（B 成品；可由 filename 推导）
    - pin_parser：与功能1相同，供开关/SAW/分频器引脚约束 DFS
    """
    paths = build_paths(
        level_matrixs,
        schematic_res,
        signal_infos=signal_infos,
        transceiver_paths=transceiver_paths,
        gps_paths=gps_paths,
        pin_parser=pin_parser,
    )
    print(f"[功能5] 路径 {len(paths or [])} 条，开始写出", flush=True)
    pairs = list(getattr(build_paths, "last_copad_pairs", ()) or [])
    structures = list(getattr(build_paths, "last_copad_structures", ()) or [])
    colay_structures = list(
        getattr(build_paths, "last_colay_structures", ()) or []
    )
    # 手册 L4 只吃完整 Co-pad；Co-lay 不得进夹心（会抽干路破坏主体）

    preview_out = None
    if write_path_preview and filename:
        from refnum.path_preview import export_path_preview as _export_preview
        try:
            preview_out = _export_preview(
                paths,
                filename,
                copad_pairs=pairs,
                copad_structures=structures,
                schematic_res=schematic_res,
                signal_infos=signal_infos,
                colay_structures=colay_structures,
            )
        except Exception as _pv_err:
            import traceback as _tb
            print(f"[功能5][中间产物·path] 写出失败（继续 handbook）: {_pv_err}")
            print(_tb.format_exc())

    handbook_out = None
    if write_handbook:
        hb = handbook_filename
        if not hb and filename:
            # .../x_path_preview.xlsx → .../x_refnum.xlsx
            base, ext = os.path.splitext(filename)
            if base.endswith("_path_preview"):
                hb = base[: -len("_path_preview")] + "_refnum" + (ext or ".xlsx")
            elif base.endswith("_refnum"):
                hb = filename
            else:
                hb = base + "_refnum" + (ext or ".xlsx")
        if hb:
            try:
                from refnum.layout.pipeline import (
                    build_handbook_workbook as _export_hb,
                )
                # Co-lay 第二支路以普通 BAND 行展示（含并位），不走 L4 夹心
                handbook_out = _export_hb(
                    paths,
                    hb,
                    copad_structures=structures,
                    copad_pairs=pairs,
                    signal_infos=signal_infos,
                    schematic_res=schematic_res,
                    pin_parser=pin_parser,
                    component_values=component_values,
                )
            except Exception as _hb_err:
                # A 中间产物可保留在磁盘，但正式手册失败必须向上抛出，
                # 禁止 MAIN 仍打印「成品手册: …」造成「跑完了却无正式产物」假成功。
                import traceback as _tb
                print(f"[功能5][最终产物·handbook] 构图失败: {_hb_err}")
                if preview_out:
                    print(
                        f"[功能5][最终产物·handbook] path 中间产物仍保留: {preview_out}"
                    )
                print(_tb.format_exc())
                raise

    if preview_out:
        print(f"[功能5] 路径中间产物: {preview_out}", flush=True)
    if handbook_out:
        print(f"[功能5] 成品手册:     {handbook_out}", flush=True)
    return handbook_out or preview_out or filename


# 跨模块公开 API（并联注入 / 位号→器件；测试可继续 import `_` 别名）
reorder_matching_rlct_before_multiport = _reorder_matching_rlct_before_multiport
inject_parallel_on_path = _inject_parallel_on_path
refs_to_devices = _refs_to_devices


# ---------------------------------------------------------------------------
# COPAD 公共 API（实现见 refnum_copad）
# ---------------------------------------------------------------------------
from refnum.copad import (  # noqa: E402
    CoPadPair,
    CoPadStructure,
    build_copad_structures,
    detect_copad_pairs,
    label_copad_structures_for_sheet,
    mark_copad_on_paths,
    pairs_from_structures,
)
from refnum.colay import (  # noqa: E402
    CoLayPair,
    CoLayStructure,
    build_colay_structures,
    label_colay_structures_for_sheet,
    mark_colay_on_paths,
    mark_colay_trunk_on_paths,
    pairs_from_colay_structures,
    synthesize_colay_branch_paths,
)
from refnum.side_chain import (  # noqa: E402
    mark_jump_attach_on_paths,
    synthesize_jump_stub_paths,
)
