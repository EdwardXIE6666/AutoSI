"""ADS 分段树：父子挂接与 PCB 文件名查找（不依赖 Keysight）。"""

import re

# 段 schematic 横轴（天线右、芯片左）。GE 占 x..x+1，pitch 必须 > 2。
# 链上 GE / IC / 级间同一格距，避免一截挤一截松。
SEG_CHAIN_PITCH = 5
SEG_GE_PITCH = SEG_CHAIN_PITCH
SEG_IC_PITCH = SEG_CHAIN_PITCH
SEG_STAGE_GAP = SEG_CHAIN_PITCH
SEG_PCB_DY = 4


def finalize_segment_chain(items):
    """叶段也不把最后一颗 IC 当芯片端拎走。

    LNA/PA 收集时已经跳过；再拎走会把两端 BPF 这类通路件丢掉，根段尤其摆不回去。
    """
    return list(items), None


def schematic_rf_ports_for_refdes(schematic, node: str) -> dict[int, str]:
    """原理图射频口 → {脚号: 网_位号_脚}。缺库 SnP 且 PCB 也没有该位号口时用。"""
    from rf.segments import (
        _is_rf_path_port,
        _pin_of_comp_on_net,
        _schematic_nets_of,
    )
    out: dict[int, str] = {}
    for net in _schematic_nets_of(schematic, node):
        pin = _pin_of_comp_on_net(schematic, node, net)
        if not _is_rf_path_port(schematic, None, node, net, pin):
            continue
        try:
            key = int(pin)
        except (TypeError, ValueError):
            key = len(out) + 1
        while key in out:
            key += 1
        out[key] = f"{net}_{node}_{pin}"
    return out


def _snp_comment_pin_number(port_name, fallback):
    """S2P 注释 `2_IN` / `4_OUT` 开头的脚号；没有则用端口序号。"""
    text = str(port_name or "").split(" ")[0]
    if text and text[0].isdigit():
        try:
            return int(text.split("_")[0])
        except ValueError:
            pass
    parts = text.split("_")
    if parts and parts[-1].isdigit():
        try:
            return int(parts[-1])
        except ValueError:
            pass
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return None


def map_snp_ports_to_component_pcb(pcb_list, port_name_list, node):
    """器件 S2P 脚号对到本器件 PCB 口。对不上的口用本器件剩下的 PCB 脚补。

    SLFL15：S2P 写 2_IN/4_OUT，原理图射频脚是 4 和 6（2 是地）。脚 2 没有 PCB 口，
    应把剩下的 FIL1600_6 补上，否则 FIL1600_4/6 缺网标、和 PCB 并不上。
    """
    pcb_by_pin = {}
    for raw in pcb_list or ():
        parts = str(raw).split(" ")[0].split("_")
        if len(parts) < 2 or not str(parts[-1]).isdigit():
            continue
        if str(parts[-2]) != str(node):
            continue
        pcb_by_pin[int(parts[-1])] = str(raw).split(" ")[0]
    used = set()
    out = {}
    pending = []
    for pnum, pname in (port_name_list or {}).items():
        pin = _snp_comment_pin_number(pname, pnum)
        if pin in pcb_by_pin:
            out[pnum] = pcb_by_pin[pin]
            used.add(pin)
        else:
            pending.append(pnum)
            out[pnum] = pname
    leftover = [pcb_by_pin[k] for k in sorted(pcb_by_pin) if k not in used]
    for i, pnum in enumerate(pending):
        if i < len(leftover):
            out[pnum] = leftover[i]
    return out


def pcb_ports_for_refdes(pcb_list, node: str) -> dict[int, str]:
    """本段 PCB 口里属于该位号的端口，按脚号排序后编成 1..N（缺库 SnP 占位用）。"""
    hits: list[tuple[int, str]] = []
    for raw in pcb_list or ():
        parts = str(raw or "").split("_")
        if len(parts) < 2 or parts[-2] != node:
            continue
        try:
            pin = int(parts[-1])
        except ValueError:
            continue
        hits.append((pin, str(raw)))
    hits.sort()
    return {i + 1: name for i, (_pin, name) in enumerate(hits)}

# 生成段 symbol 时关掉这些仿真件：TermG 带 Num，进 dual symbol 会变成层级端口；
# 子页再放同号 TermG 即 Duplicate Port，ADS 把一只脚关掉，网标对不上。
# Pin（create_pin）仍留着，才是子页该接的口。
SYMBOL_HIDE_CELLS = ("Term", "TermG", "S_Param", "VSWR")


def symbol_term_names_left_to_right(sym, ant: str) -> list[str]:
    """端子名按几何左→右。天线在右，避免 dual 脚号和画面左右相反。"""
    ant_s = str(ant or "").strip()
    rows: list[tuple] = []
    for t in sym.get_term_iter():
        name = str(getattr(t, "name", "") or "").strip()
        if not name:
            continue
        xs: list[float] = []
        for p in getattr(t, "pins", None) or ():
            sp = getattr(p, "snap_point", None)
            if sp is None:
                continue
            try:
                xs.append(float(sp.x))
            except Exception:
                pass
        rows.append((min(xs) if xs else None, name))
    located = [(x, n) for x, n in rows if x is not None]
    if rows and len(located) == len(rows):
        located.sort(key=lambda r: r[0])
        return [n for _, n in located]
    names = [n for _, n in rows]

    def _is_ant(n: str) -> bool:
        if ant_s and n == ant_s:
            return True
        return n.endswith("_ANT") or n == "MAIN_ANT"

    return [n for n in names if not _is_ant(n)] + [n for n in names if _is_ant(n)]


def step_left(x, kind):
    """向芯片侧挪一格（-x，天线在右、芯片在左）。kind 仅作调用点标记，格距相同。"""
    pitch = {"ge": SEG_GE_PITCH, "ic": SEG_IC_PITCH, "gap": SEG_STAGE_GAP}[kind]
    return x - pitch


def norm_rf_net(name, ant_num=None):
    """PCB/器件 SnP 端口名 → 仿真网标。

    末两段是器件_引脚，前面整段是网络名（DIV_ANT / $$$13730 / ANT2 都适用）。
    天线馈点网上的非无源件归一成天线名，才能和 TermG 的天线网标并上。
    """
    name = str(name).split(" ")[0]
    parts = name.split("_")
    if ant_num and len(parts) >= 3:
        net = "_".join(parts[:-2])
        comp = parts[-2]
        from rf.segments import _looks_like_matching_rlc
        if net == ant_num and not _looks_like_matching_rlc(comp):
            return str(ant_num)
    if len(parts) >= 2:
        return "_".join(parts[-2:])
    return name


def even_x_span(right, left, n):
    """n 个横坐标，从 right 到 left 均分（含两端）。n=1 取中点。"""
    n = int(n)
    if n <= 0:
        return []
    if n == 1 or right == left:
        return [(right + left) / 2.0] * n
    dx = (right - left) / (n - 1)
    return [right - i * dx for i in range(n)]


# ADS 网标非法字符（Keysight：alphanumeric and most characters other than these）。
_ADS_NET_ILLEGAL = re.compile(r'[*()\[\]{}/\<>?,:"\t\';]+')


def ads_net_label(name) -> str:
    """原理图网名 → ADS 可写的 wire/pin 网标。PADS 允许 /，ADS 不允许。"""
    s = str(name or "").split(" ")[0].strip()
    if not s:
        return ""
    s = _ADS_NET_ILLEGAL.sub("_", s)
    s = re.sub(r"_+", "_", s)
    return s


def sxp_filename_matches_model(file_name: str, component_model: str) -> bool:
    """器件型号是否出现在 S 参数文件名里。空型号不得命中任何文件。

    '-' 与 '_' 视为同一分隔符：功能2 抽出 ANT1-TRIP1600-MLB-MB-HB.s6p，
    功能3 查找键把段内连字符改成下划线，不能因此漏配。
    """
    model = str(component_model or "").strip().lower()
    if not model:
        return False
    needle = model.replace("-", "_")
    hay = str(file_name or "").lower().replace("-", "_")
    return needle in hay


def pcb_lookup_key(ant_num, band):
    """段名 → PCB .sNp 查找串。文件名不能有 /，频段网名里的斜杠改下划线。"""
    snp = str(band).replace("-", "_").replace("/", "_")
    if ant_num:
        return f"{ant_num}-{snp}"
    return snp


def pcb_lookup_keys(ant_num, band, nets=None):
    """显示名改成 B20_TX0 后，仍要找得到 $$$/NET 那份 PCB 文件。"""
    keys = []
    seen = set()
    names = [band]
    for net in nets or ():
        names.append(net)
        text = str(net)
        if text.startswith("$$$") and text[3:].isdigit():
            names.append("NET" + text[3:])
    for name in names:
        key = pcb_lookup_key(ant_num, name)
        aliases = [key]
        if "-" in key:
            aliases.append(key.replace("-", "_"))
        for alias in aliases:
            if alias not in seen:
                seen.add(alias)
                keys.append(alias)
    return keys


def snp_sweep_mhz(path):
    """Touchstone 首/末频点，转 MHz。SIwave 导出单位是 Hz。读不到则 None。"""
    text = str(path or "")
    m = re.search(r"\.s(\d+)p$", text, re.I)
    if not m:
        return None
    n = int(m.group(1))
    per = 1 + 2 * n * n
    toks = []
    try:
        with open(text, encoding="latin-1") as f:
            for line in f:
                s = line.strip()
                if not s or s[0] in "!#":
                    continue
                try:
                    toks.extend(float(x) for x in s.split())
                except ValueError:
                    continue
    except OSError:
        return None
    if len(toks) < per:
        return None
    npts = len(toks) // per
    return (toks[0] / 1e6, toks[(npts - 1) * per] / 1e6)


def _segment_arm_kind(band):
    """段名的收发臂：TX / RX / TRX / COM。用来避免双工器两臂互挂。"""
    u = str(band or "").upper()
    if u.endswith("_TRX") or "_TRX_" in u:
        return "TRX"
    if u.endswith("_TX") or "_TX_" in u:
        return "TX"
    if u.endswith("_RX") or "_RX_" in u:
        return "RX"
    if u.endswith("_COM") or u in ("COM",):
        return "COM"
    return ""


def pick_segment_parent(child_band, candidates):
    """多个父候选时：TX/RX 两臂不是彼此的父段；同器件公共端（TRX/COM）优先。

    双工/开关把 TX 与 RX 接到同一颗器件上时，两臂都会出现在候选里。
    取列表第一项会把 B28_TX 挂到 B28_RX，子页嵌 RX symbol、器件和网都错。
    """
    cands = [c for c in (candidates or ()) if c]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    child_kind = _segment_arm_kind(child_band)
    sibling = {"TX": "RX", "RX": "TX"}.get(child_kind)

    def _rank(band):
        kind = _segment_arm_kind(band)
        if sibling and kind == sibling:
            return 2
        if kind in ("TRX", "COM"):
            return 0
        return 1

    return min(cands, key=lambda b: (_rank(b), cands.index(b)))


def attach_orphan_segment_roots(tree, ant_num=None):
    """同一天线下没挂上父段的频段，挂到 COM/天线根，避免只渲 COM。

    PCB 端口对不上时所有段 parent=None；只 walk 第一个根会丢掉 B1_TX 等页。
    已有父段的树不动。
    """
    if not tree:
        return tree
    roots = [b for b, info in tree.items() if not info.get("parent")]
    if len(roots) <= 1:
        return tree
    home = None
    for cand in ("COM", ant_num):
        if cand in tree and not tree[cand].get("parent"):
            home = cand
            break
    if home is None:
        home = roots[0]
    kids = tree[home].setdefault("children", [])
    for band in roots:
        if band == home:
            continue
        tree[band]["parent"] = home
        if band not in kids:
            kids.append(band)
    return tree


def nested_uses_divider(info):
    """非根段只有找到分界器件才能嵌父 symbol + 分界 IC。挂到 COM 但 bfilter 为空时不能解包。"""
    return bool(info.get("parent") and info.get("bfilter"))


def parent_end_nets(child_bfilters, parent_ports):
    """非叶段芯片侧 TERM：只出本段 PCB 上真实存在的分界网。

    child_bfilters: 子段 bfilter (器件, 脚) 或 None。
    parent_ports: 本段 PCB 端口 (器件, 归一网标)。
    孤儿子段的分界件若不在本段 PCB 上，不造名为位号的假 Term。
    多子段共用同一颗本段分界件，只出一只脚。
    """
    first_net = {}
    for comp, net in parent_ports or ():
        if comp and comp not in first_net:
            first_net[comp] = net
    nets = []
    seen = set()
    for bf in child_bfilters or ():
        if not bf:
            continue
        comp = bf[0]
        if comp in seen or comp not in first_net:
            continue
        seen.add(comp)
        nets.append(first_net[comp])
    return nets


def series_shunt_to_ground(raw_signals, segment_nets, is_rlc, is_gnd_net) -> list[str]:
    """段网上的 RLC 再串一只到地：整条腿都要摆。

    C1411 在主路网上，R1403 只接中间网和地。两位号彼此串联，对主路却是并联到地。
    中间网不在段网里，只扫段网会把电阻丢掉；电容又因两只非地网被当成串联开路。
    """
    seg = {str(n) for n in (segment_nets or []) if n}
    net_comps: dict[str, list[str]] = {}
    comp_nets: dict[str, list[str]] = {}
    for item in raw_signals or []:
        if not item or len(item) < 2:
            continue
        net = str(item[0] or "")
        if not net:
            continue
        for comp in item[1] or []:
            comp = str(comp or "")
            if not comp:
                continue
            net_comps.setdefault(net, [])
            if comp not in net_comps[net]:
                net_comps[net].append(comp)
            comp_nets.setdefault(comp, [])
            if net not in comp_nets[comp]:
                comp_nets[comp].append(net)

    def _leg(start: str):
        chain = [start]
        seen_nets = set()
        hit_gnd = False
        pending = list(comp_nets.get(start) or [])
        while pending:
            net = pending.pop()
            if net in seen_nets:
                continue
            seen_nets.add(net)
            if net in seg:
                if net not in (set(comp_nets.get(start) or []) & seg):
                    return None
                continue
            if is_gnd_net(net):
                hit_gnd = True
                continue
            comps = net_comps.get(net) or []
            if any(not is_rlc(c) for c in comps):
                return None
            for comp in comps:
                if comp in chain:
                    continue
                if not is_rlc(comp):
                    return None
                chain.append(comp)
                for nxt in comp_nets.get(comp) or []:
                    if nxt not in seen_nets:
                        pending.append(nxt)
        if not hit_gnd or len(chain) < 2:
            return None
        return chain

    out = []
    seen = set()
    for net in seg:
        for comp in net_comps.get(net) or []:
            if comp in seen or not is_rlc(comp):
                continue
            others = [n for n in comp_nets.get(comp) or [] if n not in seg]
            if not others:
                continue
            chain = _leg(comp)
            if not chain:
                continue
            for ref in chain:
                if ref not in seen:
                    seen.add(ref)
                    out.append(ref)
    return out


def ads_place_nc(comp, gnd_comps) -> bool:
    """ADS 只摆对地并联的 NC。串联 NC 是开路，构图不放。"""
    return bool(comp) and comp in set(gnd_comps or ())


def merge_nc_into_chain(actives, nc_names, key_fn):
    """匹配链 ∪ 本段 NC：每位号只出现一次。

    已在匹配链里的 DNP 件只标 is_nc，不插第二份（否则先按过滤后的网摆、无网标，
    再被 claim_refdes 挡掉真正该用原始网的那次）。
    链上没有的 NC 按 key_fn（网序）插到左右邻之间。
    返回 [(器件, 是否IC, 是否NC)]。
    """
    nc_set = set(nc_names or ())
    seq = []
    seen = set()
    for item in actives or ():
        if isinstance(item, str):
            comp, is_ic = item, False
        else:
            comp, is_ic = item[0], item[1]
        if not comp or comp in seen:
            continue
        seen.add(comp)
        seq.append((comp, is_ic, comp in nc_set))
    for nc in nc_names or ():
        if not nc or nc in seen:
            continue
        k = key_fn(nc)
        pos = len(seq)
        for i, (comp, _ic, _nc) in enumerate(seq):
            if key_fn(comp) > k:
                pos = i
                break
        seq.insert(pos, (nc, False, True))
        seen.add(nc)
    return seq


def signal_infos_for_place(node, signal_infos, signal_infos_raw, nc_nets):
    """DNP 件已从 signal_infos 滤掉，贴网改用过滤前的 raw。"""
    if node and nc_nets and node in nc_nets:
        return signal_infos_raw or signal_infos
    return signal_infos


def deactivate_placed_instances(insts):
    """NC 去使能：实例仍在图上、原网保留，不走 deactivate_and_short。

    返回去使能失败的说明（空=全部成功）。调用方禁止改成短路。
    """
    failed = []
    for inst in insts or ():
        if inst is None:
            continue
        if not hasattr(inst, "deactivate"):
            failed.append(f"{inst!r} has no deactivate")
            continue
        try:
            inst.deactivate()
        except Exception as exc:
            failed.append(str(exc))
            continue
        flag = getattr(inst, "is_deactivated", None)
        if flag is False:
            failed.append("still active")
    return failed
