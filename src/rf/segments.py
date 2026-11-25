# -*- coding: utf-8 -*-
"""功能1 网络表分段：射频主链上一进多出才分界；按天线一条链路写完再写下一条。"""
from __future__ import annotations

import re
from collections import defaultdict

# 射频 SYZ / 网表扫频下限。0 Hz 是直流，SIwave 会丢掉该点。
RF_SWEEP_MIN_MHZ = 10
# 功能2/3 SIwave 抽取：线性扫频步进（不再用固定 100 点）。
RF_SWEEP_STEP_MHZ = 1


def rf_sweep_point_count(start_hz, stop_hz, step_mhz=None) -> int:
    """线性扫频点数：含起点和终点，步进默认 1 MHz，至少 2 点。"""
    lo = float(start_hz)
    hi = float(stop_hz)
    if hi < lo:
        lo, hi = hi, lo
    step = float(RF_SWEEP_STEP_MHZ if step_mhz is None else step_mhz)
    if step <= 0:
        step = float(RF_SWEEP_STEP_MHZ)
    n = int(round((hi - lo) / (step * 1e6))) + 1
    return max(2, n)


def clamp_rf_sweep_mhz(lo, hi):
    """把扫频区间卡到射频下限（10 MHz 起）。真实通带 617–960 等不受影响。"""
    lo_i, hi_i = int(round(float(lo))), int(round(float(hi)))
    if hi_i < lo_i:
        lo_i, hi_i = hi_i, lo_i
    lo_i = max(lo_i, RF_SWEEP_MIN_MHZ)
    if hi_i < lo_i:
        hi_i = lo_i
    return lo_i, hi_i


def clamp_rf_sweep_start_hz(start_hz):
    """功能2 SYZ 起点：网表若写 0Hz/0MHz，抬到 10 MHz。"""
    return max(float(start_hz), RF_SWEEP_MIN_MHZ * 1e6)


# 块名会进 SIwave 子工程 / PCB .sNp 文件名。Windows 把 / \ 当路径分隔，
# 段名带斜杠会拆成子目录，抽取卡死到超时；$$$ 也不能进文件名。
_FS_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def safe_segment_id(sid: str) -> str:
    """段 id：无名网 $$$→NET，路径非法字符改下划线。连字符留给写表时处理。"""
    text = _FS_ILLEGAL.sub("_", str(sid).replace("$$$", "NET"))
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_") or "UNNAMED"


def safe_component_name(name: str) -> str:
    """网络表第 2 列 / 抽取文件名：保留「口-段」之间的连字符，段内连字符与非法字符改下划线。

    ANT3-B38/41_PRX → ANT3-B38_41_PRX
    ANT3-$$$10773 → ANT3-NET10773
    DIV_ANT-COM → DIV_ANT-COM
    MAIN_ANT-B38_41_TRX → MAIN_ANT-B38_41_TRX
    """
    raw = str(name).replace("$$$", "NET")
    if "-" in raw:
        prefix, rest = raw.split("-", 1)
        if prefix and rest:
            return f"{prefix}-{safe_segment_id(rest)}"
    return safe_segment_id(raw)


def siwave_net_name(name: str) -> str:
    """SIwave 内部把网络名里的 / 存成 _。功能1 网络表第 1 列跟这个名字走。

    B32/B75_PRX → B32_B75_PRX
    B12/13/14/17-DRX → B12_13_14_17-DRX
    """
    return str(name).replace("/", "_")


_FBRX_SIWAVE = re.compile(r"FBRX", re.I)
_COUPLING_PIN = re.compile(r"^(COUPLING|COUPLED|CPL)$", re.I)
_THROUGH_PIN = re.compile(
    r"^(INPUT|IN|OUTPUT|OUT|THRU|THROUGH|ISO|ISOLATION|50OHM|50R|GND)$",
    re.I,
)
# 直通走线只用真正的通路脚。TERM/50R/ISO 是假负载，COUPLING 是抽头。
_THROUGH_WALK_PIN = re.compile(
    r"^(INPUT|IN|OUTPUT|OUT|THRU|THROUGH)$",
    re.I,
)
_PIN_FREQ_SUFFIX = re.compile(r"\(.*$")


def is_fbrx_siwave_name(name: str) -> bool:
    """网名带 FBRX 的取样段不进抽取。COUPLE 字样不够，要看耦合器脚角色。"""
    return bool(_FBRX_SIWAVE.search(str(name or "")))


def is_coupling_pin_label(label) -> bool:
    """GATE 脚名：COUPLING 才是取样口；INPUT/OUTPUT/THRU 不是。"""
    text = str(label or "").strip().upper().replace(" ", "").replace("-", "_")
    if not text or _THROUGH_PIN.fullmatch(text):
        return False
    return bool(_COUPLING_PIN.fullmatch(text))


def _coupler_pin_text(label) -> str:
    return str(label or "").strip().upper().replace(" ", "").replace("-", "_")


def _coupler_pin_role_token(label) -> str:
    """GATE 脚名去掉频段括号：Output(600-2690) → OUTPUT。"""
    return _PIN_FREQ_SUFFIX.sub("", _coupler_pin_text(label)).strip("_")


def is_coupler_through_walk_pin(label) -> bool:
    """当前段只沿 INPUT/OUTPUT/THRU 走；抽头和假负载不算直通。"""
    return bool(_THROUGH_WALK_PIN.fullmatch(_coupler_pin_role_token(label)))


def coupler_continue_nets(comp, schematic, pin_parser=None) -> set | None:
    """312701 继续走的网。有脚名时只给直通脚；没脚名不滤，避免把通路掐死。"""
    if not str(comp or "") or not _part_code(schematic, comp).startswith("312701"):
        return None
    allowed = set()
    saw_role = False
    for net in _schematic_nets_of(schematic, comp):
        pin = _pin_of_comp_on_net(schematic, comp, net)
        lab = pin_label_for_ref(schematic, pin_parser, comp, pin)
        if not lab:
            continue
        saw_role = True
        if is_coupler_through_walk_pin(lab):
            allowed.add(net)
    if not saw_role:
        return None
    return allowed


def _coupler_nets_by_pin_role(schematic, pin_parser, coupling: bool) -> set:
    """312701：COUPLING 脚上网 vs INPUT/OUTPUT/THRU 脚上网。"""
    out = set()
    parts = getattr(schematic, "part_infos", None) or {}
    for ref, code in parts.items():
        if not str(code or "").startswith("312701"):
            continue
        for net in _schematic_nets_of(schematic, ref):
            pin = _pin_of_comp_on_net(schematic, ref, net)
            lab = pin_label_for_ref(schematic, pin_parser, ref, pin)
            if not lab:
                continue
            text = _coupler_pin_role_token(lab)
            if coupling:
                if is_coupling_pin_label(text):
                    out.add(net)
            elif _THROUGH_PIN.fullmatch(text) and text not in ("GND",):
                out.add(net)
    return out


def coupler_coupling_nets(schematic, pin_parser=None) -> set:
    """从 COUPLING 脚出去的网才是 FBRX 取样。"""
    return _coupler_nets_by_pin_role(schematic, pin_parser, True)


def coupler_through_nets(schematic, pin_parser=None) -> set:
    """INPUT/OUTPUT/THRU 脚上的网是主路，即使网名带 COUPLE。"""
    return _coupler_nets_by_pin_role(schematic, pin_parser, False)


def fbrx_siwave_skip_names(paths, schematic=None, pin_parser=None) -> set:
    """取样链上的网络不进仿真网络表。器件位号不进集合。"""
    skip = set()
    for path in paths or ():
        for node in path or ():
            name = str(node)
            if not name:
                continue
            if schematic is not None:
                try:
                    if not schematic.is_network(name):
                        continue
                except Exception:
                    if not is_fbrx_siwave_name(name):
                        continue
            elif not is_fbrx_siwave_name(name):
                continue
            skip.add(name)
    skip |= coupler_coupling_nets(schematic, pin_parser)
    skip -= coupler_through_nets(schematic, pin_parser)
    return skip


def skip_fbrx_for_siwave(name: str, extra=None) -> bool:
    if is_fbrx_siwave_name(name):
        return True
    return bool(extra) and str(name) in extra


def siwave_table_expected_nets(visited, schematic, seg=None, extra_skip=None):
    """应进 SIwave 表的网：分段留下来的（FBRX 除外）。

    图上有、分段丢掉的不算缺：从开关/滤波往下只有电阻电容电感、没有下一级芯片，
    本来就不写表。没有 seg 时退回按 visited 扫。
    """
    extra_skip = set(extra_skip or ())

    def _keep_net(name):
        name = str(name or "")
        if not name:
            return False
        try:
            if not schematic.is_network(name):
                return False
        except Exception:
            return False
        return not skip_fbrx_for_siwave(name, extra_skip)

    if seg:
        out = set()
        for sid, nets in seg.items():
            if skip_fbrx_for_siwave(sid, extra_skip):
                continue
            for net in nets or ():
                if _keep_net(net):
                    out.add(str(net))
        return out
    out = set()
    for node in visited or ():
        if _keep_net(node):
            out.add(str(node))
    return out


def skipped_segment_net_label(item) -> str:
    """给用户看的网名：原理图网络，不用工具起的 LB_2。"""
    nets = [str(n) for n in (item or {}).get("nets") or () if n]
    name = str((item or {}).get("name") or "")
    if name in nets:
        return name
    for n in nets:
        if _net_is_srs(n):
            return n
    for n in nets:
        if _named_rf_net(n) and is_meaningful_segment_name(n):
            return n
    for n in nets:
        if str(n).startswith("$$$"):
            return n
    if nets:
        return nets[0]
    return name


def describe_skipped_segment(item) -> str:
    """不进网络表的原因，给人看。"""
    reason = str((item or {}).get("reason") or "")
    if reason == "fbrx":
        return "耦合器取样脚，不抽"
    if reason == "srs":
        return "SRS，接到另一根天线，不抽"
    fork = str((item or {}).get("fork") or "").strip() or "开关或滤波"
    if reason == "series_nc":
        nc = str((item or {}).get("nc") or "").strip() or "不贴件"
        return f"从{fork}往下有不贴件{nc}，通路开路，不抽"
    match = [str(x) for x in (item or {}).get("matching") or () if x]
    if match:
        return f"从{fork}往下只有{','.join(match)}，没有芯片，不抽"
    return f"从{fork}往下没有芯片，不抽"


def _gnd_refs(schematic) -> set:
    gnd_inf = getattr(schematic, "gnd_inf", None)
    if gnd_inf and len(gnd_inf) > 2:
        return {str(x) for x in (gnd_inf[2] or ()) if x}
    return set()


def _matching_refs_on_nets(nets, schematic, parent_fork=None) -> list:
    out = []
    seen = set()
    for net in nets or ():
        for ref in _devices_on_net(schematic, net):
            ref = str(ref or "")
            if not ref or ref == parent_fork or ref in seen:
                continue
            if not _looks_like_matching_rlc(ref, schematic):
                continue
            seen.add(ref)
            out.append(ref)
    return out


def _is_series_nc_ref(ref, schematic) -> bool:
    """不贴且不是对地并联：串联开路，不能再往下找。"""
    ref = str(ref or "")
    if not ref:
        return False
    active = getattr(schematic, "part_active", None) or {}
    if active.get(ref, True) is not False:
        return False
    return ref not in _gnd_refs(schematic)


def _series_nc_on_nets(nets, schematic, parent_fork=None):
    for net in nets or ():
        for ref in _devices_on_net(schematic, net):
            ref = str(ref or "")
            if not ref or ref == parent_fork:
                continue
            if _is_series_nc_ref(ref, schematic):
                return ref
    return None


def _net_is_srs(name) -> bool:
    return "SRS" in str(name or "").upper()


def _segment_hits_srs(sid, nets) -> bool:
    for n in list(nets or ()) + [sid]:
        if _net_is_srs(n):
            return True
    return False


def filter_skipped_in_50r(skipped, fifty_r=None):
    """只留 50R 类里的不写段。fifty_r 为 None 时不过滤。"""
    rows = list(skipped or ())
    if fifty_r is None:
        return rows
    names = {str(n) for n in fifty_r if n}
    out = []
    for item in rows:
        nets = [str(n) for n in (item.get("nets") or ()) if n]
        title = str(item.get("name") or "")
        if title in names or any(n in names for n in nets):
            out.append(item)
    return out


_RF_NET_HINT = re.compile(
    r"TX|RX|TRX|PRX|DRX|FBRX|ANT|GNSS|GPS|UHB|MHB|\bB\d|\bN\d",
    re.I,
)


def _named_rf_net(name) -> bool:
    text = str(name or "")
    if not text or text.startswith("$$$") or text == "COM":
        return False
    if re.fullmatch(r"ANT\d+", text, re.I):
        return False
    return bool(_RF_NET_HINT.search(text))


def _signal_tables(schematic):
    """50R 主表 + 全量（含 SRS 等未进 50R 的网）。"""
    out = []
    seen = set()
    for attr in ("signal_infos_raw", "signal_infos", "all_signal_infos_raw"):
        table = getattr(schematic, attr, None) or ()
        key = id(table)
        if not table or key in seen:
            continue
        seen.add(key)
        out.append(table)
    return out


def _schematic_nets_of(schematic, ref):
    found = []
    seen = set()
    for source in _signal_tables(schematic):
        for item in source:
            if len(item) < 2 or ref not in item[1]:
                continue
            name = item[0]
            if name in seen:
                continue
            seen.add(name)
            found.append(name)
    return found


def rf_nets_of_comp(schematic, ref):
    """该位号接到的非地网。"""
    return [n for n in _schematic_nets_of(schematic, ref) if not _is_gnd_net_name(n)]


def _devices_on_net(schematic, net):
    for source in _signal_tables(schematic):
        for item in source:
            if item and item[0] == net:
                return list(item[1])
    return []


def _pin_of_comp_on_net(schematic, comp, net):
    for source in (
        getattr(schematic, "signal_infos_raw", None) or (),
        getattr(schematic, "signal_infos", None) or (),
    ):
        for item in source:
            if not item or item[0] != net or len(item) < 3:
                continue
            if comp not in (item[1] or ()):
                continue
            for conn in item[2] or ():
                text = str(conn)
                if text.startswith(f"{comp}."):
                    return text.split(".", 1)[1]
    return None


_SKIP_PORT_LABEL = re.compile(r"^(GND\d*|NC\d*|N/?C)$", re.I)
_GENERIC_PORT_LABEL = re.compile(
    r"^(RF\d*|RFC|ANT|COM|IN\d*|OUT\d*|TX\d*|RX\d*|PRX|DRX|TRX)$",
    re.I,
)
_ROLE_SUFFIX = re.compile(
    r"(_SEC)?(_PRX|_DRX|_TX\d*|_TRX\d*|_RX|_SAW|_LNA_IN|_PA|_LNA|_COM)$",
    re.I,
)


def is_meaningful_segment_name(name) -> bool:
    """段名要能当频段/通路名。RF4/ANT/COM 这种脚名不算。"""
    text = str(name or "").strip()
    if not text or text.startswith("$$$") or text == "COM":
        return False
    if re.fullmatch(r"ANT\d+", text, re.I):
        return False
    return not _GENERIC_PORT_LABEL.fullmatch(text.replace("-", "_"))


def band_stem_from_segment_name(name) -> str:
    """B5_B26_SEC_PRX → B5_B26，B20_TX1 → B20。"""
    text = safe_segment_id(str(name or ""))
    prev = None
    while text and text != prev:
        prev = text
        nxt = _ROLE_SUFFIX.sub("", text)
        if nxt == text:
            break
        text = nxt
    return text.strip("_")


def com_name_from_child_segments(child_names):
    """后面几段的频段名合成 B5_B26_COM / B20_COM。"""
    stems = []
    for n in child_names or ():
        if not is_meaningful_segment_name(n):
            continue
        stem = band_stem_from_segment_name(n)
        if stem and stem.upper() != "COM":
            stems.append(stem)
    if not stems:
        return None
    uniq = sorted(set(stems), key=lambda s: (-len(s), s))
    return f"{uniq[0]}_COM"


def _part_infos_of(schematic) -> dict:
    return getattr(schematic, "part_infos", None) or {}


def is_snp_device(ref, schematic=None) -> bool:
    """第二层：料号前缀是射频滤波/开关/LNA/PA/多工器等，按导入 SnP 的器件。"""
    try:
        from schematic.SchematicNetComponentProfiler import valid_prefixes
    except Exception:
        valid_prefixes = (
            "312706", "312701", "312427", "312423", "312702", "312799",
            "312406", "312405", "312703", "312425",
        )
    code = str(_part_infos_of(schematic).get(ref, "") or "")
    return any(code.startswith(p) for p in valid_prefixes)


def _looks_like_matching_rlc(ref: str, schematic=None) -> bool:
    """匹配电阻电容电感。先看位号，料号能认定是 SnP 器件则不算匹配件。

    名字：C/R/L/T 后必须紧跟数字（C1504、T1400）。CON501/TRIP/LNA 不是。
    """
    if is_snp_device(ref, schematic):
        return False
    ru = str(ref or "").upper()
    if ru.startswith(("LPF", "LNA")):
        return False
    return len(ru) >= 2 and ru[0] in "CRLT" and ru[1].isdigit()


def is_ic_device(ref, schematic=None) -> bool:
    """构图用：不是匹配 RLC 就按 IC/SnP。"""
    return not _looks_like_matching_rlc(ref, schematic)


def pin_label_for_ref(schematic, pin_parser, comp, pin) -> str | None:
    """原理图 GATE 脚名（如 TRIP.3 → MLB-MB-HB）。GND/NC 不算。"""
    if pin_parser is None or not comp or pin is None or pin == "":
        return None
    pin = str(pin)
    models = []
    for attr in ("part_infos_name", "part_infos"):
        val = (getattr(schematic, attr, None) or {}).get(comp)
        if val:
            models.append(str(val))
    models.append(str(comp))
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            labels = pin_parser.get_pin_labels(model) or {}
        except Exception:
            continue
        lab = labels.get(pin) or labels.get(pin.upper()) or labels.get(pin.lower())
        text = str(lab or "").strip()
        if text and not _SKIP_PORT_LABEL.fullmatch(text):
            return text
    return None


def _device_port_segment_name(ref, lab) -> str:
    """器件脚名作段名时带上位号：DIP1200 + HB → DIP1200-HB。

    两个分频器都可以有 HB 脚；不带位号会合成 ANT3-HB 一组。
    """
    ref = str(ref or "").strip()
    lab = str(lab or "").strip()
    if not lab:
        return ref
    if not ref:
        return lab
    ru, lu = ref.upper(), lab.upper()
    if lu == ru or lu.startswith(ru + "-") or lu.startswith(ru + "_"):
        return lab
    return f"{ref}-{lab}"


def _segment_port_name(sid, nets, schematic, pin_parser, fork=None):
    """段内没有有名网时，用所接多端口件的 GATE 脚名（带位号）。"""
    if pin_parser is None:
        return None
    candidates = []
    if fork and fork[0] and fork[1]:
        candidates.append((fork[0], str(fork[1])))
    nets_to_scan = list(nets or ())
    if schematic.is_network(sid) and sid not in nets_to_scan:
        nets_to_scan.append(sid)
    for net in nets_to_scan:
        for ref in _devices_on_net(schematic, net):
            if _looks_like_matching_rlc(ref):
                continue
            pin = _pin_of_comp_on_net(schematic, ref, net)
            if pin:
                candidates.append((ref, pin))
    seen = set()
    for comp, pin in candidates:
        key = (comp, pin)
        if key in seen:
            continue
        seen.add(key)
        lab = pin_label_for_ref(schematic, pin_parser, comp, pin)
        if lab and is_meaningful_segment_name(lab):
            return _device_port_segment_name(comp, lab)
    return None


def _is_extract_payload_device(ref, schematic, parent_fork=None) -> bool:
    """本段除上一分界和 R/L/C 匹配外还有器件（开关下一级、座子、滤波、IC）才有抽取意义。"""
    ref = str(ref or "").strip()
    if not ref or ref == parent_fork:
        return False
    if _looks_like_matching_rlc(ref):
        return False
    return True


def _is_gnd_net_name(name) -> bool:
    u = str(name or "").strip().upper()
    return u in ("GND", "GROUND", "AGND", "PGND", "VSS")


def _segment_payload_nets(sid, nets, schematic):
    """判定本段有没有下一颗芯片时，段名和匹配另一侧的网也要算。

    开关掷出后常先是有名网+匹配，下一颗芯片在匹配另一侧（有名或 $$$）。
    只看本张网会误判成找不到下一颗件。
    """
    out = []
    seen = set()
    for n in list(nets or ()) + [sid]:
        name = str(n or "")
        if not name or name == "COM" or name in seen or _is_gnd_net_name(name):
            continue
        try:
            if not schematic.is_network(name):
                continue
        except Exception:
            continue
        seen.add(name)
        out.append(name)
    bridged = _bridge_named_rf_net(schematic, out)
    if bridged and bridged not in seen and not _is_gnd_net_name(bridged):
        seen.add(bridged)
        out.append(bridged)
    i = 0
    while i < len(out):
        net = out[i]
        i += 1
        for ref in _devices_on_net(schematic, net):
            if not _looks_like_matching_rlc(ref, schematic):
                continue
            if _is_series_nc_ref(ref, schematic):
                continue
            for other in _schematic_nets_of(schematic, ref):
                if (
                    not other
                    or other == net
                    or other in seen
                    or _is_gnd_net_name(other)
                ):
                    continue
                try:
                    if not schematic.is_network(other):
                        continue
                except Exception:
                    continue
                seen.add(other)
                out.append(other)
    return out


def _segment_reaches_next_fork(nets, schematic, parent_fork=None) -> bool:
    """从上一分界出发，必须接到第二个一进多出/滤波/芯片才写表。中间 NC 掐断的半截丢掉。"""
    for net in nets or ():
        for ref in _devices_on_net(schematic, net):
            if _is_extract_payload_device(ref, schematic, parent_fork):
                return True
    return False


def _bridge_named_rf_net(schematic, nets):
    """无名段网常被一颗匹配拆开，另一侧才是 B20_TX0 这种有名射频网。

    只许穿过 R/L/C 匹配。开关/DUP/滤波另一脚上的有名网是下一段，不能并进来。
    """
    for net in nets:
        for ref in _devices_on_net(schematic, net):
            if not _looks_like_matching_rlc(ref):
                continue
            for other in _schematic_nets_of(schematic, ref):
                if (
                    other != net
                    and _named_rf_net(other)
                    and not is_fbrx_siwave_name(other)
                ):
                    return other
    return None


def _part_code(schematic, ref: str) -> str:
    try:
        return str(schematic.part_infos.get(ref, "") or "")
    except Exception:
        return ""


_NON_RF_PIN = re.compile(
    r"^(GND\d*|NC\d*|N/?C|VDD\d*|VCC\d*|VSS|VBAT|VIO|VDDIO|EN|ENABLE|BIAS|CTRL)$",
    re.I,
)


def _port_role_token(label) -> str:
    """脚名角色：Output(600-2690) → OUTPUT；1_IN → IN。"""
    text = _coupler_pin_role_token(label)
    if "_" in text:
        left, right = text.split("_", 1)
        if left.isdigit() and right:
            return right
    return text


def _is_non_rf_port_label(label) -> bool:
    token = _port_role_token(label)
    raw = str(label or "").strip().upper().replace(" ", "")
    if _is_gnd_net_name(token) or _is_gnd_net_name(raw):
        return True
    if token and (_NON_RF_PIN.fullmatch(token) or _SKIP_PORT_LABEL.fullmatch(token)):
        return True
    if raw and (_NON_RF_PIN.fullmatch(raw) or _SKIP_PORT_LABEL.fullmatch(raw)):
        return True
    return False


def _gate_pin_label(schematic, pin_parser, comp, pin) -> str | None:
    """GATE 原脚名，含 GND/NC。段名接口会把这些藏掉，口数统计不能跟着藏。"""
    if pin in (None, ""):
        return None
    labels = _gate_labels_for_comp(schematic, pin_parser, comp)
    key = str(pin)
    lab = labels.get(key) or labels.get(key.upper()) or labels.get(key.lower())
    text = str(lab or "").strip()
    return text or None


def _is_rf_path_port(schematic, pin_parser, comp, net, pin) -> bool:
    """只认射频通路口。地、空脚、电源、使能不算。"""
    if _is_gnd_net_name(net):
        return False
    lab = _gate_pin_label(schematic, pin_parser, comp, pin)
    if lab and _is_non_rf_port_label(lab):
        return False
    return True


def _rf_port_count_from_snp(comp) -> int:
    """S 参数文件里的口就是射频口（地不进 sNp）。"""
    try:
        from rf.SnpFileParser import SNPFileInfo
        inst = SNPFileInfo.get_instance_by_ref_des(comp)
    except Exception:
        return 0
    info = getattr(inst, "pin_info", None) if inst else None
    if not info:
        return 0
    n = 0
    for data in info.values():
        name = str((data or {}).get("name") or "")
        if name and _is_non_rf_port_label(name):
            continue
        n += 1
    return n


def _gate_labels_for_comp(schematic, pin_parser, comp) -> dict:
    """原理图 GATE：物理脚号 → 脚名。"""
    if pin_parser is None or not comp:
        return {}
    models = []
    for attr in ("part_infos_name", "part_infos"):
        val = (getattr(schematic, attr, None) or {}).get(comp)
        if val:
            models.append(str(val))
    models.append(str(comp))
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            labels = pin_parser.get_pin_labels(model) or {}
        except Exception:
            continue
        if labels:
            return dict(labels)
    return {}


def _rf_port_count_from_schematic_nets(schematic, comp, pin_parser=None) -> int:
    """原理图连线：该位号接到的非地、非电源网。"""
    seen = set()
    for net in _schematic_nets_of(schematic, comp):
        pin = _pin_of_comp_on_net(schematic, comp, net)
        if not _is_rf_path_port(schematic, pin_parser, comp, net, pin):
            continue
        seen.add(str(pin) if pin not in (None, "") else str(net))
    return len(seen)


def _rf_port_count_on_comp(schematic, comp, pin_parser=None) -> int:
    """射频口数：S 参数口与原理图非地脚取大。

    S 参数文件里的口就是射频口（.s2p=2、.s3p=3）。文件缺脚时用原理图：
    GATE 脚名只用来去掉 GND/NC/电源，不拿整份 GATE 套到别的位号。
    """
    return max(
        _rf_port_count_from_snp(comp),
        _rf_port_count_from_schematic_nets(schematic, comp, pin_parser),
    )


def _is_coupler_through_not_fork(schematic, comp, pin_parser=None) -> bool:
    """直通 + 耦合抽头：一条射频直通，不是一进多出。

    脚名同时有 INPUT/OUTPUT 和 COUPLING 就是这类；312701 是耦合器料号前缀。
    """
    if _part_code(schematic, comp).startswith("312701"):
        return True
    names = []
    try:
        from rf.SnpFileParser import SNPFileInfo
        inst = SNPFileInfo.get_instance_by_ref_des(comp)
        info = getattr(inst, "pin_info", None) if inst else None
        if info:
            names.extend(str((d or {}).get("name") or "") for d in info.values())
    except Exception:
        pass
    names.extend(
        str(v) for v in _gate_labels_for_comp(schematic, pin_parser, comp).values()
    )
    tokens = [_port_role_token(n) for n in names if n]
    has_cpl = any(is_coupling_pin_label(t) for t in tokens)
    has_thru = any(is_coupler_through_walk_pin(t) for t in tokens)
    return bool(has_cpl and has_thru)


def _divider_pin_role(label) -> str:
    """分频/三工器脚：公共口 com，频段掷 throw。地和空脚不算。"""
    token = re.sub(r"[^A-Z0-9]", "", _port_role_token(label))
    if not token or _is_non_rf_port_label(label):
        return ""
    if token in ("ANT", "COM", "COMMON", "RFC") or token.endswith("ANT") or token.endswith("COM"):
        return "com"
    return "throw"


def coupler_exit_pins(comp, schematic, pin_parser, entry_pin):
    """耦合器只在 INPUT/OUTPUT/THRU 之间走。抽头和 50Ω 假负载不是出口。

    从抽头或假负载进来，主路径不穿过这颗器件。没脚名时不限制。
    """
    if entry_pin in (None, ""):
        return None
    labels = _gate_labels_for_comp(schematic, pin_parser, comp)
    through = []
    named = False
    for pin, lab in labels.items():
        if not str(lab or "").strip():
            continue
        named = True
        if is_coupler_through_walk_pin(lab):
            through.append(str(pin))
    if not named or len(through) < 2:
        return None
    entry = str(entry_pin).split(".")[-1]
    if entry not in through:
        return []
    return [int(pin) for pin in through if pin != entry]


def device_exit_pins(comp, schematic, pin_parser, entry_pin):
    """从这只脚进，主路径只能从这些脚出。

    返回脚号列表；None 表示这里不按脚限制。
    分频器 / 三工器 / 双工器 / 多工器：公共口 ↔ 各掷，掷和掷不通。
    耦合器：只在直通脚之间走。
    SAW 若原理图有公共口，同样只走公共口 ↔ 各掷；没有则交给 S 参数。
    开关、一进一出滤波、匹配件：None。开关另看前后端，两端件没有第三掷。
    """
    pn = _part_code(schematic, comp)
    if pn.startswith(("312702", "312703", "312706")):
        return frequency_divider_exit_pins(comp, schematic, pin_parser, entry_pin)
    if pn.startswith("312701"):
        return coupler_exit_pins(comp, schematic, pin_parser, entry_pin)
    return None


def frequency_divider_exit_pins(comp, schematic, pin_parser, entry_pin):
    """只许公共口↔各掷。LB 不能串到 MHB，再被带到另一根天线。

    认不出公共口时不限制，避免没脚名把通路掐死。
    """
    if entry_pin in (None, ""):
        return None
    labels = _gate_labels_for_comp(schematic, pin_parser, comp)
    roles = {}
    for pin, lab in labels.items():
        role = _divider_pin_role(lab)
        if role:
            roles[str(pin)] = role
    com = [p for p, role in roles.items() if role == "com"]
    throws = [p for p, role in roles.items() if role == "throw"]
    if not com or not throws:
        return None
    entry = str(entry_pin).split(".")[-1]
    if entry in com:
        return [int(p) for p in throws]
    if entry in throws:
        return [int(p) for p in com]
    return None


def is_series_through_filter(comp, schematic, pin_parser=None) -> bool:
    """一进一出滤波（射频口 < 3）。路径要穿过去，不能当终点。"""
    pn = _part_code(schematic, comp)
    if not pn.startswith(("312799", "312706")):
        return False
    return not is_segment_fork(comp, schematic, 0, pin_parser)


def is_segment_fork(comp, schematic, rf_out_count: int, pin_parser=None) -> bool:
    """一进多出 = 器件射频口 ≥ 3。口数不看料号。"""
    try:
        if schematic.is_network(comp):
            return False
    except Exception:
        pass
    if _is_coupler_through_not_fork(schematic, comp, pin_parser):
        return False
    return _rf_port_count_on_comp(schematic, comp, pin_parser) >= 3


def _is_filter_like(comp, schematic) -> bool:
    pn = _part_code(schematic, comp)
    if pn.startswith(("312706", "312799", "312702", "312703")):
        return True
    try:
        if schematic.is_SAWFBAR(comp) or schematic.is_frequency_divider(comp):
            return True
    except Exception:
        pass
    try:
        pt = (schematic.get_part_type(comp) or "").upper()
    except Exception:
        pt = ""
    return any(
        token in pt
        for token in ("SAW", "FILTER", "DIVIDER", "MULTIPLEX", "DIPLEX")
    )


def _union_mhz(a, b):
    if not a:
        return b
    if not b:
        return a
    return (min(a[0], b[0]), max(a[1], b[1]))


def _switch_pin_kind(schematic, pin_parser, comp, pin):
    """开关脚角色：COM/ANT 是公共口，RF* 是掷。不是开关则 None。"""
    try:
        if not schematic.is_electronic_switch(comp):
            return None
    except Exception:
        return None
    lab = pin_label_for_ref(schematic, pin_parser, comp, pin) or ""
    token = re.sub(r"[^A-Z0-9]", "", str(lab).upper())
    if token in ("ANT", "COM", "RFC", "COMMON") or token.endswith("ANT") or token.endswith("COM"):
        return "com"
    if token.startswith("RF"):
        return "throw"
    return None


def _cover_switch_com_freq(seg_freq, parent_named, fork_named, schematic, pin_parser):
    """多进一出：COM/ANT 侧并上各 RF 掷；RF 公共端父段抽取盖住 COM 侧。

    天线 COM 段仍是 600-6000，不改。
    """
    com_kids = []
    throw_union = None
    for child, fork in (fork_named or {}).items():
        if not fork:
            continue
        comp, pin = fork[0], fork[1] if len(fork) > 1 else None
        kind = _switch_pin_kind(schematic, pin_parser, comp, pin)
        if kind == "throw":
            throw_union = _union_mhz(throw_union, seg_freq.get(child))
        elif kind == "com":
            com_kids.append(child)
    for child in com_kids:
        seg_freq[child] = _union_mhz(seg_freq.get(child), throw_union) or seg_freq.get(child)
        parent = (parent_named or {}).get(child)
        if parent and parent != "COM" and parent in seg_freq:
            seg_freq[parent] = _union_mhz(seg_freq.get(parent), seg_freq.get(child)) or seg_freq.get(parent)
    return seg_freq


def _band_on_node(rf_graph, node_id):
    node = rf_graph.nodes.get(node_id) if rf_graph is not None else None
    bands = getattr(node, "band_info", None) if node else None
    los, his = [], []
    for bi in bands or []:
        m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(bi))
        if m:
            los.append(int(m.group(1)))
            his.append(int(m.group(2)))
    if los and his:
        return min(los), max(his)
    return None


def tx_rx_role(name) -> str | None:
    """段名里的 TX / RX 掷。TRX、TXRX 是公共端，不是掷。"""
    tokens = [t for t in re.split(r"[_-]+", str(name or "").upper()) if t]
    rx = {"RX", "DRX", "PRX"}
    if any(t in rx or re.fullmatch(r"(?:D|P)?RX\d+", t) for t in tokens):
        return "rx"
    if any(t == "TX" or re.fullmatch(r"TX\d+", t) for t in tokens):
        return "tx"
    return None


def _union_pair(a, b):
    if not a:
        return b
    if not b:
        return a
    return (min(a[0], b[0]), max(a[1], b[1]))


def _without_leading_refdes(text: str) -> str:
    """SW1103-B5_B26 → B5_B26。频段本身（B5_B26、ANT4_HB）不动。"""
    matched = re.match(r"^([A-Za-z]{2,}\d+)[-_](.+)$", str(text or ""))
    if not matched or matched.group(1).upper().startswith("ANT"):
        return str(text or "")
    return matched.group(2)


def trx_page_name(parent) -> str:
    """天线口-频段-TRX。不要器件位号，也不要 _COM。

    SW1103-B5_B26_COM → B5_B26_TRX。已经是 B5_B26_TRX / ANT4_HB_TRX 的不再加。
    """
    text = _without_leading_refdes(str(parent or ""))
    while text.upper().endswith("_COM"):
        text = text[:-4]
        text = _without_leading_refdes(text)
    upper = text.upper()
    if upper in ("", "COM"):
        return "TRX"
    if upper.endswith("TRX"):
        return text
    return f"{text}_TRX"


def _com_only_stem(name) -> str:
    """B5_B26_COM_COM / B5_B26_COM_2 → B5_B26。不是 COM 段则空。"""
    text = re.sub(r"_\d+$", "", str(name or ""))
    if not text.upper().endswith("_COM"):
        return ""
    return band_stem_from_segment_name(text)


def _absorb_same_band_com(seg, seg_freq, parent_of, fork_of, trx):
    """TRX 页上面如果只挂着同频段的 COM，并进来。不再留 B5_B26_COM_COM。"""
    changed = True
    while changed:
        changed = False
        for name, info in list(trx.items()):
            parent = parent_of.get(name)
            if not parent or parent not in seg:
                continue
            if any(c != name and p == parent for c, p in parent_of.items()):
                continue
            if _com_only_stem(parent) != band_stem_from_segment_name(name):
                continue
            seg[name] = list(seg.get(parent) or []) + list(seg.get(name) or [])
            seg_freq[name] = _union_pair(seg_freq.get(parent), seg_freq.get(name))
            info["common_nets"] = list(seg.get(parent) or []) + list(info.get("common_nets") or [])
            grand = parent_of.get(parent)
            up = fork_of.get(parent)
            seg.pop(parent, None)
            seg_freq.pop(parent, None)
            parent_of.pop(name, None)
            parent_of.pop(parent, None)
            fork_of.pop(parent, None)
            if grand:
                parent_of[name] = grand
            if up:
                fork_of[name] = up
            changed = True


def fold_trx_pages(seg, seg_freq, meta):
    """公共端只有 TX、RX 两掷时，三块合成一页，段名叫 XXX_TRX。

    抽取频段用并集，好让一个 S 参数盖住两边。
    meta["trx"][页名] 另记 TX、RX 各自的频段，ADS 放两个 SP。
    公共端还连着别的掷，或 TX/RX 自己还有下级，保持原样切开。
    """
    seg = dict(seg or {})
    seg_freq = dict(seg_freq or {})
    meta = dict(meta or {})
    parent_of = dict(meta.get("parent") or {})
    fork_of = dict(meta.get("fork") or {})
    kids = {}
    for child, parent in parent_of.items():
        kids.setdefault(parent, []).append(child)
    trx = {}
    for parent, children in list(kids.items()):
        if len(children) != 2:
            continue
        if any(kids.get(child) for child in children):
            continue
        roles = {}
        for child in children:
            role = tx_rx_role(child)
            if role:
                roles[role] = child
        if set(roles) != {"tx", "rx"}:
            continue
        name = trx_page_name(parent)
        if name in seg and name not in (parent, roles["tx"], roles["rx"]):
            continue
        tx, rx = roles["tx"], roles["rx"]
        common_nets = list(seg.get(parent) or [])
        tx_nets = list(seg.get(tx) or [])
        rx_nets = list(seg.get(rx) or [])
        parent_freq = seg_freq.get(parent)
        tx_freq = seg_freq.get(tx)
        rx_freq = seg_freq.get(rx)
        grand = parent_of.get(parent)
        up_fork = fork_of.get(parent)
        split_fork = fork_of.get(tx) or fork_of.get(rx)
        for old in (parent, tx, rx):
            seg.pop(old, None)
            seg_freq.pop(old, None)
            parent_of.pop(old, None)
            fork_of.pop(old, None)
        seg[name] = common_nets + tx_nets + rx_nets
        seg_freq[name] = _union_pair(parent_freq, _union_pair(tx_freq, rx_freq))
        if grand:
            parent_of[name] = grand
        if up_fork:
            fork_of[name] = up_fork
        trx[name] = {
            "common": parent,
            "tx": tx,
            "rx": rx,
            "tx_freq": tx_freq,
            "rx_freq": rx_freq,
            "common_nets": common_nets,
            "tx_nets": tx_nets,
            "rx_nets": rx_nets,
            "fork": split_fork,
        }
    _absorb_same_band_com(seg, seg_freq, parent_of, fork_of, trx)
    meta["parent"] = parent_of
    meta["fork"] = fork_of
    meta["trx"] = trx
    return seg, seg_freq, meta


def classify_network_nodes_by_segment(
    initial_node,
    visited,
    reverse_edges,
    schematic,
    rf_graph=None,
    pin_parser=None,
):
    """按一进多出器件分段。返回 (seg, seg_freq, meta)。

    meta["parent"]：子段 → 公共端段（构图用，有子段就要出 symbol）。
    meta["fork"]：子段 → (分界器件, 脚)。

    块名不用 $$$（$ 不能进文件名）：段内优先有名网；没有则用所接器件 GATE
    脚名（如 MLB-MB-HB）；再没有则朝芯片穿过两端件取下一只有名网。
    天线公共端频率固定 600-6000，不继承、不并支路。
    其它段频段先看段内 rf_graph；没有则朝天线继承最近滤波/SAW。
    开关多进一出（公共端是 RF 掷、COM/ANT 朝芯片）时：COM 侧并各掷，
    RF 父段抽取盖住 COM 侧，避免子页嵌父段时 ADS 外推。
    """
    graph = defaultdict(list)
    ant_of = defaultdict(list)
    for dst, src in reverse_edges:
        graph[src].append(dst)
        ant_of[dst].append(src)

    def rf_out_nets(comp):
        return [
            d
            for d in graph.get(comp, [])
            if d in visited and schematic.is_network(d)
        ]

    def is_boundary(comp):
        return is_segment_fork(
            comp, schematic, len(rf_out_nets(comp)), pin_parser
        )

    def _pin_on_net(comp, net):
        return _pin_of_comp_on_net(schematic, comp, net)

    seg = defaultdict(list)
    seen = set()
    raw_parent = {}
    raw_fork = {}
    sid_order = []
    stack = [(initial_node, "COM")]
    while stack:
        node, sid = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node not in visited:
            continue
        if schematic.is_network(node):
            if not seg[sid]:
                sid_order.append(sid)
            if node not in seg[sid]:
                seg[sid].append(node)
        boundary = is_boundary(node)
        nxts = [n for n in graph.get(node, []) if n not in seen]
        if boundary:
            def _throw_key(nxt):
                net = nxt if schematic.is_network(nxt) else None
                pin = _pin_on_net(node, net) if net else None
                try:
                    return (0, int(str(pin)))
                except (TypeError, ValueError):
                    return (1, str(nxt))

            nxts = sorted(nxts, key=_throw_key)
            # 后进先出：脚号小的最后压栈、最先走完整条再换下一脚。
            for nxt in reversed(nxts):
                child_sid = nxt if schematic.is_network(nxt) else f"{node}>{nxt}"
                stack.append((nxt, child_sid))
                if child_sid != sid:
                    raw_parent[child_sid] = sid
                    net_for_pin = nxt if schematic.is_network(nxt) else None
                    raw_fork[child_sid] = (
                        node,
                        _pin_on_net(node, net_for_pin) if net_for_pin else None,
                    )
        else:
            # 耦合器不分界，但抽头/假负载不能并进直通段。
            allowed = coupler_continue_nets(node, schematic, pin_parser)
            for nxt in nxts:
                if (
                    allowed is not None
                    and schematic.is_network(nxt)
                    and nxt not in allowed
                ):
                    continue
                stack.append((nxt, sid))

    def chip_named(sid, nets):
        seen_walk = set(nets)
        queue = list(nets)
        while queue:
            cur = queue.pop(0)
            for nxt in graph.get(cur, []):
                if nxt in seen_walk or nxt not in visited:
                    continue
                seen_walk.add(nxt)
                if schematic.is_network(nxt) and not str(nxt).startswith("$$$"):
                    if not is_fbrx_siwave_name(nxt):
                        return nxt
                    continue
                if _part_code(schematic, nxt).startswith("312701"):
                    continue
                if schematic.is_network(nxt) or not is_boundary(nxt):
                    queue.append(nxt)
        return sid

    pick_cache = {}

    def pick_name(sid, nets):
        if sid in pick_cache:
            return pick_cache[sid]
        if sid == "COM":
            pick_cache[sid] = "COM"
            return "COM"
        if (
            _named_rf_net(sid)
            and is_meaningful_segment_name(sid)
            and not is_fbrx_siwave_name(sid)
        ):
            pick_cache[sid] = sid
            return sid
        for n in nets:
            if (
                _named_rf_net(n)
                and is_meaningful_segment_name(n)
                and not is_fbrx_siwave_name(n)
            ):
                pick_cache[sid] = n
                return n
        bridged = _bridge_named_rf_net(schematic, list(nets) + [sid])
        if bridged:
            pick_cache[sid] = bridged
            return bridged
        port = _segment_port_name(
            sid, nets, schematic, pin_parser, raw_fork.get(sid),
        )
        if port and is_meaningful_segment_name(port):
            pick_cache[sid] = port
            return port
        child_names = [
            pick_name(child, seg.get(child, []))
            for child, parent in raw_parent.items()
            if parent == sid
        ]
        com = com_name_from_child_segments(child_names)
        if com:
            pick_cache[sid] = com
            return com
        fallback = chip_named(sid, nets)
        pick_cache[sid] = fallback
        return fallback

    renamed = {}
    sid_to_name = {}
    for sid, nets in seg.items():
        name = safe_segment_id(pick_name(sid, nets))
        if name in renamed:
            parent_sid = raw_parent.get(sid)
            child_of_named = parent_sid is not None and sid_to_name.get(parent_sid) == name
            parent_of_named = any(
                raw_parent.get(other) == sid and sid_to_name.get(other) == name
                for other in sid_to_name
            )
            my_fork = (raw_fork.get(sid) or (None, None))[0]
            prev_forks = {
                (raw_fork.get(other) or (None, None))[0]
                for other, other_name in sid_to_name.items()
                if other_name == name
            }
            fork_clash = bool(my_fork) and any(prev and prev != my_fork for prev in prev_forks)
            if fork_clash:
                name = safe_segment_id(_device_port_segment_name(my_fork, name))
            if child_of_named or parent_of_named or fork_clash or name in renamed:
                if name in renamed:
                    k = 2
                    uniq = f"{name}_{k}"
                    while uniq in renamed:
                        k += 1
                        uniq = f"{name}_{k}"
                    name = uniq
                renamed[name] = list(nets)
            else:
                renamed[name].extend(n for n in nets if n not in renamed[name])
        else:
            renamed[name] = list(nets)
        sid_to_name[sid] = name
    seg = renamed
    parent_named = {}
    fork_named = {}
    for child, parent in raw_parent.items():
        cn = sid_to_name.get(child, safe_segment_id(pick_name(child, [])))
        pn = sid_to_name.get(parent, "COM" if parent == "COM" else safe_segment_id(pick_name(parent, [])))
        if cn and pn and cn != pn:
            parent_named[cn] = pn
        if child in raw_fork:
            fork_named[cn] = raw_fork[child]
    kept = {}
    skipped = []
    for sid, nets in seg.items():
        if sid == "COM":
            kept[sid] = nets
            continue
        parent_fork = (fork_named.get(sid) or (None, None))[0]
        payload_nets = _segment_payload_nets(sid, nets, schematic)
        if _segment_hits_srs(sid, payload_nets):
            skipped.append({
                "name": sid,
                "nets": list(payload_nets or nets),
                "fork": parent_fork,
                "reason": "srs",
            })
            continue
        if _segment_reaches_next_fork(payload_nets, schematic, parent_fork):
            kept[sid] = list(nets)
            for n in payload_nets:
                if n not in kept[sid]:
                    kept[sid].append(n)
            continue
        # 天线后顶层分频/三工器的一掷要留下，否则下一掷会顶到 UHB 后面。
        if parent_named.get(sid) == "COM" and parent_fork:
            pn = _part_code(schematic, parent_fork)
            top = False
            try:
                top = schematic.is_frequency_divider(parent_fork)
            except Exception:
                top = False
            if top or pn.startswith(("312702", "312703")):
                kept[sid] = nets
                continue
        nc_ref = _series_nc_on_nets(payload_nets, schematic, parent_fork)
        skipped.append({
            "name": sid,
            "nets": list(payload_nets or nets),
            "fork": parent_fork,
            "reason": "series_nc" if nc_ref else "no_next_device",
            "nc": nc_ref,
            "matching": _matching_refs_on_nets(payload_nets, schematic, parent_fork),
        })
    seg = kept
    parent_named = {c: p for c, p in parent_named.items() if c in seg and p in seg}
    fork_named = {c: f for c, f in fork_named.items() if c in seg}
    ordered = []
    seen_name = set()
    for sid in sid_order:
        name = sid_to_name.get(sid)
        if name in seg and name not in seen_name:
            seen_name.add(name)
            ordered.append(name)
    ordered.extend(s for s in seg if s not in seen_name)
    seg = {s: seg[s] for s in ordered}

    def inherit_freq(nets):
        seen_w = set(nets)
        queue = list(nets)
        while queue:
            cur = queue.pop(0)
            band = _band_on_node(rf_graph, cur)
            if band and (
                schematic.is_network(cur) or _is_filter_like(cur, schematic)
            ):
                return band
            for nxt in ant_of.get(cur, []):
                if nxt in seen_w or nxt not in visited:
                    continue
                seen_w.add(nxt)
                try:
                    if schematic.is_electronic_switch(nxt):
                        queue.append(nxt)
                        continue
                except Exception:
                    pass
                queue.append(nxt)
        return None

    seg_freq = {}
    for sid, nets in seg.items():
        if sid == "COM":
            seg_freq[sid] = (600, 6000)
            continue
        los, his = [], []
        for net in nets:
            band = _band_on_node(rf_graph, net)
            if band:
                los.append(band[0])
                his.append(band[1])
        if los and his:
            seg_freq[sid] = (min(los), max(his))
            continue
        inherited = inherit_freq(nets)
        seg_freq[sid] = inherited if inherited else (600, 6000)
    _cover_switch_com_freq(
        seg_freq, parent_named, fork_named, schematic, pin_parser)
    return fold_trx_pages(seg, seg_freq, {
        "parent": parent_named,
        "fork": fork_named,
        "skipped": skipped,
    })
