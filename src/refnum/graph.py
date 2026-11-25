"""功能5 — 网表/器件图公共原语（path 与 copad 共享）。

只放拓扑与器件语义工具；不含路径搭建、COPAD 成组、Excel 写出。
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class Device:
    ref: str
    part_type: str = "Unknown Device"
    is_ic: bool = False
    is_passive: bool = False
    is_ant: bool = False
    is_sdr: bool = False
    is_lpf: bool = False
    is_pa_lna: bool = False
    is_copad: bool = False  # 属于某 Co-pad 对，预览标黄
    copad_label: str = ""   # 本页对号：对A 的两器件都标 A；对B 的两器件都标 B
    is_colay: bool = False  # 属于某 Co-lay 对（兼容并列正常支路，无 copad 合并组）
    colay_label: str = ""   # 本页 Co-lay 对号
    # COLAY 焊盘第三端干路 RLCT：两条 colay 支路的公共串联节点，
    # 允许 L3 同列 + L6 竖并（普通 RLCT 仍禁合并）
    is_colay_trunk: bool = False
    is_jump_attach: bool = False  # 接 JUMP 网的位号，预览/手册与 Co-pad/lay 同黄
    is_shunt: bool = False  # 并联/对地挂件（非串联主链）


def make_device(ref: str, schematic_res) -> Device:
    ref = str(ref)
    part_type = "Unknown Device"
    try:
        part_type = schematic_res.get_part_type(ref) or part_type
    except Exception:
        pass
    d = Device(ref=ref, part_type=part_type)
    ru = ref.upper()
    pt = (part_type or "").upper()
    if pt == "TRANSCEIVER" or "TRANSCEIVER" in pt:
        d.is_sdr = True
        d.is_ic = True
    elif pt == "ANTENNA" or ru.startswith("ANT"):
        d.is_ant = True
    elif any(k in pt for k in ("FILTER", "SWITCH", "MULTIPLEX", "COUPLER", "DIVIDER")):
        d.is_ic = True
    elif (
        pt in ("RF PA", "RF LNA", "LPAMIF")
        or pt.startswith("RF PA")
        or pt.startswith("RF LNA")
        or "QPM" in pt
        or "LPAMI" in pt
    ):
        d.is_ic = True
        d.is_pa_lna = True
    elif ru.startswith("LPF"):
        d.is_lpf = True
        d.is_ic = True
    elif re.match(r"^[RLCT]\d", ru):
        d.is_passive = True
    elif ru.startswith("U") or ru.startswith("FIL"):
        d.is_ic = True
    else:
        d.is_passive = True
    return d


def is_network(node: str, schematic_res) -> bool:
    try:
        return bool(schematic_res.is_network(node))
    except Exception:
        return False


def is_connector_ref(ref: str) -> bool:
    """连接器/座子位号：非 RF 匹配链器件。

    类别语义（不绑项目）：
      - J + 数字：PADS 常规连接器（含 LGA/天线座）
      - J- + 字母/数字：PADS 模组/栅格座（J-C、J-A），不是封装后缀
      - CONN + 数字：模组座子；位号以 C 开头，不得当电容穿越
    拒绝 CON1 等过短/歧义前缀。并联注入网纯度判定时应忽略座子，
    避免 ANT 口因座子共网而拒挂旁路 C/T，或沿座子脚把多口焊成一团。
    """
    s = str(ref or "").strip()
    if re.match(r"^J(\d|-)", s, re.I):
        return True
    return bool(re.match(r"^CONN\d", s, re.I))


# 整口 ANT1 / ANT_1 / ANT-1；网名中缀 UHB_RX5_ANT3、N77_TRX1_ANT_8
_ANT_PORT_EXACT = re.compile(r"^ANT(\d+)$", re.I)
_ANT_NUM_IN_TEXT = re.compile(r"ANT(\d+)", re.I)


def is_ant_path_node(node: str) -> bool:
    """是否真实天线口/GPS 天线网名（非中间频段支路网）。

    接受：
      - ANT0 / ANT1 / ANT12 …（ANT + 纯数字，含 0）
      - ANT_1 / ANT-8 …（与 ANTn 同口）
      - ANT_GPS* / ANT_GNSS*（GPS 页端点）
    拒绝（MTK/部分高通常见中间网，勿当天线口）：
      - ANT0_MHB_PRX3、ANT2_N41、ANT1_LB_TX1&DRX 等
    否则「禁止他 ANT 口」会把整条频段链误杀，BAND 路径数为 0。
    """
    s = str(node or "").upper().strip()
    if _ANT_PORT_EXACT.match(s):
        return True
    if s.startswith("ANT") and (s[3:].startswith("_GPS") or s[3:].startswith("_GNSS")):
        return True
    return False


# 第二种子：无名馈点才合成 ANT_J1900_A_55；有名馈点用网名（MAIN_ANT / DIV_ANT）
# 现用 MAIN / F1 仍 import 这些符号；8/31 构图判定本身不走座子叶。
_CONNECTOR_FEED_PORT = re.compile(r"^ANT_(J\d+)(?:_(.+))?$", re.I)
_GNSS_NET_HINT = re.compile(r"GPS|GNSS|GLONASS|BEIDOU|GALILEO", re.I)
_ANON_NET = re.compile(r"^(\${3}|NET\d+$)", re.I)


def _connector_pin_of(ref: str, pins) -> Optional[str]:
    """J1900.55 / J1900-A.55 → 55 / A_55。认不出脚就返回 None，禁止把整颗座子当一口。"""
    ref = str(ref or "")
    if not ref:
        return None
    ru = ref.upper()
    for raw in pins or ():
        s = str(raw or "").strip()
        if not s:
            continue
        su = s.upper()
        rest = None
        if su.startswith(ru + "."):
            rest = s[len(ref) + 1:]
        elif su.startswith(ru + "-"):
            rest = s[len(ref) + 1:]
        if rest:
            return rest.replace(".", "_")
    return None


def _has_rf_front_passive(refs) -> bool:
    """座子旁要有 C/L/T 才当馈点匹配；仅 R 的控制脚不当天线口。"""
    for d in refs or ():
        s = str(d or "")
        if len(s) >= 2 and s[0] in "CLTclt" and s[1].isdigit():
            return True
    return False


def connector_feed_port_id(net: str, devices, pins=None) -> Optional[str]:
    """50R 座子叶构图口：一口 = 一条馈点网，不是一颗座子。

    已是 ANTx 整口 / GNSS 具名网 / 空壳座子 / 仅电阻控制脚 → None。
    有名网（MAIN_ANT、DIV_ANT）用网名；无名网用 ANT_{J}_{脚}，脚解析失败则退回网名。
    """
    if is_ant_path_node(net) or _GNSS_NET_HINT.search(str(net or "")):
        return None
    refs = [str(d) for d in (devices or ()) if d]
    j_refs = [d for d in refs if is_connector_ref(d)]
    others = [d for d in refs if not is_connector_ref(d)]
    if not j_refs or not others or not _has_rf_front_passive(others):
        return None
    if not _ANON_NET.match(str(net or "")):
        return str(net)
    jref = sorted(j_refs)[0]
    pin = _connector_pin_of(jref, pins)
    if pin:
        return f"ANT_{jref}_{pin}"
    return str(net)


def is_connector_feed_port_id(node: str) -> bool:
    return bool(_CONNECTOR_FEED_PORT.match(str(node or "").strip()))


def is_antenna_port_node(node: str, extra_ports=None) -> bool:
    """构图天线根：ANTx 整口、ANT_J脚，或第二种子给出的有名馈点网。"""
    if is_ant_path_node(node) or is_connector_feed_port_id(node):
        return True
    extra = extra_ports or ()
    return bool(node) and node in extra


def antenna_port_sort_key(name: str) -> tuple:
    s = str(name or "")
    m = _ANT_PORT_EXACT.match(s)
    if m:
        return (0, int(m.group(1)), s)
    if is_ant_path_node(s):
        return (1, 0, s)
    if is_connector_feed_port_id(s):
        return (2, 0, s)
    return (3, 0, s)


def collect_connector_feed_ports(signal_rows) -> list:
    """从 [net, devices, pins] 行收集第二种子口 id，保序去重。"""
    out, seen = [], set()
    for row in signal_rows or ():
        if not row:
            continue
        net = row[0]
        devs = row[1] if len(row) > 1 else []
        pins = row[2] if len(row) > 2 else []
        pid = connector_feed_port_id(net, devs, pins)
        if pid and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def iter_antenna_port_nodes(names, extra_ports=None) -> list:
    extra = set(extra_ports or ())
    return sorted(
        (n for n in names if is_antenna_port_node(n, extra)),
        key=antenna_port_sort_key,
    )


def canonical_ant_id(ant_or_net: str) -> str:
    """整口规范名：ANT_8 / ANT-08 / ANT8 → ANT8；非整口原样大写。"""
    s = str(ant_or_net or "").strip()
    m = _ANT_PORT_EXACT.match(s)
    if m:
        return f"ANT{int(m.group(1))}"
    return s.upper()


def same_ant_port(a: str, b: str) -> bool:
    """两名是否同一天线整口（ANT1 ≡ ANT_1 ≡ ANT-1）。中间网不是口。"""
    if not a or not b:
        return False
    return is_ant_path_node(a) and is_ant_path_node(b) and (
        canonical_ant_id(a) == canonical_ant_id(b)
    )


def ant_port_number(ant_or_net: str) -> Optional[str]:
    """从 ANT3 / ANT_8 / 网名 中取天线口号数字串（去前导零）；无则 None。"""
    m = _ANT_NUM_IN_TEXT.search(str(ant_or_net or ""))
    return str(int(m.group(1))) if m else None


def is_jump_isolation_net(net: str) -> bool:
    """跨 ANT 兼容 JUMP 网：网名含 JUMP 关键字则禁止穿越继续扩展。"""
    return "JUMP" in str(net or "").upper()


def is_foreign_ant_isolation_net(net: str, home_ant: str) -> bool:
    """网名含其它 ANTx（与本 sheet 天线口不同）→ 禁止穿越。

    例：home 为本口时，网名含其它 ANTx 为异口网；本口 / 无 ANT 编号的网不拦。
    """
    home = ant_port_number(home_ant)
    if home is None:
        return False
    for m in _ANT_NUM_IN_TEXT.finditer(str(net or "")):
        if m.group(1) != home:
            return True
    return False


def is_cross_ant_isolation_net(net: str, home_ant: Optional[str] = None) -> bool:
    """跨 ANT 兼容隔绝：JUMP 网，或（给定 home 时）异 ANT 关键字网。

    器件仍可因本 ANT 侧焊盘网进入路径；禁止的是**穿越**这些网走到对侧链路。
    """
    if is_jump_isolation_net(net):
        return True
    if home_ant:
        return is_foreign_ant_isolation_net(net, home_ant)
    return False


def is_transceiver_ref(ref: str, schematic_res) -> bool:
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    return pt == "TRANSCEIVER" or "TRANSCEIVER" in pt


def is_pa_lna_ref(ref: str, schematic_res) -> bool:
    """前端有源端点：仅 RF PA / RF LNA / LPAMiF / QPM（ANT 链芯片侧终点）。

    不含 SAW/多工/分频/耦合/开关/TRX——那些是链上器件或别页职责。
    物料码只认 PA/LNA/LPAMiF 前缀（312405/406/425），禁止把 312706 等滤波码算进来。
    """
    d = make_device(ref, schematic_res)
    if d.is_pa_lna:
        return True
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    if pt in ("RF PA", "RF LNA", "LPAMIF"):
        return True
    if pt.startswith("RF PA") or pt.startswith("RF LNA"):
        return True
    if "QPM" in pt or "LPAMI" in pt:
        return True
    try:
        pn = str(getattr(schematic_res, "part_infos", {}).get(ref, "") or "")
    except Exception:
        pn = ""
    # 仅 PA / LNA / LPAMiF 物料大类（与 get_part_type 表一致，不含滤波/开关/TRX）
    if len(pn) >= 6 and pn[:6] in ("312405", "312406", "312425"):
        return True
    return False


def device_graph_from_signal_infos(
    schematic_res, signal_infos,
) -> dict[str, set[str]]:
    """器件共网邻接图（忽略网络节点本身）。"""
    net_to_devs, _ = build_net_device_maps(signal_infos or [])
    adj: dict[str, set[str]] = defaultdict(set)
    for net, devs in net_to_devs.items():
        if is_gnd_net(net):
            continue
        pure = [
            d for d in devs
            if (not is_network(d, schematic_res)) or is_ant_path_node(d)
        ]
        # 网络名即天线端（ANT1 网）时，把端点挂进邻接，便于并联锚点
        if is_ant_path_node(net) and net not in pure:
            pure = [net] + pure
        for i, a in enumerate(pure):
            for b in pure[i + 1 :]:
                adj[a].add(b)
                adj[b].add(a)
    return adj


def device_has_gnd_net(ref: str, dev_to_nets: dict[str, list[str]]) -> bool:
    """器件是否接到地平面网（GND/VSS…）。"""
    return any(is_gnd_net(n) for n in dev_to_nets.get(ref, []))


def is_series_rlct(
    ref: str,
    schematic_res,
    dev_to_nets: dict[str, list[str]],
) -> bool:
    """串联 RLCT：跨 ≥2 个非地信号网（贯通两段射频网）。"""
    if not is_rlct(ref, schematic_res):
        return False
    return nongnd_degree(ref, dev_to_nets) >= 2


def missing_series_devices(
    devices,
    net_to_devs: dict,
    dev_to_nets: dict,
    *,
    max_hop: int = 6,
) -> list[tuple[str, str, list[str]]]:
    """主链在网表上必经、却**不在这条 path 里**的器件。

    返回 ``[(a, b, [缺的器件…]), …]``；``a``/``b`` 是主链上不共网的相邻对。
    查不到中间件（网表上不可达）时第三项为空列表——那种更严重，也要报。

    这是「路径漏了真实器件」的判据：漏掉的器件在这一行失去 owner 身份，它的并柱
    会因此断段、右侧公共段孤立在一旁。

    **相邻顺序与网表不一致、但器件都在 path 里，不算缺陷**：器件都画出来了、
    owner 也齐，构图正常。

    并联挂件（``is_shunt``）不在串联链上，不参与。
    """
    main: list[str] = []
    present: set[str] = set()
    for d in devices or []:
        ref = str(getattr(d, "ref", d) or "").strip()
        if not ref:
            continue
        present.add(ref)
        if getattr(d, "is_shunt", False) or ref not in dev_to_nets:
            continue
        main.append(ref)

    def _nets(ref: str) -> set:
        return {
            str(n)
            for n in (dev_to_nets.get(ref) or [])
            if str(n) and not is_gnd_net(str(n))
        }

    def _mid(a: str, b: str) -> Optional[list[str]]:
        """a→b 最短器件路径的中间段；不可达返回 None。"""
        queue = deque([(a, [])])
        seen = {a}
        while queue:
            cur, mid = queue.popleft()
            if len(mid) > max_hop:
                continue
            for net in dev_to_nets.get(cur) or []:
                ns = str(net)
                if not ns or is_gnd_net(ns):
                    continue
                for nxt in net_to_devs.get(ns) or []:
                    ds = str(nxt)
                    if not ds or ds in seen:
                        continue
                    if ds == b:
                        return mid
                    seen.add(ds)
                    queue.append((ds, mid + [ds]))
        return None

    out: list[tuple[str, str, list[str]]] = []
    for a, b in zip(main, main[1:]):
        if _nets(a) & _nets(b):
            continue
        mid = _mid(a, b)
        if mid is None:
            out.append((a, b, []))
            continue
        lack = [m for m in mid if m not in present]
        if lack:
            out.append((a, b, lack))
    return out


def broken_series_links(
    devices,
    dev_to_nets: dict,
) -> list[tuple[str, str]]:
    """主链相邻器件在网表上**不共网**的对（越少越好，正常应为空）。

    一条 path 的主链是真实串联链路：沿显示序相邻的两只器件必须至少共一个非地
    网。不共网说明中间漏掉了真实存在的器件——典型是穿过多端口器件（分频器 /
    滤波器）时把它本身漏在了主链外，画出来的链路在网表上是断的。

    **并联挂件（``is_shunt``）不参与**：它挂在锚点旁、不在串联链上，与前后邻居
    本就不要求共网。必须先剔除，否则满屏误报。

    只依赖网表，与工程/位号无关，可直接用来扫全部工程。
    """
    main: list[str] = []
    for d in devices or []:
        if getattr(d, "is_shunt", False):
            continue
        ref = str(getattr(d, "ref", d) or "").strip()
        if not ref or ref not in dev_to_nets:
            continue
        main.append(ref)

    def _nets(ref: str) -> set:
        return {
            str(n)
            for n in (dev_to_nets.get(ref) or [])
            if str(n) and not is_gnd_net(str(n))
        }

    out: list[tuple[str, str]] = []
    for a, b in zip(main, main[1:]):
        if not (_nets(a) & _nets(b)):
            out.append((a, b))
    return out


def drop_refs_keeping_series(
    cells,
    drop: set,
    dev_to_nets: dict,
) -> list[str]:
    """剔除 ``drop`` 里的器件，但**不把主链挖断**。

    补链会把不属于自己的主干路件吸进来，需要剔除。但同一颗器件可以既是别处的
    干路件、**又是本链的必经串联中间件**——两个角色都合法。按集合无条件删除会把
    链挖断，留下一对网表上根本不共网的相邻器件；那一行随即失去该器件的 owner
    身份，它的并柱断段、右侧公共段孤立在一旁。

    逐只尝试：删掉它若使主链新增「相邻不共网」则保留。只依赖网表连通性，
    与工程/位号无关。
    """
    keep = [str(c) for c in cells or []]
    if not drop:
        return keep
    for ref in [c for c in keep if c in drop]:
        cand = [c for c in keep if c != ref]
        if len(broken_series_links(cand, dev_to_nets)) > len(
            broken_series_links(keep, dev_to_nets)
        ):
            continue
        keep = cand
    return keep


def is_shunt_device(
    ref: str,
    schematic_res,
    dev_to_nets: dict[str, list[str]],
) -> bool:
    """是否并联/对地挂件（通用，仅 R/L/C/T）。

    拓扑不变量（禁止用 is_parallel 单独盖过串联）：
      - 非地信号网度数 ≥ 2 → **串联**，绝不是并联
      - 并联 → 恰好 1 个非地信号网，且另一侧对地：
          is_parallel（gnd 集合）或器件网表中含 GND 网
      - 仅度=1、但对地证据不足的开放 stub（如部分 Co-pad L）
        **不**当路径并联挂载（避免串联/开路腿被蓝标）
    """
    if is_transceiver_ref(ref, schematic_res):
        return False
    if is_ant_path_node(ref):
        return False
    try:
        if schematic_res.is_network(ref):
            return False
    except Exception:
        pass
    if not is_rlct(ref, schematic_res):
        return False
    deg = nongnd_degree(ref, dev_to_nets)
    if deg >= 2:
        return False  # 串联形态
    if deg != 1:
        return False
    try:
        if schematic_res.is_parallel(ref):
            return True
    except Exception:
        pass
    return device_has_gnd_net(ref, dev_to_nets)


def is_multiport_anchor(ref: str, schematic_res) -> bool:
    """多端口锚点（TRX/PA/FIL/IC 等）：并联扫描须限制在路径局部共网。"""
    if is_ant_path_node(ref):
        return False
    if is_rlct(ref, schematic_res):
        return False
    try:
        if schematic_res.is_network(ref):
            return False
    except Exception:
        pass
    return True


def is_coupler_ref(ref: str, schematic_res) -> bool:
    """射频耦合器（Coupler / 物料 312701）。"""
    if not ref or is_rlct(ref, schematic_res):
        return False
    if is_ant_path_node(ref):
        return False
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper() if schematic_res else ""
    except Exception:
        pt = ""
    if "COUPLER" in pt:
        return True
    try:
        pn = str(getattr(schematic_res, "part_infos", {}).get(ref, "") or "")
    except Exception:
        pn = ""
    return len(pn) >= 6 and pn[:6] == "312701"


def is_side_chain_anchor(ref: str, schematic_res) -> bool:
    """滤波/分频/多工/SAW 类 multiport（步行/补链用；侧支管线已清除）。

    历史名 side_chain_anchor：现仅作「可中途穿越的滤波类 multiport」判定，
    不再表示「建侧支」。PA/LNA/TRX/开关/Coupler 返回 False。
    """
    if not is_multiport_anchor(ref, schematic_res):
        return False
    if is_pa_lna_ref(ref, schematic_res):
        return False
    if is_transceiver_ref(ref, schematic_res):
        return False
    if is_coupler_ref(ref, schematic_res):
        return False
    try:
        if schematic_res is not None and schematic_res.is_electronic_switch(ref):
            return False
    except Exception:
        pass
    ru = str(ref or "").strip().upper()
    if ru.startswith("FIL"):
        return True
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper() if schematic_res else ""
    except Exception:
        pt = ""
    if any(
        k in pt
        for k in (
            "FILTER",
            "DIVIDER",
            "MULTIPLEX",
            "SAW",
            "BAW",
            "DIPLEX",
            "DUPLEX",
            "FREQUENCY",
        )
    ):
        return True
    try:
        if schematic_res is not None and (
            schematic_res.is_frequency_divider(ref)
            or schematic_res.is_SAWFBAR(ref)
        ):
            return True
    except Exception:
        pass
    return False


def signal_infos_of(schematic_res, signal_infos=None) -> list:
    """主链/并联注入用：优先 raw 合并网表，否则调用方/过滤后 signal_infos。"""
    return (
        getattr(schematic_res, "signal_infos_raw", None)
        or signal_infos
        or getattr(schematic_res, "signal_infos", None)
        or []
    )


def is_plausible_comp_ref(ref: str) -> bool:
    """拒绝网表脏名（如 '\"Source'、'Path\"'、带空格的伪节点）。"""
    s = str(ref or "").strip()
    if not s or len(s) > 24:
        return False
    if any(ch in s for ch in (' ', '"', "'", "→", "/", "\\", "\n", "\t")):
        return False
    if s.lower() in {"source", "path", "net", "gnd"}:
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_]*$", s))


def is_gnd_net(net: str) -> bool:
    u = (net or "").upper()
    if not u:
        return True
    return (
        "GND" in u
        or u in {"0", "VSS", "AGND", "PGND", "DGND", "GNDA", "GNDD"}
        or u.startswith("GND")
    )


def is_non_rf_path_net(net: str) -> bool:
    """电源/控制/数字网：主链 DFS 与开关口分类时不走。"""
    u = str(net or "").upper()
    if not u or is_gnd_net(u):
        return True
    keys = (
        "VREG", "VDD", "VPH", "VPA", "VBUS", "VBAT",
        "GRFC", "RFFE", "SPMI", "GPIO", "UART", "I2C", "SPI",
        "CLK", "DATA", "RESET", "PS_HOLD", "THERM",
    )
    return any(k in u for k in keys)


def electronic_switch_rf_peers(
    switch_ref: str,
    n2d: dict,
    d2n: dict,
) -> list[str]:
    """电子开关的 RF 一跳邻接器件（排除电源/控制网）。"""
    sw = str(switch_ref or "").strip()
    if not sw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for net in d2n.get(sw, []) or []:
        ns = str(net)
        if is_non_rf_path_net(ns):
            continue
        for d in n2d.get(ns, []) or []:
            ds = str(d)
            if not ds or ds == sw or ds in seen:
                continue
            if not is_plausible_comp_ref(ds) and not is_ant_path_node(ds):
                continue
            seen.add(ds)
            out.append(ds)
    out.sort()
    return out


def classify_esw_port_sides(
    switch_ref: str,
    home_ant: str,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    max_depth: int = 14,
) -> tuple[set[str], set[str]]:
    """电子开关 RF 邻接分为 **ANT 公共侧** / **FE（LNA·PA）侧**。

    领域语义（树状汇合，不绑位号）：
      - ANT 侧：不经其它 PA/LNA 即可到达 **home_ant** 口
      - FE 侧：可到达 PA/LNA 前端
    主链穿越开关时只允许 FE↔ANT 贯通，禁止 FE↔FE（把另一天线/另一频段
    前端链误串进本口）。
    """
    from collections import deque

    sw = str(switch_ref or "").strip()
    home_u = str(home_ant or "").strip().upper()
    peers = electronic_switch_rf_peers(sw, n2d, d2n)
    if not sw or not peers:
        return set(), set()

    def _walk(start: str) -> tuple[bool, bool]:
        """从 start 出发（禁经 sw）：是否到 home ANT / 是否到 PA·LNA。"""
        if not start or start == sw:
            return False, False
        q: deque[tuple[str, int]] = deque([(start, 0)])
        seen = {sw, start}
        hit_ant = is_ant_path_node(start) and (
            not home_u or same_ant_port(start, home_u)
        )
        hit_fe = is_pa_lna_ref(start, schematic_res)
        while q:
            cur, dep = q.popleft()
            if dep >= max_depth:
                continue
            for net in d2n.get(cur, []) or []:
                ns = str(net)
                if is_non_rf_path_net(ns):
                    continue
                if is_cross_ant_isolation_net(ns, home_ant):
                    continue
                # 命名 ANT 整口本身可收口（ANT1 ≡ ANT_1）
                if is_ant_path_node(ns):
                    if not home_u or same_ant_port(ns, home_u):
                        hit_ant = True
                for d in n2d.get(ns, []) or []:
                    ds = str(d)
                    if not ds or ds in seen:
                        continue
                    if not is_plausible_comp_ref(ds) and not is_ant_path_node(ds):
                        continue
                    seen.add(ds)
                    if is_ant_path_node(ds) and (
                        not home_u or same_ant_port(ds, home_u)
                    ):
                        hit_ant = True
                    if is_pa_lna_ref(ds, schematic_res):
                        hit_fe = True
                    # 穿过另一前端后不再扩展（避免 FE 侧绕大圈误标 ANT）
                    if is_pa_lna_ref(ds, schematic_res):
                        continue
                    q.append((ds, dep + 1))
            if hit_ant and hit_fe:
                break
        return hit_ant, hit_fe

    ant_side: set[str] = set()
    fe_side: set[str] = set()
    for p in peers:
        ha, hf = _walk(p)
        # 同时可达时：到 ANT 的跳数通常更「纯」则两侧都标，由 hop 规则处理
        if ha:
            ant_side.add(p)
        if hf:
            fe_side.add(p)
    return ant_side, fe_side


def esw_through_hop_allowed(
    switch_ref: str,
    prev_dev: str,
    next_dev: str,
    home_ant: str,
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    sides: Optional[tuple[set[str], set[str]]] = None,
) -> bool:
    """主链穿越电子开关：禁止 FE 口串到另一 FE 口。

    允许：FE↔ANT、未知口（保守放行以免掐死无 home 场景）、同器件回环否决。
    """
    sw = str(switch_ref or "").strip()
    prev = str(prev_dev or "").strip()
    nxt = str(next_dev or "").strip()
    if not sw or not prev or not nxt or prev == nxt:
        return prev != nxt
    if sides is None:
        ant_side, fe_side = classify_esw_port_sides(
            sw, home_ant, n2d, d2n, schematic_res,
        )
    else:
        ant_side, fe_side = sides
    if not ant_side and not fe_side:
        return True
    prev_fe, prev_ant = prev in fe_side, prev in ant_side
    next_fe, next_ant = nxt in fe_side, nxt in ant_side
    # FE→FE：把另一前端链经开关体误并进本链
    if prev_fe and next_fe and not (prev_ant or next_ant):
        return False
    # ANT→ANT 同侧互串通常无意义
    if prev_ant and next_ant and not (prev_fe or next_fe):
        return False
    return True


def is_rlct(ref: str, schematic_res) -> bool:
    """R/L/C/T 被动件（位号或类型）。路径搜寻/并联挂载统一用 RLCT。"""
    ref = str(ref)
    try:
        if schematic_res.is_network(ref):
            return False
    except Exception:
        pass
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    if any(
        k in pt
        for k in (
            "TRANSCEIVER",
            "ANTENNA",
            "FILTER",
            "SWITCH",
            "MULTIPLEX",
            "RF PA",
            "RF LNA",
            "LPAMIF",
        )
    ):
        return False
    if pt in {"RF PA", "RF LNA"} or pt.endswith(" PA") or pt.endswith(" LNA"):
        return False
    if re.match(r"^[RLCT]\d", ref, re.I):
        return True
    return any(
        k in pt
        for k in (
            "RESISTOR",
            "CAPACITOR",
            "INDUCTOR",
            "TRANSFORMER",
            "BALUN",
        )
    )


def is_rlc(ref: str, schematic_res) -> bool:
    """兼容旧名 → RLCT。"""
    return is_rlct(ref, schematic_res)


def rlc_kind(ref: str, schematic_res) -> str:
    """返回 'C' / 'L' / 'R' / 'T' / ''。"""
    if not is_rlct(ref, schematic_res):
        return ""
    ru = str(ref).upper()
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    if ru.startswith("C") or "CAPACITOR" in pt:
        return "C"
    if ru.startswith("L") or "INDUCTOR" in pt:
        return "L"
    if ru.startswith("R") or "RESISTOR" in pt:
        return "R"
    if ru.startswith("T") or "TRANSFORMER" in pt or "BALUN" in pt:
        return "T"
    return ""


def nongnd_degree(ref: str, dev_to_nets: dict[str, list[str]]) -> int:
    """器件接到的非地信号网个数（拓扑度数，不读项目位号）。"""
    return sum(1 for n in dev_to_nets.get(ref, []) if not is_gnd_net(n))


def build_net_device_maps(signal_infos) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """net → devices；device → nets（保序去重；丢掉非位号脏名）。"""
    net_to_devs: dict[str, list[str]] = {}
    dev_to_nets: dict[str, list[str]] = defaultdict(list)
    for si in signal_infos or []:
        if not si or len(si) < 2:
            continue
        net = str(si[0] or "")
        if not net:
            continue
        raw = [str(d) for d in (si[1] or []) if d]
        seen: set[str] = set()
        devs: list[str] = []
        for d in raw:
            if d in seen:
                continue
            if not is_plausible_comp_ref(d) and not is_ant_path_node(d):
                # 允许 ANT 端点名；拒绝 Source/Path 等
                if not str(d).upper().startswith("ANT"):
                    continue
            seen.add(d)
            devs.append(d)
        if not devs:
            continue
        net_to_devs[net] = devs
        for d in devs:
            if net not in dev_to_nets[d]:
                dev_to_nets[d].append(net)
    return net_to_devs, dev_to_nets


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)
