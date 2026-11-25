"""功能5 最终产物：未进 refnum 主图的 **50R** 器件 →「others」页。

职责（非主链路径搭建）：
1. 候选**仅** TXT NET_CLASS 50R 激活器件（不含连接器；不扩 all_raw 数字/SRS 专网）
2. 对照已进手册路径/侧支的位号集合，得未覆盖集合
3. 分类两路：
   - 有名网语义（SRS / FBRX / GPS…）
   - **邻接多端口件的 port 口名**（邻接 IC 的口名如 XO_IN → 独立类）
4. 未覆盖集合内按共网连通成链路（能串则串，否则单点）
5. 写入独立「others」Sheet（仅当未覆盖非空）：每条链两行单元格
   （首列类型竖并 / 右延位号 / 下行空值），链间空一行，不用「A→B」文字

禁止：项目位号/工程名分支；不改主路径判定。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from refnum.graph import (  # noqa: E402
    build_net_device_maps,
    is_ant_path_node,
    is_gnd_net,
    is_pa_lna_ref,
    is_rlct,
    is_transceiver_ref,
    signal_infos_of,
)
from refnum.coverage_50r import (  # noqa: E402
    collect_path_device_set,
    collect_refnum_covered_device_set,
)

BUILD_ID = "2026-08-18-others-lead-label-v15"
OTHERS_CHAIN_ROWS = 2  # 位号 / 数值；类型在首列竖并
OTHERS_LINK_GAP_ROWS = 1

_THIN = Side(style="thin", color="FF000000")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_FILL_HDR = PatternFill(fill_type="solid", fgColor="FF4472C4")
_FILL_BAND = PatternFill(fill_type="solid", fgColor="FFD6DCE4")
_FILL_SRS = PatternFill(fill_type="solid", fgColor="FFFFEB9C")
_FILL_FBRX = PatternFill(fill_type="solid", fgColor="FFF8CBAD")
_FILL_GPS = PatternFill(fill_type="solid", fgColor="FFC6EFCE")
_FILL_JUMP = PatternFill(fill_type="solid", fgColor="FFFFFF00")
_FILL_OTHER = PatternFill(fill_type="solid", fgColor="FFE2EFDA")
_FILL_ANON = PatternFill(fill_type="solid", fgColor="FFD9D9D9")
_FONT_HDR = Font(color="FFFFFFFF", bold=True)
_FONT_BOLD = Font(bold=True)

# 有名网语义标签：按优先级匹配（通用标志，非项目位号）
_TAG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FBRX", re.compile(r"FBRX", re.I)),
    # SRS 标志：独立 token / _SRS / SRS数字；禁止 SRST（JTAG_SRST）误伤
    ("SRS", re.compile(r"(?:^|[_/#.-])SRS(?![A-Za-z])", re.I)),
    ("GPS/GNSS", re.compile(r"GNSS|(^|_)GPS(_|$)", re.I)),
    ("JUMP", re.compile(r"JUMP", re.I)),
    ("MIPI/RFFE", re.compile(r"MIPI|RFFE|RFCK", re.I)),
    ("CLK/CTRL", re.compile(r"(^|_)(CLK|SPI|I2C|SCLK|SDATA)(_|$)", re.I)),
)

# 固定高优先级标签；port 口名（如 XO_IN）插在其后、其它有名网之前
_FIXED_TAG_ORDER = (
    "FBRX",
    "SRS",
    "GPS/GNSS",
    "JUMP",
    "MIPI/RFFE",
    "CLK/CTRL",
)
_TAIL_TAGS = ("其它有名网", "匿名网($$$)")

TAG_ORDER = _FIXED_TAG_ORDER + _TAIL_TAGS  # 兼容旧引用；动态 port 在运行时插入

_TAG_FILL = {
    "FBRX": _FILL_FBRX,
    "SRS": _FILL_SRS,
    "GPS/GNSS": _FILL_GPS,
    "JUMP": _FILL_JUMP,
    "MIPI/RFFE": _FILL_OTHER,
    "CLK/CTRL": _FILL_OTHER,
    "其它有名网": _FILL_OTHER,
    "匿名网($$$)": _FILL_ANON,
}
_FILL_PORT = PatternFill(fill_type="solid", fgColor="FFDDEBF7")

# 邻接 IC port 标签忽略电源/地/空脚
_SKIP_PORT_LABEL = re.compile(
    r"^(GND|VSS|VDD|VCC|VBB|VIO|VBAT|VREG|NC|DNC|EP|EPAD|EPAD\d*|AGND|DGND|PGND)"
    r"($|[_0-9])",
    re.I,
)
_PIN_TOKEN_RE = re.compile(
    r"^([A-Za-z]+\d+)(?:-[A-Za-z0-9]+)?\.([A-Za-z]*\d+[A-Za-z0-9]*)$"
)


@dataclass(frozen=True)
class OthersChain:
    """一条未覆盖连通链路（或单点）。"""

    tag: str
    chain_id: str
    refs: tuple[str, ...]
    evidence_nets: tuple[str, ...] = ()


@dataclass
class OthersReport:
    uncovered: list[str] = field(default_factory=list)
    chains: list[OthersChain] = field(default_factory=list)
    tag_counts: dict[str, int] = field(default_factory=dict)
    n_50r: int = 0
    n_covered: int = 0


def _50r_class_signal_rows(schematic_res, signal_infos=None) -> list:
    """TXT NET_CLASS 50R 网的信号行（用于串链/分类，与清单同源）。

    必须用 all_signal_infos_raw + net_class_50r_nets：
    signal_infos_raw 会剔 FBRX 网名，导致同 50R 类 FBRX 网上的器件断连、
    无法按共网串成链路。
    """
    nets_50r = {
        str(n).strip()
        for n in (getattr(schematic_res, "net_class_50r_nets", None) or ())
        if str(n or "").strip()
    }
    tables = []
    for cand in (
        getattr(schematic_res, "all_signal_infos_raw", None),
        getattr(schematic_res, "signal_infos_raw", None),
        signal_infos,
        getattr(schematic_res, "signal_infos", None),
    ):
        if cand and cand not in tables:
            tables.append(cand)
    rows: list = []
    seen_net: set[str] = set()
    for sig in tables:
        for si in sig or []:
            if not si or len(si) < 2:
                continue
            net = str(si[0] or "").strip()
            if not net or is_gnd_net(net) or net in seen_net:
                continue
            if nets_50r and net not in nets_50r:
                continue
            seen_net.add(net)
            rows.append(si)
        # 有 50R 清单且已从全量表扫过则够
        if nets_50r and rows and sig is tables[0]:
            break
    return rows


def collect_50r_devices_for_others(
    schematic_res,
    signal_infos=None,
) -> list[str]:
    """others 候选：与 50R 覆盖同一收集（含并位/不贴，不含连接器）。

    直接复用 collect_50r_net_devices，避免并位被 part_active 筛掉后两边口径不一致。
    """
    from refnum.coverage_50r import collect_50r_net_devices

    return list(collect_50r_net_devices(schematic_res, signal_infos))


# 兼容旧名
collect_50r_devices_including_srs_fbrx = collect_50r_devices_for_others


def _maps_for_others(schematic_res, signal_infos=None):
    """50R 类网拓扑图（含 FBRX 名 50R 网），供未覆盖器件串链。"""
    return build_net_device_maps(_50r_class_signal_rows(schematic_res, signal_infos))


def _is_anon_net(net: str) -> bool:
    """无名网：含 $$$（原理图自动网名）。"""
    return "$$$" in str(net or "")


def _meaningful_nets(nets: list[str]) -> list[str]:
    out: list[str] = []
    for n in nets or []:
        s = str(n or "").strip()
        if not s or is_gnd_net(s) or _is_anon_net(s):
            continue
        out.append(s)
    # 稳定去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def classify_ref_by_nets(nets: list[str]) -> tuple[str, tuple[str, ...]]:
    """按有名网语义打标签。返回 (tag, evidence_meaningful_nets)。"""
    meaningful = _meaningful_nets(nets)
    if not meaningful:
        return "匿名网($$$)", ()
    for tag, pat in _TAG_RULES:
        hits = [n for n in meaningful if pat.search(n)]
        if hits:
            return tag, tuple(hits[:8])
    return "其它有名网", tuple(meaningful[:8])


def _parse_pin_token(token: str) -> Optional[tuple[str, str]]:
    """Uxxxx-B.M10 / Ryyyy.1 → (Uxxxx, M10) / (Ryyyy, 1)。"""
    s = str(token or "").strip()
    m = _PIN_TOKEN_RE.match(s)
    if not m:
        return None
    return m.group(1), m.group(2)


def _is_useful_port_label(label: str) -> bool:
    lab = str(label or "").strip()
    if not lab or len(lab) > 40:
        return False
    if _SKIP_PORT_LABEL.match(lab):
        return False
    if lab.upper() in {"GND", "NC", "DNC", "EP"}:
        return False
    return True


def _net_to_pin_tokens(schematic_res, signal_infos=None) -> dict[str, list[str]]:
    """net → ['Uxxxx-B.M10', 'Ryyyy.1', ...]（来自 signal 第 3 列 pin 列表）。"""
    out: dict[str, list[str]] = {}
    for si in _50r_class_signal_rows(schematic_res, signal_infos):
        if not si or len(si) < 3:
            continue
        net = str(si[0] or "").strip()
        if not net or is_gnd_net(net):
            continue
        pins = si[2] if isinstance(si[2], (list, tuple)) else []
        bucket = out.setdefault(net, [])
        for p in pins:
            tok = str(p or "").strip()
            if tok and tok not in bucket:
                bucket.append(tok)
    return out


def _all_gate_pin_labels_from_doc(doc_content: str, part_key: str) -> dict[str, str]:
    """解析 part 类型定义中**全部 GATE** 的 pin→label（含 XO_IN 等后级 gate）。

    ``ComponentPINParser.get_pin_labels`` 只收第一段 GATE，会漏 M10=XO_IN。
    """
    if not doc_content or not part_key:
        return {}
    wanted = str(part_key).upper().replace("-", "_")
    lines = doc_content.splitlines()
    labels: dict[str, str] = {}
    for index, raw in enumerate(lines):
        norm = raw.upper().replace("-", "_")
        if wanted not in norm or " UND " not in f" {norm} ":
            continue
        for candidate in lines[index + 1 : index + 500]:
            stripped = candidate.strip()
            if not stripped:
                continue
            if stripped.startswith("*") and labels:
                break
            if stripped.startswith("GATE "):
                continue
            parts = stripped.split()
            if (
                len(parts) >= 4
                and re.fullmatch(r"[A-Za-z]*\d+", parts[0])
                and parts[1].isdigit()
            ):
                labels[parts[0]] = parts[-1]
            elif stripped.startswith("PART ") or stripped.startswith("*PART"):
                if labels:
                    break
        if labels:
            break
    return labels


def _pin_label_lookup(
    schematic_res,
    pin_parser,
    ic_ref: str,
    pin_id: str,
    cache: dict[str, dict[str, str]],
) -> Optional[str]:
    """查 IC 物理脚语义名（优先全 GATE 解析）。"""
    ic = str(ic_ref or "").strip()
    pid = str(pin_id or "").strip()
    if not ic or not pid:
        return None
    if ic not in cache:
        labels: dict[str, str] = {}
        part_name = ""
        try:
            part_name = str(
                (getattr(schematic_res, "part_infos_name", None) or {}).get(ic) or ""
            )
        except Exception:
            part_name = ""
        doc = getattr(pin_parser, "doc_content", None) if pin_parser is not None else None
        # 尝试 part 名 / 位号 多种 key
        keys = [k for k in (part_name, ic, part_name.split("-")[0] if part_name else "") if k]
        if doc:
            for key in keys:
                labels = _all_gate_pin_labels_from_doc(doc, key)
                if labels:
                    break
        if not labels and pin_parser is not None:
            for key in keys:
                try:
                    labels = dict(pin_parser.get_pin_labels(key) or {})
                except Exception:
                    labels = {}
                if labels:
                    break
        cache[ic] = labels
    lab = (cache.get(ic) or {}).get(pid)
    return str(lab).strip() if lab else None


def collect_neighbor_port_labels(
    ref: str,
    nets: list[str],
    *,
    net_to_pins: dict[str, list[str]],
    schematic_res,
    pin_parser=None,
    label_cache: Optional[dict[str, dict[str, str]]] = None,
) -> list[tuple[str, str, str]]:
    """ref 各网上邻接 IC 的 (ic_ref, pin_id, port_label)。

    被动件与邻接 IC 共网时，从 pin 令牌取口名（如 XO_IN）。
    """
    cache = label_cache if label_cache is not None else {}
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    me = str(ref or "").strip()
    for net in nets or []:
        for tok in net_to_pins.get(net) or []:
            parsed = _parse_pin_token(tok)
            if not parsed:
                continue
            ic, pid = parsed
            if ic == me:
                continue
            # 只认多端口/有源类邻接（U/FIL/PA…），跳过 R/L/C 脚名
            if not re.match(r"^(U|FIL|IC|SW|PA|LNA|QPM|QDM|FEM)", ic, re.I):
                continue
            lab = _pin_label_lookup(schematic_res, pin_parser, ic, pid, cache)
            if not lab or not _is_useful_port_label(lab):
                continue
            key = (ic, pid, lab)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def classify_ref(
    ref: str,
    nets: list[str],
    *,
    net_to_pins: Optional[dict[str, list[str]]] = None,
    schematic_res=None,
    pin_parser=None,
    label_cache: Optional[dict[str, dict[str, str]]] = None,
) -> tuple[str, tuple[str, ...]]:
    """综合有名网 + 邻接 port 口名分类。

    优先级：
      FBRX/SRS/GPS/JUMP/MIPI（网名）
      → 邻接 IC 有用 port 名（如 XO_IN，作独立类）
      → CLK/CTRL 网名
      → 其它有名网 / 匿名网
    """
    net_tag, net_ev = classify_ref_by_nets(nets)
    # 高优先级网名直接返回
    if net_tag in ("FBRX", "SRS", "GPS/GNSS", "JUMP", "MIPI/RFFE"):
        return net_tag, net_ev

    ports = []
    if schematic_res is not None and net_to_pins is not None:
        ports = collect_neighbor_port_labels(
            ref,
            nets,
            net_to_pins=net_to_pins,
            schematic_res=schematic_res,
            pin_parser=pin_parser,
            label_cache=label_cache,
        )
    if ports:
        # 多个 port 时：FBRX/GNSS 类口优先，否则取字典序稳定第一个
        def _port_rank(lab: str) -> tuple:
            u = lab.upper()
            if "FBRX" in u:
                return (0, lab)
            if "GNSS" in u or re.search(r"(^|_)GPS(_|$)", u):
                return (1, lab)
            if "XO" in u:
                return (2, lab)
            return (3, lab)

        best = min((p[2] for p in ports), key=_port_rank)
        # port 名作类名；证据含 口@位号 与有名网
        ev: list[str] = []
        for ic, pid, lab in ports:
            if lab == best:
                ev.append(f"{lab}@{ic}.{pid}")
        for n in net_ev:
            if n not in ev:
                ev.append(n)
        return best, tuple(ev[:10])

    return net_tag, net_ev


def _ref_tag_map(
    uncovered: list[str],
    dev_to_nets: dict[str, list[str]],
    *,
    net_to_pins: Optional[dict[str, list[str]]] = None,
    schematic_res=None,
    pin_parser=None,
) -> dict[str, tuple[str, tuple[str, ...]]]:
    cache: dict[str, dict[str, str]] = {}
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for ref in uncovered:
        out[ref] = classify_ref(
            ref,
            list(dev_to_nets.get(ref) or []),
            net_to_pins=net_to_pins,
            schematic_res=schematic_res,
            pin_parser=pin_parser,
            label_cache=cache,
        )
    return out


def _tag_sort_key(tag: str) -> tuple:
    if tag in _FIXED_TAG_ORDER:
        return (0, _FIXED_TAG_ORDER.index(tag), tag)
    if tag in _TAIL_TAGS:
        return (2, _TAIL_TAGS.index(tag), tag)
    # 动态 port 类（XO_IN 等）
    return (1, 0, tag)


def _tag_fill(tag: str):
    if tag in _TAG_FILL:
        return _TAG_FILL[tag]
    return _FILL_PORT


def _build_adj_within(
    refs: set[str],
    net_to_devs: dict[str, list[str]],
    dev_to_nets: dict[str, list[str]],
) -> dict[str, set[str]]:
    """未覆盖集合内：共非地网则相邻。"""
    adj: dict[str, set[str]] = {r: set() for r in refs}
    for ref in refs:
        for n in dev_to_nets.get(ref) or []:
            if is_gnd_net(n):
                continue
            for other in net_to_devs.get(n) or []:
                o = str(other or "").strip()
                if o and o in refs and o != ref:
                    adj[ref].add(o)
    return adj


def _connected_components(adj: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    comps: list[list[str]] = []
    for start in sorted(adj.keys()):
        if start in seen:
            continue
        q = deque([start])
        seen.add(start)
        comp: list[str] = []
        while q:
            u = q.popleft()
            comp.append(u)
            for v in sorted(adj.get(u) or ()):
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        comps.append(comp)
    return comps


def _is_chain_ic_ref(ref: str) -> bool:
    """链路端点类：IC/滤波/开关等（非 R/L/C 被动）。"""
    return bool(
        re.match(r"^(U|FIL|IC|SW|PA|LNA|QPM|QDM|FEM|LPF|DIP|DUP)", str(ref or ""), re.I)
    )


def _ref_pin_net_maps_full(
    net_to_pin_tokens: dict[str, list[str]],
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    """全表 pin 图：ref→[(pin, net)…]，net→[(ref, pin)…]（含 comp 外 IC 锚点）。"""
    ref_pins: dict[str, list[tuple[str, str]]] = defaultdict(list)
    net_pins: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for net, toks in (net_to_pin_tokens or {}).items():
        if is_gnd_net(net):
            continue
        for tok in toks or []:
            parsed = _parse_pin_token(tok)
            if not parsed:
                continue
            ref, pid = parsed
            if (pid, net) not in ref_pins[ref]:
                ref_pins[ref].append((pid, net))
            if (ref, pid) not in net_pins[net]:
                net_pins[net].append((ref, pid))
    return ref_pins, net_pins


def _ic_trx_rank(ref: str, schematic_res) -> int:
    """越大越靠 TRX 侧（TRX/PA/LNA 高，ANT/连接器低）。"""
    r = str(ref or "").strip()
    if not r:
        return 0
    if is_ant_path_node(r):
        return 5
    if schematic_res is not None:
        try:
            if is_transceiver_ref(r, schematic_res):
                return 100
        except Exception:
            pass
        try:
            if is_pa_lna_ref(r, schematic_res):
                return 90
        except Exception:
            pass
        try:
            if hasattr(schematic_res, "is_electronic_switch") and schematic_res.is_electronic_switch(r):
                return 25  # SRS 开关多在 ANT 侧
        except Exception:
            pass
    # 位号数字段启发：收发器段、开关偏 ANT 段、其它前端段（与下方区间一致）
    m = re.match(r"^U(\d+)", r, re.I)
    if m:
        n = int(m.group(1))
        if 1500 <= n < 1600:
            return 95
        if 2900 <= n < 3000:
            return 30
        if 2000 <= n < 3000:
            return 70
        return 50
    if _is_chain_ic_ref(r):
        return 40
    return 0


def _order_by_pin_net_pin(
    comp: list[str],
    net_to_pin_tokens: dict[str, list[str]],
    schematic_res=None,
) -> Optional[list[str]]:
    """引脚→网络→引脚，方向 **TRX→ANT**。

    - 优先从 **TRX/PA/LNA 侧 IC 脚** 出网（IC 可在覆盖集外，只作方向锚点）
    - 同网：先对地 stub，再按远网成组写出并联贯通件，整组相邻后再进入远网
    - 禁止对同网 sibling 逐个 DFS（会把并联电感甩到链尾）
    - IC 不贯穿多脚
    """
    comp_set = set(comp)
    if len(comp_set) <= 1:
        return list(comp_set) if comp_set else []
    ref_pins, net_pins = _ref_pin_net_maps_full(net_to_pin_tokens)
    # comp 内是否有任何 pin
    if not any(ref_pins.get(r) for r in comp_set):
        return None

    # 触及 comp 的网
    touch_nets = {
        net
        for r in comp_set
        for _pid, net in (ref_pins.get(r) or [])
    }

    # starts: (ref, pin, net, trx_rank, include_start)
    starts: list[tuple[str, str, str, int, bool]] = []
    # 1) 外部 IC 锚点（不在 comp，但脚落在 touch 网上）— TRX 侧优先
    for net in touch_nets:
        for ref, pid in net_pins.get(net) or []:
            if ref in comp_set:
                continue
            if not _is_chain_ic_ref(ref):
                continue
            rank = _ic_trx_rank(ref, schematic_res)
            starts.append((ref, pid, net, rank, False))
    # 2) comp 内 IC
    for ref in sorted(comp_set):
        if not _is_chain_ic_ref(ref):
            continue
        rank = _ic_trx_rank(ref, schematic_res)
        for pid, net in ref_pins.get(ref) or []:
            starts.append((ref, pid, net, rank, True))
    # 3) 无 IC：被动脚
    if not starts:
        for ref in sorted(comp_set, key=lambda r: (len(ref_pins.get(r) or []), r)):
            for pid, net in ref_pins.get(ref) or []:
                starts.append((ref, pid, net, 0, True))
            if starts:
                break

    # TRX 侧 rank 高的起点优先尝试
    starts.sort(key=lambda t: (-t[3], t[0], t[1], t[2]))

    def _walk(
        sref: str, spin: str, snet: str, *, include_start: bool
    ) -> tuple[list[str], int]:
        ordered: list[str] = []
        visited: set[str] = set()

        def visit_net(net: str, from_ref: str, from_pin: str) -> None:
            peers = [
                (r, p)
                for r, p in (net_pins.get(net) or [])
                if r != from_ref and r in comp_set and r not in visited
            ]
            stubs: list[tuple[str, str]] = []
            groups: dict[tuple[str, ...], list[tuple[str, str]]] = {}
            for r, p in peers:
                pins = ref_pins.get(r) or []
                # 脚数按该器件在全图上的信号脚计
                if len(pins) <= 1:
                    stubs.append((r, p))
                    continue
                far = tuple(
                    sorted(
                        {
                            onet
                            for op, onet in pins
                            if op != p and not is_gnd_net(onet) and onet != net
                        }
                    )
                )
                groups.setdefault(far, []).append((r, p))
            stubs.sort(key=lambda t: t[0])
            for r, _p in stubs:
                if r in visited:
                    continue
                visited.add(r)
                ordered.append(r)
            # 连接同一对远网的贯通件是并联对：整组相邻写出，再进入远网
            for far, members in sorted(
                groups.items(),
                key=lambda kv: (kv[0], tuple(sorted(m[0] for m in kv[1]))),
            ):
                members = sorted(members, key=lambda t: t[0])
                for r, _p in members:
                    if r in visited:
                        continue
                    visited.add(r)
                    ordered.append(r)
                if any(_is_chain_ic_ref(r) for r, _p in members):
                    continue
                from_r, from_p = members[0]
                for onet in far:
                    visit_net(onet, from_r, from_p)

        if include_start and sref in comp_set and sref not in visited:
            visited.add(sref)
            ordered.append(sref)
        elif not include_start:
            # 外部锚点：不写入链，只作出网 from
            pass
        visit_net(snet, sref, spin)
        core_n = len(visited)
        for r in sorted(comp_set):
            if r not in visited:
                ordered.append(r)
        return ordered, core_n

    best: list[str] = []
    best_score: tuple = (-1, -1, -1, -1)
    for sref, spin, snet, rank, inc in starts:
        cand, core_n = _walk(sref, spin, snet, include_start=inc)
        score = (
            core_n,
            rank,  # 同覆盖时更靠 TRX 的起点胜出
            1 if cand and not _is_chain_ic_ref(cand[0]) or True else 0,
            len(cand),
        )
        # 修正：优先 core，再 TRX rank
        score = (core_n, rank, len(cand))
        if score > best_score:
            best = cand
            best_score = score

    if not best:
        return None

    # 若两端可判 TRX 远近，保证 TRX 端在前（双端都是被动时看邻接 IC）
    def _end_trx(ref: str) -> int:
        sc = _ic_trx_rank(ref, schematic_res)
        for _pid, net in ref_pins.get(ref) or []:
            for oref, _op in net_pins.get(net) or []:
                if oref == ref:
                    continue
                sc = max(sc, _ic_trx_rank(oref, schematic_res))
        return sc

    if len(best) >= 2 and _end_trx(best[0]) < _end_trx(best[-1]):
        best = list(reversed(best))
    return best


def _farthest(adj: dict[str, set[str]], start: str, allowed: set[str]) -> tuple[str, list[str]]:
    """BFS 最远点及路径（仅 allowed 内）。"""
    prev: dict[str, Optional[str]] = {start: None}
    q = deque([start])
    last = start
    while q:
        u = q.popleft()
        last = u
        for v in sorted(adj.get(u) or ()):
            if v not in allowed or v in prev:
                continue
            prev[v] = u
            q.append(v)
    path: list[str] = []
    cur: Optional[str] = last
    while cur is not None:
        path.append(cur)
        cur = prev.get(cur)
    path.reverse()
    return last, path


def _is_rlct_ref(ref: str, schematic_res) -> bool:
    """是否 R/L/C/T 被动（链路可穿过）；非 RLCT 为停止界限。"""
    r = str(ref or "").strip()
    if not r:
        return False
    if schematic_res is not None:
        try:
            return bool(is_rlct(r, schematic_res))
        except Exception:
            pass
    return bool(re.match(r"^[RLCT]\d", r, re.I))


def _segment_comp_by_non_rlct(
    comp: list[str],
    adj: dict[str, set[str]],
    schematic_res,
) -> list[list[str]]:
    """以非 RLCT（U/FIL/开关等）为界限，把连通块拆成多条链路段。

    非 RLCT 为界限：去掉 stop 后 RLCT 分成若干岛；每个岛 + 邻接的 stop 各成一段。
    """
    comp_set = set(comp)
    if len(comp_set) <= 1:
        return [list(comp_set)] if comp_set else []

    stops = {r for r in comp_set if not _is_rlct_ref(r, schematic_res)}
    rlct_set = comp_set - stops

    if not stops:
        return [list(comp_set)]

    # RLCT 子图连通岛
    sub_adj = {
        r: {x for x in (adj.get(r) or set()) if x in rlct_set}
        for r in rlct_set
    }
    islands = _connected_components(sub_adj) if rlct_set else []

    segments: list[list[str]] = []
    covered: set[str] = set()

    for island in islands:
        isl = set(island)
        border = sorted(
            s
            for s in stops
            if any(s in (adj.get(n) or set()) for n in isl)
        )
        if not border:
            segments.append(sorted(isl))
            covered |= isl
            continue
        if len(border) == 1:
            # 单侧 stop：整岛 + 该 stop（星形一臂）
            seg = list(isl | {border[0]})
            segments.append(seg)
            covered |= set(seg)
            continue
        # 多 stop：整岛 + 全部 border 为一条（stop—RLCT—stop）
        # 若一岛贴多个 stop 且实际是星形多臂，拆成 per-stop 臂
        # 判断：岛内是否存在「只连某一个 stop 的割边」— 简化：
        # 对每个 border stop，取岛内与之相邻的 RLCT，只扩到不经过其它 border 邻点的子树
        multi_arm = True
        # 若任意两个 border 能在「岛+两 stop」内不经过第三人连通，则可能是串联
        # 实用策略：每个 stop 单独一臂 = {stop} ∪ 从该 stop 的邻接 RLCT 做 BFS 只走 RLCT
        for s in border:
            arm = {s}
            q = deque(
                n for n in (adj.get(s) or set())
                if n in isl
            )
            seen = set(q)
            while q:
                u = q.popleft()
                arm.add(u)
                for v in adj.get(u) or set():
                    if v in isl and v not in seen:
                        seen.add(v)
                        q.append(v)
            if len(arm) >= 2:
                segments.append(sorted(arm))
                covered |= arm
        # 岛内未被任何臂覆盖的 RLCT
        orphan = isl - covered
        if orphan:
            segments.append(sorted(orphan))
            covered |= orphan

    # 未入段的 stop（孤立或仅 stop—stop）
    for s in sorted(stops):
        if s in covered:
            continue
        neigh = [
            x for x in (adj.get(s) or set())
            if x in comp_set and x in stops
        ]
        if not neigh:
            segments.append([s])
            covered.add(s)
            continue
        for ns in sorted(neigh):
            if s < ns:
                segments.append([s, ns])
                covered.add(s)
                covered.add(ns)

    # 去重（相同成员只留一条）
    uniq: list[list[str]] = []
    seen_fs: set[frozenset[str]] = set()
    for seg in segments:
        fs = frozenset(seg)
        if len(fs) < 1 or fs in seen_fs:
            continue
        seen_fs.add(fs)
        uniq.append(list(seg))
    return uniq


def _orient_segment_unified(
    ordered: list[str],
    schematic_res,
) -> list[str]:
    """统一段内方向，避免同 stop 两臂一正一反。

    - **恰 1 个非 RLCT stop**：一律 **RLCT… → stop**（stop 在末尾）
    - **≥2 个 stop**：TRX 侧 stop 在前、ANT 侧在后
    - **无 stop**：保持传入序（已按 TRX→ANT 锚点排）
    """
    if len(ordered) < 2:
        return ordered
    stops = [r for r in ordered if not _is_rlct_ref(r, schematic_res)]
    if len(stops) == 1:
        s = stops[0]
        body = [r for r in ordered if r != s]
        if not body:
            return [s]
        # body 保持相对序；若原序是 s→body，则 body 需反转以维持走线方向
        if ordered[0] == s:
            body = list(reversed(body))
        return body + [s]
    if len(stops) >= 2:
        # 已按 TRX→ANT 排过；若首尾 stop 的 TRX 秩反了则整段反转
        def _rk(ref: str) -> int:
            return _ic_trx_rank(ref, schematic_res)

        if _rk(ordered[0]) < _rk(ordered[-1]):
            return list(reversed(ordered))
        # 若 stop 不在两端，把 TRX stop 挪到前、ANT 到后（保守：整段反转一次试）
        return ordered
    return ordered


def _order_one_segment(
    seg: list[str],
    adj: dict[str, set[str]],
    net_to_pin_tokens: Optional[dict[str, list[str]]] = None,
    schematic_res=None,
) -> list[str]:
    """单段内排序：引脚→网络→引脚；再统一 stop 朝向。"""
    remaining = set(seg)
    if len(remaining) <= 1:
        return list(remaining)

    ordered: list[str] = []
    if net_to_pin_tokens:
        pin_ord = _order_by_pin_net_pin(
            list(remaining), net_to_pin_tokens, schematic_res=schematic_res,
        )
        if pin_ord:
            ordered = [r for r in pin_ord if r in remaining]
            for r in sorted(remaining):
                if r not in ordered:
                    ordered.append(r)

    if not ordered:
        start = min(
            remaining,
            key=lambda r: (len((adj.get(r) or set()) & remaining), r),
        )
        end1, _ = _farthest(adj, start, remaining)
        _end2, spine = _farthest(adj, end1, remaining)
        if not spine:
            spine = [start]
        ordered = list(spine)
        leftover = remaining - set(ordered)
        while leftover:
            placed = False
            for at_end in (True, False):
                node = ordered[-1] if at_end else ordered[0]
                cands = sorted((adj.get(node) or set()) & leftover)
                if not cands:
                    continue
                nxt = cands[0]
                if at_end:
                    ordered.append(nxt)
                else:
                    ordered.insert(0, nxt)
                leftover.discard(nxt)
                placed = True
                break
            if placed:
                continue
            for i, node in enumerate(ordered):
                cands = sorted((adj.get(node) or set()) & leftover)
                if not cands:
                    continue
                nxt = cands[0]
                ordered.insert(i + 1, nxt)
                leftover.discard(nxt)
                placed = True
                break
            if not placed:
                nxt = min(leftover)
                ordered.append(nxt)
                leftover.discard(nxt)

    return _orient_segment_unified(ordered, schematic_res)


def _chain_order_component(
    comp: list[str],
    adj: dict[str, set[str]],
    net_to_pin_tokens: Optional[dict[str, list[str]]] = None,
    schematic_res=None,
) -> list[list[str]]:
    """连通块 → 若干有序链路。

    1) **非 RLCT 为停止界限**（U/FIL/开关等），拆成多段
    2) 每段内 **引脚→网络→引脚** 且 **TRX→ANT**
    """
    if not comp:
        return []
    segments = _segment_comp_by_non_rlct(comp, adj, schematic_res)
    out: list[list[str]] = []
    for seg in segments:
        if not seg:
            continue
        ordered = _order_one_segment(
            seg, adj, net_to_pin_tokens=net_to_pin_tokens, schematic_res=schematic_res,
        )
        if ordered:
            out.append(ordered)
    return out


def build_others_report(
    schematic_res,
    paths,
    signal_infos=None,
    pin_parser=None,
    *,
    copad_structures=None,
    covered_refs=None,
) -> OthersReport:
    """计算未覆盖 **50R** 器件 + 分类（网名+port）+ 链路。"""
    _ = signal_infos_of  # 保持与其它模块一致的 import 面
    net_to_devs, dev_to_nets = _maps_for_others(schematic_res, signal_infos)
    net_to_pins = _net_to_pin_tokens(schematic_res, signal_infos)
    all_50r = collect_50r_devices_for_others(schematic_res, signal_infos)
    # 与 50R 覆盖同一真相：手册格子上真有该位号才算覆盖
    if covered_refs is not None:
        covered = {str(x).strip() for x in covered_refs if str(x).strip()}
    else:
        covered = collect_refnum_covered_device_set(
            paths,
            copad_structures=copad_structures,
        )
    uncovered = [r for r in all_50r if r not in covered]
    report = OthersReport(
        uncovered=list(uncovered),
        n_50r=len(all_50r),
        n_covered=sum(1 for r in all_50r if r in covered),
    )
    if not uncovered:
        return report

    tag_map = _ref_tag_map(
        uncovered,
        dev_to_nets,
        net_to_pins=net_to_pins,
        schematic_res=schematic_res,
        pin_parser=pin_parser,
    )
    # 拓扑优先串链：未覆盖集合内按共网连通（禁止先按标签分桶再串，
    # 否则匿名网与 SRS 标签会把同一贯通链拆成两条）。
    unc_set = set(uncovered)
    adj = _build_adj_within(unc_set, net_to_devs, dev_to_nets)
    comps = _connected_components(adj)
    raw_paths: list[list[str]] = []
    for comp in comps:
        raw_paths.extend(
            _chain_order_component(
                comp, adj, net_to_pin_tokens=net_to_pins, schematic_res=schematic_res,
            )
        )

    def _chain_tag(path: list[str]) -> str:
        """整链标签：成员标签中优先级最高者（SRS 优于 匿名网）。"""
        tags = [tag_map[r][0] for r in path if r in tag_map]
        if not tags:
            return "匿名网($$$)"
        return min(tags, key=_tag_sort_key)

    # 按链标签分组编号
    seq_by_tag: dict[str, int] = defaultdict(int)
    chains: list[OthersChain] = []
    # 稳定：先按链标签优先级，再按首器件名
    for path in sorted(
        raw_paths,
        key=lambda p: (_tag_sort_key(_chain_tag(p)), p[0] if p else ""),
    ):
        tag = _chain_tag(path)
        seq_by_tag[tag] += 1
        seq = seq_by_tag[tag]
        ev: list[str] = []
        seen_n: set[str] = set()
        # 证据：先放与链标签一致的成员证据，再补其余
        ordered_refs = sorted(
            path,
            key=lambda r: (0 if tag_map.get(r, ("",))[0] == tag else 1, r),
        )
        for r in ordered_refs:
            for n in tag_map.get(r, ("", ()))[1]:
                if n not in seen_n:
                    seen_n.add(n)
                    ev.append(n)
        # 链上共网有名网也记一笔（便于看贯通）
        for r in path:
            for n in dev_to_nets.get(r) or []:
                if is_gnd_net(n) or "$$$" in n:
                    continue
                if n not in seen_n:
                    seen_n.add(n)
                    ev.append(n)
        prefix = {
            "FBRX": "F",
            "SRS": "S",
            "GPS/GNSS": "G",
            "JUMP": "J",
            "MIPI/RFFE": "M",
            "CLK/CTRL": "C",
            "其它有名网": "O",
            "匿名网($$$)": "A",
        }.get(tag, "P")
        chains.append(
            OthersChain(
                tag=tag,
                chain_id=f"{prefix}{seq}",
                refs=tuple(path),
                evidence_nets=tuple(ev[:12]),
            )
        )

    report.chains = chains
    tc: Counter[str] = Counter()
    for ch in chains:
        tc[ch.tag] += len(ch.refs)
    report.tag_counts = dict(tc)
    return report


def _style(cell, *, fill=None, header=False) -> None:
    cell.border = _BORDER
    cell.alignment = _CENTER
    if header:
        cell.font = _FONT_HDR
        cell.fill = _FILL_HDR
    elif fill is not None:
        cell.fill = fill


def write_others_sheet(ws, report: OthersReport) -> None:
    """写入 others 工作表：每条未覆盖链路两行（首列类型竖并，右延位号/空值）。

    类型取链分类（SRS / FBRX / 口名…），落在链路首列并竖并两行。
    往右一列一颗器件：上行位号，下行数值留空给人填。
    链与链之间空 ``OTHERS_LINK_GAP_ROWS`` 行。不改分类、串链、覆盖判定。
    """
    ws.cell(1, 1, "功能5 · others（50R 未进 refnum 主图的器件）")
    ws.cell(1, 1).font = _FONT_BOLD
    ws.cell(2, 1, f"others_build={BUILD_ID}")
    ws.cell(
        3, 1,
        f"50R合计={report.n_50r}  主图已覆盖={report.n_covered}  "
        f"未覆盖={len(report.uncovered)}  链路数={len(report.chains)}"
        "  每条链路两行：首列类型竖并，右延位号 / 数值（空，人手填）；链间空一行。",
    )
    ws.cell(3, 1).font = _FONT_BOLD

    r = 5
    if not report.chains:
        ws.cell(r, 1, "（无未覆盖器件 — 50R 已全部进入 refnum 主图）")
        return

    max_col = 1
    for idx, ch in enumerate(report.chains):
        if idx:
            r += OTHERS_LINK_GAP_ROWS
        fill = _tag_fill(ch.tag)
        _style(ws.cell(r, 1, ch.tag), fill=fill)
        _style(ws.cell(r + 1, 1), fill=fill)
        ws.merge_cells(
            start_row=r, start_column=1, end_row=r + 1, end_column=1,
        )
        for i, ref in enumerate(ch.refs, start=2):
            _style(ws.cell(r, i, ref), fill=fill)
            _style(ws.cell(r + 1, i), fill=fill)
            max_col = max(max_col, i)
        r += OTHERS_CHAIN_ROWS

    ws.column_dimensions["A"].width = 18
    for c in range(2, max_col + 1):
        ws.column_dimensions[get_column_letter(c)].width = 12
    for row_i in range(5, r):
        ws.row_dimensions[row_i].height = 18


def attach_others_sheet(
    wb,
    paths,
    schematic_res=None,
    signal_infos=None,
    pin_parser=None,
    *,
    copad_structures=None,
    sheet_name: str = "others",
    covered_refs=None,
) -> OthersReport:
    """在 Workbook 挂 others 页；无未覆盖时不建页。"""
    if schematic_res is None:
        return OthersReport()
    report = build_others_report(
        schematic_res,
        paths,
        signal_infos,
        pin_parser=pin_parser,
        copad_structures=copad_structures,
        covered_refs=covered_refs,
    )
    if not report.uncovered:
        return report
    # 放在末尾，避免打乱 ANT 顺序
    ws = wb.create_sheet(str(sheet_name)[:31])
    write_others_sheet(ws, report)
    return report


def summarize_others(report: OthersReport) -> dict[str, Any]:
    return {
        "n_50r": report.n_50r,
        "n_covered": report.n_covered,
        "n_uncovered": len(report.uncovered),
        "n_chains": len(report.chains),
        "by_tag": dict(report.tag_counts),
        "note": (
            f"others: 未覆盖{len(report.uncovered)}/"
            f"{report.n_50r} 链路{len(report.chains)}"
            if report.uncovered
            else f"others: 全覆盖({report.n_50r})"
        ),
    }


__all__ = [
    "BUILD_ID",
    "OthersChain",
    "OthersReport",
    "attach_others_sheet",
    "build_others_report",
    "classify_ref",
    "classify_ref_by_nets",
    "collect_50r_devices_for_others",
    "collect_50r_devices_including_srs_fbrx",  # 兼容别名
    "collect_neighbor_port_labels",
    "collect_refnum_covered_device_set",
    "summarize_others",
    "write_others_sheet",
]
