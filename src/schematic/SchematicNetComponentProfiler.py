import copy
from collections import defaultdict
import re
from app.LogTool import *
from schematic.net_class_50r import (
    decode_pads_text,
    Missing50RNetsError,
    extract_50r_nets_from_lines,
    require_50r_nets,
)
from rf.segments import _looks_like_matching_rlc
valid_prefixes = ('312706', '312701',
                  '312427',
                  '312423', '312702', '312799',
                  '312406', '312405',
                  '312703', "312425")

# 312706 SAWFBAR
# 312701 耦合器
# 312427 Transceiver
# 312423 电子开关
# 312702 分频器

# 312799 其它滤波器
# 312406 射频LNA
# 312405 射频PA
# 312703 多工器


def pads_pin_refdes(symbol: str) -> str:
    """PADS 连线符号 → 位号。J-C.R36 → J-C；FIL3006.2 → FIL3006。

    位号可以含 '-'（模组座 J-C）。封装名在器件头第二段，不要按 '-' 切断第一段。
    """
    s = str(symbol or "").strip()
    if not s or "@@@" in s:
        return ""
    return s.split()[0].split(".")[0]


def pads_component_refdes(header_line):
    """PADS 器件头第一段是位号。封装名在第二段，常含 '-'，不得拼进位号。"""
    token = str(header_line or "").split()
    if not token:
        return ""
    return pads_pin_refdes(token[0])


class SchematicAnalysisResult:
    """封装原理图分析结果的数据结构"""

    def determine_node_color(self, node_name: str) -> str:
        """按天线/网络/料号前缀给出路径图节点色。"""
        from refnum.graph import is_ant_path_node
        if is_ant_path_node(node_name):
            return '#ff7500'  # 橙色

        for signal_data in self.signal_infos:
            if signal_data[0] == node_name:
                return '#a4e2c6'  # 浅绿色

        material_code = self.part_infos.get(node_name, '')

        # 射频LNA (312406)
        if material_code.startswith('312406'):
            return '#ef5b9c'  # 粉红色

        # 射频PA (312405)
        elif material_code.startswith('312405'):
            return '#d71345'  # 红色

        # 射频模块 (312425)
        elif material_code.startswith('312425'):
            return '#b22c46'  # 暗红色

        # SAW滤波器 (312706)
        elif material_code.startswith('312706'):
            return '#8a2be2'  # 紫罗兰色

        # 分频器 (312702)
        elif material_code.startswith('312702'):
            return '#815fcc'  # 紫色

        # 多工器 (312703)
        elif material_code.startswith('312703'):
            return '#7d3f98'  # 深紫色

        # 耦合器 (312701)
        elif material_code.startswith('312701'):
            return '#ffa500'  # 橙色

        # 收发器 (312427)
        elif material_code.startswith('312427'):
            return '#20b2aa'  # 青绿色

        # 电子开关 (312423)
        elif material_code.startswith('312423'):
            return '#9370db'  # 中紫色

        # 其它滤波器 (312799)
        elif material_code.startswith('312799'):
            return '#da70d6'  # 兰花色

        return '#cccccc'  # 灰色
    def determine_node_color_light(self, node_name: str) -> str:
        """浅色版节点色：IC / 并联件共用浅蓝，其余粉。"""
        from refnum.graph import is_ant_path_node
        if is_ant_path_node(node_name):
            return '#D6E4Ff'

        for signal_data in self.signal_infos:
            if signal_data[0] == node_name:
                return '#a4e2c6'

        if self.is_ic_component(node_name) : return '#D6E4F0'
        return '#D6E4F0' if self.is_parallel(node_name) else '#F8C3CD'

    def get_common_network_components(self, refdes_list):
        """同网并联的其它 T/C/R/L 位号（不含 LPF），供矩阵插列。"""
        if len(refdes_list) < 2:
            print(f"{LogStyle.WARNING}⚠️ 需要至少两个位号作为输入{LogStyle.ENDC}")
            return []

        other_components = set()

        has_network = False
        for item in refdes_list:
            if any(item == net_info[0] for net_info in self.signal_infos_raw):
                has_network = True
                break

        if has_network:
            for net_name in refdes_list:
                if not any(net_name == net_info[0] for net_info in self.signal_infos_raw):
                    continue

                net_info = next(
                    (info for info in self.signal_infos_raw if info[0] == net_name), None)
                if net_info:
                    components = net_info[1]

                    for comp in components:
                        if comp not in refdes_list and _looks_like_matching_rlc(comp, self):
                            other_components.add(comp)
            # 按 ref 字符串稳定排序，消除 set 迭代对 PYTHONHASHSEED 的敏感性：
            # 同网并联器件插入矩阵的列序必须与进程无关，否则列位会随哈希种子漂移。
            return sorted(other_components)

        for net_info in self.signal_infos_raw:
            net_name = net_info[0]
            components = net_info[1]

            if set(refdes_list).issubset(set(components)):
                for comp in components:
                    if comp not in refdes_list and _looks_like_matching_rlc(comp, self):
                            other_components.add(comp)

        if not other_components:
            refdes_networks = defaultdict(list)
            for net_info in self.signal_infos_raw:
                net_name = net_info[0]
                components = net_info[1]

                for refdes in refdes_list:
                    if refdes in components:
                        refdes_networks[refdes].append(net_name)

            shared_networks = defaultdict(list)
            for refdes, networks in refdes_networks.items():
                for network in networks:
                    shared_networks[network].append(refdes)

            for network, refdeses in shared_networks.items():
                if len(refdeses) >= 2:
                    net_info = next(
                        (info for info in self.signal_infos_raw if info[0] == network), None)
                    if net_info:
                        components = net_info[1]
                        for comp in components:
                            if comp not in refdes_list and _looks_like_matching_rlc(comp, self):
                                    other_components.add(comp)

        # 稳定排序，消除哈希种子对并联器件插入顺序的影响。
        return sorted(other_components)

    prefix_types = {
        '312706': 'SAWFBAR',
        '312701': '耦合器',
        '312427': 'Transceiver',
        '312423': '电子开关',
        '312702': 'Frequency Divider',
        '312799': '其它滤波器',
        '312406': '射频LNA',
        '312405': '射频PA',
        '312703': '多工器',
    }

    def __init__(self, signal_infos, gnd_info, part_infos, part_infos_name, part_active, part_desc=None, part_mfr=None, part_value=None, part_type_token=None):
        self.signal_infos = signal_infos
        self.signal_infos_raw = signal_infos
        self.gnd_inf = gnd_info      # 参数名必须与传入一致
        self.part_infos = part_infos
        self.part_infos_name = part_infos_name
        self.part_active = part_active
        self.part_desc = part_desc or {}   # 器件 Description 文本，用于区分射频/非射频开关
        self.part_mfr = part_mfr or {}     # 器件 Manufacturer 文本，用于判断收发器厂商（MTK 平台）
        self.part_value = part_value or {}  # "Value" 栏，如 47nH / @4.7pF
        self.part_type_token = part_type_token or {}  # 位号行第二段，如 L_RF_47N_100M

    def analyze_electronic_switches(self, visited, reverse_edges):
        """按反向子树收集电子开关的引脚、同网器件与相关边。"""
        electronic_switches = {}
        for node in visited:
            if self.is_electronic_switch(node):
                part_no = self.part_infos.get(node, "")
                part_name = self.get_part_name(node)
                electronic_switches[node] = {
                    "part_no": part_no,
                    "part_name": part_name,
                    "connections": [],
                    "reverse_edges": []
                }

        if not electronic_switches:
            return {}


        for edge in reverse_edges:
            dst, src = edge
            if dst in electronic_switches or src in electronic_switches:
                switch_node = dst if dst in electronic_switches else src

                electronic_switches[switch_node]["reverse_edges"].append(edge)

        for net_info in self.signal_infos_raw:
            if len(net_info) < 3:
                continue

            net_name = net_info[0]
            components = net_info[1]
            pins = net_info[2]

            for i, comp in enumerate(components):
                if comp in electronic_switches:
                    pin = pins[i] if i < len(pins) else "Unknown"

                    connected_components = []
                    for j, other_comp in enumerate(components):
                        if j != i:
                            other_pin = pins[j] if j < len(pins) else "Unknown"
                            connected_components.append({
                                "component": other_comp,
                                "pin": other_pin,
                                "part_no": self.part_infos.get(other_comp, ""),
                                "part_name": self.get_part_name(other_comp)
                            })

                    connection = {
                        "pin": pin,
                        "network": net_name,
                        "connected_components": connected_components
                    }
                    electronic_switches[comp]["connections"].append(connection)

        return electronic_switches

    def get_ic_components(self):
        """IC 类器件（排除匹配电阻电容电感），按 6 位料号前缀归类。"""
        ic_components = {}

        for comp, part_no in self.part_infos.items():
            if _looks_like_matching_rlc(comp, self):
                continue

            if not part_no or len(part_no) < 6:
                continue

            prefix = part_no[:6]
            if prefix in self.valid_prefixes:
                ic_type = self.prefix_types.get(prefix, "未知IC")

                ic_components[comp] = {
                    "part_no": part_no,
                    "type": ic_type,
                    "prefix": prefix
                }

        return ic_components

    def copy(self):
        """深拷贝分析结果。signal_infos_raw 必须单独拷，不能跟已过滤 signal_infos 混。"""
        _new = SchematicAnalysisResult(
            signal_infos=copy.deepcopy(self.signal_infos),
            gnd_info=copy.deepcopy(self.gnd_inf),
            part_infos=copy.deepcopy(self.part_infos),
            part_active=copy.deepcopy(self.part_active),
            part_infos_name=copy.deepcopy(self.part_infos_name),
            part_desc=copy.deepcopy(self.part_desc),
            part_mfr=copy.deepcopy(self.part_mfr),
            part_value=copy.deepcopy(getattr(self, "part_value", {}) or {}),
            part_type_token=copy.deepcopy(getattr(self, "part_type_token", {}) or {}),
        )
        # 保留「过滤前」的原始网络（含 NC/不贴器件）；否则 __init__ 会把 signal_infos_raw
        # 设成已过滤的 signal_infos，NC 器件信息丢失。功能3 据此把 NC 器件按原拓扑摆出并 Deactivate。
        _new.signal_infos_raw = copy.deepcopy(self.signal_infos_raw)
        _new.all_signal_infos_raw = copy.deepcopy(
            getattr(self, "all_signal_infos_raw", self.signal_infos_raw)
        )
        _new.signal_net_fragments = copy.deepcopy(
            getattr(self, "signal_net_fragments", ())
        )
        _new.all_signal_net_fragments = copy.deepcopy(
            getattr(self, "all_signal_net_fragments",
                    getattr(self, "signal_net_fragments", ()))
        )
        _new.net_class_50r_nets = tuple(
            getattr(self, "net_class_50r_nets", ()) or ()
        )
        return _new

    def is_frequency_divider(self, node_id: str) -> bool:
        """料号前缀 312702 = 分频器。"""
        part_code = self.part_infos.get(node_id, '')
        return part_code.startswith('312702')

    def is_electronic_switch(self, node_id: str) -> bool:
        """料号前缀 312423 = 电子开关。"""
        part_code = self.part_infos.get(node_id, '')
        return part_code.startswith('312423')

    def is_SAWFBAR(self, node_id: str) -> bool:
        """料号前缀 312706 = SAW 滤波器。"""
        part_code = self.part_infos.get(node_id, '')
        return part_code.startswith('312706')
    def is_network(self, node_id: str) -> bool:
        """判断节点身份是否为网络名（而非器件位号）。

        必须查 raw/全量网表，不能只查当前 signal_infos：
        signal_infos 在 process_schematic 后只保留「激活 + 多端」50R 网，
        DNP/不贴支路上的 $$$ 网仍会出现在 GPS 等路径图中。若此处漏判，
        refnum 拓扑会把网络当成器件，抛 invalid device/network position。
        """
        node = str(node_id)
        for source in (
            self.signal_infos,
            getattr(self, "signal_infos_raw", None) or (),
            getattr(self, "all_signal_infos_raw", None) or (),
        ):
            for items in source:
                if items and str(items[0]) == node:
                    return True
        # Cadence 匿名网络前缀：路径图里可能仍带 $$$，但过滤后已不在表中
        if node.startswith("$$$"):
            return True
        return False

    def is_parallel(self, node):
        if node in self.gnd_inf[2]:
            return True
        return False

    def is_ic_component(self, comp):
        if self.is_network(comp):
            return False
        return not _looks_like_matching_rlc(comp, self)

    def get_part_name(self, node_ref):
        return self.part_infos_name.get(node_ref, "")

    def get_components_for_net(self, net_name: str) -> list:
        """网上的匹配电阻电容电感。同时查 raw：NC/不贴匹配件仍在 raw，反向补图与天线 stub 要看见。"""
        sources = [self.signal_infos]
        raw = getattr(self, "signal_infos_raw", None)
        if raw and raw is not self.signal_infos:
            sources.append(raw)

        filtered_components = []
        seen = set()
        for source in sources:
            net_info = next(
                (info for info in (source or []) if info and info[0] == net_name),
                None,
            )
            if not net_info:
                continue
            components = net_info[1] if len(net_info) > 1 else []
            for comp in components:
                if not comp or comp in seen:
                    continue
                if _looks_like_matching_rlc(comp, self):
                    seen.add(comp)
                    filtered_components.append(comp)

        return filtered_components

    def get_part_type(self, node_id: str) -> str:
        """料号只取前 6 位匹配器件类型；不足 6 位或未登记则 Unknown Device。"""
        part_code = self.part_infos.get(node_id, '')

        code_to_type = {
            '312706': 'SAW Filter',         # SAW滤波器 (SAWFBAR)
            '312701': 'Coupler',            # 耦合器
            '312423': 'Electronic Switch',  # 电子开关
            '312702': 'Frequency Divider',  # 分频器
            '312799': 'Other Filter',       # 其他滤波器
            '312703': 'Multiplexer',        # 多工器
            '312406': 'RF LNA',             # 射频LNA
            '312405': 'RF PA',              # 射频PA
            '312427': 'Transceiver',        # 收发器
            '312425': 'LPAMiF'              # 射频前端模块
        }

        if len(part_code) >= 6:
            core_code = part_code[:6]
            if core_code in code_to_type:
                return code_to_type[core_code]

        return 'Unknown Device'

    def remove_nodes_from_networks(self, networks_to_remove):
        """路径偶索引为器件、奇索引为网络；把路径上的器件从网表摘掉。

        两端都是待摘被动件的网整条删除，避免留下空匹配网。
        """
        nodes_to_remove = set()
        if not networks_to_remove:
            return

        anchors = set()
        for path in networks_to_remove:
            if path:
                anchors.add(path[-1])
            for i in range(1, len(path), 2):
                if i - 1 >= 0:
                    left_node = path[i - 1]
                    nodes_to_remove.add(left_node)
                if i + 1 < len(path):
                    right_node = path[i + 1]
                    nodes_to_remove.add(right_node)
        # 路径终点（PA/LNA）自己网上的匹配还要给下一次 PA→天线搜索当出口。
        # 摘掉的话，功放和后面的开关/耦合器断开，只剩耦合器抽头能串进去。
        for entry in self.signal_infos:
            comps = entry[1] if len(entry) > 1 else []
            if anchors.intersection(comps or []):
                nodes_to_remove.difference_update(comps or [])

        updated_signal_infos = []
        removed_count = 0
        removed_networks = 0

        for entry in self.signal_infos:
            net_name = entry[0]
            components = entry[1]
            pins = entry[2]

            new_components = []
            new_pins = []
            removed_in_net = []

            if len(components) == 2:
                comp1, comp2 = components
                pin1, pin2 = pins

                comp1_passive = _looks_like_matching_rlc(comp1, self)
                comp2_passive = _looks_like_matching_rlc(comp2, self)

                comp1_in_list = comp1 in nodes_to_remove
                comp2_in_list = comp2 in nodes_to_remove

                if comp1_passive and comp2_passive and comp1_in_list and comp2_in_list:
                    removed_in_net.extend([comp1, comp2])
                    removed_count += 2
                    removed_networks += 1
                    continue

            for comp, pin in zip(components, pins):
                if comp in nodes_to_remove and _looks_like_matching_rlc(comp, self):
                    removed_in_net.append(comp)
                    removed_count += 1
                else:
                    new_components.append(comp)
                    new_pins.append(pin)

            if new_components:
                updated_signal_infos.append(
                    [net_name, new_components, new_pins])
            else:
                removed_networks += 1

        self.signal_infos = updated_signal_infos

        if removed_count or removed_networks:
            print(
                f"{LogStyle.SUCCESS}✅ 节点移除完成: 移除 {removed_count} 个节点，"
                f"{removed_networks} 条网络{LogStyle.ENDC}"
            )

    def filter_inactive_components_with_log(self):
        """按 part_active 丢掉 NC/不贴器件；字典里没有的位号默认激活。"""
        filtered_list = []

        for net_info in self.signal_infos:
            if len(net_info) < 3 or not isinstance(net_info[1], list):
                filtered_list.append(net_info)
                continue

            net_name, components, pins = net_info

            new_components = []
            new_pins = []

            for comp, pin in zip(components, pins):
                if comp in self.part_active:
                    if self.part_active[comp]:
                        new_components.append(comp)
                        new_pins.append(pin)
                else:
                    new_components.append(comp)
                    new_pins.append(pin)

            if new_components:
                filtered_list.append([net_name, new_components, new_pins])

        self.signal_infos = filtered_list

    def classify_networks_by_connection_count(self):
        """按连接数分多端/单端/孤立。网名含 ANT 即使不足两端也当多端（天线口）。"""
        multi_ended = []
        single_ended = []
        isolated = []

        for net_info in self.signal_infos:
            if not self._is_valid_net_info(net_info):
                continue

            net_name, components, _ = net_info

            active_components = [comp for comp in components if comp]
            connection_count = len(active_components)

            if connection_count >= 2 or "ANT" in net_name:
                multi_ended.append(net_info)
            elif connection_count == 1  :
                single_ended.append(net_info)
            else:
                isolated.append(net_info)
        self.signal_infos = multi_ended
        return multi_ended, single_ended, isolated

    def _is_valid_net_info(self, net_info):
        return (isinstance(net_info, list) and
                len(net_info) >= 3 and
                isinstance(net_info[1], list))

    def get_s_parameter_required_components(self):
        s_param_components = {}
        # 312706 SAWFBAR
        # 312701 耦合器
        # 312423 电子开关
        # 312702 分频器

        # 312799 其它滤波器
        # 312703 多工器
        s_param_prefixes = ('312706', '312701', '312423', '312702',
                            '312799', '312703')

        for comp, part_no in self.part_infos.items():
            if not part_no or len(part_no) < 6:
                continue
            if self.part_active[comp]:
                prefix = part_no[:6]
                if prefix not in s_param_prefixes:
                    continue

                # 电子开关类(312423)中，按描述剔除 USB/模拟开关等非射频器件——
                # 它们与真射频开关共用同一物料前缀，但不在射频通路上、库中无 S 参数
                if prefix == '312423':
                    desc = (self.part_desc.get(comp, '') or '').upper()
                    if ('USB' in desc or 'ANALOG SWITCH' in desc) and 'RF SWITCH' not in desc:
                        print(f"{LogStyle.INFO}ℹ️ {comp}（{self.part_infos_name.get(comp, '')}）为非射频开关（USB/模拟），跳过 S 参数{LogStyle.ENDC}")
                        continue

                desc = self.part_desc.get(comp, "") or ""
                if part_no not in s_param_components:
                    s_param_components[part_no] = {
                        "part_no": part_no,
                        "type": self.prefix_types.get(prefix, "未知射频器件"),
                        "prefix": prefix,
                        "external_model": self.part_infos_name[comp],
                        "components": [comp],
                        "part_desc": desc,
                    }
                else:
                    s_param_components[part_no]["components"].append(comp)
                    prev = s_param_components[part_no].get("part_desc") or ""
                    if desc and desc not in prev:
                        s_param_components[part_no]["part_desc"] = (prev + " " + desc).strip()
        return s_param_components


class SchematicNetComponentProfiler:
    """原理图网络与器件特征分析引擎"""
    @staticmethod
    def process_schematic(file_path):
        """解析 PADS Logic ASCII：50R 网、器件料号、走线 fragment。"""
        def read_schematic_file(file_path):
            """读取原理图。换行折叠与编码回退见 decode_pads_text（GBK/UTF-8/CP1250/latin-1）。"""
            try:
                with open(file_path, "rb") as fh:
                    raw = fh.read()
            except FileNotFoundError:
                print(
                    f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 文件 {file_path} 未找到{LogStyle.ENDC}")
                return []
            except Exception as e:
                print(
                    f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 读取文件时发生错误: {e}{LogStyle.ENDC}")
                return []

            text = decode_pads_text(raw)
            content = [ln + "\n" for ln in text.split("\n")]
            if content and content[-1] == "\n":
                content.pop()
            return content

        def split_sections_by_empty_line(content_lines):
            sections, current_section = [], []
            for line in content_lines:
                stripped_line = line.strip()
                if stripped_line:
                    current_section.append(line)
                elif current_section:
                    sections.append(current_section)
                    current_section = []
            if current_section:
                sections.append(current_section)

            return sections

        def filter_signal_sections(sections):
            filtered = [section for section in sections if any(
                '*SIGNAL*' in line for line in section)]
            return filtered

        def filter_part_sections(sections):
            filtered = [section for section in sections if any(
                'part name' in line.lower() for line in section)]
            return filtered

        def extract_signal_data(signal_section):
            """从信号部分提取 NET_NAME、组件，以及按走线连通分量拆开的局部 fragment。

            同名 *SIGNAL* 在原理图上常画成多段走线（电气同网、几何不连通）。
            每段只含局部贯通件与其旁路匹配；fragment 必须按走线连通分量拆分，
            否则同网多旁路会失去“贴靠哪颗贯通件”的事实，refnum 左右顺序会反。
            """
            net_name = ""
            components = []
            components2 = []
            adj: dict[str, set[str]] = defaultdict(set)

            match = re.match(r'\*SIGNAL\* ([^\s]+)', signal_section[0])
            if match:
                net_name = match.group(1)

            def _endpoint_key(symbol: str) -> str:
                if "@@@" in symbol:
                    return symbol
                return pads_pin_refdes(symbol)

            for line in signal_section[1:]:
                if "50R" in line:
                    continue
                parts = line.split()
                if len(parts) > 2:
                    left, right = parts[0], parts[1]
                    ka, kb = _endpoint_key(left), _endpoint_key(right)
                    if ka and kb and ka != kb:
                        adj[ka].add(kb)
                        adj[kb].add(ka)
                    for symbol in (left, right):
                        if "@@@" not in symbol:
                            comp = pads_pin_refdes(symbol)
                            if not comp:
                                continue
                            components.append(comp)
                            components2.append(symbol)

            # 走线连通分量 → 仅含器件位号的局部 fragment
            seen_nodes: set[str] = set()
            wire_fragments: list[tuple[str, ...]] = []
            for start in list(adj.keys()):
                if start in seen_nodes:
                    continue
                stack = [start]
                seen_nodes.add(start)
                comp_devices: list[str] = []
                while stack:
                    cur = stack.pop()
                    if "@@@" not in cur:
                        if cur not in comp_devices:
                            comp_devices.append(cur)
                    for nb in adj.get(cur, ()):
                        if nb not in seen_nodes:
                            seen_nodes.add(nb)
                            stack.append(nb)
                if comp_devices:
                    wire_fragments.append(tuple(dict.fromkeys(comp_devices)))

            in_frag = {dev for frag in wire_fragments for dev in frag}
            for comp in components:
                if comp not in in_frag:
                    wire_fragments.append((comp,))
                    in_frag.add(comp)
            if not wire_fragments and components:
                wire_fragments = [tuple(dict.fromkeys(components))]

            return net_name, components, components2, wire_fragments

        def merge_signal_data(signal_data):
            merged_data = defaultdict(lambda: [set(), set()])

            for net_name, components, components2 in signal_data:
                key = net_name
                merged_data[key][0].update(components)
                merged_data[key][1].update(components2)

            result = [[key, sorted(value[0]), sorted(value[1])]
                      for key, value in merged_data.items()]
            return result

        def filter_alpha_numeric(items):
            pattern = re.compile(r'^[A-Za-z]+\d+$')
            filtered = [item for item in items if pattern.match(item)]
            return filtered

        print(f"{LogStyle.NOTICE}📝 开始处理原理图: {file_path}{LogStyle.ENDC}")

        schematic_content = read_schematic_file(file_path)
        if not schematic_content:
            print(
                f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 无法读取文件，处理终止{LogStyle.ENDC}")
            return None

        # 没有 NET_CLASS 50R 则立刻中止，不建空图
        net_list_50r = extract_50r_nets_from_lines(schematic_content)
        try:
            require_50r_nets(net_list_50r)
        except Missing50RNetsError:
            print(
                f"{LogStyle.ERROR}❌ 原理图没有 NET_CLASS 50R（或该类下没有网络），已中止{LogStyle.ENDC}")
            raise
        print(
            f"{LogStyle.SUCCESS}✅ 成功提取50R网络类别，包含 {len(net_list_50r)} 个网络{LogStyle.ENDC}")
        net_class_50r = {"net_class_name": "50R", "nets": net_list_50r}

        schematic_sections = split_sections_by_empty_line(schematic_content)

        signal_sections = filter_signal_sections(schematic_sections)

        part_sections = filter_part_sections(schematic_sections)

        gnd_inf = ['GND', 'GND', []]
        signal_info_list = []
        all_signal_info_list = []

        signal_wire_fragments: list[tuple[str, tuple[str, ...]]] = []
        all_signal_wire_fragments: list[tuple[str, tuple[str, ...]]] = []
        for section in signal_sections:
            net_name, components, components2, wire_fragments = extract_signal_data(section)
            if net_name and net_name != 'GND':
                all_signal_info_list.append([net_name, components, components2])
                for frag in wire_fragments:
                    if frag:
                        all_signal_wire_fragments.append((str(net_name), frag))
            # SRS 仍不进主图 50R signal_infos（次收独立语义）。
            # FBRX：Coupler→TRX 取样支路，进主图以便 ANT 构图（与普通链路同等）。
            if "_SRS" in net_name:
                continue
            elif 'GND' == net_name:
                gnd_components = filter_alpha_numeric(components)
                gnd_inf[2] += gnd_components
            elif net_name in net_class_50r["nets"]:
                signal_info_list.append([net_name, components, components2])
                for frag in wire_fragments:
                    if frag:
                        signal_wire_fragments.append((str(net_name), frag))

        gnd_inf[2] = list(dict.fromkeys(gnd_inf[2]))

        part_infos = {}
        part_infos_name = {}
        part_active = {}
        part_desc = {}
        part_mfr = {}
        part_value = {}
        part_type_token = {}

        for part_info in part_sections:
            key = pads_component_refdes(part_info[0])
            if not key:
                continue
            part_infos[key] = ""
            part_infos_name[key] = ""
            part_desc[key] = ""
            part_mfr[key] = ""
            part_value[key] = ""
            toks = str(part_info[0] or "").split()
            part_type_token[key] = toks[1] if len(toks) > 1 else ""

            for info in part_info:
                if 'Value' in info:
                    if '@' in info or "NC" in info:
                        part_active[key] = False
                        break
                    else:
                        part_active[key] = True
                else:
                    part_active[key] = True

            part_number_found = False
            for info in part_info:
                if "part number" in info.lower():
                    cleaned_info = ''.join(
                        re.sub(r'"[^"]*"', '', info).split())
                    if cleaned_info != "":
                        if not cleaned_info.isdigit():
                            if cleaned_info.lower().replace(" ", "") not in (
                                "partnumber",
                                "part_number",
                            ):
                                print(
                                    f"{LogStyle.WARNING}⚠️ 检测到非纯数字部件号: '{cleaned_info}' (器件: {key}){LogStyle.ENDC}")
                            continue

                        if len(cleaned_info) < 6:
                            print(
                                f"{LogStyle.WARNING}⚠️ 部件号过短: '{cleaned_info}' (器件: {key}){LogStyle.ENDC}")
                            continue
                    part_infos[key] = cleaned_info
                    part_number_found = True
                    break

            for info in part_info:
                if "\"PART NAME\"" in info or "\"Part Name\"" in info:
                    temp = ''.join(
                        re.sub(r'"[^"]*"', '', info.upper()).split())
                    if "part" not in temp.lower() and temp != "":
                        part_infos_name[key] = temp
                        break

            for info in part_info:
                if '"Value"' in info:
                    part_value[key] = info.split('"Value"', 1)[1].strip()
                    break

            # Description：区分射频开关 vs USB/模拟开关
            for info in part_info:
                if "\"Description\"" in info:
                    part_desc[key] = info.split("\"Description\"", 1)[1].strip()
                    break

            # Manufacturer：收发器是否联发科 → MTK 平台
            for info in part_info:
                if "\"Manufacturer\"" in info:
                    part_mfr[key] = ''.join(
                        re.sub(r'"[^"]*"', '', info).split())
                    break

        # 合并前保留 *SIGNAL* 走线连通 fragment：同名网络常拆成多段走线，
        # 每段只含局部贯通件与其旁路匹配。合并后 set/sorted 会丢掉
        # “旁路件贴靠哪颗贯通件”的事实，导致 refnum 同网附件左右反序。
        # 同一 *SIGNAL* 区块内若走线不连通，也按连通分量拆成多条 fragment。
        signal_net_fragments = list(signal_wire_fragments)
        all_signal_net_fragments = list(all_signal_wire_fragments)

        signal_info_list = merge_signal_data(signal_info_list)
        all_signal_info_list = merge_signal_data(all_signal_info_list)

        res = SchematicAnalysisResult(
            signal_infos=signal_info_list,
            gnd_info=gnd_inf,
            part_infos=part_infos,
            part_infos_name=part_infos_name,
            part_active=part_active,
            part_desc=part_desc,
            part_mfr=part_mfr,
            part_value=part_value,
            part_type_token=part_type_token,
        )
        # 功能主图仍只使用已确认的 50R 网络。all_signal_infos_raw 仍保留含 SRS
        # 的全量非 GND 信号，供其它分析复用；功能5 路径构建不再消费 SRS 扩展。
        res.all_signal_infos_raw = all_signal_info_list
        res.signal_net_fragments = signal_net_fragments
        res.all_signal_net_fragments = all_signal_net_fragments
        # TXT NET_CLASS 50R 网名清单（供 50R 覆盖/others 全量收器件；
        # 网名带 FBRX/SRS 但仍落在 50R 类里的也要收）
        res.net_class_50r_nets = tuple(
            str(n) for n in (net_class_50r.get("nets") or []) if n
        )

        res.filter_inactive_components_with_log()

        multi, single, isolated = res.classify_networks_by_connection_count()
        print(f"{LogStyle.SUCCESS}✅ 网络分类完成: 多端={len(multi)}, 单端={len(single)}, 孤立={len(isolated)}{LogStyle.ENDC}")

        return res

    @staticmethod
    def removeBBcomponents(signal_infos, part_infos, RF_chip_ref, BB_chip_ref, LGA_res):
        """移除基带相关网络，保留射频路径可用网表。

        硬性保留：
          - 天线口网 ANTn（n 为数字）。天线上常见无料号天线开关/连接器
            （天线开关/连接器无料号）会使 validate 失败；若整网丢弃，
            find_PA_LNA_ANT 无终点 → T_PA_LNA_ANT 空 → rf_graph 0 节点
            （BB 移除后全部 ANT 网消失）。
          - 含 RF Transceiver 的网
        仍丢弃：含 BB 芯片的网；其余按器件料号前缀校验。
        """
        valid_prefixes = ('312706', '312701', '312427', '312423', "312425",
                          '312702', '312799', '312406', '312405', '312703')
        filtered_signals = []
        removed = 0
        kept_ant = 0

        def _is_antenna_port_net(net_name: str) -> bool:
            try:
                from refnum.graph import is_ant_path_node
                return is_ant_path_node(net_name)
            except Exception:
                s = str(net_name or "").upper()
                return bool(
                    s.startswith("ANT") and len(s) > 3 and s[3:].isdigit()
                    and not s.startswith("ANTCTL")
                )

        def validate_components(components):
            for component in components:
                if not component.startswith(('T', 'C', 'R', 'L', "J")) or component.startswith('LPF'):
                    if not (part_infos.get(component, '').startswith(valid_prefixes) or LGA_res == component):
                        return False
            return True

        for signal in signal_infos:
            if not signal:
                continue
            comps = signal[1] if len(signal) > 1 else []
            if BB_chip_ref and BB_chip_ref in comps:
                removed += 1
                continue

            # 天线口网：终点必须在网表中，禁止因无料号开关被当基带丢掉
            if _is_antenna_port_net(signal[0]):
                filtered_signals.append(signal)
                kept_ant += 1
                continue

            if validate_components(comps) or (RF_chip_ref and RF_chip_ref in comps):
                filtered_signals.append(signal)
            else:
                removed += 1

        print(
            f"{LogStyle.SUCCESS}✅ 基带组件移除完成: 保留 {len(filtered_signals)}，"
            f"移除 {removed}，其中天线口网保留 {kept_ant}{LogStyle.ENDC}")
        return filtered_signals


class ComponentPINParser:
    """从 PADS 原理图解析器件 GATE 引脚数与引脚语义名。编码与正文同一套 decode_pads_text。"""

    def __init__(self, file_path=None, encoding='gbk'):
        self.file_path = file_path
        self.encoding = encoding
        self.doc_content = None
        self.components = {}  # {器件名: 引脚数}
        self.component_pin_labels = {}

        if file_path:
            self.load_file(file_path)

    def _handle_file_error(self, file_path, exception):
        print(
            f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 文件读取错误: {file_path}{LogStyle.ENDC}")
        print(f"{LogStyle.ERROR}错误详情: {exception}{LogStyle.ENDC}")
        self.doc_content = None
        self.file_path = None
        self.encoding = None
        self.components = {}
        self.component_pin_labels = {}

    def load_file(self, file_path, encoding='gbk'):
        """按 PADS 编码回退读全文（GBK / UTF-8 / CP1250 / latin-1）。"""
        try:
            with open(file_path, "rb") as fh:
                raw = fh.read()
            self.doc_content = decode_pads_text(raw)
            self.encoding = encoding or "gbk"
            self.file_path = file_path
            self.components = {}
            self.component_pin_labels = {}
        except Exception as e:
            self._handle_file_error(file_path, e)

    def load_content(self, content):
        self.doc_content = content
        self.file_path = None
        self.components = {}
        self.component_pin_labels = {}

    def get_pin_count(self, component_name):
        """按型号全名或后缀匹配 GATE 行第 3 字段为引脚数。"""
        if component_name in self.components:
            pin_count = self.components[component_name]
            if pin_count is not None:
                return pin_count

        if self.doc_content is None:
            if self.file_path:
                self.load_file(self.file_path, self.encoding)
            else:
                print(
                    f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 错误: 未加载原理图内容{LogStyle.ENDC}")
                return None

        lines = self.doc_content.split('\n')
        found_component = None

        normalized_name = component_name.upper().replace('-', '_')
        name_variants = {
            normalized_name,
            normalized_name.replace('IC_', ''),
            normalized_name.replace('IC_SW', ''),
            normalized_name.replace('FILTER_', ''),
            normalized_name.replace('L_', ''),
            normalized_name.replace('C_', ''),
            normalized_name.replace('R_', '')
        }

        for i, line in enumerate(lines):
            line = line.strip()
            if component_name in line:
                parts = line.split()
                if not parts:
                    continue

                comp_full_name = parts[0]
                comp_base_name = comp_full_name.split('_')[-1]
                comp_full_name=comp_full_name.upper().replace('-', '_')
                comp_base_name=comp_base_name.upper().replace('-', '_')
                if (normalized_name == comp_full_name or
                    comp_full_name.endswith(normalized_name) or
                    normalized_name == comp_base_name or
                        comp_base_name in name_variants):

                    found_component = comp_full_name

                    # GATE 通常在器件名后，向后最多 20 行
                    for j in range(i+1, min(i+20, len(lines))):
                        gate_line = lines[j].strip()
                        if gate_line.startswith('GATE'):
                            gate_parts = gate_line.split()
                            if len(gate_parts) >= 3:
                                try:
                                    pin_count = int(gate_parts[2])
                                    self.components[component_name] = pin_count
                                    return pin_count
                                except (ValueError, IndexError):
                                    continue

                    # GATE 也可能写在器件名之前，向前最多 10 行
                    for j in range(max(0, i-10), i):
                        gate_line = lines[j].strip()
                        if gate_line.startswith('GATE'):
                            gate_parts = gate_line.split()
                            if len(gate_parts) >= 3:
                                try:
                                    pin_count = int(gate_parts[2])
                                    self.components[component_name] = pin_count
                                    return pin_count
                                except (ValueError, IndexError):
                                    continue

        self.components[component_name] = None
        return None

    def get_pin_labels(self, component_name):
        """PADS 器件类型：物理脚号 → 语义名。"""
        if component_name in self.component_pin_labels:
            return dict(self.component_pin_labels[component_name])
        if self.doc_content is None:
            return {}

        wanted = str(component_name or "").upper().replace("-", "_")
        lines = self.doc_content.splitlines()
        for index, raw_line in enumerate(lines):
            normalized = raw_line.upper().replace("-", "_")
            if wanted not in normalized or " UND " not in f" {normalized} ":
                continue
            labels = {}
            gate_seen = False
            pin_rows_started = False
            for candidate in lines[index + 1:index + 160]:
                stripped = candidate.strip()
                if not stripped:
                    if pin_rows_started:
                        break
                    continue
                if stripped.startswith("GATE "):
                    gate_seen = True
                    continue
                if not gate_seen:
                    continue
                parts = stripped.split()
                if (
                    len(parts) >= 4
                    and re.fullmatch(r"[A-Za-z]*\d+", parts[0])
                    and parts[1].isdigit()
                ):
                    labels[parts[0]] = parts[-1]
                    pin_rows_started = True
                elif pin_rows_started:
                    break
            if labels:
                self.component_pin_labels[component_name] = labels
                return dict(labels)
        self.component_pin_labels[component_name] = {}
        return {}


class ComponentElectronicSwitch:
    """电子开关脚位图。mapping 形如 ``源脚,...->目标脚,...``，源与目标双向互通。"""

    def __init__(self, switch_type, pin_count, mapping_str):
        self.switch_type = switch_type.upper()
        self.pin_count = pin_count
        self.connections = {}
        self._parse_mapping(mapping_str)

    def _parse_mapping(self, mapping_str):
        if "->" not in mapping_str:
            raise ValueError(
                "Invalid mapping format, must use '->' to separate sources and targets")

        sources_str, targets_str = mapping_str.split("->")
        sources = [int(pin.strip())
                   for pin in sources_str.split(",") if pin.strip()]
        targets = [int(pin.strip())
                   for pin in targets_str.split(",") if pin.strip()]

        all_pins = sources + targets
        if any(pin < 1 or pin > self.pin_count for pin in all_pins):
            raise ValueError(f"Pin numbers must be between 1-{self.pin_count}")

        # 源脚全部连到目标脚，目标脚反向连回源脚
        for source in sources:
            self.connections[source] = set(targets)

        for target in targets:
            if target not in self.connections:
                self.connections[target] = set()
            self.connections[target].update(sources)

    def get_connected_pins(self, pin):
        pin = int(pin)
        if pin < 1 or pin > self.pin_count:
            raise ValueError(f"Pin number must be between 1-{self.pin_count}")
        return self.connections.get(pin, set())

    def __repr__(self):
        connections = []
        for pin in sorted(self.connections):
            if self.connections[pin]:
                connected = sorted(self.connections[pin])
                connections.append(f"{pin}↔{connected}")
        return f"Component:{self.switch_type} Switch({self.pin_count}pins): {'; '.join(connections)}"


def explain_divider_parse_error(node, err) -> str:
    """分频器口名对不上时给人看的原因。"""
    code = str(err or "")
    if "no_band" in code or "No band pins" in code:
        why = "S参数脚名里没有 HB/LB/UHB/MB 这种频段口，不能按分频器拆口"
    elif "no_com" in code or "No COM/ANT" in code:
        why = "S参数脚名里没有 COM 或 ANT 公共口，不能按分频器拆口"
    elif "multi_com" in code or "Multiple COM" in code:
        why = "S参数里出现多个 COM/ANT 口，不能按分频器拆口"
    elif "no_file" in code or "Failed to parse" in code:
        why = "S参数文件读不出来，不能按分频器拆口"
    else:
        why = "S参数口名对不上分频器（要有公共口 COM/ANT，和频段口 HB/LB 等）"
    return (
        f"[分频器口名] {node}：{why}。"
        f"通路仍按原理图走。"
    )


class ComponentDivider:
    """分频器脚位：一个 COM/ANT 桥接全部频段脚；频段脚彼此不通。"""

    def __init__(self, snp_info, current_node):
        self.device_type = "Divider"
        self.snp_info = snp_info
        if not self.snp_info.state:
            raise ValueError("no_file")

        self.com_pin = None
        self.band_pins = {}
        self.pin_roles = {}
        self.connections = {}

        self._analyze_divider()
        self._build_connections()

    def _analyze_divider(self):
        for port in self.snp_info.ports:
            pin_num = port['pin_number']
            pin_name = port['pin_name'].upper()
            role = self.snp_info.pin_info.get(pin_num, {}).get('role', '')

            self.pin_roles[pin_num] = role

            if role == 'ANT' or 'COM' in pin_name or 'ANT' in pin_name:
                if self.com_pin is not None:
                    raise ValueError("multi_com")
                self.com_pin = pin_num
                continue

            band_match = re.search(
                r'(HB|LB|UHB|MB|MHB|H_BAND|L_BAND)', pin_name)
            if band_match:
                band_type = band_match.group(0)
                self.band_pins[pin_num] = band_type

                freq_range = self.snp_info.get_pin_band(pin_num)
                if freq_range and re.match(r'^\d+-\d+$', freq_range):
                    self.band_pins[pin_num] = f"{band_type} ({freq_range} MHz)"

        if self.com_pin is None:
            raise ValueError("no_com")

        if not self.band_pins:
            raise ValueError("no_band")

    def _build_connections(self):
        """TX 频段→COM、RX COM→频段都要通；频段脚彼此不通。"""
        band_set = set(self.band_pins)
        for band_pin in band_set:
            self.connections[band_pin] = {self.com_pin}
        # COM 扇出到每个频段脚（曾写成空集，天线侧 DFS 进不了闲置频段 stub）
        self.connections[self.com_pin] = set(band_set)

    def get_connected_pins(self, pin):
        pin = int(pin)
        return self.connections.get(pin, set())

    def get_com_pin(self):
        return self.com_pin

    def get_band_pins(self):
        return self.band_pins

    def __repr__(self):
        band_info = []
        for pin, band_type in self.band_pins.items():
            pin_name = self.snp_info.pin_info.get(pin, {}).get('name', '')
            band_info.append(f"{band_type}:{pin}({pin_name}) ↔ COM")

        return (f"Component:{self.device_type}({self.snp_info.external_model})\n"
                f"  Connections:\n    " + "\n    ".join(band_info))
class ComponentDUPLEXER:
    """双工器脚位：与分频器相同，COM 双向桥接全部频段脚。"""

    def __init__(self, snp_info, current_node):
        self.device_type = "DUPLEXER"
        self.snp_info = snp_info
        if not self.snp_info.state:
            raise ValueError("no_file")

        self.com_pin = None
        self.band_pins = {}
        self.pin_roles = {}
        self.connections = {}

        self._analyze_divider()
        self._build_connections()

    def _analyze_divider(self):
        for port in self.snp_info.ports:
            pin_num = port['pin_number']
            pin_name = port['pin_name'].upper()
            role = self.snp_info.pin_info.get(pin_num, {}).get('role', '')

            self.pin_roles[pin_num] = role

            if role == 'ANT' or 'COM' in pin_name or 'ANT' in pin_name:
                if self.com_pin is not None:
                    raise ValueError("multi_com")
                self.com_pin = pin_num
                continue

            band_match = re.search(
                r'(HB|LB|UHB|MB|MHB|H_BAND|L_BAND)', pin_name)
            if band_match:
                band_type = band_match.group(0)
                self.band_pins[pin_num] = band_type

                freq_range = self.snp_info.get_pin_band(pin_num)
                if freq_range and re.match(r'^\d+-\d+$', freq_range):
                    self.band_pins[pin_num] = f"{band_type} ({freq_range} MHz)"

        if self.com_pin is None:
            raise ValueError("no_com")

        if not self.band_pins:
            raise ValueError("no_band")

    def _build_connections(self):
        """与分频器相同：COM 双向桥接全部频段脚，频段脚彼此不通。"""
        band_set = set(self.band_pins)
        for band_pin in band_set:
            self.connections[band_pin] = {self.com_pin}
        self.connections[self.com_pin] = set(band_set)

    def get_connected_pins(self, pin):
        pin = int(pin)
        return self.connections.get(pin, set())

    def get_com_pin(self):
        return self.com_pin

    def get_band_pins(self):
        return self.band_pins

    def __repr__(self):
        band_info = []
        for pin, band_type in self.band_pins.items():
            pin_name = self.snp_info.pin_info.get(pin, {}).get('name', '')
            band_info.append(f"{band_type}:{pin}({pin_name}) ➔ COM")

        return (f"Component:{self.device_type}({self.snp_info.external_model})\n"
                f"  Connections:\n    " + "\n    ".join(band_info))


PINParser = None
