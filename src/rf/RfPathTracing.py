from collections import defaultdict, deque
from rf.SnpFileParser import *
from rf.segments import (
    _looks_like_matching_rlc,
    device_exit_pins,
    is_series_through_filter,
)
from rf.PathGraphBuilder import *
from schematic.SchematicNetComponentProfiler import *
from rf.RfGraphModel import *
from refnum.graph import (
    connector_feed_port_id,
    is_ant_path_node,
    is_connector_ref,
)


def antenna_terminus_id(net, network_dict, home_ant=None) -> str | None:
    """本 50R 网若是天线口，返回构图节点 id：整口用网名，座子叶用 ANT_J脚。"""
    if is_ant_path_node(net):
        return net
    if home_ant and str(net).strip().upper() == str(home_ant).strip().upper():
        return str(home_ant)
    info = (network_dict or {}).get(net)
    if not info:
        return None
    devices = info[0] if len(info) > 0 else []
    pins = info[1] if len(info) > 1 else []
    return connector_feed_port_id(net, devices, pins)


def sort_paths_by_special_nodes(paths):
    """
    按照路径中特殊节点的数量进行排序

    参数:
        paths: 包含多条路径的列表，每条路径是一个字符串列表

    返回:
        排序后的路径列表，按照以下规则排序:
        1. 先筛选出符合"字母+数字"格式的节点（如L3333、LPF222）
        2. 然后计算符合以下条件的节点数量:
           - 不以'T'、'C'、'R'、'L'开头
           - 或以'LPF'开头
        3. 最后按这个数量降序排列

    示例:
        >>> paths = [
        ...     ['SDR1500', 'SDR1_GNSS_IN_L1', 'C3308', '$$$31663', 'FIL3300', '$$$4468', 'C3307', '$$$11829', 'U3300'],
        ...     ['SDR1500', 'SDR1_GNSS_IN_L1', 'C3312', '$$$31155', 'C3311', '$$$11829', 'U3300'],
        ...     ['SDR1500', 'SDR1_GNSS_IN_L5', 'C3315', '$$$28654', 'FIL3302', '$$$28653', 'C3314', '$$$30210', 'U3301'],
        ...     ['SDR1500', 'SDR1_GNSS_IN_L5', 'C3318', '$$$31088', 'C3317', '$$$30210', 'U3301']
        ... ]
        >>> sorted_paths = sort_paths_by_special_nodes(paths)
    """
    def count_special_nodes(path):
        """计算单条路径中符合条件的特殊节点数量"""
        count = 0
        for node in path:
            # 匹配字母+数字格式（如L3333、LPF222）
            if re.fullmatch(r'^[A-Za-z]+\d+$', node):
                # 检查特殊条件
                if not _looks_like_matching_rlc(node):
                    count += 1
        return count

    # 按特殊节点数量降序排序
    # 按特殊节点数量降序 -> 路径总长度降序的双条件排序
    return sorted(
        paths,
        key=lambda x: (-count_special_nodes(x), -len(x)),  # 双重降序
        reverse=True
    )


def find_LNA_PA_RFM(SchematicRes, start_node,PINParser):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    network_data = SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:

            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    visited_nodes = set()  # 全局已访问节点（避免环路）
    paths = []             # 存储所有合法路径

    def dfs(current_node, current_path,current_nets):
        # 检查当前节点是否非法
        if (not _looks_like_matching_rlc(current_node)) and start_node != current_node:
            if SchematicRes.part_infos.get(current_node, '').startswith(('312405', '312406', '312425')):
                paths.append(current_path + [current_node])
                return  # 终止当前路径
            if SchematicRes.part_infos.get(current_node, '').startswith(('312706', '312701', '312427', '312702', '312799', '312703')):
                if not is_series_through_filter(current_node, SchematicRes, PINParser):
                    return

        # 避免环路
        if current_node in visited_nodes:
            return
        visited_nodes.add(current_node)

        # 遍历当前节点所属的所有网络
        for net in node_to_networks[current_node]:
            last_node = current_path[-1] if current_path else ""
            if net == last_node:
                continue
            _tid = antenna_terminus_id(net, network_dict)
            if _tid:
                paths.append(current_path + [current_node] + [_tid])
                continue
            # 遍历该网络的其他节点
            for neighbor in network_dict[net][0]:
                neighbor_pin = exit_pins_from_entry(
                    SchematicRes, current_node, last_node, network_dict, PINParser)

                if neighbor == current_node:  # 避免直接回溯
                    continue
                if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                    continue
                if neighbor_pin is not None:
                    if is_pin_in_neighbor(current_node, network_dict[net], neighbor_pin):
                        dfs(neighbor, current_path +
                            [current_node, net], current_nets)  # 记录网络和节点
                else:
                    dfs(neighbor, current_path +
                        [current_node, net], current_nets)  # 记录网络和节点
                        
        visited_nodes.remove(current_node)  # 回溯

    # 开始DFS
    dfs(start_node, [],set())
    return paths


def find_LNA_PA(SchematicRes, start_node,PINParser):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    
    network_data = SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}
    
    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
    
        for node in nodes:
            #print(f"DEBUG:node in nodes  = {node}")
            node_to_networks[node].append(net)
            

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    visited_nodes = set()  # 全局已访问节点（避免环路）
    paths = []             # 存储所有合法路径
    #visited_nodes.add(start_node)
    visited_net = set()    # 全局已访问网络（避免重复访问）
    def dfs(current_node, current_path, current_nets):
        if not _looks_like_matching_rlc(current_node):
            # 初始节点可以多次访问
            if current_node != start_node:
                part_info = SchematicRes.part_infos.get(current_node, '')

                # PA/LNA节点
                if part_info.startswith(('312405', '312406')):
                    paths.append(current_path + [current_node])
                    return

                # 滤波器等有源器件
                if part_info.startswith(('312706', '312701', '312427', '312702', '312799', '312703', '312425')):
                    if not is_series_through_filter(current_node, SchematicRes, PINParser):
                        return

        # 避免环路
        if current_node in visited_nodes:
            return

        visited_nodes.add(current_node)
        # 遍历当前节点所属的所有网络
        for net in node_to_networks[current_node]:
            if len(current_path) != 0:
                last_node = current_path[-1]
            else:
                last_node = ""
            if net == last_node:
                continue
            _tid = antenna_terminus_id(net, network_dict)
            if _tid:
                paths.append(current_path + [current_node] + [_tid])
                continue

            neighbor_pin = exit_pins_from_entry(
                SchematicRes, current_node, last_node, network_dict, PINParser)

            for neighbor in network_dict[net][0]:
                if net == last_node:
                    continue
                if neighbor == current_node:  # 避免直接回溯
                    continue
                if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                    continue
                if neighbor_pin is not None:
                    if is_pin_in_neighbor(current_node, network_dict[net], neighbor_pin):
                        dfs(neighbor, current_path +
                            [current_node, net], current_nets)  # 记录网络和节点
                else:
                    dfs(neighbor, current_path +
                        [current_node, net], current_nets)  # 记录网络和节点

        visited_nodes.remove(current_node)  # 回溯

    # 开始DFS
    dfs(start_node, [], set())
    return paths


def find_RFM_ANT(SchematicRes, start_node):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    # 312706 SAWFBAR
    # 312701 耦合器
    # 312427 Transceiver
    # 312423 电子开关
    # 312702 分频器
    # 312799 其它滤波器
    # 312406 射频LNA
    # 312405 射频PA
    # 312703_多工器
    # 312425模组
    def filter_paths_with_duplicate_nodes(paths):
        """
        过滤掉所有包含重复节点的路径（即使是非连续重复）
        返回：仅保留完全不包含重复节点的路径
        """
        return [path for path in paths if len(path) == len(set(path))]
    network_data = SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:
            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    visited_nodes = set()  # 全局已访问节点（避免环路）
    visited_nets = set()   # 新增：全局已访问网络[2,6](@ref)
    paths = []             # 存储所有合法路径

    def dfs(current_node, current_path, current_nets):
        # or part_infos.get(current_node, '').startswith(('312425'))
        if (
            is_connector_ref(current_node)
            or SchematicRes.part_infos.get(current_node, '').startswith(('312425', '312406', '312405', '312427'))
            and start_node != current_node
        ):
            # if (current_node.startswith('J') or SchematicRes.part_infos.get(current_node, '').startswith(('312425', '312406', '312405')) and start_node != current_node):
            return  # 终止当前路径
        # 避免环路
        if current_node in visited_nodes:
            return

        visited_nodes.add(current_node)
        # 遍历当前节点所属的所有网络
        for net in node_to_networks[current_node]:
            _tid = antenna_terminus_id(net, network_dict)
            if _tid:
                paths.append(current_path + [current_node] + [_tid])
                continue
            # 遍历该网络的其他节点
            for neighbor in network_dict[net][0]:
                if neighbor == current_node:  # 避免直接回溯
                    continue
                if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                    continue
                dfs(neighbor, current_path +
                    [current_node, net], current_nets)  # 记录网络和节点

        visited_nodes.remove(current_node)  # 回溯

    # 开始DFS
    dfs(start_node, [], set())
    # res = filter_paths_with_duplicate_nodes(paths)
    return paths
    return res


def extract_pin_number(node_name, net_data):
    """
    从网络数据中提取指定节点的引脚号

    参数:
        node_name: 要查询的节点名称(如'U2703')
        net_data: 网络数据元组 (nodes_list, pins_list)

    返回:
        引脚号字符串(如'4')，未找到返回None
    """
    nodes, pins = net_data
    try:
        index = nodes.index(node_name)
        pin_full = pins[index]
        return pin_full.split('.')[1]  # 分割并返回引脚号部分
    except (ValueError, IndexError):
        return None


def exit_pins_from_entry(SchematicRes, current_node, last_node, network_dict, pin_parser):
    """这颗器件从上一张网进来，主路径允许出去的脚。

    None：不按脚号卡。开关靠前后端规则；匹配件和一进一出滤波没有第三掷。
    列表：只能走到这些脚上的网。空列表表示这只入口不能再穿过该器件。
    """
    if SchematicRes.is_electronic_switch(current_node):
        pin_index = extract_pin_number(
            current_node, network_dict[last_node]) if last_node else None
        return resolve_electronic_switch_neighbor_pins(
            SchematicRes, current_node, pin_index, pin_parser)
    pin_index = extract_pin_number(
        current_node, network_dict[last_node]) if last_node else None
    pins = device_exit_pins(current_node, SchematicRes, pin_parser, pin_index)
    if pins is None and SchematicRes.is_SAWFBAR(current_node) and pin_index is not None:
        snp = SNPFileInfo(
            SchematicRes.get_part_type(current_node),
            SchematicRes.get_part_name(current_node))
        if snp.pin_info:
            pins = get_other_category_pins_with_exclusion(
                snp.pin_info, pin_index,
                component_name=f"{current_node}({SchematicRes.get_part_name(current_node)})"
            )
    return pins


def is_pin_in_neighbor(node_name, net_data, pin_set):
    """
    检查节点的引脚号是否在邻居引脚集合中

    参数:
        node_name: 要检查的节点名称(如'U2703')
        net_data: 网络数据元组 (nodes_list, pins_list)
        pin_set: 邻居引脚号集合(如{2,3,5,6})

    返回:
        bool: 如果节点的引脚号在集合中返回True，否则False
    """
    nodes, pins = net_data
    try:
        index = nodes.index(node_name)
        pin_full = pins[index]
        pin_num = int(pin_full.split('.')[1])  # 转换为整数
        return pin_num in pin_set
    except (ValueError, IndexError, AttributeError):
        return False


def is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor) -> bool:
    """是否电子开关→电子开关的矩阵互连跳转。

    DPDT/ASDIV/SRS 等多口开关之间常有 RFI 直连（如 DPDT#2_RFI2），
    用于跨天线口转接。功能5 主链只走「天线↔LNA/PA」，禁止经此类
    开关互跳串到别的 ANT 域。规则只依赖器件类别（电子开关），不绑位号。
    """
    try:
        return bool(
            SchematicRes.is_electronic_switch(current_node)
            and SchematicRes.is_electronic_switch(neighbor)
        )
    except Exception:
        return False


def _is_path_shunt_hop(SchematicRes, neighbor, node_to_networks) -> bool:
    """邻接件是否为「纯并联」——主链 DFS 不得把它当串联跳点。

    多点网 {耦合器, 匹配 C, 对地 L} 上若 hop 经对地 L，会把并联塞进串联序
    （L2463 夹在 U2401—C2441 之间）。判定与 refnum_graph.is_shunt_device 对齐：
    RLCT + 非地信号网度数恰好 1 + 对地/is_parallel。
    """
    if not neighbor:
        return False
    try:
        from refnum.graph import is_rlct, is_gnd_net
    except Exception:
        return False
    if not is_rlct(neighbor, SchematicRes):
        return False
    nets = [str(n) for n in (node_to_networks.get(neighbor) or []) if n]
    nongnd = [n for n in nets if not is_gnd_net(n)]
    if len(nongnd) != 1:
        return False
    try:
        if SchematicRes.is_parallel(neighbor):
            return True
    except Exception:
        pass
    return any(is_gnd_net(n) for n in nets)


def resolve_electronic_switch_neighbor_pins(
        SchematicRes, current_node, pin_index, PINParser):
    """电子开关「脚集合约束」解析（功能5 §3.0）。

    **通断真相 = 原理图网表**（器件.pin→网→邻接器件），不是 SNP。

    历史实现用 SNP active-pair / pin_count 拓扑表限制「进脚→出脚」，
    残缺 SNP（只建模 COM+单 RF）会把原理图上真实存在的其它 RF 脚整段掐死
    （典型：U2203 原理图 pin8→C2232，SNP 仅 2/4 → 开库后 ANT7 缺 C2232 臂）。

    现约定：
      - **一律返回 None**：调用方不对电子开关做 pin 集合否决
      - 防串台靠网表侧规则：`esw_through_hop_allowed`（FE↔ANT）、
        `is_switch_matrix_cross_hop`（开关↔开关）等
      - SNP 仍可用于频段/展示，**不得**决定「有没有这条边」

    参数保留为兼容旧调用方；禁止按位号特判。
    """
    _ = (SchematicRes, current_node, pin_index, PINParser)
    return None


def get_other_category_pins_with_exclusion(pin_info, pindex, component_name=None):
    """
    将引脚信息分为两类：有频率的和无频率的
    根据给定的pindex返回另一类引脚信息
    如果另一类为空，则返回当前类但排除pindex自身

    若端口名呈 OUT/IN、RFOUT/RFIN 等多极结构，优先按 I/O 角色分类，
    避免同侧 OUT1→OUT2 被当成合法出口（FG390 U2502/AW12024 DP4T）。
    
    参数：
    pin_info: 引脚信息字典
    pindex: 要查找的引脚索引
    
    返回：
    引脚信息的字典
    """
    pindex=int(pindex)
    if pindex not in pin_info:
        avail = sorted(pin_info.keys())
        # pin_info 为空说明 S参数文件根本不存在/未加载，该问题已在上游单独报告，此处不再重复警告
        if avail:
            comp_str = f"{component_name} " if component_name else ""
            print(f"警告: {comp_str}引脚索引 {pindex} 不存在于 pin_info 中")
            add_issue('WARNING',
                f"[引脚对不上] {comp_str}原理图第 {pindex} 脚在 S 参数里没有"
                f"（文件现有口: {avail}）。"
                f"通路仍按原理图走；这只脚传不了频段。"
                f"请换带第{pindex}脚的S参数，或核对原理图脚号。",
                key=f"pin_oob_{component_name}_{pindex}"
            )
        return None

    # OUT/IN、RFOUT/RFIN 等多极：按 I/O 角色取对侧，禁止同侧互串
    try:
        entry_role = SNPFileInfo._pin_io_role(pin_info[pindex].get("name", ""))
    except Exception:
        entry_role = "other"
    if entry_role in {"pole", "throw"}:
        other_role = "throw" if entry_role == "pole" else "pole"
        role_exits = [
            pin_key for pin_key, pin_data in pin_info.items()
            if pin_key != pindex
            and SNPFileInfo._pin_io_role(pin_data.get("name", "")) == other_role
        ]
        if role_exits:
            return role_exits

    # 双工/双通道滤波器：COM/ANT ↔ 各 band，禁止 LB↔HB 互串
    # （FG390 DIP2501：B25 口不得直通 B66/L2504 侧）
    def _is_com_pin(pin_key, pin_data):
        name = str(pin_data.get("name", "")).upper()
        return bool(
            pin_data.get("is_common")
            or "COM" in name
            or re.search(r"(^|_)ANT($|_|\d)", name)
            or "COMMON" in name
        )

    com_pins = [k for k, d in pin_info.items() if _is_com_pin(k, d)]
    if com_pins and len(pin_info) >= 2:
        if pindex in com_pins:
            return [k for k in pin_info if k not in com_pins]
        return list(com_pins)
    
    # 分为两类
    pins_with_freq = {}  # 有频率的引脚
    pins_without_freq = {}  # 无频率的引脚
    
    for pin_key, pin_data in pin_info.items():
        # 检查是否有频率（非空字符串）
        freq = pin_data.get('frequency', '')
        if freq and str(freq).strip():
            pins_with_freq[pin_key] = pin_data
        else:
            pins_without_freq[pin_key] = pin_data
    
    # 判断给定的pindex属于哪一类
    pin_name = pin_info[pindex]['name']
    
    if pindex in pins_with_freq:
        pin_category = "有频率引脚"
        other_category = pins_without_freq
        same_category = pins_with_freq
    else:
        pin_category = "无频率引脚"
        other_category = pins_with_freq
        same_category = pins_without_freq
    
    # 如果另一类不为空，返回另一类
    if other_category:
        return list(other_category)
    else:
        same_category_without_pindex = {k: v for k, v in same_category.items() if k != pindex}
        return list(same_category_without_pindex)


def find_PA_LNA_ANT(SchematicRes, start_node, PINParser, home_ant=None):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    开关穿越规则与位号无关（见 resolve_electronic_switch_neighbor_pins）。
    home_ant：本 ANT sheet 口（如 ANT3）；给定时不穿越 JUMP/异 ANTx 网，
    且只收尾到该口（跨 ANT 兼容跳线隔绝）。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    # 312706 SAWFBAR
    # 312701 耦合器
    # 312427 Transceiver
    # 312423 电子开关
    # 312702 分频器
    # 312799 其它滤波器
    # 312406 射频LNA
    # 312405 射频PA
    # 312703_多工器
    try:
        from refnum.graph import (
            is_cross_ant_isolation_net,
            esw_through_hop_allowed,
            classify_esw_port_sides,
            build_net_device_maps,
            is_ant_path_node,
            same_ant_port,
        )
    except Exception:
        def is_cross_ant_isolation_net(net, home=None):  # type: ignore
            return "JUMP" in str(net or "").upper()

        def esw_through_hop_allowed(*_a, **_k):  # type: ignore
            return True

        def classify_esw_port_sides(*_a, **_k):  # type: ignore
            return set(), set()

        def build_net_device_maps(sig):  # type: ignore
            return {}, {}

        def is_ant_path_node(n):  # type: ignore
            s = str(n or "").upper()
            return s.startswith("ANT") and len(s) > 3 and s[3:].isdigit()

        def same_ant_port(a, b):  # type: ignore
            def _n(x):
                s = str(x or "").upper()
                if s.startswith("ANT") and len(s) > 3 and s[3:].isdigit():
                    return "ANT" + str(int(s[3:]))
                return s
            return bool(a) and bool(b) and _n(a) == _n(b)

    network_data = SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:
            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    visited_nodes = set()  # 全局已访问节点（避免环路）
    visited_nets = set()   # 新增：全局已访问网络[2,6](@ref)
    paths = []             # 存储所有合法路径
    home_u = str(home_ant).upper() if home_ant else None
    n2d_map, d2n_map = build_net_device_maps(network_data)
    _esw_sides: dict = {}

    def _sides(sw: str):
        if sw not in _esw_sides:
            _esw_sides[sw] = classify_esw_port_sides(
                sw, home_ant or "", n2d_map, d2n_map, SchematicRes,
            )
        return _esw_sides[sw]

    def dfs(current_node, current_path, current_nets):
        if (
            is_connector_ref(current_node)
            or SchematicRes.part_infos.get(current_node, '').startswith(('312425', '312406', '312405', '312427'))
            and start_node != current_node
        ):
            return  # 终止当前路径
        # 避免环路
        if current_node in visited_nodes:
            return

        visited_nodes.add(current_node)
        # 上一器件（路径里跳过网名）
        prev_dev = ""
        for x in reversed(current_path):
            if x in network_dict:
                continue
            prev_dev = str(x)
            break
        # 遍历当前节点所属的所有网络
        for net in node_to_networks[current_node]:
            if len(current_path) != 0:
                last_node = current_path[-1]
            else:
                last_node = ""
            if net == last_node:
                continue
            # 跨 ANT JUMP / 异口网：禁止穿越到对侧链路
            if is_cross_ant_isolation_net(net, home_ant):
                continue
            # 蜂窝 RF 主链不走 FBRX 取样网（取样链另建，禁止 U2801 经 FBRX 串到 ANT）
            if "FBRX" in str(net).upper():
                continue
            _tid = antenna_terminus_id(net, network_dict, home_ant=home_ant)
            if _tid:
                # ANTx 整口或有名馈点（DIV_ANT）都是构图口。
                # home_ant 给定时只收本口；他口只拦住、不走进去。
                is_home = (
                    home_u is None
                    or same_ant_port(_tid, home_ant)
                    or same_ant_port(net, home_ant)
                )
                if is_home:
                    paths.append(current_path + [current_node] + [_tid])
                continue

            neighbor_pin = exit_pins_from_entry(
                SchematicRes, current_node, last_node, network_dict, PINParser)

            for neighbor in network_dict[net][0]:
                if net == last_node:
                    continue
                if neighbor == current_node:  # 避免直接回溯
                    continue
                # 禁止 DPDT 等开关互连跳到别的 ANT 域（主链只走天线↔LNA/PA）
                if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                    continue
                # 多点网：不把纯并联 RLCT 当串联跳点
                if _is_path_shunt_hop(SchematicRes, neighbor, node_to_networks):
                    continue
                # 电子开关：FE↔ANT 贯通，禁止 FE↔FE（跨前端/跨口结构串台）
                if (
                    home_ant
                    and SchematicRes.is_electronic_switch(current_node)
                    and prev_dev
                    and not esw_through_hop_allowed(
                        current_node,
                        prev_dev,
                        str(neighbor),
                        home_ant,
                        n2d_map,
                        d2n_map,
                        SchematicRes,
                        sides=_sides(current_node),
                    )
                ):
                    continue
                if neighbor_pin is not None:
                    if is_pin_in_neighbor(current_node, network_dict[net], neighbor_pin):
                        dfs(neighbor, current_path +
                            [current_node, net], current_nets)  # 记录网络和节点
                else:
                    dfs(neighbor, current_path +
                        [current_node, net], current_nets)  # 记录网络和节点

        visited_nodes.remove(current_node)  # 回溯

    # 开始DFS
    dfs(start_node, [], set())
    # res = filter_paths_with_duplicate_nodes(paths)
    return paths


def find_FBRX_SW(SchematicRes, start_node):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    # 312706 SAWFBAR
    # 312701 耦合器
    # 312427 Transceiver
    # 312423 电子开关
    # 312702 分频器
    # 312799 其它滤波器
    # 312406 射频LNA
    # 312405 射频PA
    # 312703 多工器
    def filter_paths_with_duplicate_nodes(paths):
        """
        过滤掉所有包含重复节点的路径（即使是非连续重复）
        返回：仅保留完全不包含重复节点的路径
        """
        return [path for path in paths if len(path) == len(set(path))]
    network_data = SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:
            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    visited_nodes = set()  # 全局已访问节点（避免环路）
    visited_nets = set()   # 新增：全局已访问网络[2,6](@ref)
    paths = []             # 存储所有合法路径

    def dfs(current_node, current_path, current_nets):
        # or part_infos.get(current_node, '').startswith(('312701'))
        if is_connector_ref(current_node):
            return  # 终止当前路径
        if (not _looks_like_matching_rlc(current_node)) and start_node != current_node:
            if SchematicRes.part_infos.get(current_node, '').startswith(('312701')):
                # if any(part_infos.get(item, '').startswith('312423') for item in current_path):
                paths.append(current_path + [current_node])
                return  # 终止当前路径
            if SchematicRes.part_infos.get(current_node, '').startswith(('312706', '312427', '312702', '312799', '312703', '312405', '312406', "312425")):
                if not is_series_through_filter(current_node, SchematicRes, None):
                    return

        # 避免环路
        if current_node in visited_nodes:
            return
        visited_nodes.add(current_node)
        # 遍历当前节点所属的所有网络
        for net in node_to_networks[current_node]:
            # if ("ANT" in net) and start_node != current_node:
            #     paths.append(current_path + [current_node]+[net])
            # return  # 终止当前路径
            # 遍历该网络的其他节点
            for neighbor in network_dict[net][0]:

                if neighbor == current_node:  # 避免直接回溯
                    continue
                if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                    continue
                dfs(neighbor, current_path +
                    [current_node, net], current_nets)  # 记录网络和节点

        visited_nodes.remove(current_node)  # 回溯

    # 开始DFS
    dfs(start_node, [], set())
    res = filter_paths_with_duplicate_nodes(paths)
    return res


def find_FBRX_SW_to_COMPLER_RFM(SchematicRes, start_node):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    # 312706 SAWFBAR
    # 312701 耦合器
    # 312423 电子开关
    # 312702 分频器
    # 312799 其它滤波器
    # 312703 多工器
    # 312406 射频LNA
    # 312405 射频PA
    # 312427 Transceiver
    # 312425 RFM

    def filter_paths_with_duplicate_nodes(paths):
        """
        过滤掉所有包含重复节点的路径（即使是非连续重复）
        返回：仅保留完全不包含重复节点的路径
        """
        return [path for path in paths if len(path) == len(set(path))]
    network_data = SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:
            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    visited_nodes = set()  # 全局已访问节点（避免环路）
    visited_nets = set()   # 新增：全局已访问网络[2,6](@ref)
    paths = []             # 存储所有合法路径

    def dfs(current_node, current_path, current_nets):
        # or part_infos.get(current_node, '').startswith(('312701'))
        if is_connector_ref(current_node):
            return  # 终止当前路径
        if (not _looks_like_matching_rlc(current_node)) and start_node != current_node:
            if SchematicRes.part_infos.get(current_node, '').startswith(('312701')):
                paths.append(current_path + [current_node])
                return  # 终止当前路
            if SchematicRes.part_infos.get(current_node, '').startswith(("312425")):
                paths.append(current_path + [current_node])
                # return  # 终止当前路径
            if SchematicRes.part_infos.get(current_node, '').startswith(('312706', '312427', '312702', '312799', '312703', '312405', '312406')):
                if not is_series_through_filter(current_node, SchematicRes, None):
                    return

        # 避免环路
        if current_node in visited_nodes:
            return
        visited_nodes.add(current_node)
        # 遍历当前节点所属的所有网络
        for net in node_to_networks[current_node]:
            # if ("ANT" in net) and start_node != current_node:
            #     paths.append(current_path + [current_node]+[net])
            # return  # 终止当前路径
            # 遍历该网络的其他节点
            if "FBRX" in net.upper():
                for neighbor in network_dict[net][0]:

                    if neighbor == current_node:  # 避免直接回溯
                        continue
                    if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                        continue
                    dfs(neighbor, current_path +
                        [current_node, net], current_nets)  # 记录网络和节点

        visited_nodes.remove(current_node)  # 回溯

    # 开始DFS
    dfs(start_node, [], set())
    res = filter_paths_with_duplicate_nodes(paths)
    return res


def get_previous_element(tag_list, target):
    if target in tag_list:
        idx = tag_list.index(target)
        return tag_list[:idx+1]
    return None


_GNSS_CHIP_MARKERS = ('GPS', 'GNSS', 'GLONASS', 'BEIDOU', 'GALILEO')


def _is_gnss_rf_net(name) -> bool:
    """GNSS 射频网：名字带 GNSS/GPS。GPS_PPS 这类数字脚不算。"""
    text = str(name).upper()
    if 'PPS' in text:
        return False
    return 'GNSS' in text or 'GPS' in text


def _net_shares_device_with_gnss_named(net, network_dict, node_to_networks) -> bool:
    """芯片脚常在无名网上，一颗匹配把它和 GNSS 具名网拆开。

    网上若有器件同时也落在 GNSS/GPS 具名网上，这条网与具名网是同一段，
    不是另一条蜂窝匹配林。
    """
    if _is_gnss_rf_net(net):
        return True
    nodes = (network_dict.get(net) or ([], []))[0]
    for node in nodes:
        for other in node_to_networks.get(node, []):
            if other != net and _is_gnss_rf_net(other):
                return True
    return False


def _gps_walk_net_allowed(net, current_path, start_node, network_dict, node_to_networks) -> bool:
    """GNSS 具名网，或与具名网隔一颗匹配的脚网，可以走。

    路径上已经有 GNSS 网名后，只继续走无名短网去 LNA；
    主芯片还在的蜂窝大网即使同路径也禁止。
    """
    if _net_shares_device_with_gnss_named(net, network_dict, node_to_networks):
        return True
    if not any(_is_gnss_rf_net(step) for step in current_path):
        return False
    nodes = (network_dict.get(net) or ([], []))[0]
    if start_node in nodes:
        return False
    return str(net).startswith("$$$")


def has_gnss_rf_evidence(SchematicRes) -> bool:
    """是否存在该跑 GNSS 抽取的射频证据。

    有则抽 GPS；没有则整段跳过、进入下一步。证据只看领域语义：
    - 网名含 GNSS，或天线网名同时含 ANT 与 GPS
    - 收发器（312427）型号/描述标明 GPS/GNSS/GLONASS/BEIDOU/GALILEO
    GPS_PPS 这类数字脚不算。
    """
    network_data = getattr(SchematicRes, 'signal_infos_raw', None) or getattr(
        SchematicRes, 'signal_infos', None) or ()
    for item in network_data:
        name = str(item[0]).upper()
        if 'GNSS' in name:
            return True
        if 'GPS' in name and 'ANT' in name:
            return True
    part_infos = getattr(SchematicRes, 'part_infos', None) or {}
    names = getattr(SchematicRes, 'part_infos_name', None) or {}
    descs = getattr(SchematicRes, 'part_desc', None) or {}
    for ref, part_no in part_infos.items():
        if not part_no or not str(part_no).startswith('312427'):
            continue
        identity = ' '.join((str(names.get(ref, '')), str(descs.get(ref, '')))).upper()
        if any(marker in identity for marker in _GNSS_CHIP_MARKERS):
            return True
    return False


def find_GPS_LNA(SchematicRes, start_node, platform_info, require_gnss_network=True):
    """
    从节点出发的DFS，记录所有路径直到遇到第一个非法节点。
    非法节点条件：不以 T/C/R/L 开头，或以 LPF 开头。
    :param network_data: 网络连接数据列表
    :param start_node: 起始节点名称
    :return: 所有合法路径的列表，每条路径以第一个非法节点结束（或无非法节点时完整路径）
    """
    # 312706 SAWFBAR
    # 312701 耦合器
    # 312427 Transceiver
    # 312423 电子开关
    # 312702 分频器
    # 312799 其它滤波器
    # 312406 射频LNA
    # 312405 射频PA
    # 312703_多工器
    def filter_paths_with_duplicate_nodes(paths):
        """
        过滤掉所有包含重复节点的路径（即使是非连续重复）
        返回：仅保留完全不包含重复节点的路径
        """
        return [path for path in paths if len(path) == len(set(path))]
# 312406

    def classify_and_filter(paths, part_infos, required_prefix=('312706', "312406")):
        """
        路径分类与筛选：
        1. 按终点分类所有路径
        2. 每类中保留至少包含一个指定部件节点的路径组

        参数:
            paths: 路径列表
            part_infos: 节点到部件号的字典
            required_prefix: 需要匹配的部件前缀

        返回:
            {终点: [合格路径1, 合格路径2]}
        """
        # 第一步：按终点分类
        end_groups = defaultdict(list)
        for path in paths:
            end_node = path[-1]
            end_groups[end_node].append(path)

        # 第二步：筛选各类
        result = {}
        for end, group in end_groups.items():
            # 检查该组是否包含合格路径
            valid_group = [
                path for path in group
                if any(part_infos.get(node, '').startswith(required_prefix)
                       for node in path)
            ]
            
            for path in group:

                for node in path:
                    part_no=part_infos.get(node,'')
                    match=part_no.startswith(required_prefix) if part_no else False
            

            if valid_group:  # 只保留有合格路径的组
                result[end] = group

        return result
    # 使用 signal_infos_raw：profiler 的非激活器件过滤会删掉 SDR_GNSS_L5 等网络，
    # 导致 L5 链路无法被 DFS 发现；signal_infos_raw 保留全量网络
    network_data = getattr(SchematicRes, 'signal_infos_raw', None) or SchematicRes.signal_infos
    # 后置过滤只要路径里出现 GNSS 网名。无此类网时穷举蜂窝匹配网是 n!，直接空返回。
    # 有 GNSS 网但主 TRX 不在那些网上时，同样不能从 TRX 穷举蜂窝匹配林。
    if require_gnss_network and not any(
            'GNSS' in str(item[0]).upper() for item in network_data):
        print(f"{LogStyle.INFO}ℹ️ 网表无 GNSS 网络，跳过 GPS 路径搜索{LogStyle.ENDC}")
        return []
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:
            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    paths = []
    queue = deque([(start_node, [start_node])])
    seen_nodes = {start_node}
    while queue:
        current_node, current_path = queue.popleft()
        if (
            current_node != start_node
            and (not _looks_like_matching_rlc(current_node))
        ):
            if is_connector_ref(current_node):
                continue
            if SchematicRes.part_infos.get(current_node, '').startswith(('312406')):
                paths.append(current_path)
                continue
            if SchematicRes.part_infos.get(current_node, '').startswith(
                ('312701', '312427', '312423', '312702', '312799', '312703', '312405', '312406')
            ):
                continue
        for net in node_to_networks[current_node]:
            if not _gps_walk_net_allowed(
                net, current_path, start_node, network_dict, node_to_networks
            ):
                continue
            for neighbor in network_dict[net][0]:
                if neighbor == current_node or neighbor in seen_nodes:
                    continue
                if is_switch_matrix_cross_hop(SchematicRes, current_node, neighbor):
                    continue
                seen_nodes.add(neighbor)
                queue.append((neighbor, current_path + [net, neighbor]))

    _deduped = filter_paths_with_duplicate_nodes(paths)
    # 使用 signal_infos_raw 后 DFS 可能找到非 GPS 的 LNA 路径（如蜂窝接收 LNA），
    # 只保留经过 GNSS 网络的路径（SDR_GNSS_L1 / SDR_GNSS_L5 等）
    if require_gnss_network:
        _deduped = [p for p in _deduped if any('GNSS' in str(n).upper() for n in p)]
    # GNSS 路径已经用网名收过口；LNA 是终点。不因 Others 还要 SAW 而丢掉芯片脚。
    res = classify_and_filter(_deduped, SchematicRes.part_infos, ('312706', '312406'))
    #res = classify_and_filter(filter_paths_with_duplicate_nodes(paths), SchematicRes.part_infos, ('312706'))

    # 遍历每个网络字符串
    return [network_str for category, network_list in res.items() for network_str in network_list]


def find_GPS_LNA_from_GNSS_chips(SchematicRes, platform_info):
    """从器件描述明确标注 GPS/GNSS 的 Transceiver 查找其 LNA 路径。"""
    markers = ('GPS', 'GNSS', 'GLONASS', 'BEIDOU', 'GALILEO')
    paths = []
    seen = set()
    for ref, part_no in SchematicRes.part_infos.items():
        if not part_no or not part_no.startswith('312427'):
            continue
        identity = ' '.join((
            str(getattr(SchematicRes, 'part_infos_name', {}).get(ref, '')),
            str(getattr(SchematicRes, 'part_desc', {}).get(ref, '')),
        )).upper()
        if not any(marker in identity for marker in markers):
            continue
        for path in find_GPS_LNA(
                SchematicRes, ref, platform_info, require_gnss_network=False):
            key = tuple(path)
            if key not in seen:
                seen.add(key)
                paths.append(path)
    return paths


def find_GPS_ANT(SchematicRes, start_node, LGA_ref):
    """
    从 LNA 节点出发做 BFS，找到到达 GPS 天线网络（网络名含 ANT）的最短路径。
    只穿越被动件 / SAW / LNA，避免回到 Transceiver 或其他有源 RF IC 导致路径绕远或串到别的天线。
    返回路径格式仍为 [器件, 网络, 器件, ..., ANT网络]。
    """
    # 使用 signal_infos_raw：MAIN.py 的 filter_signal_infos 会删除 GPS_LNA_paths 涉及的网络，
    # 导致 BFS 找不到 C3302/FIL3301 等器件所在网络；signal_infos_raw 保留全量未过滤网络
    network_data = getattr(SchematicRes, 'signal_infos_raw', None) or SchematicRes.signal_infos
    # 构建网络字典：{网络名: (节点列表, 连接点列表)}
    network_dict = {item[0]: (item[1], item[2]) for item in network_data}

    # 构建节点到网络的映射：{节点: [所属网络列表]}
    node_to_networks = defaultdict(list)
    for net, (nodes, _) in network_dict.items():
        for node in nodes:
            node_to_networks[node].append(net)

    # 检查起始节点是否存在
    if start_node not in node_to_networks:
        return []

    def _can_traverse(node):
        """GPS 天线侧允许穿越的器件：被动件/SAW/LNA；连接器/Transceiver/开关等阻断。"""
        if node == start_node:
            return True
        if is_connector_ref(node) or node == LGA_ref:
            return False
        # 阻断回到 SDR/开关/PA 等有源器件，防止绕到别的天线
        part_no = SchematicRes.part_infos.get(node, '')
        if part_no.startswith(('312427', '312423', '312425', '312405')):
            return False
        # 允许被动件 / SAW / 未知的小型变压器等
        if node.startswith(('T', 'C', 'R', 'L', 'FIL', 'SAW', 'DIP')):
            return True
        return False

    paths = []
    visited_devs = {start_node}
    queue = deque([(start_node, [start_node])])

    while queue:
        cur_dev, path = queue.popleft()
        for net in node_to_networks[cur_dev]:
            if "ANT" in net:
                paths.append(path + [net])
                continue
            next_path = path + [net]
            for nb in network_dict[net][0]:
                if nb == cur_dev or nb in visited_devs:
                    continue
                if not _can_traverse(nb):
                    continue
                visited_devs.add(nb)
                queue.append((nb, next_path + [nb]))

    # 过滤掉含重复节点的异常路径（保险）
    return [p for p in paths if len(p) == len(set(p))]


def find_GNSS_ANT_from_LNAs(SchematicRes, LGA_ref):
    """为独立 GNSS 芯片架构查找 LNA 到具名 GNSS 天线的 RF 路径。"""
    paths = []
    seen = set()
    lna_refs = [
        ref for ref, part_no in SchematicRes.part_infos.items()
        if part_no and part_no.startswith('312406')
    ]
    for lna_ref in lna_refs:
        for path in find_GPS_ANT(SchematicRes, lna_ref, LGA_ref):
            antenna_net = str(path[-1]).upper()
            if 'GPS' not in antenna_net and 'GNSS' not in antenna_net:
                continue
            key = tuple(path)
            if key not in seen:
                seen.add(key)
                paths.append(path)
    return paths


# def removeGNDpart(signal_infos, gnd_inf):
#     signal_infos_without_gnd = []
#     for signal_info in signal_infos:
#         com1 = []
#         com2 = []
#         for i in range(len(signal_info[1])):
#             if signal_info[1][i].startswith(('T', 'C', 'R', 'L')) and not signal_info[1][i].startswith('LPF'):
#                 if signal_info[1][i] in gnd_inf[2]:
#                     continue
#             com1.append(signal_info[1][i])
#             com2.append(signal_info[2][i])
#         signal_info[1] = com1
#         signal_info[2] = com2
#     return signal_infos
def removeGNDpart(signal_infos, gnd_inf):
    """
    从信号网络中移除接地部件
    
    参数:
        signal_infos: 信号网络信息列表
        gnd_inf: 接地信息列表
        
    返回:
        移除接地部件后的信号网络信息
    """
    total_removed = 0  # 记录移除的接地部件总数
    
    for signal_info in signal_infos:
        net_name = signal_info[0]
        original_components = signal_info[1]
        original_pins = signal_info[2]
        
        removed_in_net = []  # 记录在此网络中移除的接地部件
        new_components = []
        new_pins = []
        
        # 遍历当前网络的所有组件
        for i in range(len(original_components)):
            comp = original_components[i]
            pin = original_pins[i]
            
            # 检查是否为接地部件
            if comp in gnd_inf[2] and _looks_like_matching_rlc(comp):
                removed_in_net.append(comp)
                total_removed += 1
            else:
                new_components.append(comp)
                new_pins.append(pin)
        
        # 更新网络组件信息
        signal_info[1] = new_components
        signal_info[2] = new_pins
        
        # 记录移除信息
        # if removed_in_net:
        #     print(f"{LogStyle.INFO}ℹ️ 从网络 {net_name} 移除接地部件: {', '.join(removed_in_net)}{LogStyle.ENDC}")
    
    print(f"{LogStyle.SUCCESS}✅ 移除接地部件完成: 共移除 {total_removed} 个接地部件{LogStyle.ENDC}")
    return signal_infos

def filter_signal_infos(signal_infos, networks_to_remove, all_flag=False):
    """
    从 signal_infos 中过滤掉 networks_to_remove 指定的网络。

    参数：
        signal_infos: 信号列表，每个信号的第一个元素是网络标识（如WiFi名）
        networks_to_remove: 要删除的网络字典，结构为 {类别: [网络1, 网络2,...]}

    返回：
        过滤后的信号列表
    """

    if all_flag:
        return [sig for sig in signal_infos if sig[0] not in [item for network_to_remove in networks_to_remove for item in network_to_remove]]
    else:
        # 1. 提取需要删除的网络标识（保持原始三层循环 + 步进2逻辑）
        networks_to_exclude = []
        for network_str in networks_to_remove:  # 明确命名字典的键值
            # 按步长2提取字符（跳过首尾，从第2字符开始每隔1个取1个）
            for i in range(1, len(network_str) - 1, 2):
                networks_to_exclude.append(network_str[i])

        # 2. 直接过滤（比逐个append更高效）
        return [sig for sig in signal_infos if sig[0] not in networks_to_exclude]

# 外部API
def group_paths_by_last_element(paths):
    """
    按照路径的最后一个元素进行分组

    参数:
        paths: 包含所有路径的列表，每个路径是一个子列表

    返回:
        dict: 键为路径最后一个元素，值为所有以该元素结尾的路径列表
    """
    grouped = defaultdict(list)
    for path in paths:
        if path:  # 确保路径非空
            last_element = path[-1]
            grouped[last_element].append(path)
    return dict(grouped)
