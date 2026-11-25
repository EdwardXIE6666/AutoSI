
from pathlib import Path
import os
import re
from app.LogTool import *
from rf.segments import RF_SWEEP_MIN_MHZ, clamp_rf_sweep_mhz, clamp_rf_sweep_start_hz


_FREQ_SPAN = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-~～至到]+\s*(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz|GHZ|MHZ|KHZ)",
    re.I,
)


def snp_name_matches_model(file_name, model) -> bool:
    """型号是否出现在 S 参数文件名里。空型号不得命中。只认文件名，不认文件夹。"""
    model = str(model or "").strip().lower()
    if not model:
        return False
    file_l = str(file_name or "").lower()
    if not re.search(r"\.s\d+p$", file_l):
        return False
    return model in file_l


def list_snp_filename_mismatches(snp_dir, model) -> list[tuple[Path, list[str]]]:
    """文件夹名含型号、但其中 .sNp 文件名都不含型号（库文件名笔误一类）。"""
    model = str(model or "").strip().lower()
    hits: list[tuple[Path, list[str]]] = []
    if not model:
        return hits
    root = Path(snp_dir)
    if not root.is_dir():
        return hits
    for dirpath, _, files in os.walk(root):
        parent = Path(dirpath).name.lower()
        if model not in parent:
            continue
        snps = [f for f in files if re.search(r"\.s\d+p$", f, re.I)]
        if not snps:
            continue
        if any(model in f.lower() for f in snps):
            continue
        hits.append((Path(dirpath), sorted(snps)))
    return hits


def pick_canonical_snp_file(paths, model=""):
    """同一型号多份文件时留一份：NB 优先，其次父目录/文件名含型号。

    开关多状态、Hch/Lch 双通道不走这里。TEMP 副本和正式目录各一份 s5p
    时，构图按文件数摆 DPX_1/DPX_2，物理上仍是一颗器件。
    """
    files = [p for p in (paths or ()) if p]
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    model_l = str(model or "").lower()

    def rank(path):
        p = Path(path)
        stem = p.stem.lower()
        parent = p.parent.name.lower()
        nb = 0 if "_nb_" in stem else 1
        in_parent = 0 if model_l and model_l in parent else 1
        in_stem = 0 if model_l and model_l in stem else 1
        return (nb, in_parent, in_stem, len(stem), str(p).replace("\\", "/").lower())

    return min(files, key=rank)


def parse_mhz_span(text):
    """从描述/注释里抽出 MHz 并集。必须带单位，避免把插损 dB 当频率。"""
    if not text:
        return None
    los, his = [], []
    for match in _FREQ_SPAN.finditer(str(text)):
        lo_raw, hi_raw, unit = float(match.group(1)), float(match.group(2)), match.group(3).upper()
        if unit.startswith("G"):
            mul = 1000.0
        elif unit.startswith("K"):
            mul = 0.001
        else:
            mul = 1.0
        lo_mhz, hi_mhz = lo_raw * mul, hi_raw * mul
        if lo_mhz > hi_mhz:
            lo_mhz, hi_mhz = hi_mhz, lo_mhz
        los.append(int(round(lo_mhz)))
        his.append(int(round(hi_mhz)))
    if not los:
        return None
    return min(los), max(his)


def pin_io_role(pin_name) -> str:
    """RF 脚名分成两侧。pole 是 OUT/COM/ANT，throw 是 IN/RF，其余 other。

    开关只能 pole↔throw。IN1 不能穿到 IN2，OUT1 也不能穿到 OUT2。
    """
    name = str(pin_name or "").upper()
    token = name.split("_", 1)[1] if "_" in name else name
    if re.match(r"^(OUT|RFOUT|RFO)\d*$", token) or token in {
            "COM", "ANT", "COMMON"}:
        return "pole"
    if re.match(r"^(IN|RFIN|RFI)\d*$", token):
        return "throw"
    if re.match(r"^RF\d+$", token):
        return "throw"
    return "other"


class SNPFileInfo:
    SNP_DIRECTORY = "SNP"
    _instance_cache = {}
    _ref_des_map = {}  # RefDes → 实例

    @classmethod
    def batch_initialize(cls, s_param_components):
        """批量初始化 SNP 器件，返回 (success_dict, failed_dict)。"""
        success_dict = {}
        failed_dict = {}

        for part_no, comp_data in s_param_components.items():
            device_type = comp_data["type"]
            external_model = comp_data["external_model"]
            ref_des = comp_data["components"]
            try:
                snp_instance = cls(
                    device_type,
                    external_model,
                    ref_des=ref_des,
                    part_desc=comp_data.get("part_desc") or "",
                )
                if snp_instance.state:
                    success_dict[part_no] = snp_instance
                elif getattr(snp_instance, "_filename_mismatch", False):
                    continue
                else:
                    failed_dict[part_no] = {
                        "error": f"找不到SNP文件: {external_model}",
                        "external_model": external_model,
                        "components": comp_data["components"],
                        "type": device_type
                    }
            except Exception as e:
                failed_dict[part_no] = {
                    "error": f"初始化异常: {str(e)}",
                    "components": comp_data["components"]
                }
        cls._print_failed_components(failed_dict)
        return success_dict, failed_dict

    def __new__(cls, device_type, external_model, file_path=None, ref_des=None, part_desc=None):
        """按外部型号缓存实例，避免重复解析同一 SNP。"""
        key = external_model.lower()

        if key in cls._instance_cache:
            instance = cls._instance_cache[key]
            if ref_des:
                instance._add_ref_des(ref_des)
            return instance

        instance = super().__new__(cls)
        cls._instance_cache[key] = instance
        return instance

    def __init__(self, device_type, external_model, file_path=None, ref_des=None, part_desc=None):
        """初始化 SNP 解析器；已缓存实例只补 RefDes。"""
        if hasattr(self, 'state'):
            if ref_des:
                self._add_ref_des(ref_des)
            if self.state:
                self.apply_missing_port_frequency(part_desc or "")
            return

        self.device_type = device_type
        self.external_model = external_model
        self.ref_des_list = []
        self.refs = []
        self.file_path = self._resolve_file_path(file_path)
        self._initialize_properties()

        if isinstance(self.file_path, list) and len(self.file_path) > 1:
            # 第一优先：开关状态端口检测
            _sigs = [tuple(self._extract_active_pairs(p)) for p in self.file_path]
            _is_switch_states = all(_sigs)
            # 双工/双通道滤波器常拆成 Hch/Lch 两个 s2p，端口需合并而非互相覆盖
            _names_l = [str(p).lower() for p in self.file_path]
            _is_dual_channel = (
                any(("hch" in n or re.search(r'[^a-z]hb[^a-z]', n) or "_h_" in n)
                    for n in _names_l)
                and any(("lch" in n or re.search(r'[^a-z]lb[^a-z]', n) or "_l_" in n)
                        for n in _names_l)
            )
            if _is_switch_states:
                # 开关多状态文件，属正常情况；同一状态若有 NB/WB，优先选 NB
                from collections import defaultdict as _dd
                _sig_groups = _dd(list)
                for p, sig in zip(self.file_path, _sigs):
                    _sig_groups[sig].append(p)
                _selected = []
                for files in _sig_groups.values():
                    _nb = [f for f in files if '_nb_' in str(f).lower()]
                    _selected.append(_nb[0] if _nb else files[0])
                self.file_path = _selected if len(_selected) > 1 else _selected[0]
            elif _is_dual_channel:
                # 保留 Hch+Lch 全部文件，解析阶段合并 COM/HB/LB 端口
                pass
            else:
                # 非开关、非双通道：多份是库重复，只留一份，避免构图摆成器件_1/_2
                _nb_files = [p for p in self.file_path if '_nb_' in str(p).lower()]
                pool = _nb_files or list(self.file_path)
                paths_str = '\n      '.join(str(p) for p in self.file_path)
                print(f"{LogStyle.WARNING}⚠️ {external_model} 找到 {len(self.file_path)} 个匹配文件，后加载的数据会覆盖前者{LogStyle.ENDC}")
                add_issue('WARNING',
                    f"[文件重复] {external_model} 在 S参数库中找到 {len(self.file_path)} 个同名文件\n"
                    f"   多个文件会互相覆盖，频率信息可能丢失，建议删除旧版本\n"
                    f"   找到的文件:\n      {paths_str}",
                    key=f"dup_snp_{external_model}"
                )
                self.file_path = pick_canonical_snp_file(pool, external_model)

        if ref_des:
            self._add_ref_des(ref_des)

        if self.file_path:
            try:
                self._parse_file()
                self._warn_if_nonstandard_port_format()
                self.apply_missing_port_frequency(part_desc or "")
                self._print_device_info()
                self.state = True
            except Exception as e:
                self._print_error(f"解析文件失败: {str(e)}")
                self.state = False
                print(
                    f"{LogStyle.ERROR}❌ 器件模型初始化失败: {external_model}{LogStyle.ENDC}")
        else:
            mismatches = list_snp_filename_mismatches(
                Path(self.SNP_DIRECTORY).absolute(), external_model
            )
            if mismatches:
                self._filename_mismatch = True
                self._print_filename_mismatch_reminder(mismatches)
            else:
                self._print_error(f"找不到对应文件！搜索条件: '{external_model}'")
            self.state = False

    def _add_ref_des(self, ref_des):
        if isinstance(ref_des, str):
            self._add_single_ref_des(ref_des)
        elif isinstance(ref_des, list):
            for rd in ref_des:
                self._add_single_ref_des(rd)

    def _add_single_ref_des(self, ref_des):
        if ref_des and ref_des not in self.ref_des_list:
            self.ref_des_list.append(ref_des)
            SNPFileInfo._ref_des_map[ref_des] = self

    @classmethod
    def get_instance_by_ref_des(cls, ref_des):
        """按位号取 SNPFileInfo 实例。"""
        return cls._ref_des_map.get(ref_des)

    def get_pin_band(self, pin_num):
        """获取指定引脚的频段信息"""
        if pin_num in self.pin_info:
            return self.pin_info[pin_num].get('frequency')
        return None

    @classmethod
    def find_snp_file(cls, search_string):
        """按型号查找 SNP 文件；优先文件名含 nol，否则回退并警告。"""

        search_string = str(search_string or "").strip().lower()
        matching_files = []
        snp_dir = Path(cls.SNP_DIRECTORY).absolute()
        if not search_string:
            return matching_files
        if not snp_dir.exists():
            print(f"{LogStyle.ERROR}❌ SNP目录不存在: {snp_dir}{LogStyle.ENDC}")
            return matching_files
        
        def _search(require_nol):
            found = []
            for root, _, files in os.walk(snp_dir):
                for file in files:
                    file_lower = file.lower()
                    if require_nol and "nol" not in file_lower:
                        continue
                    if snp_name_matches_model(file, search_string):
                        found.append((Path(root) / file).absolute())
            return found

        # 第一轮：严格要求含 nol
        matching_files = _search(require_nol=True)

        if not matching_files:
            # 第二轮：兼容读取——不要求 nol，但警告用户
            fallback_files = _search(require_nol=False)
            if fallback_files:
                print(f"{LogStyle.WARNING}⚠️ 未找到含 _nol 的文件，兼容读取无后缀版本: {search_string}{LogStyle.ENDC}")
                paths_str = '\n      '.join(str(p) for p in fallback_files)
                add_issue('WARNING',
                    f"[_nol 后缀缺失] {search_string}\n"
                    f"   已兼容读取不含 _nol 后缀的文件，但请确认该文件是否应为插值（nol）版本\n"
                    f"   建议将文件重命名为含 _nol 后缀的格式\n"
                    f"   实际读取:\n      {paths_str}",
                    key=f"no_nol_{search_string}"
                )
                matching_files = fallback_files
            else:
                if not list_snp_filename_mismatches(snp_dir, search_string):
                    print(f"{LogStyle.WARNING}⚠️ 未找到任何匹配文件: {search_string}{LogStyle.ENDC}")

        if len(matching_files) == 1:
            return matching_files[0]
        return matching_files

    def _print_failed_components(failed_dict):
        """打印未成功初始化的器件。"""

        if not failed_dict:
            print(f"{LogStyle.SUCCESS}✅ 所有器件型号均已成功初始化{LogStyle.ENDC}")
            return

        print(f"\n{LogStyle.ERROR}{'='*60}{LogStyle.ENDC}")
        print(
            f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 以下器件型号未初始化成功:{LogStyle.ENDC}")

        for part_no, failure_info in failed_dict.items():
            error_msg = failure_info.get("error", "未知错误")
            affected_components = failure_info.get("components", [])
            device_type = failure_info.get("type", "未知错误")
            print(f"{LogStyle.ERROR}❌ 型号: {part_no}{LogStyle.ENDC}")
            print(f"{LogStyle.ERROR}  └─ 错误: {error_msg}{LogStyle.ENDC}")
            print(f"{LogStyle.ERROR}  └─ 类型: {device_type}{LogStyle.ENDC}")
            if affected_components:
                print(f"{LogStyle.ERROR}  └─ 受影响的组件: {LogStyle.ENDC}")
                for i, comp in enumerate(affected_components):
                    prefix = "     ├─" if i < len(
                        affected_components) - 1 else "     └─"
                    print(f"{LogStyle.ERROR}  {prefix} {comp}{LogStyle.ENDC}")

            print(f"{LogStyle.ERROR}{'-'*60}{LogStyle.ENDC}")

        total_failed = len(failed_dict)
        print(f"{LogStyle.ERROR}❌ 总计 {total_failed} 个器件型号未初始化成功{LogStyle.ENDC}")
        print(f"{LogStyle.ERROR}{'='*60}{LogStyle.ENDC}")

    def _resolve_file_path(self, file_path):
        if file_path:
            return Path(file_path)
        return self.find_snp_file(self.external_model)

    def _initialize_properties(self):
        self.ports = []
        self.pin_info = {}
        self.pin_name_to_number = {}  # 引脚名称到引脚号的映射
        self._active_name_pairs = []  # 开关多状态文件名中的端口对 token
        self.switch_connections = {}  # {pin: set(connected_pins)} 开关允许穿越

    def _extract_active_pairs(self, file_path):
        """从文件名提取激活路径端口对列表。
        返回 List[Tuple[str,str]]，空列表表示无法解析（降级为原逻辑）。

        支持的命名规范：
          ANT_RF   : AW13504HDFLR / AW13524TQNR / S25114-51   → [('ANT','RF1')]
          OUT_IN   : AW12024TQNR                               → [('OUT1','IN1')]
          RF{a}-{b}: AW12022TQNR STATE 文件                    → [('RF2','RF4'),('RF1','RF3')]
          RFI_RFO  : MXD8546C                                  → [('RFI1','RFO1'),('RFI2','RFO2')]
          RF_RF    : MXD8539S                                   → [('RF1','RF2')]
        """
        stem = Path(file_path).stem.upper()
        pairs = []

        # ANT_RF 系列
        m = re.search(r'ANT[_\-]?(RF\d+)|(RF\d+)[_\-]?ANT', stem)
        if m:
            return [('ANT', m.group(1) or m.group(2))]

        # OUT_IN 系列（AW12024TQNR）
        m = re.search(r'(OUT\d+)[_\-](IN\d+)', stem)
        if m:
            return [(m.group(1), m.group(2))]

        # RF{a}-{b} 连接符（AW12022TQNR STATE 文件，可含多对）
        for m in re.finditer(r'RF(\d+)-(\d+)', stem):
            pairs.append((f'RF{m.group(1)}', f'RF{m.group(2)}'))
        if pairs:
            return pairs

        # RFI_RFO 对（MXD8546C，可含多对）
        for m in re.finditer(r'(RFI\d+)[_\-](RFO\d+)', stem):
            pairs.append((m.group(1), m.group(2)))
        if pairs:
            return pairs

        # RF_RF 直连格式（MXD8539S，放最后防止误匹配）
        m = re.search(r'_(RF\d+)[_\-](RF\d+)', stem)
        if m:
            return [(m.group(1), m.group(2))]

        return []

    @staticmethod
    def _pin_name_token(pin_name):
        """'3_OUT1' / 'OUT1' → 'OUT1'。"""
        name = str(pin_name or "").upper()
        if "_" in name:
            return name.split("_", 1)[1]
        return name

    def _find_pin_by_token(self, token):
        """用文件名/端口 token 反查引脚号。"""
        token = str(token or "").upper()
        if not token:
            return None
        for pin_num, data in self.pin_info.items():
            name = str(data.get("name", "")).upper()
            short = self._pin_name_token(name)
            if token == short or token == name or name.endswith("_" + token):
                return int(pin_num)
        return None

    @staticmethod
    def _pin_io_role(pin_name):
        """将 RF 端口分为 pole(OUT/COM 侧) / throw(IN/RF 侧) / other。"""
        return pin_io_role(pin_name)

    def _build_switch_connections(self):
        """从多状态 active pairs 或 OUT/IN 角色构建开关允许穿越图。"""
        from collections import defaultdict as _dd
        conn = _dd(set)

        for name_a, name_b in self._active_name_pairs or []:
            pin_a = self._find_pin_by_token(name_a)
            pin_b = self._find_pin_by_token(name_b)
            if pin_a is None or pin_b is None or pin_a == pin_b:
                continue
            conn[pin_a].add(pin_b)
            conn[pin_b].add(pin_a)

        if not conn and self.pin_info:
            poles, throws = [], []
            for pin_num, data in self.pin_info.items():
                role = self._pin_io_role(data.get("name", ""))
                if role == "pole":
                    poles.append(int(pin_num))
                elif role == "throw":
                    throws.append(int(pin_num))
            # 仅在两侧都明确时用角色补全（避免把普通滤波器全连通）
            if poles and throws:
                for pole in poles:
                    for throw in throws:
                        conn[pole].add(throw)
                        conn[throw].add(pole)

        self.switch_connections = {pin: set(dsts) for pin, dsts in conn.items()}

    def get_switch_connected_pins(self, pin):
        """返回开关从指定引脚允许走到的其它 RF 引脚集合。"""
        if not self.switch_connections:
            self._build_switch_connections()
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            return set()
        return set(self.switch_connections.get(pin, set()))

    def _parse_file(self):
        current_section = None
        file_paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
        try:
            self._active_name_pairs = []
            for file_path in file_paths:
                active_pairs = self._extract_active_pairs(file_path)
                if active_pairs:
                    self._active_name_pairs.extend(active_pairs)
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()

                        if not line or line.startswith('#'):
                            continue

                        if line.startswith('!'):
                            content = line[1:].strip()

                            if self._is_section_header(content):
                                current_section = self._detect_section(content)
                                continue

                            if all(char in '!*- ' for char in content):
                                continue

                            # Murata 等库常用 "! Port1 = 1_COM (...)" 而无 PORT DEFINITIONS 段头
                            is_port_assign = bool(re.match(
                                r"Port\[?\d+\]?\s*=", content, re.IGNORECASE))
                            if current_section == 'port' or is_port_assign:
                                self._process_port_line(content, file_path, active_pairs)
                            elif current_section == 'ref_matching':
                                self._process_ref_line(content)
                            continue
            self._deduplicate_ports()
            self._validate_pins()
            self._build_switch_connections()

        except Exception as e:
            self._print_error(f"解析文件失败: {str(e)}")

    def _validate_pins(self):
        """多引脚器件须有 COM/ANT；DP4T 等多极开关没有单一 COM，禁止把 pin1 误标 COM。"""
        pin_count = len(self.pin_info)

        if pin_count > 2:
            com_pins = []
            for pin_num, pin_data in self.pin_info.items():
                pin_name = pin_data['name'].upper()
                if 'COM' in pin_name or 'ANT' in pin_name or 'COMMON' in pin_name:
                    com_pins.append(pin_num)

            if not com_pins:
                # DP4T 等多极 OUT/IN 开关没有单一 COM；禁止把 pin1(IN1) 误标 COM
                roles = [
                    self._pin_io_role(data.get("name", ""))
                    for data in self.pin_info.values()
                ]
                multi_pole_switch = (
                    "pole" in roles and "throw" in roles
                    and sum(1 for r in roles if r == "pole") >= 1
                )
                if multi_pole_switch:
                    return
                if 1 in self.pin_info:
                    com_pins = [1]
                else:
                    raise ValueError("多引脚器件缺少明确的COM引脚")

            # 为COM引脚添加特殊标记
            for pin_num in com_pins:
                self.pin_info[pin_num]['is_common'] = True

    def _deduplicate_ports(self):
        """去重端口列表
        - file: 优先保留能唯一确定激活路径的文件（非空且最早出现）
        - candidate_files: 合并所有激活状态下的候选文件（供多路径开关 ADS 阶段精确选择）
        """
        best = {}
        for port in self.ports:
            key = port['pin_name']
            if key not in best:
                best[key] = dict(port)
                best[key]['candidate_files'] = list(port.get('candidate_files', []))
            else:
                if not best[key]['file'] and port['file']:
                    best[key]['file'] = port['file']
                for cf in port.get('candidate_files', []):
                    if cf not in best[key]['candidate_files']:
                        best[key]['candidate_files'].append(cf)
        self.ports = list(best.values())

    def _is_section_header(self, content):
        return "PORT DEFINITIONS" in content or "REF MATCHING" in content

    def _detect_section(self, content):
        if "PORT DEFINITIONS" in content:
            return 'port'
        elif "REF MATCHING" in content:
            return 'ref_matching'
        return None

    def _process_port_line(self, line, file_path, active_pairs=None):
        # 端口名允许含 '/' 等非空格字符（如双频端口 2_B3/B66TX），否则频率会被漏读
        # re.IGNORECASE: 兼容小写 'port[1]=...' 标注，否则该文件端口全漏读、pin_info 为空
        match = re.search(
            r"Port\[?(\d+)\]?\s*=\s*(\d+_[^\s(]+)(?:\s*\(([^)]+)\))?", line, re.IGNORECASE)
        if match:
            port_index = int(match.group(1))
            pin_name = match.group(2)
            frequency = match.group(3)

            # 兼容: 全角括号「（）」回退读取，并提醒用户
            if frequency is None:
                cn_match = re.search(r"（([^）]+)）", line)
                if cn_match:
                    frequency = cn_match.group(1)
                    add_issue("WARNING",
                        f"[格式不标准] {self.external_model} 用了中文括号（），已读入。"
                        f"请改成：! Port[{port_index}] = {pin_name} ({frequency})",
                        key=f"fmt_cn_{self.external_model}_{pin_name}"
                    )
            # 兼容: 括号前无空格，已读取成功，提醒用户规范格式
            elif re.search(rf"{re.escape(pin_name)}\(", line):
                add_issue("WARNING",
                    f"[格式不标准] {self.external_model} 名字和括号之间少了空格，已读入。"
                    f"请改成：! Port[{port_index}] = {pin_name} ({frequency})",
                    key=f"fmt_nospace_{self.external_model}_{pin_name}"
                )

            pin_number = int(pin_name.split("_")[0])

            pin_upper = pin_name.upper()
            is_com = "COM" in pin_upper or "ANT" in pin_upper
            if active_pairs:
                # 引脚名出现在任意激活端口对中，且非 COM 端口 → 激活
                is_active = not is_com and any(
                    p in pin_upper for pair in active_pairs for p in pair
                )
            else:
                # 无法解析文件名（单文件或未知命名规范）：非 COM 引脚均记录
                is_active = not is_com
            assigned_file = str(file_path) if is_active else ""

            self.ports.append({
                "port_index": port_index,
                "pin_number": pin_number,
                "pin_name": pin_name,
                "frequency": frequency,
                "file": assigned_file,
                "candidate_files": [str(file_path)] if is_active else []
            })

            self.pin_info[pin_number] = {
                "name": pin_name,
                "frequency": frequency
            }
            self.pin_name_to_number[pin_name] = pin_number

    def _process_ref_line(self, line):
        """解析 REF MATCHING：`脚名--值 [series|parallel]--...`。"""
        parts = re.split(r'--+', line)
        if len(parts) < 2:
            return

        pin_name = parts[0].strip()
        components = []

        for part in parts[1:]:
            part = part.strip()
            component_type = None
            if 'series' in part:
                component_type = 'series'
                part = part.replace('series', '').strip()
            elif 'parallel' in part:
                component_type = 'parallel'
                part = part.replace('parallel', '').strip()

            components.append({
                'value': part,
                'type': component_type
            })

        self.refs.append({
            'pin_name': pin_name,
            'components': components
        })

        pin_number = self.pin_name_to_number.get(pin_name)
        if pin_number and pin_number in self.pin_info:
            self.pin_info[pin_number]['matching'] = components

    def _print_device_info(self):
        ref_str = f"[{', '.join(self.ref_des_list)}]" if self.ref_des_list else "[]"
        ports_str = '  '.join(
            (p['pin_name'] + '(' + p['frequency'] + ')') if p['frequency'] else p['pin_name']
            for p in sorted(self.ports or [], key=lambda x: x['port_index'])
        )
        print(f"{LogStyle.SUCCESS}✅ {ref_str} → {self.external_model} [{self.device_type}]"
              + (f" | {ports_str}" if ports_str else "") + LogStyle.ENDC)

    def _print_filename_mismatch_reminder(self, mismatches) -> None:
        """文件夹名对得上、文件名对不上：不加载，只提醒改库文件名。"""
        _refdes = ('（位号: ' + ', '.join(self.ref_des_list) + '）') if self.ref_des_list else ''
        lines = []
        for folder, names in mismatches[:3]:
            lines.append(f"   文件夹: {folder}")
            for name in names[:6]:
                lines.append(f"      {name}")
        detail = "\n".join(lines)
        print(f"{LogStyle.WARNING}{'-' * 60}{LogStyle.ENDC}")
        print(
            f"{LogStyle.WARNING}⚠️ 提醒: 型号 '{self.external_model}'{_refdes} "
            f"库里有对应文件夹，但 S 参数文件名不含该型号，未加载。{LogStyle.ENDC}"
        )
        print(f"{LogStyle.WARNING}{detail}{LogStyle.ENDC}")
        print(
            f"{LogStyle.WARNING}   请把文件名改成含正确型号（建议带 _nol），或核对是否下错料。{LogStyle.ENDC}"
        )
        print(f"{LogStyle.WARNING}{'-' * 60}{LogStyle.ENDC}")
        add_issue(
            "WARNING",
            f"[S参数文件名对不上] {self.external_model}"
            + (('  位号: ' + ', '.join(self.ref_des_list)) if self.ref_des_list else '')
            + "\n   库文件夹名含此型号，但其中的 .sNp 文件名不含该型号，没有加载。\n"
            + detail
            + "\n   请改正文件名后重跑。",
            key=f"snp_name_mismatch_{self.external_model}",
        )

    def _print_error(self, message):
        """打印 SNP 解析错误，附原理图位号。"""
        _refdes = ('（位号: ' + ', '.join(self.ref_des_list) + '）') if self.ref_des_list else ''
        error_msg = (
            f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 错误: 外部型号 '{self.external_model}'{_refdes}{LogStyle.ENDC}\n"
            f"{LogStyle.ERROR}  ➤ {message}{LogStyle.ENDC}"
        )
        separator = f"{LogStyle.ERROR}{'-' * 60}{LogStyle.ENDC}"
        print(separator)
        print(error_msg)
        print(separator)

        if '找不到对应文件' in message:
            _refdes = ('  位号: ' + ', '.join(self.ref_des_list)) if self.ref_des_list else ''
            add_issue('ERROR',
                f"[S参数未找到] {self.external_model}{_refdes}\n"
                f"   S参数库中没有这个型号",
                key=f"snp_missing_{self.external_model}"
            )
        elif '解析文件失败' in message:
            detail = message.replace('解析文件失败: ', '', 1)
            add_issue('ERROR',
                f"[文件解析失败] {self.external_model}: {detail}"
                + (f"\n   文件路径: {self.file_path}" if self.file_path else ""),
                key=f"parse_fail_{self.external_model}"
            )

    def _port_count_from_path(self):
        paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
        for path in paths:
            match = re.search(r"\.s(\d+)p$", str(path or ""), re.I)
            if match:
                return max(int(match.group(1)), 1)
        return 2

    def _sweep_mhz_span(self):
        paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
        freqs = []
        unit_mul = 1e-6
        for path in paths:
            if not path:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        text = line.strip()
                        if not text:
                            continue
                        if text.startswith("#"):
                            tok = text[1:].split()
                            if tok:
                                unit = tok[0].upper()
                                if unit.startswith("GHZ"):
                                    unit_mul = 1000.0
                                elif unit.startswith("MHZ"):
                                    unit_mul = 1.0
                                elif unit.startswith("KHZ"):
                                    unit_mul = 0.001
                                else:
                                    unit_mul = 1e-6
                            continue
                        if text.startswith("!"):
                            continue
                        try:
                            freqs.append(float(text.split()[0]) * unit_mul)
                        except (ValueError, IndexError):
                            continue
            except OSError:
                continue
        if not freqs:
            return None
        lo_mhz, hi_mhz = int(round(min(freqs))), int(round(max(freqs)))
        if hi_mhz < lo_mhz:
            lo_mhz, hi_mhz = hi_mhz, lo_mhz
        if hi_mhz <= 0:
            return None
        return clamp_rf_sweep_mhz(lo_mhz, hi_mhz)

    def has_standard_port_format(self):
        """文件头是否含可解析的标准端口频段: Port[n] = 脚号_名字 (lo-hi)。"""
        return any((port or {}).get("frequency") for port in (self.ports or []))

    def _warn_if_nonstandard_port_format(self):
        if self.has_standard_port_format():
            return
        paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
        path_str = "\n      ".join(str(p) for p in paths if p)
        print(
            f"{LogStyle.WARNING}⚠️ {self.external_model} 的 S 参数没写清每个口的引脚和频率{LogStyle.ENDC}"
        )
        add_issue(
            "WARNING",
            f"[SNP格式不标准] {self.external_model}：文件没写清每个口的引脚和频率，"
            f"网络表频段可能不准、仿真口可能对不齐。\n"
            f"   请在文件头补：! Port[1] = 1_IN (1710-1785)\n"
            f"   文件: {path_str.replace(chr(10) + '      ', '; ')}",
            key=f"snp_fmt_{self.external_model}",
        )

    def _has_pin_frequency(self):
        return any((data or {}).get("frequency") for data in (self.pin_info or {}).values())

    def _assign_fallback_frequency(self, freq_s, *, overwrite=False):
        self.default_frequency = freq_s
        if not self.pin_info:
            n_ports = self._port_count_from_path()
            for pin in range(1, n_ports + 1):
                name = f"{pin}_P{pin}"
                self.pin_info[pin] = {"name": name, "frequency": freq_s}
                self.ports.append({
                    "port_index": pin,
                    "pin_number": pin,
                    "pin_name": name,
                    "frequency": freq_s,
                    "file": "",
                    "candidate_files": [],
                })
                self.pin_name_to_number[name] = pin
            return
        for data in self.pin_info.values():
            if overwrite or not data.get("frequency"):
                data["frequency"] = freq_s
        for port in self.ports:
            if overwrite or not port.get("frequency"):
                port["frequency"] = freq_s

    def apply_missing_port_frequency(self, part_desc=""):
        """SNP 无 Port 频段注释时：原理图描述优先，其次扫频轴。描述可覆盖扫频回退。"""
        desc_span = parse_mhz_span(part_desc or "")
        if desc_span:
            from_sweep = getattr(self, "_freq_from_sweep", False)
            if (not self._has_pin_frequency()) or from_sweep:
                self._assign_fallback_frequency(
                    f"{desc_span[0]}-{desc_span[1]}", overwrite=from_sweep
                )
                self._freq_from_sweep = False
            return
        if self._has_pin_frequency():
            return
        sweep = self._sweep_mhz_span()
        if not sweep:
            return
        self._assign_fallback_frequency(f"{sweep[0]}-{sweep[1]}")
        self._freq_from_sweep = True

    def frequency_for_pin(self, pin_number):
        """取该引脚频段。缺注释时用 default_frequency，再没有则并其他端口。"""
        info = (self.pin_info or {}).get(pin_number)
        if info and info.get("frequency"):
            return info["frequency"]
        if getattr(self, "default_frequency", None):
            return self.default_frequency
        spans = []
        for data in (self.pin_info or {}).values():
            freq = (data or {}).get("frequency")
            if not freq:
                continue
            match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(freq))
            if match:
                spans.append((int(match.group(1)), int(match.group(2))))
        if not spans:
            return None
        return f"{min(s[0] for s in spans)}-{max(s[1] for s in spans)}"

