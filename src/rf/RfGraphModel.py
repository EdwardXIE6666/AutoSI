from collections import defaultdict
from graphviz import Digraph
import collections
import re
from collections import deque
from app.LogTool import add_issue, LogStyle
from rf.PdfPinLabel import (
    build_edge_pin_labels,
    format_pin_badge,
    pin_node_size_attrs,
)


def add_legend_to_dot(dot, legend_items, title='Legend'):
    """在图的右下角插入颜色图例。
    使用 dot.body.append 直接注入原始 DOT 语句，避免 Python graphviz 库
    对 HTML label 自动加引号导致语法失效。
    labelloc=b + labeljust=r 将标签置于图边界框的右下方。
    文字使用英文，规避 Graphviz 默认字体不支持 CJK 的乱码问题。
    """
    rows = ''.join(
        f'<TR>'
        f'<TD BGCOLOR="{color}" WIDTH="22" HEIGHT="18"> </TD>'
        f'<TD ALIGN="LEFT" BALIGN="LEFT">'
        f'<FONT FACE="Arial" POINT-SIZE="9"> {label} </FONT>'
        f'</TD>'
        f'</TR>'
        for color, label in legend_items
    )
    table = (
        f'<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="2" CELLPADDING="4" BGCOLOR="white">'
        f'<TR><TD COLSPAN="2" ALIGN="CENTER">'
        f'<FONT FACE="Arial" POINT-SIZE="10"><B> {title} </B></FONT>'
        f'</TD></TR>'
        f'{rows}'
        f'</TABLE>'
    )
    dot.body.append(f'\tlabel=<{table}>')
    dot.body.append('\tlabelloc="b"')
    dot.body.append('\tlabeljust="r"')


class Node:
    def __init__(self, id, band_info=None):
        """射频图节点；band_info 存成列表。"""
        self.id = id
        self.band_info = band_info if isinstance(
            band_info, list) else ([band_info] if band_info else [])
        self.forward_edges = []    # 正向边（从该节点出发）
        self.reverse_edges = []     # 反向边（指向该节点）

    def add_forward_edge(self, target_id):
        """添加正向边"""
        if target_id not in self.forward_edges:
            self.forward_edges.append(target_id)

    def add_reverse_edge(self, source_id):
        """添加反向边"""
        if source_id not in self.reverse_edges:
            self.reverse_edges.append(source_id)

    def add_band_info(self, band):
        """添加频段信息（去重）"""
        if band and band not in self.band_info:
            self.band_info.append(band)

    def get_band_info(self):
        """返回频段列表的字符串形式。"""
        return f"{self.band_info}" if self.band_info else None

    def __str__(self):
        band_str = f" ({', '.join(self.band_info)})" if self.band_info else ""
        return f"Node({self.id}{band_str})"

    def __repr__(self):
        return str(self)


class RfSignalGraph:
    def __init__(self, adjacency_dict=None):
        """射频信号图；可从邻接表 {source: [target, ...]} 构建。"""
        self.nodes = {}  # {node_id: Node}
        self.band_colors = {}

        if adjacency_dict:
            self.build_from_adjacency(adjacency_dict)

    def add_node(self, node_id, band_info=None):
        """添加节点（可指定频段信息）"""
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id, band_info)
        return self.nodes[node_id]

    def add_edge(self, source_id, target_id):
        """添加双向边（自动创建正向和反向连接）"""
        source_node = self.add_node(source_id)
        target_node = self.add_node(target_id)

        source_node.add_forward_edge(target_id)
        target_node.add_reverse_edge(source_id)

    def has_edge(self, source_id, target_id):
        node = self.nodes.get(source_id)
        return bool(node and target_id in node.forward_edges)

    def add_one_way_edge(self, source_id, target_id) -> bool:
        """只保留一条方向：已有对向则丢，先到为准。"""
        if source_id == target_id:
            return False
        if self.has_edge(target_id, source_id):
            return False
        self.add_edge(source_id, target_id)
        return True

    def build_from_adjacency(self, adjacency_dict):
        """从邻接表构建双向图"""
        for source, targets in adjacency_dict.items():
            for target in targets:
                self.add_edge(source, target)

    def propagate_band_info_upstream(self):
        """
        从已标记频段信息的节点出发，沿反向边(上游方向)传播频段信息
        使所有能通过forward_edges到达频段节点的节点获得对应频段
        """
        source_nodes = [node for node in self.nodes.values() if node.band_info]

        for source_node in source_nodes:
            visited = set()
            queue = deque([source_node.id])
            visited.add(source_node.id)

            while queue:
                current_id = queue.popleft()
                current_node = self.nodes.get(current_id)
                # 遍历当前节点的正向边（下游节点）
                for current_id in current_node.reverse_edges:
                    upstream_node = self.nodes.get(current_id)
                    # 将源节点的所有频段添加到下游节点
                    for band in source_node.band_info:
                        upstream_node.add_band_info(band)

                    if current_id not in visited:
                        visited.add(current_id)
                        queue.append(current_id)

    def propagate_band_info_downstream(self):
        # 获取所有有频段信息的节点
        source_nodes = [node for node_id,
                        node in self.nodes.items() if node.band_info]
        # 使用队列进行BFS
        from collections import deque
        for source_node in source_nodes:
            visited = set()
            queue = deque([source_node.id])
            visited.add(source_node.id)

            while queue:
                current_id = queue.popleft()
                current_node = self.nodes.get(current_id)

                # 遍历当前节点的正向边（下游节点）
                for downstream_id in current_node.forward_edges:
                    downstream_node = self.nodes.get(downstream_id)
                    # 将源节点的所有频段添加到下游节点
                    for band in source_node.band_info:
                        downstream_node.add_band_info(band)

                    if downstream_id not in visited:
                        visited.add(downstream_id)
                        queue.append(downstream_id)

    def propagate_band_info(self, SchematicRes):
        def find_pin_number(signal_infos, downstream_node_id, current_id):
            """在下游网络的器件列表里取当前器件脚号。"""
            for info in signal_infos:
                if info[0] == downstream_node_id:
                    comp_list = info[1]
                    pin_list = info[2]
                    try:
                        index = comp_list.index(current_id)
                        full_pin = pin_list[index]
                        return int(full_pin.split('.')[1])
                    except (ValueError, IndexError):
                        return None
            return None
        def _is_switch(nid):
            try:
                return bool(SchematicRes.is_electronic_switch(nid))
            except Exception:
                return False

        source_nodes = [node for node_id,
                        node in self.nodes.items() if node.band_info]

        visited = set()
        queue = deque()
        for source_node in source_nodes:
            visited.add(source_node.id)
            for downstream_id in source_node.reverse_edges:
                downstream_node = self.nodes.get(downstream_id)
                pin_number = find_pin_number(
                    SchematicRes.signal_infos,  downstream_node.id, source_node.id)

                for band in source_node.band_info:
                    freq_fn = getattr(band, "frequency_for_pin", None)
                    freq = freq_fn(pin_number) if callable(freq_fn) else None
                    if not freq:
                        pin_info = getattr(band, "pin_info", None) or {}
                        if pin_number not in pin_info:
                            continue
                        freq = (pin_info.get(pin_number) or {}).get("frequency")
                    if freq:
                        downstream_node.add_band_info(freq)
                    elif pin_number is not None:
                        pin_info = getattr(band, "pin_info", None) or {}
                        _pin_name = (pin_info.get(pin_number) or {}).get("name", str(pin_number))
                        print(f"{LogStyle.WARNING}⚠️ {getattr(band, 'external_model', band)} 引脚 {pin_number} ({_pin_name}) 无频率标注{LogStyle.ENDC}")
                        add_issue('WARNING',
                            f"[端口无频率] {getattr(band, 'external_model', band)} "
                            f"第 {pin_number} 脚（{_pin_name}）没写频率，后面网络频段可能不准。"
                            f"请补：! Port[{pin_number}] = {_pin_name} (1710-1785)",
                            key=f"no_freq_{getattr(band, 'external_model', band)}_{pin_number}"
                        )

                if downstream_id not in visited:
                    queue.append(downstream_id)
                    visited.add(downstream_id)

        visited = set()
        while queue:
            current_id = queue.popleft()
            current_node = self.nodes.get(current_id)
            if _is_switch(current_id):
                continue
            for downstream_id in current_node.reverse_edges:
                if _is_switch(downstream_id):
                    continue
                downstream_node = self.nodes.get(downstream_id)
                for band in current_node.band_info:
                    if isinstance(band, str) and downstream_node.band_info == []:
                        downstream_node.add_band_info(band)

                if downstream_id not in visited:
                    visited.add(downstream_id)
                    queue.append(downstream_id)

        # 公共端处理
        for source_node in source_nodes:
            visited.add(source_node.id)
            for downstream_id in source_node.forward_edges:
                downstream_node = self.nodes.get(downstream_id)
                pin_number = find_pin_number(
                    SchematicRes.signal_infos,  downstream_node.id, source_node.id)
                for band in source_node.band_info:
                    freq_fn = getattr(band, "frequency_for_pin", None)
                    freq = freq_fn(pin_number) if callable(freq_fn) else None
                    if not freq:
                        pin_info = getattr(band, "pin_info", None) or {}
                        if pin_number is not None and pin_number in pin_info:
                            freq = (pin_info.get(pin_number) or {}).get("frequency")
                    if not freq and getattr(band, "device_type", "") == "Frequency Divider":
                        freq = "600-6000"
                    if freq:
                        downstream_node.add_band_info(freq)

                if downstream_id not in visited:
                    queue.append(downstream_id)
                    visited.add(downstream_id)

        visited = set()
        while queue:
            current_id = queue.popleft()
            current_node = self.nodes.get(current_id)
            if _is_switch(current_id):
                continue
            for downstream_id in current_node.forward_edges:
                if _is_switch(downstream_id):
                    continue
                downstream_node = self.nodes.get(downstream_id)
                for band in current_node.band_info:
                    if isinstance(band, str) and band not in downstream_node.band_info:
                        downstream_node.add_band_info(band)

                if downstream_id not in visited:
                    visited.add(downstream_id)
                    queue.append(downstream_id)



def _raw_net_device_maps(schematic_res):
    """从 signal_infos_raw（含 NC）建 net↔device 保序映射。"""
    raw = (
        getattr(schematic_res, "signal_infos_raw", None)
        or getattr(schematic_res, "signal_infos", None)
        or []
    )
    n2d = defaultdict(list)
    d2n = defaultdict(list)
    for si in raw:
        if not si or len(si) < 2:
            continue
        net = str(si[0] or "")
        if not net:
            continue
        for d in si[1] or []:
            d = str(d or "").strip()
            if not d:
                continue
            if d not in n2d[net]:
                n2d[net].append(d)
            if net not in d2n[d]:
                d2n[d].append(net)
    return n2d, d2n


def _is_connector_like(ref: str) -> bool:
    """与 is_connector_ref 同一套：J* / CONN* 座子，不是匹配件。"""
    from refnum.graph import is_connector_ref

    return is_connector_ref(ref)


def _is_gnd_net_name(net: str) -> bool:
    u = str(net or "").strip().upper()
    return u == "GND" or u.startswith("GND") or u in ("AGND", "PGND", "DGND")


def _is_stub_passive(ref: str, schematic_res) -> bool:
    """天线 stub 可穿越的被动件（R/L/C/T/FB），含 NC。

    功能5 / 路径图要画 NC 链，stub 必须能穿过串联 NC。
    功能1/3/4 用 prune_reverse_at_series_nc 在保存 _rev_full 之后再剪。
    """
    ref = str(ref or "").strip()
    if not ref or _is_connector_like(ref):
        return False
    try:
        if schematic_res is not None and schematic_res.is_network(ref):
            return False
    except Exception:
        pass
    ru = ref.upper()
    if ru.startswith("LPF"):
        return False
    return bool(ru[:1] in "CRLTFB" and any(c.isdigit() for c in ru[1:]))


def diversity_port_side(pin_name) -> str:
    """分集前端的两侧。ant 是天线口，radio 是频段收发口。

    天线口都是输入，不能从 ANT_LB 穿到 ANT_MHB。频段口也不能互穿。
    AUX、电源、控制认不出侧，不因此被删。
    """
    raw = str(pin_name or "").upper()
    if re.match(r"^\d+_", raw):
        raw = raw.split("_", 1)[1]
    name = re.sub(r"[^A-Z0-9_]", "", raw)
    if re.match(r"^ANT(\d+)?($|_)", name):
        return "ant"
    if re.search(r"(^|_)(RX|TX|TRX|PRX|DRX)\d*$", name):
        return "radio"
    return ""


def is_difem_module(schematic_res, ref: str) -> bool:
    """312425 里，描述开头的器件类型是 DIFEM。分集前端不是抽取终点。"""
    try:
        infos = getattr(schematic_res, "part_infos", None) or {}
        pn = str(infos.get(ref, "") or "")
    except Exception:
        return False
    if not pn.startswith("312425"):
        return False
    try:
        desc = str((getattr(schematic_res, "part_desc", None) or {}).get(ref, "") or "")
    except Exception:
        return False
    head = re.split(r"[,，;；]", desc.strip().strip('"'), maxsplit=1)[0]
    token = re.sub(r"[^A-Za-z]", "", head).upper()
    return token.startswith("DIFEM")


def _is_rf_frontend_ref(ref: str, schematic_res) -> bool:
    """PA / LNA / LPAMiF 等前端有源端点（stub 挂到此处即停）。"""
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    if pt in ("RF PA", "RF LNA", "LPAMIF"):
        return True
    if "LPAMIF" in pt:
        return True
    return False


def _is_stub_expandable(ref: str, schematic_res) -> bool:
    """天线 stub 可继续向前扩展的器件（通用，不绑位号）。

    含：R/L/C/T/FB 被动件、滤波/分频/多工/耦合、**电子开关**。
    不含：连接器、PA/LNA（挂接端点）、纯未知有源。

    匹配经电子开关才接到主 RF 图时，只扩被动件会在开关处断链。
    """
    ref = str(ref or "").strip()
    if not ref or _is_connector_like(ref):
        return False
    if _is_rf_frontend_ref(ref, schematic_res):
        return False
    if _is_stub_passive(ref, schematic_res):
        return True
    try:
        pt = (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        pt = ""
    # 开关/滤波类 RF 中间件（含 Electronic Switch）
    for kw in (
        "SWITCH", "FILTER", "DIVIDER", "MULTIPLEX", "COUPLER",
        "SAW", "FBAR", "DIPLEX", "TRIPLEX",
    ):
        if kw in pt:
            return True
    ru = ref.upper()
    if ru.startswith("FIL") and any(c.isdigit() for c in ru[1:]):
        return True
    return False


def _pad_peer_ok_for_home(
    peer,
    pad_net,
    home_ant,
    d2n,
    n2d,
    schematic_res,
    *,
    cache=None,
):
    """共网邻器件是否属于本口臂。

    须：星型共盘 = 至少 3 只**贯通** RLCT（非地 RF 网 ≥2）。
        其上远侧最近整口多于一个 → 丢掉异口 ANT 向；FE / 并地留下。
    可：两只贯通件（可另加并地）是串联/π，不过滤。
    禁：并地不算第三只腿；不问对端器件种类。
    """
    from refnum.graph import is_ant_path_node, is_gnd_net, same_ant_port
    from refnum.copad import leg_nearest_ant_port

    peer = str(peer or "").strip()
    pad_net = str(pad_net or "")
    home_ant = str(home_ant or "")
    if not peer or not pad_net or not home_ant:
        return True
    if is_ant_path_node(pad_net):
        return True
    if is_ant_path_node(peer) and not same_ant_port(peer, home_ant):
        return False
    split_key = ("split", pad_net)
    if cache is not None and split_key in cache:
        port_of, ant_ports = cache[split_key]
    else:
        members = [
            str(m)
            for m in (n2d.get(pad_net) or [])
            if m and not is_gnd_net(str(m))
        ]
        port_of = {}

        def _through_rlct(ref: str) -> bool:
            if not _is_stub_passive(ref, schematic_res):
                return False
            n_rf = sum(
                1 for n in (d2n.get(ref) or [])
                if n and not is_gnd_net(str(n))
            )
            return n_rf >= 2

        # 并地不算第三只。两只贯通 ± 并地 = 串联/π，不是匹配星型盘。
        through = [m for m in members if _through_rlct(m)]
        if len(through) < 3:
            if cache is not None:
                cache[split_key] = (port_of, set())
            return True
        rlct = through
        for r in rlct:
            port_of[r] = leg_nearest_ant_port(
                r, pad_net, d2n, n2d, schematic_res,
            )
        ant_ports = {p for p in port_of.values() if p}
        if cache is not None:
            cache[split_key] = (port_of, ant_ports)
    if len(ant_ports) < 2:
        return True
    if peer not in port_of:
        return True
    port = port_of[peer]
    if not port:
        return True
    return same_ant_port(port, home_ant)


def _grow_antenna_stub(rf_graph, schematic_res, ant, on_ant, n2d, d2n, max_hops):
    """匹配件 → 天线口，再沿 stub 向外扩到主图/FE。含串联 NC（功能5 要画这条链）。"""
    from refnum.graph import same_ant_port

    for comp in on_ant:
        rf_graph.add_one_way_edge(comp, ant)
    frontier = list(on_ant)
    seen = {ant, *on_ant}
    port_cache: dict = {}
    for _ in range(max_hops):
        if not frontier:
            break
        nxt = []
        for cur in frontier:
            for net in d2n.get(cur, []):
                if _is_gnd_net_name(net) or same_ant_port(net, ant) or net == ant:
                    continue
                rf_graph.add_one_way_edge(net, cur)
                for peer in n2d.get(net, []):
                    if peer in seen or _is_connector_like(peer) or peer == ant:
                        continue
                    if not _pad_peer_ok_for_home(
                        peer, net, ant, d2n, n2d, schematic_res,
                        cache=port_cache,
                    ):
                        continue
                    in_main = peer in rf_graph.nodes and peer not in on_ant
                    can_expand = _is_stub_expandable(peer, schematic_res)
                    is_fe = _is_rf_frontend_ref(peer, schematic_res)
                    if not in_main and not can_expand and not is_fe:
                        if not rf_graph.nodes.get(peer):
                            continue
                    seen.add(peer)
                    rf_graph.add_one_way_edge(peer, net)
                    if in_main or is_fe:
                        continue
                    if can_expand:
                        nxt.append(peer)
        frontier = nxt
    return bool(rf_graph.nodes.get(ant))


def seed_missing_antenna_stubs(rf_graph, schematic_res, *, ant_indices=None, max_hops=12):
    """把「因 NC 过滤未进 PA→ANT 主图」的天线 stub 补入 rf_graph。

    通用规则（不绑工程/位号）：
      1. 考察 ANT{i}（默认 i=0..9）整口
      2. 再考察 50R 座子叶（无名网 + J* + 匹配件）
      3. 若主图已有该口节点 → 跳过
      4. 仅有连接器、无匹配件的天线网 → 不建页
      5. 共网邻器件按远侧最近整口筛：异口 ANT 向不进本口 stub

    返回：成功补入的天线 id 列表。
    """
    if rf_graph is None or schematic_res is None:
        return []
    if ant_indices is None:
        ant_indices = range(10)

    from refnum.graph import connector_feed_port_id, same_ant_port

    n2d, d2n = _raw_net_device_maps(schematic_res)
    seeded = []

    def _non_connector_on(devs):
        on_ant, seen_on = [], set()
        for d in devs or ():
            if d in seen_on or _is_connector_like(d):
                continue
            seen_on.add(d)
            on_ant.append(d)
        return on_ant

    for i in ant_indices:
        ant = f"ANT{i}"
        if rf_graph.nodes.get(ant):
            continue
        on_ant = []
        seen_on = set()
        for k, devs in n2d.items():
            if not same_ant_port(k, ant):
                continue
            for d in _non_connector_on(devs):
                if d not in seen_on:
                    seen_on.add(d)
                    on_ant.append(d)
        if not on_ant:
            continue
        if _grow_antenna_stub(
            rf_graph, schematic_res, ant, on_ant, n2d, d2n, max_hops
        ):
            seeded.append(ant)

    fifty = {str(n) for n in (getattr(schematic_res, "net_class_50r_nets", None) or ())}
    raw_list = getattr(schematic_res, "signal_infos_raw", None) or []
    for item in raw_list:
        if not item or len(item) < 2:
            continue
        net, devs = item[0], item[1]
        pins = item[2] if len(item) > 2 else []
        if fifty and str(net) not in fifty:
            continue
        port = connector_feed_port_id(net, devs, pins)
        if not port or rf_graph.nodes.get(port):
            continue
        on_ant = _non_connector_on(devs)
        if not on_ant:
            continue
        if _grow_antenna_stub(
            rf_graph, schematic_res, port, on_ant, n2d, d2n, max_hops
        ):
            seeded.append(port)

    return seeded


def _comp_ref_aliases(ref):
    raw = str(ref or "").strip()
    if not raw:
        return ()
    first = raw.split()[0]
    out = []
    for alias in (raw, first):
        if alias and alias not in out:
            out.append(alias)
    # C1504-0402 这种才把 '-' 前当别名；J-C 的 J 不是位号。
    short = first.split("-")[0]
    if short and short != first and any(ch.isdigit() for ch in short) and short not in out:
        out.append(short)
    return tuple(out)


def _bfs_filter_to_graph(start_comp, rf_nodes, n2d, d2n, schematic_res, max_hops=10):
    """把还不在射频图上的滤波器锚定到已有节点。

    可走与 stub 补图相同的射频中间件（被动件 / 开关 / 滤波 / 分频 / 耦合），
    撞上已有图即停，不穿 PA/LNA，避免把另一天线前端链串进来。
    """
    visited = {start_comp}
    queue = deque([(start_comp, False, [])])
    while queue:
        curr, is_net, path = queue.popleft()
        if is_net:
            for comp in n2d.get(curr, []):
                if comp in visited:
                    continue
                new_path = path + [comp]
                if comp in rf_nodes:
                    return new_path, comp
                if len(new_path) > max_hops:
                    continue
                if _is_stub_expandable(comp, schematic_res):
                    visited.add(comp)
                    queue.append((comp, False, new_path))
        else:
            for net in d2n.get(curr, []):
                if net in visited or _is_gnd_net_name(net):
                    continue
                new_path = path + [net]
                if net in rf_nodes:
                    return new_path, net
                if len(new_path) <= max_hops:
                    visited.add(net)
                    queue.append((net, True, new_path))
    return [], None


def attach_and_propagate_filter_bands(rf_graph, saw_define, schematic_res, *, gps_handled=None, max_hops=10):
    """stub 补图之后：把滤波器 SNP 挂到图上并传播频段。

    返回 (锚定成功数, 未挂上的位号列表)，供日志沿用 [DRX] 行。
    """
    if rf_graph is None or not saw_define:
        return 0, []
    gps_handled = set(gps_handled or ())
    alias_to_node = {}
    for nid in list(rf_graph.nodes):
        for alias in _comp_ref_aliases(nid):
            alias_to_node.setdefault(alias, nid)

    attached = set()
    for saw, snp in saw_define.items():
        if saw in gps_handled:
            continue
        node = None
        if saw in rf_graph.nodes:
            node = saw
        else:
            for alias in _comp_ref_aliases(saw):
                if alias in rf_graph.nodes:
                    node = alias
                    break
                if alias in alias_to_node:
                    node = alias_to_node[alias]
                    break
        if node is None:
            continue
        rf_graph.nodes[node].add_band_info(snp)
        attached.add(saw)

    n2d, d2n = _raw_net_device_maps(schematic_res)
    drx_ok, drx_fail = 0, []
    for saw, snp in saw_define.items():
        if saw in attached or saw in gps_handled:
            continue
        if saw in rf_graph.nodes:
            continue
        path, anchor = _bfs_filter_to_graph(
            saw, rf_graph.nodes, n2d, d2n, schematic_res, max_hops=max_hops
        )
        if anchor is None:
            drx_fail.append(saw)
            comp_nets = d2n.get(saw, [])
            if len(comp_nets) == 0:
                add_issue(
                    "WARNING",
                    f"[滤波器未接入] {saw}\n"
                    f"   器件两端被 NC（兼容设计），频段分析将跳过此器件",
                    key=f"drx_fail_{saw}",
                )
            else:
                add_issue(
                    "WARNING",
                    f"[滤波器未接入] {saw}\n"
                    f"   原理图中未找到该器件与射频通路的连接，频段分析将跳过此器件\n"
                    f"   请检查: ① 原理图是否存在该器件  ② 该器件是否连接到射频路径",
                    key=f"drx_fail_{saw}",
                )
            continue
        chain = [saw] + path
        for i in range(len(chain) - 1):
            rf_graph.add_edge(chain[i], chain[i + 1])
        rf_graph.nodes[saw].add_band_info(snp)
        drx_ok += 1
        attached.add(saw)

    rf_graph.propagate_band_info(schematic_res)
    return drx_ok, drx_fail


def _raw_device_nets(schematic):
    """器件 → 非地网（含 NC）。"""
    _n2d, d2n = _raw_net_device_maps(schematic)
    out = {}
    for ref, nets in d2n.items():
        out[str(ref)] = [n for n in nets if n and not _is_gnd_net_name(n)]
    return out


def is_series_nc(ref, schematic, device_nets=None) -> bool:
    """不贴且至少两只非地网：串联 NC（开路）。并联 NC 只有一只射频网。"""
    ref = str(ref or "").strip()
    if not ref or schematic is None:
        return False
    try:
        if schematic.is_network(ref):
            return False
    except Exception:
        pass
    active = getattr(schematic, "part_active", None) or {}
    if active.get(ref, True):
        return False
    nets = (device_nets or {}).get(ref)
    if nets is None:
        nets = _raw_device_nets(schematic).get(ref, [])
    return len(nets) >= 2


def prune_reverse_at_series_nc(start, visited, reverse_edges, schematic):
    """功能1/3/4：串联 NC 当断。器件本身留下，不穿过远端网。

    功能5 仍用未剪的 reverse_edges。
    """
    visited = set(visited or ())
    edges = [tuple(e) for e in (reverse_edges or []) if e and len(e) == 2]
    if not start or not edges:
        return visited, edges
    dnets = _raw_device_nets(schematic)
    series = {n for n in visited if is_series_nc(n, schematic, dnets)}
    if not series:
        return visited, edges
    toward = defaultdict(list)
    for dst, src in edges:
        toward[src].append(dst)
    seen = set()
    live = []
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in series:
            continue
        for nxt in toward.get(cur, []):
            live.append((nxt, cur))
            if nxt not in seen:
                stack.append(nxt)
    return seen, live


def reverse_cone_has_live_rf(visited, schematic, start=None) -> bool:
    """断开串联 NC 后，锥里还有贴装射频通路才进仿真表。

    只有天线网 / 座子 / 不贴件 / 只挂在天线网上的并地件（TVS）→ 空口，不出 ANT-COM。
    """
    active = getattr(schematic, "part_active", None) or {}
    dnets = _raw_device_nets(schematic)
    vis = {str(n or "").strip() for n in (visited or ()) if n}
    ant = str(start or "").strip()
    for name in vis:
        if not name:
            continue
        try:
            if schematic.is_network(name):
                continue
        except Exception:
            pass
        if _is_connector_like(name):
            continue
        if not active.get(name, True):
            continue
        nets = [n for n in dnets.get(name, []) if n in vis]
        if ant and nets and all(n == ant for n in nets):
            continue
        return True
    return False


def _symbol_gate_labels(parser, model: str) -> dict:
    """符号头不是 UND 时，按符号名把 GATE 脚名读出来。只给开关分侧用。"""
    doc = getattr(parser, "doc_content", None) if parser is not None else None
    if not doc or not model:
        return {}
    wanted = str(model).upper().replace("-", "_")
    lines = str(doc).splitlines()
    for index, raw in enumerate(lines):
        cols = raw.split()
        if not cols:
            continue
        token = cols[0].upper().replace("-", "_")
        if token != wanted and not token.endswith("_" + wanted):
            continue
        labels = {}
        gate = False
        started = False
        for candidate in lines[index + 1:index + 40]:
            stripped = candidate.strip()
            if not stripped:
                if started:
                    break
                continue
            if stripped.startswith("GATE "):
                gate = True
                continue
            if not gate:
                if not stripped.startswith("TIMESTAMP"):
                    break
                continue
            parts = stripped.split()
            if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
                labels[parts[0]] = parts[-1]
                started = True
            elif started:
                break
        if labels:
            return labels
    return {}


def find_reverse_paths(graph, start_node_id, SchematicRes, pin_parser=None):
    """从天线口反向 BFS，收集本口结构用的器件锥。

    规则（保持简单）：
      1) 先走 rf_graph 上的反向边（主路径已建的边）
      2) 碰到**电子开关**时，网表上对侧 RF 口都要当岔路继续扫
         （IN 侧对 OUT/COM/ANT 侧；一侧多前端时不能只扫到其中一支）。
         同侧不穿：IN1 不到 IN2，OUT1 不到 OUT2。
         脚名认不出两侧时仍扫全部 RF 口，缺一份 S 参数也不删原理图上的腿。
      3) 碰到 **SAW / 其它滤波 / 多工** 时同样扫网表全部 RF 口
         （天线侧已进图、芯片侧被单向边丢掉时，仍要接到开关后再到 PA/LNA）
      4) 网上按网表把同网器件都扫进来（并联对地件常不在 rf_graph）
      5) 从岔路口沿网表往芯片侧扫到 PA/LNA 为止
      6) 共网邻器件：远侧最近整口是别的 ANT 则丢掉（本口臂 + FE 臂留下）
      7) 收边单向：已有对向则丢（先到为准）。网表补口/并联件时
         不得把刚走过的网↔器件再反写回去。
      8) DIFEM 只在天线口和频段口之间穿。两只天线口不互穿，
         两只频段口也不互穿。脚名分不出两侧时不限制。
      9) 分频/三工器从公共口进来，每个频段掷都要留下；从一掷进来
         只回公共口，不串到别的掷。认不出公共口时射频口都补上。

    不做位号名单；电源/控制/FBRX/跨 ANT JUMP 网不走。
    共焊盘分臂不问对端器件种类。
    """
    from refnum.graph import (
        build_net_device_maps,
        is_ant_path_node,
        is_cross_ant_isolation_net,
        is_gnd_net,
        is_non_rf_path_net,
        is_pa_lna_ref,
        is_plausible_comp_ref,
        is_transceiver_ref,
        same_ant_port,
    )

    visited = set()
    edges = set()
    nodes = set()
    queue = collections.deque([start_node_id])
    home_ant = str(start_node_id or "")
    # 用 raw 网表扫岔路：50R 过滤后的 signal_infos 会丢掉同网部分器件
    _sig = (
        getattr(SchematicRes, "signal_infos_raw", None)
        or getattr(SchematicRes, "all_signal_infos_raw", None)
        or getattr(SchematicRes, "signal_infos", None)
        or []
    )
    n2d, d2n = build_net_device_maps(_sig)
    _fe_types = ("RF LNA", "RF PA", "Transceiver", "LPAMiF")
    _port_cache: dict = {}
    _pin_label_cache: dict = {}

    def _keep_home_pad_neigh(src: str, dst: str, src_is_net: bool) -> bool:
        if src_is_net:
            return _pad_peer_ok_for_home(
                dst, src, home_ant, d2n, n2d, SchematicRes,
                cache=_port_cache,
            )
        try:
            dst_is_net = bool(SchematicRes.is_network(dst))
        except Exception:
            dst_is_net = str(dst).startswith("$$$")
        if dst_is_net:
            return True
        if is_ant_path_node(dst) and not same_ant_port(dst, home_ant):
            return False
        for net in d2n.get(src) or []:
            ns = str(net)
            if not ns or is_gnd_net(ns) or not _ok_rf_net(ns):
                continue
            members = {str(x) for x in (n2d.get(ns) or [])}
            if str(dst) not in members:
                continue
            if not _pad_peer_ok_for_home(
                dst, ns, home_ant, d2n, n2d, SchematicRes,
                cache=_port_cache,
            ):
                return False
        return True

    def _is_fe_stop(nid: str) -> bool:
        # DIFEM 也是 312425，类型表里仍叫 LPAMiF。抽取要穿过去，不在这里停。
        if is_difem_module(SchematicRes, nid):
            return False
        try:
            pt = SchematicRes.get_part_type(nid) or ""
        except Exception:
            pt = ""
        if pt in _fe_types:
            return True
        try:
            if is_pa_lna_ref(nid, SchematicRes) or is_transceiver_ref(
                nid, SchematicRes
            ):
                return True
        except Exception:
            pass
        return False

    def _ok_rf_net(ns: str) -> bool:
        if is_non_rf_path_net(ns) or is_cross_ant_isolation_net(ns, home_ant):
            return False
        if "FBRX" in str(ns).upper():
            return False
        return True

    def _is_rf_filter(nid: str) -> bool:
        try:
            if SchematicRes.is_SAWFBAR(nid):
                return True
        except Exception:
            pass
        try:
            pn = str(SchematicRes.part_infos.get(nid, "") or "")
        except Exception:
            pn = ""
        return pn.startswith(("312706", "312799", "312703"))

    def _netlist_neighbors(cur: str) -> list[str]:
        """网表一跳：器件只接到 RF 网，网只接到邻器件。禁止跳网直连。"""
        out: list[str] = []
        try:
            cur_is_net = bool(SchematicRes.is_network(cur))
        except Exception:
            cur_is_net = str(cur).startswith("$$$")
        if cur_is_net:
            if not _ok_rf_net(cur):
                return out
            for d in n2d.get(cur) or []:
                ds = str(d)
                if ds == cur:
                    continue
                if not is_plausible_comp_ref(ds) and not is_ant_path_node(ds):
                    continue
                out.append(ds)
            return out
        for net in d2n.get(cur) or []:
            ns = str(net)
            if _ok_rf_net(ns):
                out.append(ns)
        return out

    def _difem_net_is_other_ant(net_name: str) -> bool:
        """这只口离开 DIFEM 后先碰到别的天线，就不是本口要往下抽的一侧。"""
        seen = {str(current_id)}
        queue_local = collections.deque()
        for dev in n2d.get(net_name) or []:
            ds = str(dev or "").strip()
            if ds and ds not in seen:
                queue_local.append(ds)
        hops = 0
        while queue_local and hops < 40:
            hops += 1
            cur = queue_local.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            if is_ant_path_node(cur):
                return not same_ant_port(cur, home_ant)
            if is_transceiver_ref(cur, SchematicRes):
                continue
            if is_pa_lna_ref(cur, SchematicRes) and not is_difem_module(
                SchematicRes, cur
            ):
                continue
            for net in d2n.get(cur) or []:
                ns = str(net)
                if not ns or ns in seen:
                    continue
                # 异口天线名会被射频网过滤丢掉，这里要先认出来。
                if is_ant_path_node(ns):
                    return not same_ant_port(ns, home_ant)
                if not _ok_rf_net(ns):
                    continue
                seen.add(ns)
                for dev in n2d.get(ns) or []:
                    ds = str(dev or "").strip()
                    if ds and ds not in seen:
                        queue_local.append(ds)
        return False

    _switch_roles: dict = {}

    def _comp_pin_on_net(ref: str, net) -> str | None:
        ref = str(ref)
        prefix = ref + "."
        for source in (
            getattr(SchematicRes, "signal_infos_raw", None) or (),
            getattr(SchematicRes, "all_signal_infos_raw", None) or (),
            getattr(SchematicRes, "signal_infos", None) or (),
        ):
            for item in source:
                if not item or str(item[0]) != str(net) or len(item) < 3:
                    continue
                comps = list(item[1] or ())
                conns = list(item[2] or ())
                hit = None
                for conn in conns:
                    text = str(conn)
                    if text.startswith(prefix):
                        hit = text.split(".", 1)[1]
                        break
                if hit:
                    return hit
                if ref not in comps:
                    continue
                try:
                    idx = comps.index(ref)
                except ValueError:
                    continue
                if 0 <= idx < len(conns) and str(conns[idx]).isdigit():
                    return str(conns[idx])
        return None

    def _gate_pin_labels(ref: str) -> dict:
        """原理图脚名优先。S 参数只补没写到的脚，不拿残缺口表删腿。"""
        if ref in _pin_label_cache:
            return _pin_label_cache[ref]
        from rf.SnpFileParser import SNPFileInfo

        labels: dict = {}
        parser = pin_parser
        models = []
        for attr in ("part_infos_name", "part_type_token"):
            val = str((getattr(SchematicRes, attr, None) or {}).get(ref, "") or "")
            if val and val not in models:
                models.append(val)
        if parser is not None:
            for model in models:
                try:
                    got = parser.get_pin_labels(model) or {}
                except Exception:
                    got = {}
                if not got:
                    got = _symbol_gate_labels(parser, model)
                if got:
                    labels = {str(k): str(v) for k, v in got.items() if v}
                    break
        try:
            inst = SNPFileInfo.get_instance_by_ref_des(ref)
            info = getattr(inst, "pin_info", None) or {}
        except Exception:
            info = {}
        for pin, data in info.items():
            key = str(pin)
            name = str((data or {}).get("name") or "")
            if key not in labels and name:
                labels[key] = name
        _pin_label_cache[ref] = labels
        return labels

    def _switch_pin_roles(ref: str) -> dict:
        from rf.SnpFileParser import pin_io_role

        if ref in _switch_roles:
            return _switch_roles[ref]
        roles = {
            pin: pin_io_role(name) for pin, name in _gate_pin_labels(ref).items()
        }
        _switch_roles[ref] = roles
        return roles

    def _cross_side_neighbors(ref: str, neigh_list: list, roles: dict,
                              side_a: str, side_b: str) -> list:
        """进来的那一侧，只能往对侧走。认不出两侧时不限制。"""
        if side_a not in roles.values() or side_b not in roles.values():
            return neigh_list
        entry = set()
        for net in d2n.get(ref) or []:
            ns = str(net)
            if ns not in visited:
                continue
            role = roles.get(str(_comp_pin_on_net(ref, ns) or ""))
            if role in (side_a, side_b):
                entry.add(role)
        if len(entry) != 1:
            return neigh_list
        side = next(iter(entry))
        kept = []
        for peer in neigh_list:
            pin = _comp_pin_on_net(ref, peer)
            if pin is not None and roles.get(str(pin)) == side:
                continue
            kept.append(peer)
        return kept

    def _difem_pin_roles(ref: str) -> dict:
        return {
            pin: diversity_port_side(name)
            for pin, name in _gate_pin_labels(ref).items()
        }

    def _divider_port_neighbors(ref: str, neigh_list: list) -> list:
        """分频/三工器从公共口补上每个频段掷。从一掷进来只回公共口。

        这类器件不在滤波补口名单里。天线侧已经进图、某个掷没写进反向边时，
        那一支会整段消失。认不出公共口就把射频口都补上。
        """
        try:
            pn = str(SchematicRes.part_infos.get(ref, "") or "")
        except Exception:
            pn = ""
        if not pn.startswith("312702"):
            return neigh_list
        from rf.segments import frequency_divider_exit_pins

        entries = []
        device_nets = []
        for net in d2n.get(ref) or []:
            ns = str(net)
            if _ok_rf_net(ns):
                device_nets.append(ns)
            if ns not in visited:
                continue
            pin = _comp_pin_on_net(ref, ns)
            if pin:
                entries.append(str(pin))
        if not entries:
            for peer in _netlist_neighbors(ref):
                if peer not in neigh_list:
                    neigh_list.append(peer)
            return neigh_list

        allowed_pins = None
        for entry in entries:
            exits = frequency_divider_exit_pins(
                ref, SchematicRes, pin_parser, entry)
            if exits is None:
                for peer in _netlist_neighbors(ref):
                    if peer not in neigh_list:
                        neigh_list.append(peer)
                return neigh_list
            pins = {str(pin) for pin in exits}
            allowed_pins = pins if allowed_pins is None else (allowed_pins & pins)
        if not allowed_pins:
            return neigh_list

        allowed_nets = []
        for net in device_nets:
            pin = _comp_pin_on_net(ref, net)
            if pin is not None and str(pin) in allowed_pins:
                allowed_nets.append(net)
        allowed_set = set(allowed_nets)
        device_set = set(device_nets)
        kept = [
            peer for peer in neigh_list
            if str(peer) not in device_set or str(peer) in allowed_set
        ]
        for net in allowed_nets:
            if net not in kept:
                kept.append(net)
        return kept

    while queue:
        current_id = queue.popleft()
        try:
            pt_cur = SchematicRes.get_part_type(current_id) or ""
        except Exception:
            pt_cur = ""
        if current_id in visited and pt_cur not in _fe_types:
            continue

        visited.add(current_id)
        nodes.add(current_id)
        current_node = graph.nodes.get(current_id)

        # 邻接 = 图反向边 ∪ 网表一跳。
        # 已在射频图上的网：只补并联被动件。同网上的开关/分频仍走图边，
        # 否则会把另一天线口的前端整条灌进来。
        # 图上还没有的网（滤波芯片侧新扫到的）：器件全要。
        # 开关 / SAW·滤波·多工 / 图上无点的串联件：网表补 RF 口。
        # 只有一只 RF 网的并联被动件到此为止。
        neigh: list[str] = []
        if current_node:
            neigh.extend(current_node.reverse_edges)
        try:
            is_sw = bool(SchematicRes.is_electronic_switch(current_id))
        except Exception:
            is_sw = False
        try:
            cur_is_net = bool(SchematicRes.is_network(current_id))
        except Exception:
            cur_is_net = str(current_id).startswith("$$$")

        def _rf_net_count(ref: str) -> int:
            return sum(
                1 for net in (d2n.get(ref) or []) if _ok_rf_net(str(net))
            )

        if cur_is_net and current_node is not None:
            # 串联件（两只射频网）已经在图边上。再从网表拉进来会把边记反，
            # 天线往芯片走不过去，剪串联 NC 后远侧网整段丢失。
            for peer in _netlist_neighbors(current_id):
                if (
                    _is_stub_passive(peer, SchematicRes)
                    and _rf_net_count(peer) <= 1
                    and peer not in neigh
                ):
                    neigh.append(peer)
        elif cur_is_net and current_node is None:
            for peer in _netlist_neighbors(current_id):
                if peer not in neigh:
                    neigh.append(peer)
        elif is_sw or _is_rf_filter(current_id) or current_node is None:
            shunt_stop = (
                current_node is None
                and _is_stub_passive(current_id, SchematicRes)
                and _rf_net_count(current_id) <= 1
            )
            if not shunt_stop:
                for peer in _netlist_neighbors(current_id):
                    if peer not in neigh:
                        neigh.append(peer)
        if is_difem_module(SchematicRes, current_id):
            # 图只画到模组天线侧。DIFEM 从网表穿到芯片侧；别的天线口不带进来。
            for peer in _netlist_neighbors(current_id):
                if peer in neigh or _difem_net_is_other_ant(peer):
                    continue
                neigh.append(peer)

        # 已到 PA/LNA/LPAMiF/收发器：不再往芯片外侧扩。DIFEM 已在上面排除。
        if _is_fe_stop(current_id):
            continue

        if is_sw:
            neigh = _cross_side_neighbors(
                current_id, neigh, _switch_pin_roles(current_id), "pole", "throw")
        elif is_difem_module(SchematicRes, current_id):
            neigh = _cross_side_neighbors(
                current_id, neigh, _difem_pin_roles(current_id), "ant", "radio")
        if not cur_is_net:
            neigh = _divider_port_neighbors(current_id, neigh)

        for source_id in neigh:
            if source_id == current_id:
                continue
            if not _keep_home_pad_neigh(current_id, source_id, cur_is_net):
                continue
            # 先到为准：当前件曾作为对端写入过 source→current，不能再写回去。
            if (current_id, source_id) not in edges:
                edges.add((source_id, current_id))
            if source_id not in visited:
                queue.append(source_id)

    return nodes, edges


def visualize_rf_graph(graph, output_path='rf_graph.pdf'):
    """
    优化版射频信号全图可视化函数
    - 完整绘制所有节点和连接
    - 节点横向排列（从左到右）
    - 频段信息着色
    - 自定义节点样式
    """
    # 创建Graphviz图对象
    dot = Digraph(comment='RF Signal Graph')
    dot.attr(rankdir='LR')
    dot.attr('graph', nodesep='0.25', ranksep='0.4')

    # 频段颜色映射
    band_colors = {
        'B1': '#FF6B6B', 'B2': '#4ECDC4', 'B3': '#45B7D1',
        'B4': '#FFD166', 'B5': '#9B5DE5', None: '#D3D3D3'
    }

    for node_id, node in graph.nodes.items():
        band_info = getattr(node, 'band_info', None)
        band_label = f"\n({band_info})" if band_info else ""

        label = f"<<B>{node_id}</B>\n{band_label}>"

        # 基础节点样式
        node_attrs = {
            'style': 'filled,rounded',
            'fillcolor': band_colors.get(node.get_band_info(), band_colors[None]),
            'fontname': 'Arial',
            'fontsize': '10',
            # 方向组件特殊形状
            'shape': 'box' if node_id in getattr(graph, 'directional_comps', []) else 'ellipse'
        }

        dot.node(node_id, label=label, **node_attrs)

    for node_id, node in graph.nodes.items():
        if hasattr(node, 'forward_edges'):
            for target_id in node.forward_edges:
                dot.edge(node_id, target_id,
                         color='#2F4F4F',
                         penwidth='1.5',
                         arrowhead='normal')

    dot.attr(overlap='false')

    # 右下角频段颜色图例
    add_legend_to_dot(dot, [
        ('#FF6B6B', 'B1'),
        ('#4ECDC4', 'B2'),
        ('#45B7D1', 'B3'),
        ('#FFD166', 'B4'),
        ('#9B5DE5', 'B5'),
        ('#D3D3D3', 'Unassigned'),
    ])

    # 保存PDF
    dot.render(output_path, view=False, cleanup=True)
    print(f"射频信号全图已保存至: {output_path}")
    return dot


def draw_reverse_paths_new(graph, SchematicRes, start_node_id, output_path='reverse_paths.pdf', pin_parser=None):
    """
    优化版射频信号路径可视化函数（无拓扑排序）
    - 起点在右侧
    - 节点横向排列
    - 参考用户自定义样式
    """
    # 获取节点和边（BFS反向遍历）
    visited, reverse_edges = find_reverse_paths(
        graph, start_node_id, SchematicRes, pin_parser=pin_parser)

    # 创建Graphviz图
    dot = Digraph(comment='Path Graph', engine='dot')
    dot.attr(rankdir='LR')
    dot.attr('graph', nodesep='0.65', ranksep='0.45')  # 为多端 pin 标签保留纵向间距

    displayed_edges = {(dst, src) for src, dst in reverse_edges}
    reverse_edges_new = set()
    visited_new = set()
    for node_id in visited:
        node = graph.nodes.get(node_id)
        band = node.get_band_info() if node else None
        color = SchematicRes.determine_node_color(node_id)
        label = f"{node_id}\n({band})" if band else node_id
        # 自定义节点样式
        node_attrs = {
            'style': 'filled,rounded',
            'fillcolor': color,
            'fontname': 'Arial',
            'fontsize': '10'
        }
        node_attrs.update(pin_node_size_attrs(SchematicRes.get_part_type(node_id)))
        dot.node(node_id, label=label, **node_attrs)
        if SchematicRes.is_network(node_id):
            new_components = SchematicRes.get_components_for_net(node_id)
            for new_component in new_components:
                if new_component not in visited:
                    node = graph.nodes.get(new_component)
                    band = node.get_band_info() if node else None
                    color = SchematicRes.determine_node_color(new_component)
                    node_attrs = {
                        'style': 'filled,rounded',
                        'fillcolor': color,
                        'fontname': 'Arial',
                        'fontsize': '10'
                    }
                    node_attrs.update(
                        pin_node_size_attrs(
                            SchematicRes.get_part_type(new_component)
                        )
                    )
                    visited_new.add(new_component)
                    reverse_edges_new.add((new_component, node_id))
                    displayed_edges.add((node_id, new_component))

                    dot.node(new_component, label=new_component, **node_attrs)
    edge_pin_labels = build_edge_pin_labels(SchematicRes, displayed_edges)
    for component, net_name in reverse_edges_new:
        displayed_edge = (net_name, component)
        edge_attrs = {
            'color': '#2F4F4F',
            'dir': 'back',
            'penwidth': '1.5',
            'arrowhead': 'vee',
        }
        if displayed_edge in edge_pin_labels:
            label_position = (
                'headlabel' if not SchematicRes.is_network(net_name)
                else 'taillabel'
            )
            edge_attrs.update({
                label_position: format_pin_badge(edge_pin_labels[displayed_edge]),
                'labeldistance': '2.4',
                'labelangle': '0',
            })
        dot.edge(component, net_name, **edge_attrs)

    for src, dst in reverse_edges:
        displayed_edge = (dst, src)
        edge_attrs = {
            'color': '#2F4F4F',
            'dir': 'back',
            'penwidth': '1.5',
            'arrowhead': 'vee',
        }
        if displayed_edge in edge_pin_labels:
            label_position = (
                'headlabel' if not SchematicRes.is_network(dst)
                else 'taillabel'
            )
            edge_attrs.update({
                label_position: format_pin_badge(edge_pin_labels[displayed_edge]),
                'labeldistance': '2.4',
                'labelangle': '0',
            })
        dot.edge(src, dst, **edge_attrs)

    # 右下角器件类型颜色图例
    add_legend_to_dot(dot, [
        ('#ff7500', 'Antenna (ANT)'),
        ('#a4e2c6', 'Signal Net'),
        ('#ef5b9c', 'LNA'),
        ('#d71345', 'PA'),
        ('#b22c46', 'RF Module'),
        ('#8a2be2', 'SAW Filter'),
        ('#815fcc', 'Diplexer'),
        ('#7d3f98', 'Multiplexer'),
        ('#ffa500', 'Coupler'),
        ('#20b2aa', 'Transceiver'),
        ('#9370db', 'Switch'),
        ('#da70d6', 'Other Filter'),
        ('#cccccc', 'Other'),
    ])

    # 保存PDF
    try:
        dot.render(output_path, view=False, cleanup=True)
        print(f"路径图已保存至: {output_path}")
    except Exception as e:
        print(f"⚠️ 路径图渲染失败（{output_path}）: {e}")
        # 把 dot 源码写出便于离线排错
        try:
            with open(output_path + '.dot', 'w', encoding='utf-8') as _f:
                _f.write(dot.source)
            print(f"路径图 DOT 源码已保存至: {output_path}.dot")
        except Exception:
            pass

    return visited, reverse_edges


SAW_DEFINE = {}


def remove_bidirectional_edges(graph):
    """移除邻接表中的双向边。"""
    to_remove = set()
    for node, neighbors in graph.items():
        for neighbor in neighbors:
            if neighbor in graph and node in graph[neighbor]:
                to_remove.add(frozenset({node, neighbor}))

    for pair in to_remove:
        node1, node2 = pair
        if node1 in graph and node2 in graph[node1]:
            graph[node1].remove(node2)
        if node2 in graph and node1 in graph[node2]:
            graph[node2].remove(node1)

    return graph
