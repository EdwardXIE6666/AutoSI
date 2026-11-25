from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator, Iterator, List, Optional, Set, Tuple
import os
import pathlib
import re
import shutil

from keysight.ads import de
from keysight.ads.de import db_uu
from keysight.ads.de.experimental import text_maker
from keysight.ads.de.experimental import generate_symbol as _gensym
from ads.ads_runtime import create_subproject, next_ads_workspace_path
from ads.instance_names import (
    claim_refdes, ge_instance_names, snp_display_and_wire_names, snp_instance_names)
from ads.segment_tree import (
    ads_net_label,
    ads_place_nc,
    series_shunt_to_ground,
    attach_orphan_segment_roots,
    deactivate_placed_instances,
    finalize_segment_chain,
    merge_nc_into_chain,
    nested_uses_divider,
    parent_end_nets,
    pcb_lookup_key,
    pcb_lookup_keys,
    pick_segment_parent,
    snp_sweep_mhz,
    pcb_ports_for_refdes,
    map_snp_ports_to_component_pcb,
    schematic_rf_ports_for_refdes,
    sxp_filename_matches_model,
    SYMBOL_HIDE_CELLS,
    symbol_term_names_left_to_right,
    signal_infos_for_place,
    step_left,
    even_x_span,
    norm_rf_net,
    SEG_PCB_DY,
)
from rf.SnpFileParser import *
from rf.rlc_seed import schematic_rlc_seed
from rf.segments import (
    _looks_like_matching_rlc,
    _schematic_nets_of,
    is_segment_fork,
)
from refnum.graph import is_connector_ref, is_pa_lna_ref

# 打包修复（详见「功能3打包排查记录.md」）：打包后只要 de_generate_symbol_ex 在调用栈上，
# ADS 原生 ael.call.X(...) 的自动转换全部失效（返回未转换的 AELValue，len()/算术即崩）；
# 栈干净时显式转换正常，源码态不触发。解法：拦 _AELBridge.__getattr__——
#   db_get_terms_in_netlist_order(design) → 返回 list(design.get_term_iter())（新鲜 db_uu 端子）；
#   几个标量 getter → 用上层在干净栈预取、显式转换并缓存的值（值类型，安全）。
_TERM_CACHE = {}
try:
    import keysight.ads.ael as _ael_pkg_fix
    _BridgeCls = type(_ael_pkg_fix.call())
    _orig_bridge_getattr = _BridgeCls.__getattr__
    _SCALAR_CACHE_FUNCS = ("db_get_context_db_to_font_point_factor",
                           "db_get_layerid_for_symbol_body",
                           "db_get_layerid_for_symbol_text")

    def _patched_bridge_getattr(self, attrname):
        _v = _orig_bridge_getattr(self, attrname)
        if attrname == "db_get_terms_in_netlist_order":
            # 直接取新鲜的 db_uu 端子（in-stack 有效），避开会失效的 ael.call 自动转换，
            # 也避开「缓存活对象被释放」。顺序差异不影响（exclude_ref_pin 默认 False）。
            def _terms_call(design, *a, **k):
                try:
                    return list(design.get_term_iter())
                except Exception:
                    return _v(design, *a, **k)
            return _terms_call
        if attrname in _SCALAR_CACHE_FUNCS:
            def _cached_call(arg0, *a, **k):
                try:
                    _ck = (attrname, id(arg0))
                    if _ck in _TERM_CACHE:
                        return _TERM_CACHE[_ck]
                except Exception:
                    pass
                return _v(arg0, *a, **k)
            return _cached_call
        return _v
    _BridgeCls.__getattr__ = _patched_bridge_getattr
except Exception:
    pass


# 方案B：SnP 飘线改由 AEL 修复宏在引脚落位后按真实坐标绘制。
# 这里记录每个设计(ANTx)里各 SnP 实例的端口净名（按端口序），供功能3 生成
# 修复宏时把净名烤进去；AEL 只负责按引脚 snap_point 画短线+贴净名，不做字符串解析。
# 结构：{ 设计cell名: [ (snp实例名, [净名1, 净名2, ...]), ... ] }
_SNP_WIRE_REGISTRY = {}

# 分端分层：登记「有 symbol 视图的段 cell」（如 ANTx_COM 及各非叶父段）。
# headless 生成的 symbol 在 ads.exe 里不渲染（同 SnP Pcell 之患），故功能3 生成修复宏时
# 据此对每个 cell 在完整 ADS 引擎里用 AEL de_generate_symbol_ex 由其 schematic 重生成符号。
_SEGMENT_SYMBOL_REGISTRY = []
# 段 symbol 端子名，按 term number 1..N，供子页 X_* 飘线烤进修复宏（与 SnP 同一套）。
_SEGMENT_SYMBOL_TERMS: dict[str, list[str]] = {}


def get_snp_wire_registry():
    """返回 SnP 飘线净名注册表（功能3 生成修复宏时读取）。"""
    return _SNP_WIRE_REGISTRY


def get_segment_symbol_registry():
    """返回「需在完整 ADS 里重生成符号的段 cell」列表（功能3 生成修复宏时读取）。"""
    return _SEGMENT_SYMBOL_REGISTRY


def get_node_net_parallel(node, signal_infos):
    for item in signal_infos:
        if node in item[1]:
            # 与 series 一致，拼成 <网络>_<器件>_<引脚>，避免裸网（否则与 SnP 端口命名不一致、对不上）
            for conn in item[2]:
                if str(conn).startswith(f"{node}."):
                    return f"{item[0]}_{node}_{str(conn).split('.', 1)[1]}"
            return f"{item[0]}_{node}_1"
    return ""


def get_node_net_series(node, signal_infos):
    pin_in = ""
    pin_out = ""
    for item in signal_infos:
        if f"{node}.1" in item[2]:
            pin_in = item[0]+f"_{node}_1"
        if f"{node}.2" in item[2]:
            pin_out = item[0]+f"_{node}_2"
    return pin_in, pin_out
# endregion


class SchematicDesigner:
    """封装ADS原理图设计相关操作的类"""

    def __init__(self, project_info: Dict[str, str], SchematicRes_raw):
        self.project_info = project_info
        self.signal_infos = SchematicRes_raw.signal_infos
        self.gnd_inf = SchematicRes_raw.gnd_inf
        self.part_infos = SchematicRes_raw.part_infos
        self.part_infos_name = SchematicRes_raw.part_infos_name
        self._part_value = getattr(SchematicRes_raw, "part_value", None) or {}
        self._part_desc = getattr(SchematicRes_raw, "part_desc", None) or {}
        self._part_type_token = getattr(SchematicRes_raw, "part_type_token", None) or {}
        self.library_name = project_info["project_name"]
        self._cell_seq = 0  # 段 cell 全局排序序号（跨天线累加：ANT1→ANT2→…）
        # NC（不贴/兼容设计）器件：从「过滤前」原始网络 + part_active 还原 {器件: [所连网络]}，
        # 供功能3 把它们按原拓扑摆出来并 Deactivate（part_active[器件]==False 即 NC）。
        self._signal_infos_raw = getattr(SchematicRes_raw, 'signal_infos_raw', None) or []
        self.signal_infos_raw = self._signal_infos_raw
        self.all_signal_infos_raw = getattr(
            SchematicRes_raw, "all_signal_infos_raw", None) or self._signal_infos_raw
        _part_active = getattr(SchematicRes_raw, 'part_active', None) or {}
        self._nc_nets = {}
        for _ni in self._signal_infos_raw:
            if not isinstance(_ni, (list, tuple)) or len(_ni) < 2 or not isinstance(_ni[1], list):
                continue
            for _c in _ni[1]:
                if _part_active.get(_c, True) is False:
                    self._nc_nets.setdefault(_c, [])
                    if _ni[0] not in self._nc_nets[_c]:
                        self._nc_nets[_c].append(_ni[0])
        # 同一 design 内每个物理位号只放一套实例（共享开关/匹配件会在矩阵或区块里出现多次）。
        self._placed_refdes_by_design = {}
        self._designs_by_cell = {}
        self._region_refdes_by_design = {}

    def placed_refdes(self, design) -> set[str]:
        return set(self._placed_refdes_by_design.get(id(design), set()))

    def region_refdes(self, design) -> dict[int, set[str]]:
        raw = self._region_refdes_by_design.get(id(design), {})
        return {int(k): set(v) for k, v in raw.items()}

    def listed_cells(self) -> list[str]:
        return list(self._designs_by_cell.keys())

    def open_design(self, cell: str):
        return self._designs_by_cell.get(cell)

    @staticmethod
    def extract_ports_from_touchstone(file_path: str, snp: bool = False) -> Dict:
        """从Touchstone文件中提取端口信息"""
        ports_info = {}
        try:
            with open(file_path, 'r') as file:
                for line in file:
                    if line.startswith("! Port["):
                        port_num = int(line.split('[')[1].split(']')[0])
                        port_content = line.split('=')[1].strip()
                        ports_info[port_num] = port_content
        except FileNotFoundError:
            print(f"Error: File '{file_path}' not found.")
        except Exception as e:
            print(f"Error parsing file: {e}")
        return ports_info

    @staticmethod
    def get_all_sxp_files(directory: str) -> List[str]:
        """获取目录下所有.sXp文件"""
        return [os.path.join(root, f)
                for root, _, files in os.walk(directory)
                for f in files if re.match(r".*\.s\d+p$", f, re.IGNORECASE)]

    @staticmethod
    def find_sxp_file_by_component(sxp_files: List[str], component_model: str,
                                   is_pcb: bool = True) -> str:
        """根据组件查找对应的S参数文件。空型号不得命中任何文件。"""
        for file_path in sxp_files:
            file_name = os.path.basename(file_path).lower()
            if sxp_filename_matches_model(file_name, component_model):
                if is_pcb or 'nol' in file_name:
                    return file_path
        return "No S-parameters.S0P"

    def add_odd_even_wires(self, design: object, port_name_list: Dict[int, str],
                           base_x: float, base_y: float, is_odd: bool = True) -> None:
        """添加奇数/偶数端口连线，使用左右两列布局"""
        # 1. 分离左右两列
        l_list = []  # 左列列表 (奇数引脚)
        r_list = []  # 右列列表 (偶数引脚)

        # 分离引脚
        for pin_num, port_name in port_name_list.items():
            if pin_num % 2 == 1:  # 奇数引脚
                l_list.append((pin_num, port_name))
            else:  # 偶数引脚
                r_list.append((pin_num, port_name))

        # 2. 确保右列长度与左列相同（不足则添加占位符）
        # 3. 按照引脚号倒序排布
        l_list.sort(key=lambda x: x[0], reverse=True)  # 倒序排列
        r_list.sort(key=lambda x: x[0], reverse=True)  # 倒序排列
        if len(r_list) < len(l_list):
            r_list = [(0, "")] * (len(l_list) - len(r_list))+r_list
            # r_list+=[(0, "")] * (len(l_list) - len(r_list))

        # 4. 计算起始位置
        max_rows = max(len(l_list), len(r_list))
        y_start = base_y - (max_rows - 1) * 0.5  # 从底部开始
        y_start = base_y
        x_left = base_x - 3
        x_right = base_x + 2
        # 4. 计算名称长度相关的偏移量
        max_len_left = max(len(name)
                           for _, name in l_list if name) if l_list else 0
        max_len_right = max(len(name)
                            for _, name in r_list if name) if r_list else 0

        # x_left = base_x - max(3, max_len_left * 0.3)  # 根据名称长度调整左侧偏移
        # x_right = base_x + max(3, max_len_right * 0.3)  # 根据名称长度调整右侧偏移

        name_offset = max_len_right * 0.25
        # 5. 添加连线
        for row in range(max_rows):
            y_pos = y_start + row * 0.5

            # 添加左列连线（奇数引脚）
            if row < len(l_list) and l_list[row][1]:  # 非占位符
                pin_num, port_name = l_list[row]
                wire = add_wire(design, [(x_left, y_pos), (x_left + 3, y_pos)])
                # wire = add_wire(design, [(x_left - name_offset, y_pos), (x_left + 2, y_pos)])
                add_wire_label(port_name, wire)

            # 添加右列连线（偶数引脚）
            if row < len(r_list) and r_list[row][1]:  # 非占位符
                pin_num, port_name = r_list[row]
                # wire = add_wire(design, [(x_right - 1, y_pos), (x_right, y_pos)])
                wire = add_wire(design, [(x_right-1, y_pos), (x_right, y_pos)])
                add_wire_label(port_name, wire)

    def _norm_net(self, name):
        """归一化网标，使 PCB SnP 与 TermG / 器件 SnP 并上。见 norm_rf_net。"""
        return norm_rf_net(name, getattr(self, "_ant_num", None))

    def _norm_snp_ports(self, port_dict):
        """归一化 SnP 端口名字典，并做每-SnP 去重保险：若归一后多端口同名（主要是
        天线→ANTx 命中多个端口），只保留首个，其余退回各自 comp_pin，避免 SnP Pcell
        因端口重名(db_create_pin "already has a term")崩溃。"""
        norm = {_k: self._norm_net(_v) for _k, _v in port_dict.items()}
        _ant = getattr(self, "_ant_num", None)
        if not _ant:
            return norm
        ant_keys = [_k for _k, _v in norm.items() if _v == _ant]
        if len(ant_keys) > 1:
            for _k in ant_keys[1:]:
                # 退回末两段 comp_pin（绕开天线特例，直接取原值末两段）
                _orig = str(port_dict[_k]).split(" ")[0].split("_")
                norm[_k] = "_".join(_orig[-2:]) if len(_orig) >= 2 else port_dict[_k]
        return norm

    def process_ic_component(self, design: object, node: str, position: Tuple[float, float],
                             all_sxp_files: List[str], all_pcb_port_name_list,
                             keep_pin=None, seg_nets=None) -> None:
        if not str(node or "").strip():
            return
        if is_connector_ref(node) or is_pa_lna_ref(node, self):
            return
        placed = self._placed_refdes_by_design.setdefault(id(design), set())
        if not claim_refdes(placed, node):
            return
        if getattr(self, '_seg_pos', None) is not None:
            self._seg_pos[node] = position[0]   # 记录器件横坐标，供 NC 器件对齐
        """处理IC组件"""
        file_path = self.find_sxp_file_by_component(
            all_sxp_files, (self.part_infos_name or {}).get(node, ""), is_pcb=False)
        _missing = (not file_path) or ("No S-parameters" in str(file_path))
        port_name_list = {}
        if not _missing:
            port_name_list = self.extract_ports_from_touchstone(
                file_path, snp=True)
        instance = SNPFileInfo.get_instance_by_ref_des(node)

        if instance is None:
            snp_files_path = [] if _missing else [file_path]
        else:
            if isinstance(instance.file_path, pathlib.WindowsPath):
                # WindowsPath对象转为单元素列表
                snp_files_path = [instance.file_path]
            elif isinstance(instance.file_path, (list, tuple)):
                # 如果是列表/元组则直接使用
                snp_files_path = list(instance.file_path)
            else:
                # 其他类型（如字符串）包装为列表
                snp_files_path = [instance.file_path]

        # 点4：放分界器件(开关/分频器)时，只保留通向本段那条路径的状态文件。
        # 该器件可能有多条路径(如 U2301 的 5→1 与 5→3)，本段只走 keep_pin 那条；其余路径的
        # SnP 与本段无关、不放。判据：状态文件归一后端口标签含「{器件}_{keep_pin}」(如 U2301_1)。
        if keep_pin is not None and len(snp_files_path) > 1:
            _target = f"{node}_{keep_pin}"
            _kept = []
            for _f in snp_files_path:
                _fp = self.extract_ports_from_touchstone(str(_f), snp=True) or {}
                _rp = self._norm_snp_ports(
                    map_snp_ports_to_component_pcb(
                        all_pcb_port_name_list, _fp, node))
                if _target in set(_rp.values()):
                    _kept.append(_f)
            if _kept:
                snp_files_path = _kept

        original_position = position  # 保存原始位置
        current_position = list(original_position)

        # 判断 SNP 文件数量
        num_files = len(snp_files_path)

        # 缺 S 参数：仍放占位 SnP（端口取本段 PCB 上该位号的脚），链路不断；
        # 用户补库后把 File 指到真 sNp 即可。
        if num_files == 0:
            _cell = (
                getattr(self, "_cur_cell", "")
                or getattr(design, "cell_name", "")
                or ""
            )
            _model = self.part_infos_name.get(node, "")
            _ports = pcb_ports_for_refdes(all_pcb_port_name_list, node)
            if not _ports:
                _ports = schematic_rf_ports_for_refdes(self, node)
            print(ConsoleColor.warning_text(
                f"⚠ 器件 {node}（型号 {_model}）缺 S 参数、库中无对应文件；"
                f"schematic「{_cell}」放占位 SnP（{len(_ports)} 口），"
                f"请补该型号 S 参数后把 File 指过去"))
            if not _ports:
                print(ConsoleColor.warning_text(
                    f"⚠ 器件 {node} 原理图也没有射频口，无法占位，跳过"))
                return
            _named = {k: self._norm_net(v) for k, v in _ports.items()}
            add_snp_component_ds(
                design=design,
                port_name_list=_named,
                snp_name=node,
                position=tuple(original_position),
                file_path=f"No_S_parameters.s{len(_named)}p",
                wire_ports=set(_named.values()),
            )
            return

        name_list = snp_instance_names(node, num_files)
        for i, snp_file_path in enumerate(snp_files_path):
            current_node = name_list[i]

            # 每个文件单独提取端口名（开关类器件多个状态文件的端口名不同，
            # 如 5_1_ANT-RF2 与 5_3_ANT-RF1），wire 标签与端子绑定共用同一份
            # PCB 替换后的名单，否则净名对不上
            _file_ports = self.extract_ports_from_touchstone(
                str(snp_file_path), snp=True) or port_name_list
            _pre_norm = map_snp_ports_to_component_pcb(
                all_pcb_port_name_list, _file_ports, node)
            # 归一化网标，使器件 SnP 与 PCB SnP 同一网络对得上（仿真前提）
            replaced_port_name_list = self._norm_snp_ports(_pre_norm)

            # 点2：器件 SnP 控件口全留（Display port names / NumPorts 不减）。
            # 只给「本通路经过」的端口贴飘线网标（端口所属网络∈本段）；
            # 本器件 PCB 脚（FIL1600_4）即使网名被归一掉也要贴。
            # 其余口登记空名，宏跳过、不画网标。seg_nets 未给则全贴。
            _wire_ports = None
            if seg_nets is not None:
                _seg = set(seg_nets)
                _wire_ports = set()
                for _k, _raw in _pre_norm.items():
                    _net, _comp, _pin = self._split_raw(_raw)
                    if _net in _seg or _comp == node:
                        _wire_ports.add(
                            str(replaced_port_name_list.get(_k, "")).split(" ")[0])

            # 添加 SNP 组件
            add_snp_component_ds(
                design=design,
                snp_name=current_node,  # 使用修改后的节点名称
                position=current_position,
                file_path=str(snp_file_path),
                port_name_list=replaced_port_name_list,
                wire_ports=_wire_ports
            )

            # 下移位置（Y 轴减少 1.5）
            current_position[1] -= 1.5

            # 方案B：飘线不再在此凭固定偏移绘制（与实际引脚对不齐、过长）；
            # 改由修复宏在引脚落位后按 snap_point 真实坐标绘制（净名已登记到 _SNP_WIRE_REGISTRY）。
            # self.add_odd_even_wires(
            #     design, replaced_port_name_list, position[0], position[1], instance)

    def process_ge_component(self, design: object, node: str, position: Tuple[float, float],
                             deactivate: bool = False) -> None:
        """处理GE组件。对地并联 NC 仍摆进去使能；串联 NC 是开路，不摆。"""
        if self._skip_series_nc(node):
            return
        placed = self._placed_refdes_by_design.setdefault(id(design), set())
        if not claim_refdes(placed, node):
            return
        if getattr(self, '_seg_pos', None) is not None:
            self._seg_pos[node] = position[0]   # 记录器件横坐标
        if node in (getattr(self, "_nc_nets", None) or {}):
            deactivate = True
        _si = signal_infos_for_place(
            node, self.signal_infos,
            getattr(self, "_signal_infos_raw", None),
            getattr(self, "_nc_nets", None))
        x, y = position
        kind, token = schematic_rlc_seed(
            node,
            (getattr(self, "_part_value", None) or {}).get(node, ""),
            (getattr(self, "_part_desc", None) or {}).get(node, ""),
            self.part_infos_name.get(node, ""),
            (getattr(self, "_part_type_token", None) or {}).get(node, ""),
        )
        c_val = token if kind == "C" else "999 pF"
        l_val = token if kind == "L" else "999 nH"
        r_val = token if kind == "R" else "0"
        _insts = []
        _keep = []
        if node in self.gnd_inf[2]:  # 接地组件
            x += 2
            _c_name, _l_name = ge_instance_names(node, grounded=True)
            _c = add_capacitor(design, (x, y), _c_name, c_val, angle=-90)
            _l = add_inductor(design, (x+1, y), _l_name, l_val, angle=-90)
            _insts.extend((_c, _l))
            if kind == "C":
                _keep.append(_c)
            elif kind == "L":
                _keep.append(_l)
            else:
                _keep.extend((_c, _l))
            wire_label = self._norm_net(get_node_net_parallel(node, _si))
            add_wire_label(wire_label, add_wire(design, [(x, y), (x+1, y)]))
            add_ground(design, (x, y-1))
            add_ground(design, (x+1, y-1))

        else:  # 非接地组件
            pin_in, pin_out = get_node_net_series(node, _si)
            pin_in = self._norm_net(pin_in)
            pin_out = self._norm_net(pin_out)
            _c_name, _l_name, _r_name = ge_instance_names(node, grounded=False)
            _c = add_capacitor(design, (x, y), _c_name, c_val, angle=0)
            _l = add_inductor(design, (x, y-1), _l_name, l_val, angle=0)
            _r = add_resistor(design, (x, y-2), _r_name, r_val, angle=0)
            _insts.extend((_c, _l, _r))
            if kind == "C":
                _keep.append(_c)
            elif kind == "L":
                _keep.append(_l)
            elif kind == "R":
                _keep.append(_r)
            else:
                _keep.extend((_c, _l, _r))

            add_wire_label(pin_in, add_wire(design, [(x, y), (x, y-1)]))
            add_wire(design, [(x, y-1), (x, y-2)])

            add_wire_label(pin_out, add_wire(design, [(x+1, y-1), (x+1, y-2)]))
            add_wire(design, [(x+1, y), (x+1, y-1)])

        _off = _insts if deactivate else [i for i in _insts if i not in _keep]
        if _off:
            _fail = deactivate_placed_instances(_off)
            if _fail and deactivate:
                print(ConsoleColor.warning_text(
                    f"⚠ NC {node} 已摆并连网，但去使能失败: {'; '.join(_fail)}"))

    def create_subproject_design_old(self, subproject_name, ads_component_matrix, all_sxp_files_pcb, all_sxp_files_component, SNP_list) -> None:
        def trim_and_align_matrix(matrix):
            """
            删除每行末尾的空字符串，并将所有行对齐到相同长度

            参数:
                matrix: 二维列表，包含字符串元素

            返回:
                处理后的二维列表
            """
            # 1. 删除每行末尾的空字符串
            trimmed_matrix = []
            for row in matrix:
                # 从后往前遍历，找到第一个非空元素的位置
                last_non_empty = len(row)
                while last_non_empty > 0 and row[last_non_empty-1] == "":
                    last_non_empty -= 1

                # 截取到第一个非空元素位置
                trimmed_row = row[:last_non_empty]
                trimmed_matrix.append(trimmed_row)

            # 2. 找到最大行长度
            max_len = max(len(row)
                          for row in trimmed_matrix) if trimmed_matrix else 0

            # 3. 将所有行扩展到相同长度（用空字符串填充）
            aligned_matrix = []
            for row in trimmed_matrix:
                # 计算需要填充的长度
                padding_len = max_len - len(row)
                # 填充空字符串
                aligned_row = row + [""] * padding_len
                aligned_matrix.append(aligned_row)

            return aligned_matrix
        """为子项目创建原理图设计"""
        design = db_uu.create_schematic(
            name=(self.library_name, subproject_name, "schematic"))
        design.clear_design()

        # 添加基本组件
        all_pcb_port_name_list = self._add_basic_components(
            design, SNP_list, all_sxp_files_pcb)

        # 分类组件
        scale_x = -2.5
        # 宽
        scale_y = -6
        # 高
        # 初始化 Y 轴偏移量
        y_offset = 0
        for index in ads_component_matrix:
            component_matrix = ads_component_matrix[index]
            component_matrix = trim_and_align_matrix(component_matrix)
            for current_x in range(len(component_matrix)):
                for current_y in range(len(component_matrix[current_x])):
                    # 遍历当前行的每一列
                    node = component_matrix[current_x][current_y]
                    if not (current_x == 0 and current_y == 0):
                        if node != "":
                            if _looks_like_matching_rlc(node, self):
                                self.process_ge_component(
                                    design, node, (current_y*scale_y, current_x*scale_x+y_offset))
                            else:
                                self.process_ic_component(
                                    design, node,  (current_y*scale_y, +current_x*scale_x+y_offset), all_sxp_files_component, [pcb_port for pcb_port in all_pcb_port_name_list if node in pcb_port])
            add_rectangle(design, ll_or_box=(current_y*scale_y-15,
                          current_x*scale_x+y_offset-2), ur=(0, y_offset+1))
            add_port(design, [current_y*scale_y-5, y_offset],
                     "P"+str(int(y_offset)))
            add_vswr_equation(design, (current_y*scale_y-10, y_offset -
                              int(current_y/3)), name="VSWR"+str(int(y_offset)), s_param="S11")
            add_scattering_parameter_component(design, (current_y*scale_y-15, y_offset-int(2*current_y/3)), name="SP"+str(
                int(y_offset)), start=f"{index.split("-")[0]}MHz", stop=f"{index.split("-")[1]}MHz")
            current_x += 1
            y_offset += (current_x*scale_x-5)
        design.save_design()
        print(ConsoleColor.warning_text(
            f"Design {subproject_name} has been saved."))

    def create_subproject_design(self, subproject_name, level_matrix,net_work_class,schematic_res, SNP_list,all_sxp_files_pcb,all_sxp_files_component, reverse_edges=None, seg_meta=None) -> None:
        # 网名归一化所需：本设计天线口名（ANT2 / DIV_ANT），供 _norm_net 识别馈点网络。
        self._ant_num = subproject_name
        self._seg_meta = seg_meta or {}
        def trim_and_align_matrix(matrix):
            """
            删除每行末尾的空字符串，并将所有行对齐到相同长度

            参数:
                matrix: 二维列表，包含字符串元素

            返回:
                处理后的二维列表
            """
            # 1. 删除每行末尾的空字符串
            trimmed_matrix = []
            for row in matrix:
                # 从后往前遍历，找到第一个非空元素的位置
                last_non_empty = len(row)
                while last_non_empty > 0 and row[last_non_empty-1] == "":
                    last_non_empty -= 1

                # 截取到第一个非空元素位置
                trimmed_row = row[:last_non_empty]
                trimmed_matrix.append(trimmed_row)

            # 2. 找到最大行长度
            max_len = max(len(row)
                          for row in trimmed_matrix) if trimmed_matrix else 0

            # 3. 将所有行扩展到相同长度（用空字符串填充）
            aligned_matrix = []
            for row in trimmed_matrix:
                # 计算需要填充的长度
                padding_len = max_len - len(row)
                # 填充空字符串
                aligned_row = row + [""] * padding_len
                aligned_matrix.append(aligned_row)

            return aligned_matrix
        def clean_matrix(matrix):
            """
            清理矩阵：删除全空行
            
            参数:
                matrix (list of list): 二维矩阵
            
            返回:
                list of list: 清理后的新矩阵
            """
            if not matrix:
                return []
            
            # 1. 删除全空行
            cleaned = [row for row in matrix if any(cell is not None for cell in row)]
            
            return cleaned

        def analyze_network_regions(matrix, net_work_class, schematic_res):
            def find_connected_regions(matrix, components):
                """
                基于行间相同列元素检测连通区域
                
                参数:
                    matrix (list of list): 二维矩阵
                    components (list): 网络相关的组件列表
                
                返回:
                    list: 连通区域列表，每个区域包含坐标范围和尺寸信息
                """
                # 步骤1: 识别所有包含相关组件的行
                valid_rows = []
                for row_idx, row in enumerate(matrix):
                    # 检查该行是否包含任何相关组件
                    if any(cell in components for cell in row):
                        # 记录该行中每个组件的位置
                        row_components = {}
                        for col_idx, cell in enumerate(row):
                            if cell in components:
                                row_components[col_idx] = cell
                        valid_rows.append((row_idx, row_components))
                
                # 步骤2: 构建行连接图
                # 使用并查集数据结构管理连通区域
                parent = list(range(len(valid_rows)))
                
                def find(x):
                    """查找元素的根节点"""
                    if parent[x] != x:
                        parent[x] = find(parent[x])
                    return parent[x]
                
                def union(x, y):
                    """合并两个元素所在的集合"""
                    root_x = find(x)
                    root_y = find(y)
                    if root_x != root_y:
                        parent[root_y] = root_x
                
                # 步骤3: 检测行间连接
                # 比较每一对行，检查是否有相同列的元素
                for i in range(len(valid_rows)):
                    row_i_idx, row_i_comps = valid_rows[i]
                    for j in range(i + 1, len(valid_rows)):
                        row_j_idx, row_j_comps = valid_rows[j]
                        
                        # 检查两行是否有相同列的元素
                        connected = False
                        for col in set(row_i_comps.keys()) & set(row_j_comps.keys()):
                            # 如果相同列有相同元素，则连接
                            if row_i_comps[col] == row_j_comps[col]:
                                connected = True
                                break
                        
                        # 如果连接，则合并这两个行
                        if connected:
                            union(i, j)
                
                # 步骤4: 分组连通区域
                regions = []
                groups = {}
                for idx in range(len(valid_rows)):
                    root = find(idx)
                    if root not in groups:
                        groups[root] = []
                    groups[root].append(valid_rows[idx])
                
                # 步骤5: 计算每个区域的范围
                for group in groups.values():
                    # 初始化边界
                    min_row = float('inf')
                    max_row = float('-inf')
                    min_col = float('inf')
                    max_col = float('-inf')
                    
                    # 收集所有组件
                    region_components = set()
                    
                    # 遍历组内所有行
                    for row_idx, row_comps in group:
                        # 更新行边界
                        min_row = min(min_row, row_idx)
                        max_row = max(max_row, row_idx)
                        
                        # 更新列边界
                        for col in row_comps.keys():
                            min_col = min(min_col, col)
                            max_col = max(max_col, col)
                            region_components.add(row_comps[col])
                    
                    # 计算区域尺寸
                    width = max_col - min_col + 1 if min_col != float('inf') else 0
                    height = max_row - min_row + 1 if min_row != float('inf') else 0
                    
                    # 添加区域信息
                    regions.append({
                        'top_left': (min_row, min_col),
                        'bottom_right': (max_row, max_col),
                        'width': width,
                        'height': height,
                        'components': list(region_components)
                    })
                
                return regions
         
         
            """
            分析矩阵中每个网络的连通区域
            
            参数:
                matrix (list of list): 二维矩阵数据
                net_work_class (dict): 网络分类信息
                schematic_res: 原理图解析对象，提供get_components_for_net方法
            
            返回:
                dict: 每个网络的连通区域信息
            """
            results = {}
            
            # 遍历每个频段和网络
            for band, net_list in net_work_class.items():
                band_components=[]
                for net in net_list:
                    # 获取网络相关组件
                    band_components += schematic_res.get_components_for_net(net)
                    
                # 创建过滤后的矩阵（保持原始矩阵大小）
                filtered_matrix = []
                for row in matrix:
                    filtered_row = []
                    for cell in row:
                        if cell in band_components:
                            filtered_row.append(cell)
                        else:
                            filtered_row.append(None)
                    filtered_matrix.append(filtered_row)
                
                # 查找连通区域
                regions = find_connected_regions(filtered_matrix, band_components)
                print(filtered_matrix)
                # 存储结果
                results[band] = {
                    'components': band_components,
                    'regions': regions
                }
            
            return results

        def remove_duplicate_nodes(matrix):
            """
            处理矩阵中的重复节点：每列只保留最上面的一个节点
            
            参数:
                matrix (list of list): 二维矩阵
            
            返回:
                list of list: 去重后的新矩阵
            """
            if not matrix:
                return matrix
            
            # 确定矩阵的最大列数
            max_cols = max(len(row) for row in matrix)
            
            # 创建新矩阵（初始化为None）
            new_matrix = [[None] * max_cols for _ in range(len(matrix))]
            
            # 遍历每一列
            for col in range(max_cols):
                seen_nodes = set()  # 记录该列已出现的节点
                
                # 遍历每一行（从上到下）
                for row_idx in range(len(matrix)):
                    # 检查当前行是否有足够的列
                    if col < len(matrix[row_idx]):
                        node = matrix[row_idx][col]
                        
                        # 如果是空节点，直接复制
                        if node is None:
                            new_matrix[row_idx][col] = None
                        # 如果是新节点，保留并记录
                        elif node not in seen_nodes:
                            new_matrix[row_idx][col] = node
                            seen_nodes.add(node)
                        # 如果是重复节点，设置为None
                        else:
                            new_matrix[row_idx][col] = None
                    else:
                        # 当前行没有该列，设置为None
                        new_matrix[row_idx][col] = None
            
            return new_matrix
    # 1. 镜像并清理矩阵
        processed_matrix = clean_matrix(level_matrix)
        print("镜像并清理后的矩阵:")
        # for row in processed_matrix:
        #     print(row)
        
        # 2. 分析网络区域
        network_data = analyze_network_regions(processed_matrix, net_work_class, schematic_res)
        processed_matrix=remove_duplicate_nodes(processed_matrix)
        # 添加基本组件
        all_pcb_port_name_list = self._add_basic_components(
            None, SNP_list, all_sxp_files_pcb, ant_num=subproject_name,
            net_work_class=net_work_class)

        # 只生成分段原理图。名为天线号的合并页会再放一遍同一批位号。
        try:
            self._render_segmented(net_work_class, network_data, processed_matrix,
                                   all_pcb_port_name_list, all_sxp_files_component)
        except Exception as _e:
            import traceback
            print(ConsoleColor.warning_text(
                f"分端渲染失败: {_e}\n{traceback.format_exc()}"))

    def _add_basic_components(self, design: object, SNP_list,
                              all_sxp_files_pcb, ant_num="",
                              net_work_class=None) -> None:
        """计算各频段 PCB SnP 的端口名（供 IC 处理匹配 PCB 端口用），并登记每频段的
        PCB 文件/实例名/正规化端口，供区域循环按分端把 PCB 摆进各自横带。
        实际 PCB 摆放、天线/芯片 Term 改到区域循环里做（每区域一条自包含横带）。"""
        self._pcb_by_band = {}
        all_pcb_port_name_list = []
        nwc = net_work_class or {}
        for snp_pcb_raw in SNP_list:
            snp_pcb = str(snp_pcb_raw).replace("-", "_").replace("/", "_")
            nets = nwc[snp_pcb_raw] if isinstance(nwc, dict) and snp_pcb_raw in nwc else []
            pcb_file_path = "No S-parameters.S0P"
            pcb_key = pcb_lookup_key(ant_num, snp_pcb_raw)
            for key in pcb_lookup_keys(ant_num, snp_pcb_raw, nets):
                found = self.find_sxp_file_by_component(
                    all_sxp_files_pcb, key, is_pcb=True)
                if found and not str(found).endswith("S0P"):
                    pcb_file_path = found
                    pcb_key = key
                    break
            pcb_port_name_list = self.extract_ports_from_touchstone(
                pcb_file_path)
            all_pcb_port_name_list += [item for _idx,
                                       item in pcb_port_name_list.items()]
            # 归一化 PCB SnP 网标，使其与器件 SnP 同一网络对得上（仿真前提）
            _raw = self.extract_ports_from_touchstone(pcb_file_path, snp=True)
            _pcb_ports = self._norm_snp_ports(_raw)
            # 用原始频段名(带'-')做键，与 network_data 的 band 键一致
            self._pcb_by_band[snp_pcb_raw] = {
                "file": pcb_file_path, "snp_name": snp_pcb,
                "ports": _pcb_ports, "raw_ports": list(_raw.values())}
        return all_pcb_port_name_list

    def _boundary_net(self, boundary_comp, band_ports):
        """由边界器件名 → 该频段 PCB 端口里对应的正规化网标。
        天线节点(== self._ant_num)直接返回 ANTx；否则找 comp 段相同的端口。"""
        if not boundary_comp:
            return None
        if boundary_comp == getattr(self, "_ant_num", None):
            return self._ant_num
        for p in band_ports:
            parts = str(p).split("_")
            comp = "_".join(parts[:-1]) if len(parts) >= 2 else p
            if comp == boundary_comp:
                return p
        return None

    def _band_end_net(self, band, target_net, region_comps, prefer_ant=False):
        """兜底：matrix 相邻扫不到边界器件时（分支起点），用频段端网络名定边界 Term 网标。
        net_work_class[band] 首=天线侧网、尾=芯片侧网；在该频段 PCB 端口里挑「网络号==该端网、
        且器件不属于本区块（即真正的边界外部器件）」的端口，正规化后即 Term 网标。"""
        info = self._pcb_by_band.get(band)
        if not info or not target_net:
            return None
        ant = getattr(self, "_ant_num", None)
        fallback = None
        for raw in info.get("raw_ports", []):
            parts = str(raw).split("_")
            netnum = "_".join(parts[:-2]) if len(parts) >= 3 else (parts[0] if parts else "")
            if netnum != target_net:
                continue
            norm = self._norm_net(raw)
            comp = "_".join(norm.split("_")[:-1]) if "_" in norm else norm
            if prefer_ant and norm == ant:
                return norm
            if comp not in region_comps:
                fallback = fallback or norm
        return fallback

    def _add_term_with_label(self, design, x, y, name, net_label, num=None):
        """放一个 TermG，并在其引脚引一段带网络标签的短线，靠网标并网。"""
        add_port(design, [x, y], name, num=num)
        if net_label:
            add_wire_label(net_label, add_wire(design, [(x, y), (x, y + 1)]))

    def _build_segment_tree(self, net_work_class):
        """从 net_work_class + 各频段 PCB 原始端口推导分段父子树（分端分层用）。
        返回 {band: {'parent':父band或None, 'bfilter':(分界器件,child_pin)或None,
                     'ant_net':天线侧网, 'chip_net':芯片侧网, 'children':[...]}}。
        规则(已离线在 FG161/ANT2 全部验证)：本段天线侧网 net_work_class[band][0] 在本段 PCB
        端口里对应的非无源件即分界滤波器；父段=其它含该滤波器、且该滤波器非其自身天线侧分界的段。"""
        def split_port(p):
            parts = str(p).split(" ")[0].split("_")
            if len(parts) >= 3:
                return ("_".join(parts[:-2]), parts[-2], parts[-1])
            if len(parts) == 2:
                return ("", parts[0], parts[1])
            return ("", str(p), "")

        def is_passive(comp):
            return _looks_like_matching_rlc(comp, self)

        raw = {b: self._pcb_by_band.get(b, {}).get("raw_ports", [])
               for b in net_work_class}
        ant = getattr(self, "_ant_num", None)

        def bfilter(b):
            antnet = net_work_class[b][0] if net_work_class[b] else None
            for p in raw[b]:
                nn, comp, pin = split_port(p)
                if nn == antnet and not is_passive(comp):
                    return (comp, pin)
            return None

        def antnet_on_comp(b, comp):
            antnet = net_work_class[b][0] if net_work_class[b] else None
            for p in raw[b]:
                nn, c, _ = split_port(p)
                if nn == antnet and c == comp:
                    return True
            return False

        tree = {}
        for b in net_work_class:
            nets = net_work_class[b]
            ant_net = nets[0] if nets else None
            chip_net = nets[-1] if nets else None
            bf = bfilter(b)
            parent = None
            # 根：天线侧网就是天线名(ANTx)，无需上溯
            if bf is not None and not (ant and ant_net == ant):
                comp = bf[0]
                cands = [x for x in net_work_class if x != b
                         and comp in {split_port(p)[1] for p in raw[x]}
                         and not antnet_on_comp(x, comp)]
                parent = pick_segment_parent(b, cands)
            tree[b] = {"parent": parent, "bfilter": bf,
                       "ant_net": ant_net, "chip_net": chip_net, "children": []}
        for b, info in tree.items():
            if info["parent"] in tree:
                tree[info["parent"]]["children"].append(b)
        meta = getattr(self, "_seg_meta", None) or {}
        parents = meta.get("parent") or {}
        forks = meta.get("fork") or {}
        if parents:
            for child, parent in parents.items():
                if child not in tree or parent not in tree:
                    continue
                tree[child]["parent"] = parent
                fork = forks.get(child)
                if fork and fork[0]:
                    tree[child]["bfilter"] = fork
            for info in tree.values():
                info["children"] = []
            for band, info in tree.items():
                p = info.get("parent")
                if p in tree:
                    tree[p]["children"].append(band)
        attach_orphan_segment_roots(tree, ant)
        return tree

    def _seg_cell(self, band, is_root):
        """段 cell 名：带前置排序序号 NNN_（_render_segmented 预先按顺序登记到
        _seg_cell_names）。根=NNN_ANTx_COM，其余=NNN_ANTx_频段。"""
        names = getattr(self, "_seg_cell_names", None)
        if names and band in names:
            return names[band]
        return f"{self._ant_num}_COM" if is_root else f"{self._ant_num}_{band}"

    def _seg_freq_mhz(self, band):
        """段 SP 扫频 = 该段 PCB .sNp 覆盖。多进一出时网表已把 RF 父段抽宽到 COM 侧。"""
        info = self._pcb_by_band.get(band)
        if not info or not info.get("file"):
            return None
        return snp_sweep_mhz(info["file"])

    def _split_raw(self, p):
        parts = str(p).split(" ")[0].split("_")
        if len(parts) >= 3:
            return ("_".join(parts[:-2]), parts[-2], parts[-1])
        if len(parts) == 2:
            return ("", parts[0], parts[1])
        return ("", str(p), "")

    def _seg_chip_label(self, band, tree):
        """段芯片侧 Term 的网标：该段芯片侧网上的非无源件(芯片)归一网，如 3300→U2900_25。"""
        chip_net = tree[band]["chip_net"]
        fallback = None
        for raw in self._pcb_by_band.get(band, {}).get("raw_ports", []):
            nn, comp, _ = self._split_raw(raw)
            if nn == chip_net:
                fallback = fallback or self._norm_net(raw)
                if not _looks_like_matching_rlc(comp, self):
                    return self._norm_net(raw)
        return fallback or chip_net

    def _seg_matching_comps(self, band, network_data, matrix):
        """按 processed_matrix 列序（天线→芯片信号链顺序）取本段匹配件，
        而非 region['components'] 的无序集合。"""
        comps = []
        for r in network_data.get(band, {}).get("regions", []):
            row = r["top_left"][0]
            c0 = r["top_left"][1]
            c1 = r["bottom_right"][1]
            rcomps = set(r["components"])
            if 0 <= row < len(matrix):
                for c in range(c0, min(c1 + 1, len(matrix[row]))):
                    node = matrix[row][c]
                    if node and node in rcomps and node not in comps:
                        if self._skip_series_nc(node):
                            continue
                        comps.append(node)
        return comps

    def _seg_chain(self, band, network_data, matrix):
        """非根段：沿主区(min_col 最小、最靠天线)所在矩阵行，从 min_col 起向芯片走，
        按序收无源匹配件，遇到第一个 IC 即本段芯片端节点(返回)并停止——这样 U2301 这类
        芯片会被摆出来，且不会把该 IC 之后属于子支路的器件误并入本段。
        返回 (无源件按序, 芯片端IC或None)。"""
        regions = network_data.get(band, {}).get("regions", [])
        if not regions:
            return ([], None)
        r = min(regions, key=lambda rr: rr["top_left"][1])
        row = r["top_left"][0]
        c0 = r["top_left"][1]
        rcomps = set(r["components"])
        passives, chip_ic = [], None
        if 0 <= row < len(matrix):
            for c in range(c0, len(matrix[row])):
                node = matrix[row][c]
                if not node:
                    continue
                if _looks_like_matching_rlc(node, self):
                    if node in rcomps and node not in passives:
                        if not self._skip_series_nc(node):
                            passives.append(node)
                else:
                    if is_pa_lna_ref(node, self) or is_connector_ref(node):
                        break
                    chip_ic = node
                    break
        return (passives, chip_ic)

    def _sim_port_instances(self, design):
        """原理图上的仿真口/控制器（生成 symbol 时必须先关掉）。"""
        insts = list(getattr(design, "instances", None) or ())
        if not insts:
            fn = getattr(design, "get_instance_iter", None)
            if callable(fn):
                insts = list(fn())
        out = []
        for inst in insts:
            cell = str(getattr(inst, "cell_name", "") or "")
            if cell in SYMBOL_HIDE_CELLS:
                out.append(inst)
        return out

    def _set_sim_ports_hidden(self, design, hidden: bool) -> None:
        for inst in self._sim_port_instances(design):
            if hidden:
                inst.deactivate()
            else:
                inst.activate()

    def _build_segment_symbol(self, cell_name, sch_design):
        """由段 schematic 自动生成 symbol（黑盒，引脚=schematic 的开头/结尾端子，
        按引脚位置排布：开头ANT2在天线侧、结尾在芯片侧）。绝不手画空符号。

        生成前关掉 TermG/SP/VSWR：它们带仿真 Num，进 dual symbol 会变成层级端口；
        子页再放同号 TermG 即 Duplicate Port，ADS 关掉一只脚、网标接不上。
        层级口只留 create_pin。生成后再打开，父页仍可单独仿真。
        """
        self._set_sim_ports_hidden(sch_design, True)
        sch_design.save_design()
        sym = db_uu.create_symbol(name=(self.library_name, cell_name, "symbol"))
        sym.clear_design()
        # 干净栈预取端子（显式转换可用），缓存供 generate_symbol 内的调用拦截命中，
        # 绕开「栈内自动转换失效」的打包 bug。
        try:
            from keysight.ads import ael as _ael_pre
            for _fn, _arg in (("db_get_context_db_to_font_point_factor", sym),
                              ("db_get_layerid_for_symbol_body", sym),
                              ("db_get_layerid_for_symbol_text", sym)):
                try:
                    _r = getattr(_ael_pre.call, _fn)(_arg)
                    _TERM_CACHE[(_fn, id(_arg))] = (
                        _r.as_python_value(None) if hasattr(_r, "as_python_value") else _r)
                except Exception:
                    pass
        except Exception:
            pass
        _gensym.de_generate_symbol_ex(sym, sch_design, 0.25, 0.25,
                                      is_dual_symbol_type=True, replace=True)
        sym.save_design()
        self._set_sim_ports_hidden(sch_design, False)
        sch_design.save_design()
        # 登记该 cell：ads.exe 修复宏会在完整引擎里据此重生成符号（headless 这份不渲染）
        if cell_name not in _SEGMENT_SYMBOL_REGISTRY:
            _SEGMENT_SYMBOL_REGISTRY.append(cell_name)
        names = symbol_term_names_left_to_right(sym, str(getattr(self, "_ant_num", "") or ""))
        _SEGMENT_SYMBOL_TERMS[cell_name] = names
        print(ConsoleColor.warning_text(
            f"段 symbol 由原理图生成: {cell_name}  端子={names}"))

    def _embed_parent_symbol(self, design, parent_cell, position):
        """在下游 schematic 实例化父段 symbol。

        不在 headless 里画引脚线：那份 symbol 的 snap_point 和 ads.exe 里
        gen_segment_symbol 重生后的真脚位对不上，线会停在盒子外面、脚变红。
        引脚短线+网标由 fix_snp 宏在重生之后按真实 snap_point 画。
        """
        inst_name = f"X_{parent_cell}"
        inst = design.add_instance(
            (self.library_name, parent_cell, "symbol"), tuple(position),
            name=inst_name)
        names = list(_SEGMENT_SYMBOL_TERMS.get(parent_cell) or ())
        if not names:
            by_num: dict[int, str] = {}
            for ip in inst.get_inst_pin_iter():
                term = getattr(ip, "inst_term", None)
                pin = getattr(ip, "master_pin", None)
                num = 0
                lbl = ""
                if term is not None:
                    num = int(getattr(term, "number", 0) or 0)
                    lbl = str(getattr(term, "name", "") or "")
                if not num and pin is not None:
                    num = int(getattr(pin, "term_number", 0) or 0)
                    lbl = str(getattr(pin, "term_name", "") or "")
                if num >= 1 and lbl:
                    by_num[num] = lbl
            if by_num:
                names = [by_num.get(i, "") for i in range(1, max(by_num) + 1)]
        child = str(getattr(self, "_cur_cell", "") or getattr(design, "cell_name", "") or "")
        if child and names:
            _SNP_WIRE_REGISTRY.setdefault(child, []).append((inst_name, names))
        return inst

    def _is_gnd_net_name(self, net) -> bool:
        text = str(net or "").upper()
        return text == "GND" or text.startswith("GND_")

    def _shunt_leg_parts(self, band) -> list[str]:
        """本段主路上，串起来再并到地的那条腿。"""
        raw = getattr(self, "_signal_infos_raw", None) or []
        nets = list((getattr(self, "_net_work_class", None) or {}).get(band) or [])
        return series_shunt_to_ground(
            raw, nets, lambda comp: _looks_like_matching_rlc(comp, self), self._is_gnd_net_name,
        )

    def _shunt_leg_refs(self) -> set:
        nwc = getattr(self, "_net_work_class", None) or {}
        if not nwc:
            return set()
        key = id(nwc)
        if getattr(self, "_shunt_leg_key", None) == key:
            return self._shunt_leg_ref_set
        refs = set()
        for band in nwc:
            refs.update(self._shunt_leg_parts(band))
        self._shunt_leg_key = key
        self._shunt_leg_ref_set = refs
        return refs

    def _skip_series_nc(self, comp) -> bool:
        """串联不贴件是开路，ADS 不构图。对地并联，以及串起来再并到地的腿，仍可摆。"""
        if comp and comp in self._shunt_leg_refs():
            return False
        if not comp or comp not in (getattr(self, "_nc_nets", None) or {}):
            return False
        gnd = (self.gnd_inf[2] if self.gnd_inf and len(self.gnd_inf) > 2 else []) or []
        return not ads_place_nc(comp, gnd)

    def _chain_with_nc(self, actives, band):
        """本段匹配链 ∪ NC：每位号一次。已在匹配链里的 DNP 只标 NC（用原始网贴标），
        不插第二份。其余 NC 按网序插到左右邻之间。actives 为器件名或 (器件, 是否IC)。
        返回 [(器件, 是否IC, 是否NC)]。自检：NC 网序键须落在左右邻之间。"""
        ordered = list(getattr(self, "_net_work_class", {}).get(band, []))
        idx = {n: i for i, n in enumerate(ordered)}
        seg = set(ordered)

        def _key(comp):
            # 器件信号网在天线→芯片网序里的均值（串联件取两端网中点、并联件取其网点）
            ks = [idx[_ni[0]] for _ni in self._signal_infos_raw
                  if len(_ni) >= 2 and isinstance(_ni[1], list)
                  and comp in _ni[1] and _ni[0] in idx]
            return (sum(ks) / len(ks)) if ks else float("inf")

        _gnd = (self.gnd_inf[2] if self.gnd_inf and len(self.gnd_inf) > 2 else []) or []
        ncs = [c for c, nets in (getattr(self, "_nc_nets", None) or {}).items()
               if c not in self._nc_placed
               and any(n in seg for n in nets)
               and ads_place_nc(c, _gnd)]
        seq = merge_nc_into_chain(actives, ncs, _key)
        for c, _ic, is_nc in seq:
            if is_nc:
                self._nc_placed.add(c)
        for i, (nc, _ic, is_nc) in enumerate(seq):
            if not is_nc:
                continue
            k = _key(nc)
            _lk = _key(seq[i - 1][0]) if i > 0 else float("-inf")
            _rk = _key(seq[i + 1][0]) if i + 1 < len(seq) else float("inf")
            _ok = _lk <= k <= _rk
            print(ConsoleColor.warning_text(
                f"NC {nc} 内联插入(网序键={k:.1f}) 左邻={_lk:.1f} 右邻={_rk:.1f} "
                f"拓扑{'一致 ✓' if _ok else '不一致 ✗（请检查！）'}"))
        return seq

    def _seg_middle_and_chip(self, band):
        """非根段中段器件：段信号网(net_work_class[band])上的所有有源器件，按网序排好，
        返回 (middle:[(器件, 是否IC)], 叶段芯片端 IC 或 None)。
        排除：本段分界器件、各子段分界器件、天线节点（耦合器旁路件因其网不在段网集自然排除）。
        叶段把网序最大的 IC 拎出作 chip_ic（末端单独摆 + term2）；非叶段中段含全部 IC。
        解决多 IC 段（如耦合器段 U3102+LPF3102+芯片）只摆得出一个、其余被漏的问题。"""
        tree = getattr(self, "_seg_tree", {}) or {}
        info = tree.get(band, {})
        ordered = list(self._net_work_class.get(band, []))
        idx = {n: i for i, n in enumerate(ordered)}
        excl = set()
        bf = info.get("bfilter")
        if bf and is_segment_fork(bf[0], self, 2):
            excl.add(bf[0])
        for c in info.get("children", []):
            cbf = tree.get(c, {}).get("bfilter")
            if cbf and is_segment_fork(cbf[0], self, 2):
                excl.add(cbf[0])
        ant = self._ant_num
        comp_keys = {}
        sources = [self.signal_infos, getattr(self, "_signal_infos_raw", None) or []]
        for src in sources:
            for ni in src:
                if len(ni) >= 2 and isinstance(ni[1], list) and ni[0] in idx:
                    for comp in ni[1]:
                        if not str(comp or "").strip():
                            continue
                        comp_keys.setdefault(comp, []).append(idx[ni[0]])
        # 两端通路件：射频口 < 3，只要有网在本段就必须摆（COM 上的 BPF）
        for comp in list(self.part_infos or ()):
            if _looks_like_matching_rlc(comp, self):
                continue
            if is_segment_fork(comp, self, 0):
                continue
            if is_connector_ref(comp) or is_pa_lna_ref(comp, self):
                continue
            hit = [idx[n] for n in _schematic_nets_of(self, comp) if n in idx]
            if hit:
                comp_keys.setdefault(comp, []).append(min(hit))
        items = []
        for comp, ks in comp_keys.items():
            if not str(comp or "").strip():
                continue
            if comp in excl or comp == ant:
                continue
            if self._skip_series_nc(comp):
                continue
            if is_connector_ref(comp):
                continue
            if is_pa_lna_ref(comp, self):
                continue
            is_ic = not _looks_like_matching_rlc(comp, self)
            items.append((comp, sum(ks) / len(ks), is_ic))
        items.sort(key=lambda t: t[1])
        items, chip_ic = finalize_segment_chain(items)
        return [(c, is_ic) for c, _k, is_ic in items], chip_ic

    def _seg_topo_chain(self, band, network_data, matrix):
        """匹配件 + 通路 IC，按天线→芯片网序排，和原理图拓扑一致。"""
        mid, _ = self._seg_middle_and_chip(band)
        seen = {c for c, _ in mid}
        items = list(mid)
        for c in self._seg_matching_comps(band, network_data, matrix):
            if c and c not in seen:
                items.append((c, False))
                seen.add(c)
        for c in self._shunt_leg_parts(band):
            if c and c not in seen:
                items.append((c, False))
                seen.add(c)
        ordered = list(self._net_work_class.get(band, []))
        idx = {n: i for i, n in enumerate(ordered)}

        def _k(item):
            comp = item[0]
            ks = [
                idx[_ni[0]]
                for _ni in self._signal_infos_raw
                if len(_ni) >= 2 and isinstance(_ni[1], list)
                and comp in _ni[1] and _ni[0] in idx
            ]
            return (sum(ks) / len(ks)) if ks else float("inf")

        items.sort(key=_k)
        return items

    def _comps_along(self, nets, skip):
        """这些网上的匹配件和通路器件，按网序从天线走到芯片。"""
        idx = {n: i for i, n in enumerate(nets or [])}
        keys = {}
        sources = [self.signal_infos, getattr(self, "_signal_infos_raw", None) or []]
        for src in sources:
            for ni in src or []:
                if len(ni) >= 2 and isinstance(ni[1], list) and ni[0] in idx:
                    for comp in ni[1]:
                        if comp and comp not in skip:
                            keys.setdefault(comp, []).append(idx[ni[0]])
        items = []
        ant = getattr(self, "_ant_num", None)
        for comp, ks in keys.items():
            if comp == ant or comp in skip:
                continue
            if self._skip_series_nc(comp):
                continue
            if is_connector_ref(comp) or is_pa_lna_ref(comp, self):
                continue
            is_ic = not _looks_like_matching_rlc(comp, self)
            items.append((sum(ks) / len(ks), comp, is_ic))
        items.sort()
        out = [(comp, is_ic) for _k, comp, is_ic in items]
        seen = {comp for comp, _ic in out}
        legs = series_shunt_to_ground(
            getattr(self, "_signal_infos_raw", None) or [],
            list(nets or []),
            lambda comp: _looks_like_matching_rlc(comp, self),
            self._is_gnd_net_name,
        )
        for comp in legs:
            if comp in seen or comp in skip:
                continue
            seen.add(comp)
            out.append((comp, False))
        return out

    def _end_label(self, band, nets):
        """芯片侧 Term 网标：该侧最后一张网上的非无源口。"""
        if not nets:
            return None
        target = nets[-1]
        fallback = target
        for raw in (self._pcb_by_band.get(band) or {}).get("raw_ports") or []:
            nn, comp, _pin = self._split_raw(raw)
            if nn != target:
                continue
            fallback = self._norm_net(raw)
            if not _looks_like_matching_rlc(comp, self):
                return fallback
        return fallback

    def _build_trx_schematic(self, band, tree, network_data, matrix,
                             all_pcb_port_name_list, all_sxp_files_component,
                             is_root):
        """TX + RX + 分出这两掷之前的公共端，画在一页。

        三只 Term：右边 term2 是公共端前面，左边 term1 是 TX、term3 是 RX。
        非根段把上一段 symbol 嵌进来（如 B20_TRX 嵌 ANT1_TRX0_LB_COUPLE）。
        两个 S 参数仿真各扫自己的频段，VSWR 公式名不重复。
        """
        info = (getattr(self, "_seg_meta", None) or {}).get("trx", {}).get(band) or {}
        cell = self._seg_cell(band, is_root)
        self._cur_cell = cell
        design = db_uu.create_schematic(name=(self.library_name, cell, "schematic"))
        design.clear_design()
        ant = self._ant_num
        y_tx, y_rx = 0.0, -6.0
        snp_tag = str(band).replace("-", "_").replace("/", "_")
        fork = info.get("fork") or (None, None)
        fork_ref = fork[0] if fork else None
        skip = {c for c in (fork_ref, ant) if c}
        common = self._comps_along(info.get("common_nets"), skip)
        tx_chain = self._comps_along(info.get("tx_nets"), skip)
        rx_chain = self._comps_along(info.get("rx_nets"), skip)
        tx_chip = self._end_label(band, info.get("tx_nets"))
        rx_chip = self._end_label(band, info.get("rx_nets"))

        def _place_row(comps, y, x):
            seg_nets = list(self._net_work_class.get(band, []))
            for comp, is_ic in comps:
                x = step_left(x, "ic" if is_ic else "ge")
                if is_ic:
                    self.process_ic_component(
                        design, comp, (x, y), all_sxp_files_component,
                        [p for p in all_pcb_port_name_list if comp in p],
                        seg_nets=seg_nets)
                else:
                    self.process_ge_component(design, comp, (x, y))
            return x

        # 右边一只 term2：公共端前面（天线侧）。TX/RX 不再各放一只天线 Term。
        x = 0.0
        self._add_term_with_label(design, x, y_tx, "term2", ant, num=2)
        create_pin(design, ant, (x, y_tx), 0.0)
        upward = None
        if not is_root:
            parent = tree[band]["parent"]
            parent_is_root = tree[parent]["parent"] is None
            parent_cell = self._seg_cell(parent, parent_is_root)
            x = step_left(x, "gap")
            self._embed_parent_symbol(design, parent_cell, (x, y_tx))
            x = step_left(x, "ic")
            if nested_uses_divider(tree[band]):
                upward, up_pin = tree[band]["bfilter"]
                self.process_ic_component(
                    design, upward, (x, y_tx), all_sxp_files_component,
                    [p for p in all_pcb_port_name_list if upward in p],
                    keep_pin=up_pin,
                    seg_nets=(list(self._net_work_class.get(band, []))
                              + list(self._net_work_class.get(parent, []))))
                x = step_left(x, "ic")
        x = _place_row(common, y_tx, x)
        x = step_left(x, "gap")
        pcb = self._pcb_by_band.get(band)
        if pcb:
            add_snp_component_ds(
                design=design, snp_name=f"PCB_{snp_tag}",
                position=(x, (y_tx + y_rx) / 2 - SEG_PCB_DY), file_path=pcb["file"],
                port_name_list=pcb["ports"])
        if fork_ref and fork_ref != upward:
            x = step_left(x, "ic")
            self.process_ic_component(
                design, fork_ref, (x, y_tx), all_sxp_files_component,
                [p for p in all_pcb_port_name_list if fork_ref in p],
                seg_nets=list(self._net_work_class.get(band, [])))
        x_tx = _place_row(tx_chain, y_tx, x)
        x_rx = _place_row(rx_chain, y_rx, x)
        x_tx = step_left(x_tx, "gap")
        x_rx = step_left(x_rx, "gap")
        self._add_term_with_label(design, x_tx, y_tx, "term1", tx_chip, num=1)
        create_pin(design, tx_chip, (x_tx, y_tx), 180.0)
        self._add_term_with_label(design, x_rx, y_rx, "term3", rx_chip, num=3)
        create_pin(design, rx_chip, (x_rx, y_rx), 180.0)

        def _mhz(pair):
            if not pair:
                return {}
            return {"start": f"{pair[0]:.0f}MHz", "stop": f"{pair[1]:.0f}MHz"}

        add_vswr_equation(design, (-4, y_tx + 3), name=f"VSWR_TX_{snp_tag}", s_param="S11")
        add_scattering_parameter_component(
            design, (2, y_tx + 3), name=f"SP_TX_{snp_tag}", **_mhz(info.get("tx_freq")))
        add_vswr_equation(design, (-4, y_rx - 3), name=f"VSWR_RX_{snp_tag}", s_param="S33")
        add_scattering_parameter_component(
            design, (2, y_rx - 3), name=f"SP_RX_{snp_tag}", **_mhz(info.get("rx_freq")))
        design.save_design()
        self._designs_by_cell[cell] = design
        print(ConsoleColor.warning_text(f"段 schematic 生成: {cell} (TRX)"))
        return design

    def _build_segment_schematic(self, band, tree, network_data, matrix,
                                 all_pcb_port_name_list, all_sxp_files_component,
                                 is_root, end_nets):
        """造段 schematic（支持整棵树递归，四种组合）。
        上游起点：根→ANTx term/pin；非根→term1+嵌父 symbol+分界器件。
        中段：匹配件 + 本段 PCB。
        下游终点：有子段(非叶)→每子段一个结尾 term/pin（供下游嵌本段 symbol 并网）；
                  叶段→芯片端 IC(若有)+term1。VSWR/SP 浮上方。
        天线侧(右)=term2 Num=2；芯片侧(左)=term1 Num=1。VSWR 看 PA/LNA 侧，用 S11。"""
        cell = self._seg_cell(band, is_root)
        self._cur_cell = cell   # 供 process_ic_component 在缺 S 参数时报出受影响的 schematic
        design = db_uu.create_schematic(name=(self.library_name, cell, "schematic"))
        design.clear_design()
        ant = self._ant_num
        row = 0.0
        x = 0.0
        snp_tag = str(band).replace("-", "_").replace("/", "_")
        has_children = bool(end_nets)
        self._seg_pos = {}   # 本段各器件横坐标 {器件: x}，供 NC 器件与同网有源件对齐

        # ── 上游起点 ──
        chip_ic = None
        if is_root:
            # 开头 ANTx：天线侧(右)=term2 Num=2 + pin(=ANTx)
            self._add_term_with_label(design, x, row, "term2", ant, num=2)
            create_pin(design, ant, (x, row), 0.0)
            _seg_pn = list(self._net_work_class.get(band, []))
            for comp, _is_ic, _is_nc in self._chain_with_nc(
                    self._seg_topo_chain(band, network_data, matrix), band):
                x = step_left(x, "ic" if _is_ic else "ge")
                if _is_ic:
                    self.process_ic_component(
                        design, comp, (x, row), all_sxp_files_component,
                        [p for p in all_pcb_port_name_list if comp in p],
                        seg_nets=_seg_pn)
                else:
                    self.process_ge_component(
                        design, comp, (x, row), deactivate=_is_nc)
            x = step_left(x, "gap")
        else:
            parent = tree[band]["parent"]
            parent_is_root = tree[parent]["parent"] is None
            parent_cell = self._seg_cell(parent, parent_is_root)
            # 天线侧源端(右)=term2 Num=2，网=ANTx；点5：天线侧也必须贴 pin(=ANTx)，
            # 否则本段(支线)生成的 symbol 缺天线那只脚，下游嵌入时接不上。
            self._add_term_with_label(design, x, row, "term2", ant, num=2)
            create_pin(design, ant, (x, row), 0.0)
            x = step_left(x, "gap")
            self._embed_parent_symbol(design, parent_cell, (x, row))
            x = step_left(x, "ic")
            if nested_uses_divider(tree[band]):
                divider, _child_pin = tree[band]["bfilter"]
                # 分界器件（本段开头），靠归一网标接父 symbol 结尾引脚。
                # 点4：只放通向本段的那条路径 SnP（keep_pin=本段在该器件上的 pin）。
                # seg_nets 含本段∪父段：分界器件的进端口连父段网(公共端侧)、出端口连本段网，
                # 两者都要贴；兄弟段的端口(网∈兄弟段)才滤掉。否则进端口(如 FIL3101_7)会被误删。
                self.process_ic_component(
                    design, divider, (x, row), all_sxp_files_component,
                    [p for p in all_pcb_port_name_list if divider in p],
                    keep_pin=_child_pin,
                    seg_nets=(list(self._net_work_class.get(band, []))
                              + list(self._net_work_class.get(parent, []))))
                x = step_left(x, "ic")
            else:
                print(ConsoleColor.warning_text(
                    f"⚠ 段 {band} 无分界器件，跳过分界 IC，继续生成 schematic"))

        # ── 中段：本段 PCB ──
        pcb = self._pcb_by_band.get(band)
        if pcb:
            add_snp_component_ds(
                design=design, snp_name=f"PCB_{snp_tag}",
                position=(x, row - SEG_PCB_DY), file_path=pcb["file"],
                port_name_list=pcb["ports"])
        chip_ic = None
        if not is_root:
            # 非根中段：段内全部器件(无源+IC)按网序摆，IC 走 ic、无源走 ge、NC 内联插入。
            # 叶段把芯片端 IC 拎出留到末端摆+term1；非叶段中段含全部 IC（解决耦合器段多 IC 漏件）。
            _middle, chip_ic = self._seg_middle_and_chip(band)
            _seg_pn = (list(self._net_work_class.get(band, []))
                       + list(self._net_work_class.get(parent, [])))
            for comp, _is_ic, _is_nc in self._chain_with_nc(_middle, band):
                x = step_left(x, "ic" if _is_ic else "ge")
                if _is_ic:
                    self.process_ic_component(
                        design, comp, (x, row), all_sxp_files_component,
                        [p for p in all_pcb_port_name_list if comp in p],
                        seg_nets=_seg_pn)
                else:
                    self.process_ge_component(design, comp, (x, row), deactivate=_is_nc)

        # ── 下游终点 ──
        x = step_left(x, "gap")
        if has_children:
            # 非叶段：每子段一个结尾 term/pin（下游嵌本段 symbol 时按网标并网）
            for i, en in enumerate(end_nets):
                # 芯片/子段侧在左，Num=1,3,4…（2 留给右边天线）
                n = 1 if i == 0 else i + 2
                self._add_term_with_label(design, x, row, f"T_{en}", en, num=n)
                create_pin(design, en, (x, row), 180.0)
        else:
            # 叶段：芯片端 IC（如 U2301/U2900）摆成 SnP 节点 + 芯片侧 term2
            if (not is_root) and chip_ic:
                self.process_ic_component(
                    design, chip_ic, (x, row), all_sxp_files_component,
                    [p for p in all_pcb_port_name_list if chip_ic in p],
                    seg_nets=self._net_work_class.get(band, []))
                x = step_left(x, "ic")
            # 叶段芯片侧(左)=term1 Num=1 + pin
            _chip = self._seg_chip_label(band, tree)
            self._add_term_with_label(design, x, row, "term1", _chip, num=1)
            create_pin(design, _chip, (x, row), 180.0)

        _mid = x / 2
        add_vswr_equation(design, (_mid, row + 6), name=f"VSWR_{snp_tag}",
                          s_param="S11")
        _fr = self._seg_freq_mhz(band)
        _sp_kw = ({"start": f"{_fr[0]:.0f}MHz", "stop": f"{_fr[1]:.0f}MHz"}
                  if _fr else {})
        add_scattering_parameter_component(
            design, (_mid + 5, row + 6), name=f"SP_{snp_tag}", **_sp_kw)
        design.save_design()
        self._designs_by_cell[cell] = design
        print(ConsoleColor.warning_text(f"段 schematic 生成: {cell}"))
        return design

    def _render_segmented(self, net_work_class, network_data, processed_matrix,
                          all_pcb_port_name_list, all_sxp_files_component):
        """分端分层渲染：递归渲染整棵分段树的所有段。
        每个非叶段(有子段)结尾按子段开一个 term/pin 并生成 symbol；下游子段嵌父段 symbol。
        先序遍历(父先于子)保证子段嵌入时父 symbol 已生成。"""
        tree = self._build_segment_tree(net_work_class)
        self._net_work_class = net_work_class   # 段→网络，供 NC 器件归段
        self._seg_tree = tree                   # 段父子树，供中段器件收集排除分界器件
        self._nc_placed = set()                 # 本天线已摆放的 NC 器件，避免跨段重复
        roots = [b for b, i in tree.items() if i["parent"] is None]
        if not roots:
            print("⚠ 分端渲染：未找到根段，跳过")
            return

        # 段 cell 排序：先序遍历，每个节点先列叶子子段、再下探干路(有子段)子段
        # → COM→UHB→下一层COM(MHB)→下一层分支(B40,B1_3)→…，与工作习惯一致。
        def _ordered_children(band):
            kids = tree[band]["children"]
            return ([c for c in kids if not tree[c]["children"]]
                    + [c for c in kids if tree[c]["children"]])

        order = []
        def _walk(band):
            order.append(band)
            for c in _ordered_children(band):
                _walk(c)
        _walk(roots[0])

        # 预先登记带前置序号的 cell 名（全局累加，跨天线保持 ANT1→ANT2→… 顺序）。
        # 功能名清理(点1/点3)：去尾部 _ANTx；段名以 _SAW 结尾时——叶段(SAW 一进一出)去掉
        # _SAW，干路段(在此遇一进多出分叉)把 _SAW 改成 _COM。仅改显示名，.sNp 查找仍用原段 id。
        def _clean_fn(b):
            fn = re.sub(r'_ANT\d+$', '', str(b))
            if fn.endswith('_SAW'):
                fn = fn[:-4] + ('_COM' if tree[b]['children'] else '')
            return fn
        self._seg_cell_names = {}
        for b in order:
            self._cell_seq += 1
            fn = _clean_fn(b)
            trx_pages = (getattr(self, "_seg_meta", None) or {}).get("trx") or {}
            if b == roots[0] and b not in trx_pages and not str(fn).upper().endswith("TRX"):
                fn = "COM"
            self._seg_cell_names[b] = f"{self._cell_seq:03d}_{self._ant_num}_{fn}"

        def _end_nets(band):
            ports = []
            for raw in self._pcb_by_band.get(band, {}).get("raw_ports", []):
                _, comp, _ = self._split_raw(raw)
                ports.append((comp, self._norm_net(raw)))
            return parent_end_nets(
                (tree[c].get("bfilter") for c in tree[band]["children"]),
                ports)

        def _render(band, is_root):
            trx_pages = (getattr(self, "_seg_meta", None) or {}).get("trx") or {}
            try:
                if band in trx_pages:
                    sch = self._build_trx_schematic(
                        band, tree, network_data, processed_matrix,
                        all_pcb_port_name_list, all_sxp_files_component,
                        is_root=is_root)
                else:
                    sch = self._build_segment_schematic(
                        band, tree, network_data, processed_matrix, all_pcb_port_name_list,
                        all_sxp_files_component, is_root=is_root, end_nets=_end_nets(band))
            except Exception as _seg_err:
                import traceback
                print(ConsoleColor.warning_text(
                    f"⚠ 段 {band} 生成失败，继续其余段: {_seg_err}\n{traceback.format_exc()}"))
                return
            if tree[band]["children"]:
                # 非叶段需生成 symbol，供其各子段下游嵌入
                self._build_segment_symbol(self._seg_cell(band, is_root), sch)
            for c in _ordered_children(band):
                _render(c, is_root=False)

        _render(roots[0], is_root=True)


class ConsoleColor:
    WARNING = '\033[33m'
    RESET = '\033[0m'

    @staticmethod
    def warning_text(text: str) -> str:
        return f"{ConsoleColor.WARNING}{text}{ConsoleColor.RESET}"


@contextmanager
def create_and_open_an_empty_workspace(workspace_path: str) -> Iterator[de.Workspace]:
    """创建并打开一个空的 ADS 工作空间。已有工程不删，改写到 _v2/_v3。"""
    if de.workspace_is_open():
        de.close_workspace()
        print(ConsoleColor.warning_text(
            f"Closing existing workspace before creating: {workspace_path}"))

    requested = pathlib.Path(workspace_path)
    workspace_directory = next_ads_workspace_path(requested)
    if workspace_directory.exists():
        shutil.rmtree(workspace_directory)
    if workspace_directory.resolve() != requested.resolve():
        print(
            f"已有 ADS 工程，未覆盖 {requested.name}。本次写到: {workspace_directory.name}"
        )

    workspace = de.create_workspace(workspace_directory)
    original_working_directory = pathlib.Path(os.getcwd())
    workspace.open()
    print(ConsoleColor.warning_text(
        f"Workspace {workspace_path} is created and opened."))

    try:
        yield workspace
    finally:
        os.chdir(original_working_directory)


def create_library(workspace: de.Workspace, library_name: str) -> None:
    """在工作空间内创建库"""
    library_path = workspace.path / library_name
    de.create_new_library(library_name, library_path)
    workspace.add_library(library_name, library_path, de.LibraryMode.SHARED)
    print(ConsoleColor.warning_text(
        f"Library '{library_name}' has been added to the workspace."))


def add_port(design, position, name, angle=-90, num=None):
    """仿真口：TermG（等效 Term+接地），不再另放 GROUND。"""
    inst = design.add_instance(db_uu.LCVName("ads_simulation", "TermG", "symbol"), tuple(
        position), name=name, angle=angle)
    if num is not None:
        inst.parameters["Num"].value = str(num)
    return inst


def create_pin(design, net_name, loc, angle=0.0):
    """在 design 的指定坐标放一个带网标的引脚（Term + PinFig 绑成 Pin）。
    分端分层用：在子电路 schematic 的开头/结尾 term 网上放 Pin → 该 cell 的 symbol
    即暴露同名引脚，下游 schematic 实例化后靠该网标级联连通。net_name 即引脚网标。"""
    net_name = ads_net_label(net_name)
    if not net_name:
        return None
    net = design.find_or_add_net(net_name)
    term = design.add_term(net, net_name, db_uu.TermType.INPUT_OUTPUT)
    pin_fig = design.add_pin_fig_for_term_type(db_uu.TermType.INPUT_OUTPUT, tuple(loc))
    return design.add_pin(term, pin_fig, angle=angle)


def add_rectangle(design,  layer_id=1, ll_or_box=(0.0, 0.0), ur=(10.0, 5.0)):
    """添加线框"""
    layer_id = design.create_layer_id("cond")
    design.add_rectangle(layer_id, ll_or_box, ur)


def add_ground(design, position):
    """添加地（GROUND）"""
    design.add_instance(db_uu.LCVName("ads_rflib", "GROUND",
                        "symbol"), position, name="", angle=-90)


def add_resistor(design, position, name, value, angle=0):
    """添加电阻"""
    r = design.add_instance(db_uu.LCVName(
        "ads_rflib", "R", "symbol"), position, name=name, angle=angle)
    r.parameters["R"].value = value
    return r


def add_capacitor(design, position, name, value, angle=-90):
    """添加电容"""
    c = design.add_instance(db_uu.LCVName(
        "ads_rflib", "C", "symbol"), position, name=name, angle=angle)
    c.parameters["C"].value = value
    return c


def add_inductor(design, position, name, value, angle=0):
    """添加电感"""
    l = design.add_instance(db_uu.LCVName(
        "ads_rflib", "L", "symbol"), position, name=name, angle=angle)
    l.parameters["L"].value = value
    return l


def add_wire(design, points):
    """添加导线"""
    return design.add_wire(points)


def add_wire_label(label, d_line):
    """添加导线网标。原理图里的 / 等 ADS 非法字符改成下划线。"""
    label = ads_net_label(label)
    if not label:
        return
    d_line.add_wire_label(label)


def add_scattering_parameter_component(design, position, name="SP1", start="791 MHz", stop="821 MHz", step="1 MHz"):
    """添加 S 参数仿真控制器"""
    # design.add_instance(db_uu.LCVName("ads_simulation", "S_Param", "symbol"), position, name=name)
    s_param = design.add_instance(db_uu.LCVName(
        "ads_simulation", "S_Param", "symbol"), position, name=name)
    # 设置 S 参数仿真器的起始、终止频率和步长
    s_param.parameters["Start"].value = start
    s_param.parameters["Stop"].value = stop
    s_param.parameters["Step"].value = step

    print(ConsoleColor.warning_text(
        f"S 参数仿真器 {name} 设置为：{start} - {stop}, 步长 {step}"))


def add_snp_component(design: db_uu.Design, position: tuple, file_path: str, num_ports: int = 2):
    def extract_port_num(file_path: str) -> int:
        """
        从 .sNp 文件名中提取端口数 (N)

        :param file_path: 文件完整路径
        :return: 端口数 (N)
        """
        # 获取文件名（去掉路径）
        file_name = os.path.basename(file_path).upper()  # 转大写，避免 s4p / S4P 影响

        # 使用正则匹配 SnP 格式（例：S2P, S4P, S8P）
        match = re.search(r'S(\d+)P', file_name)

        if match:
            return int(match.group(1))  # 提取数字部分
        else:
            raise ValueError("未找到有效的 SnP 文件格式")
    """
    在 ADS 设计中添加 N-Port S-Parameter File（SnP 组件）

    :param design: ADS 设计对象
    :param position: (x, y) 坐标
    :param file_path: SNP 文件路径（完整路径）
    :param num_ports: 端口数量，默认 2
    """
    snp_instance = design.add_variable_to_var_instance(
        ("ads_datacmps", "SnP", "symbol"), position)
    # 设置 SnP 参数：端口数 & 文件路径
    # snp_instance.parameters["NumPorts"].value = str(extract_port_num(file_path))
    snp_instance.parameters["File"].value = file_path
    snp_instance.parameters["FileName"].value = file_path
    # 保存设计

    return snp_instance


def add_snp_component_ds(design: db_uu.Design, port_name_list, snp_name="", position=(-2, -2), file_path=r"F:\workspace\6-AUTO\test.s4p", wire_ports=None) -> None:
    # 从文件路径提取端口数（示例：test.s4p -> 4端口）
    match = re.search(r'\.s(\d+)p$', file_path, re.IGNORECASE)
    if not match:
        raise ValueError("无效的S参数文件名，请使用.sNp格式。")
    num_ports = match.group(1)

    # 默认动态 SnP 元件（外观与 GUI 手动放置一致）。
    # 引脚由 SnP 的 Pcell 宏（ads_datacmps/%Sn%P/symbol/artwork.ael 的
    # draw_snp_symbol）在 GUI 求值生成；headless 写入正确参数后，在 GUI
    # 中打开并 Apply（或运行 修复SNP引脚.py 批量刷新）即生效。
    snp_instance = design.add_instance(
        ("ads_datacmps", "SnP", "symbol"), tuple(position), name=snp_name)

    # 设置SNP组件参数
    snp_instance.parameters["File"].value = "\"" + \
        file_path+"\""    # 设置S参数文件路径
    snp_instance.parameters["NumPorts"].value = num_ports  # 设置端口数
    # 枚举类参数必须绑定 form（form_name 赋值，与 GUI 对话框写法一致），
    # 写裸字符串值会存成无 form 的串，Pcell 宏框架按 form 集合解析不到，
    # 宏 draw_snp_symbol 的入参（HasReferencePin/PinLayout/PinSpacing 等）
    # 收到 NULL，重求值在 snp_ael_artwork_macro_functions.ael line 699
    # 乘法报 NULL、控件消失。GUI 里"先点一下 File name 再 Apply 才成功"
    # 正是 file_name_changed 回调把参数规范成 form 绑定。
    # form 名见 ads_datacmps/%Sn%P/itemdef.ael。
    snp_instance.parameters["Type"].form_name = "TouchstoneType"          # Touchstone
    snp_instance.parameters["InterpDom"].form_name = "ID0"                # Data based
    snp_instance.parameters["ExtrapMode"].form_name = "EMode2"            # Constant extrapolation
    snp_instance.parameters["ExcludeUnterminatedPorts"].form_name = "NoAction"  # No
    snp_instance.parameters["PinLayout"].form_name = "Channel"
    snp_instance.parameters["PinSpacing"].form_name = "Loose"
    snp_instance.parameters["HasReferencePin"].form_name = "NoRefPin"

    # port_name_list 即 GUI"Display port names on symbol"的数据载体。
    # 真实格式（GUI 成功实例 detailed design information 实证）：
    #   "1 <最长名字字符数> 名字1 名字2 ... 名字N"
    # 首 token "1"=勾选显示/"0"=关；第二 token 是最长端口名长度（宏
    # snp_length_of_longest_name 用它做宽度计算，缺失时把名字当数字
    # 乘法报 NULL，line 699 炸、控件消失）。名字不能含空格。
    if isinstance(port_name_list, dict):
        _names = [str(port_name_list[k]).split(" ")[0]
                  for k in sorted(port_name_list)]
    else:
        _names = [str(v).split(" ")[0] for v in (port_name_list or [])]
    # Display=唯一 term 名（禁止空/`-`/重名，否则 Pcell already has a term）。
    # 通路筛选只作用在飘线网标。
    _display, _reg_names = snp_display_and_wire_names(_names, wire_ports)
    if _display:
        _longest = max(len(n) for n in _display)
        snp_instance.parameters["port_name_list"].value = (
            f"1 {_longest} " + " ".join(_display))
    try:
        _design_cell = design.cell_name
    except Exception:
        _design_cell = ""
    _SNP_WIRE_REGISTRY.setdefault(_design_cell, []).append(
        (snp_name, _reg_names))

    snp_instance.update_item_annotation()
    print()

# 使用示例


def add_snp_component_ds_fixed(
    design: db_uu.Design,
    snp_name: str = "",
    position: tuple = (-2, -2),
    file_path: str = r"F:\workspace\6-AUTO\test.s4p"
) -> Optional[db_uu.Instance]:
    """
    在ADS设计中添加SnP组件，并设置相关参数。

    参数:
        design (db_uu.Design): 当前设计对象。
        snp_name (str): SnP组件的实例名称，默认为空。
        position (tuple): SnP组件的放置位置，默认为(-2, -2)。
        file_path (str): S参数文件的路径，默认为"F:\\workspace\\6-AUTO\\test.s4p"。

    返回:
        db_uu.Instance: 添加的SnP组件实例。如果失败，返回None。

    异常:
        ValueError: 如果文件路径无效或S参数文件名格式不正确。
    """
    # 从文件路径提取端口数（示例：test.s4p -> 4端口）
    match = re.search(r'\.s(\d+)p$', file_path, re.IGNORECASE)
    if not match:
        raise ValueError("无效的S参数文件名，请使用.sNp格式（如test.s4p）。")
    num_ports = match.group(1)

    try:
        # 添加SnP组件实例
        snp_instance = design.add_instance(
            ("ads_datacmps", "SnP", "symbol"), position, name=snp_name)

        # 设置SnP组件参数
        snp_instance.parameters["File"].value = f'"{file_path}"'  # 设置S参数文件路径
        snp_instance.parameters["NumPorts"].value = num_ports  # 设置端口数
        # 设置文件类型为Touchstone
        snp_instance.parameters["Type"].value = "Touchstone"
        snp_instance.parameters["InterpDom"].value = "Data based"  # 设置插值域为基于数据
        # 设置外推模式为常数外推
        snp_instance.parameters["ExtrapMode"].value = "Constant extrapolation"
        # 不排除未终止端口
        snp_instance.parameters["ExcludeUnterminatedPorts"].value = "No"

        # 设置端口名称列表（根据端口数动态生成）
        port_names = [f"P{i+1}" for i in range(int(num_ports))]
        snp_instance.parameters["port_name_list"].value = f'[{",".join(port_names)}]'

        # 更新组件注释
        snp_instance.update_item_annotation()

        print(f"SnP组件 '{snp_name}' 添加成功，文件路径: {file_path}，端口数: {num_ports}。")
        return snp_instance

    except Exception as e:
        print(f"添加SnP组件失败: {e}")
        return None


def add_vswr_equation(design, position, name="VSWR1", s_param="S11"):
    """
    在 ADS 设计中添加 VSWR 计算公式
    :param design: ADS 设计对象
    :param position: 组件的放置坐标 (x, y)
    :param name: 公式名称
    :param s_param: 计算 VSWR 的 S 参数 (默认 "S11")
    """
    # 默认公式恒为 VSWR1=vswr(S11)。同一页放两只（TRX 的 TX/RX）会重定义 VSWR1。
    # Function 是可重复参数，整串赋给 .value 会按字符去对齐，ADS 直接报 zip 长度不对。
    vswr_eq = design.add_instance(db_uu.LCVName("ads_simulation", "VSWR", "symbol"),
                                  position, name=name)
    expr = str(s_param or "S11").strip() or "S11"
    _set_vswr_function(vswr_eq, f"{name}=vswr({expr})")

    return vswr_eq  # 返回实例，方便后续修改


def _set_vswr_function(inst, equation: str) -> None:
    """写 VSWR 的 Function。它是重复参数，只能改第一条，不能把整串赋给 .value。"""
    param = inst.parameters["Function"]
    repeats = getattr(param, "repeats", None)
    if repeats is not None and len(repeats) >= 1:
        item = repeats[0]
        subs = getattr(item, "sub_params", None)
        if subs is not None and len(subs) >= 1:
            subs[0].value = equation
            return
        item.value = equation
        return
    param.value = [equation]
