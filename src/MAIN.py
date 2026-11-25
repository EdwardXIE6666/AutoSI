import configparser
import os
import sys


def _runtime_base_dir():
    if "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    env = os.environ.get("AUTOSI_RUNTIME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    default = r"C:\Project\AutoSI"
    os.makedirs(os.path.join(default, "_internal"), exist_ok=True)
    return default


def _show_runtime_license_error(message):
    text = f"AutoSI 授权已失效\n\n{message}"
    print(text)
    try:
        import tkinter as _tk
        from tkinter import messagebox as _messagebox
        root = _tk.Tk()
        root.withdraw()
        _messagebox.showerror("AutoSI 授权已失效", text)
        root.destroy()
    except Exception:
        pass


def _enforce_runtime_license():
    if os.environ.get("AUTOSI_DEV") == "1":
        return
    from app.LicenseGuard import verify_runtime_license
    ok, message = verify_runtime_license(_runtime_base_dir())
    if not ok:
        _show_runtime_license_error(message)
        raise SystemExit(1)


_enforce_runtime_license()

# 无 --worker 参数时只启动 GUI；GUI 以子进程方式运行 "MAIN --worker --choice N" 跑流水线。
# 发布版固定 PySide6；源码可用 AUTOSI_LEGACY_GUI=1 回退旧界面。
if "--worker" not in sys.argv:
    from app.GuiStartupPolicy import load_gui_runner, report_packaged_gui_error
    _packaged = "__compiled__" in globals()
    try:
        run_app = load_gui_runner(
            packaged=_packaged,
            legacy_requested=os.environ.get("AUTOSI_LEGACY_GUI") == "1",
        )
    except ImportError as _gui_import_error:
        if _packaged:
            report_packaged_gui_error(_runtime_base_dir(), _gui_import_error)
            raise SystemExit(1) from _gui_import_error
        raise
    run_app()
    raise SystemExit(0)

from ads.ads_runtime import create_subproject
from schematic.SchematicNetComponentProfiler import *
from rf.PathGraphBuilder import *
from rf.RfPathTracing import *
from rf.segments import (
    classify_network_nodes_by_segment,
    describe_skipped_segment,
    skipped_segment_net_label,
    fbrx_siwave_skip_names,
    filter_skipped_in_50r,
    is_fbrx_siwave_name,
    rf_nets_of_comp,
    safe_component_name,
    siwave_net_name,
    siwave_table_expected_nets,
    skip_fbrx_for_siwave,
)
from rf.SnpFileParser import clamp_rf_sweep_mhz
from matrix.SchematicLevelMatrixGenerator import *
from refnum.path import export_repair_manual


def identify_platform(part_infos, signal_infos, part_mfr, part_infos_name):
    """识别收发器(RF)/CPU(BB)关键芯片与平台类型。

    芯片识别按料号前缀自动完成，无需逐平台登记料号：
      前缀 312427 = Transceiver（射频收发器）
      前缀 312401 = CPU/MCU（基带处理器）
    一块板出现多个同类芯片时（如独立 GNSS 收发），取关联网络最多的为主芯片。
    MTK/Others 判据：主收发器为联发科芯片则判 MTK，否则 Others。
    判联发科：厂商字段含 MTK/MEDIATEK/联发科，或芯片名以 MT 开头（厂商字段时有缺失，故以芯片名兜底）。
    """
    def net_count(ref):
        """统计该位号在 signal_infos 网络中的关联次数，用于主芯片消歧。"""
        return sum(1 for sig in signal_infos if len(sig) > 1 and ref in sig[1])

    def pick_main(prefix):
        cands = [k for k, v in part_infos.items() if v and v.startswith(prefix)]
        return max(cands, key=net_count) if cands else None

    RF_chip_ref = pick_main('312427')
    BB_chip_ref = pick_main('312401')

    # MTK/Others：主收发器为联发科 → MTK
    platform_info = "Others"
    if RF_chip_ref:
        mfr = part_mfr.get(RF_chip_ref, "")
        name = part_infos_name.get(RF_chip_ref, "")
        if ("MTK" in mfr.upper() or "MEDIATEK" in mfr.upper() or "联发科" in mfr
                or name.upper().startswith("MT")):
            platform_info = "MTK"

    if RF_chip_ref or BB_chip_ref:
        print(f"{LogStyle.SUCCESS}{LogStyle.EMOJI['success']} 平台识别结果: {platform_info}{LogStyle.ENDC}\n")
        print(f"{LogStyle.INFO}ℹ️ 关键芯片:{LogStyle.ENDC}")
        if RF_chip_ref:
            mfr = part_mfr.get(RF_chip_ref, "")
            print(f"  - {RF_chip_ref} ({LogStyle.HIGHLIGHT}Transceiver{LogStyle.ENDC}{(' / ' + mfr) if mfr else ''})")
        if BB_chip_ref:
            print(f"  - {BB_chip_ref} ({LogStyle.HIGHLIGHT}CPU/MCU{LogStyle.ENDC})")
    else:
        print(f"{LogStyle.ERROR}{LogStyle.EMOJI['error']} 未识别到收发器/CPU 芯片{LogStyle.ENDC}")

    return RF_chip_ref, BB_chip_ref, platform_info


# region 参数初始化与原理图解析
# worker 配置来自命令行 + setting.ini（GUI 确认时已写入）
choice = sys.argv[sys.argv.index("--choice") + 1] if "--choice" in sys.argv else "1"

_base_dir = _runtime_base_dir()

# 打包 exe（致崩根因，dmp 坐实）：worker 进程 cwd 若落在 Nuitka onefile 临时目录，则它派生的
# 任何 ADS 进程（headless 引擎后台 hpeesofde + 收尾 ads.exe）会继承该 cwd，并按 DLL 搜索顺序从
# cwd 加载临时目录里 bundle 的 python312.dll；worker 退出后该目录被清 → hpeesofde 原生崩溃。
# 启动即把 cwd 固定到 _base_dir（无 python312.dll，且是 GUI 启动 worker 时设定的预期 cwd），
# 使子进程加载到 ADS 自带 Python。仅打包态需要，python 直跑 cwd 本就不在临时目录。
if "__compiled__" in globals():
    try:
        os.chdir(_base_dir)
    except Exception:
        pass

import atexit as _atexit

# 先读配置取项目名，使日志按项目区分：8_log/<项目>/<功能>/时间戳.log
_ini = configparser.ConfigParser()
from pathlib import Path as _Path
from app.workspace import sidecar_path as _sidecar_path

def _sidecar(name, write=False):
    return str(_sidecar_path(_Path(_base_dir), name, write=write))


_ini.read(_sidecar("setting.ini"), encoding="utf-8")
config_result = {k: v for sect in _ini.sections() for k, v in _ini.items(sect)}

file_path = config_result["schematic_ascii_file"]
CON_REF = config_result.get("schematic_lga_ref", "")

# 文件名 `_SCH` 前缀为项目名
_stem = os.path.splitext(os.path.basename(file_path))[0]
_idx = _stem.upper().find('_SCH')
project_name = _stem[:_idx] if _idx != -1 else _stem

from app.workspace import (
    OUTPUT_LOG,
    OUTPUT_NETS,
    OUTPUT_OTHER,
    OUTPUT_PREVIEW,
    OUTPUT_REFNUM,
    ensure_layout as _ensure_ws_layout,
    has_workspace_layout as _has_ws_layout,
    modernize_output_path as _modernize_output,
    output_dir as _ws_output,
    workspace_name as _ws_name,
)

_workspace_raw = config_result.get("workspace_directory") or ""
_workspace = _Path(_workspace_raw) if _workspace_raw and os.path.isdir(_workspace_raw) else _Path(_base_dir)
if _has_ws_layout(_workspace):
    _ensure_ws_layout(_workspace)
_ws_label = _ws_name(_workspace)

_choice_log = {
    "1": "功能1_网络频段信息",
    "2": "功能3_ADS仿真",
    "3": "功能5_refnum生成",
    "4": "功能6_器件Pin路径",
}
_file_logger = Logger(log_dir=str(_ws_output(_workspace, OUTPUT_LOG) / project_name / _choice_log.get(choice, f"功能{choice}")))
_file_logger.__enter__()
_atexit.register(lambda: _file_logger.__exit__(None, None, None))

_pdf_dir     = str(_ws_output(_workspace, OUTPUT_OTHER, project_name))
_siwave_dir  = str(_ws_output(_workspace, OUTPUT_NETS))
_refnum_dir  = str(_ws_output(_workspace, OUTPUT_REFNUM))
_refnum_mid_dir = str(_ws_output(_workspace, OUTPUT_REFNUM, OUTPUT_PREVIEW))
os.makedirs(_pdf_dir,    exist_ok=True)
os.makedirs(_siwave_dir, exist_ok=True)
os.makedirs(_refnum_dir, exist_ok=True)
os.makedirs(_refnum_mid_dir, exist_ok=True)
component_directory = config_result.get("component_s_parameter_library", "")
pcb_directory = config_result.get("pcb_s_parameters", "")
# S参数库完整性校验：检出库被改动/新增/删除即拒绝运行（AUTOSI_DEV=1 时跳过，供开发者调试）
if os.environ.get("AUTOSI_DEV") != "1" and component_directory:
    from app.SnpLibrarySeal import verify_library, CACHE_NAME
    _snp_ok, _snp_problems = verify_library(
        component_directory, os.path.join(_base_dir, CACHE_NAME))
    if not _snp_ok:
        print(f"\n{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}")
        print(f"{LogStyle.ERROR}❌ S参数库完整性校验失败，已终止运行{LogStyle.ENDC}")
        print(f"{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}")
        _detail = "\n".join(f"   • {p}" for p in _snp_problems[:20])
        if len(_snp_problems) > 20:
            _detail += f"\n   • …… 等共 {len(_snp_problems)} 项"
        add_issue('ERROR',
            f"[S参数库被篡改] 库内容与官方版本不一致，禁止使用\n"
            f"{_detail}\n"
            f"   S参数为严格标准数据，请勿自行修改。请联系开发者获取/恢复官方库。",
            key="snp_lib_tampered"
        )
        print_issue_summary()
        raise SystemExit(1)

from schematic.net_class_50r import Missing50RNetsError
PINParser = ComponentPINParser(file_path)
try:
    SchematicRes = SchematicNetComponentProfiler.process_schematic(
        file_path)
except Missing50RNetsError as exc:
    print(f"\n{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}")
    print(f"{LogStyle.ERROR}❌ {exc}{LogStyle.ENDC}")
    add_issue("ERROR", f"[没有50R网络] {exc}", key="missing_50r")
    print_issue_summary()
    raise SystemExit(1)
if SchematicRes is None:
    print(f"\n{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}")
    print(f"{LogStyle.ERROR}❌ 原理图读取失败，已终止运行{LogStyle.ENDC}")
    add_issue("ERROR", f"[原理图读取失败] {file_path}", key="schematic_read")
    print_issue_summary()
    raise SystemExit(1)
from schematic.name_check import scan_schematic_naming
from app.LogTool import add_issue, LogStyle
for i, msg in enumerate(scan_schematic_naming(SchematicRes, CON_REF)):
    issue = "[原理图命名] " + msg
    print(LogStyle.ERROR + issue + LogStyle.ENDC)
    add_issue("ERROR", issue, key="schematic_naming_"+str(i))

# temp 走流水线过滤；raw 给构图/ADS 保留过滤前网络
SchematicRes_temp = SchematicRes.copy()
SchematicRes_raw = SchematicRes.copy()

RF_chip_ref, BB_chip_ref, platform_info = identify_platform(
    SchematicRes.part_infos, SchematicRes.signal_infos,
    SchematicRes.part_mfr, SchematicRes.part_infos_name)
if RF_chip_ref == None:
    pass
else:
    SchematicRes.signal_infos = SchematicNetComponentProfiler.removeBBcomponents(
    SchematicRes.signal_infos,
    SchematicRes.part_infos,
    RF_chip_ref,
    BB_chip_ref,
    CON_REF
)

SchematicRes.signal_infos = removeGNDpart(
    SchematicRes.signal_infos, SchematicRes.gnd_inf)

# 为频段传播加载滤波器/开关/多工器端口频段
print(f"\n{'='*50}")
print("📡 正在加载 RF 器件 S 参数（滤波器 / 开关 / 多工器）...")
print(f"{'='*50}")
SNPFileInfo.SNP_DIRECTORY = component_directory
successful_components, failed_components = SNPFileInfo.batch_initialize(
    SchematicRes.get_s_parameter_required_components()
)
print(f"{'='*50}\n")
# endregion

# region GPS路径分析
# 无 GNSS 射频证据（网名/独立 GNSS 芯片）时整段跳过，进入 FBRX。
GPS_LNA_paths = []
GPS_LNA_to_ANT_paths = []
GPS_ALL_PATHS = []
if not has_gnss_rf_evidence(SchematicRes):
    print(f"{LogStyle.INFO}ℹ️ 无 GNSS 射频证据，跳过 GPS 抽取，进入下一步{LogStyle.ENDC}")
else:
    GPS_LNA_paths = find_GPS_LNA(SchematicRes, RF_chip_ref, platform_info)
    if not GPS_LNA_paths:
        GPS_LNA_paths = find_GPS_LNA_from_GNSS_chips(SchematicRes, platform_info)

    SchematicRes.signal_infos = filter_signal_infos(
        SchematicRes.signal_infos, GPS_LNA_paths)

    end_nodes = list(set([path[-1] for path in GPS_LNA_paths]))
    for end_node in end_nodes:
        GPS_LNA_to_ANT_paths += find_GPS_ANT(
            SchematicRes, end_node, CON_REF)

    # 某些平台（如独立 GNSS Transceiver）没有从主蜂窝 Transceiver 到 GNSS LNA 的
    # RF 网络。此时从全部 LNA 反向补找明确命名为 GPS/GNSS 的天线路径，避免把 GPIO
    # 控制连接误画成射频链路。
    if not GPS_LNA_paths:
        GPS_LNA_to_ANT_paths = find_GNSS_ANT_from_LNAs(SchematicRes, CON_REF)

    GPS_LNA_paths = sort_paths_by_special_nodes(GPS_LNA_paths)
    GPS_LNA_to_ANT_paths = sort_paths_by_special_nodes(GPS_LNA_to_ANT_paths)
    GPS_ALL_PATHS = GPS_LNA_paths + GPS_LNA_to_ANT_paths

    if GPS_ALL_PATHS:
        graph, edges, dir_comps = build_and_visualize_path_graph(
            GPS_ALL_PATHS, SchematicRes, output_filename=os.path.join(_pdf_dir, "GPS_ALL_PATHS"))
    SchematicRes.remove_nodes_from_networks(GPS_ALL_PATHS)
# endregion


# region FBRX路径分析
# 不从网表移除 FBRX 节点：功能5 ANT 构图需保留 Coupler 耦合口拓扑
# （FBRX 与普通旁路链路同等，不单独特殊化）。
T_FBRX_paths = find_FBRX_SW(SchematicRes, RF_chip_ref)

T_FBRX_to_COUPLER_RFM_paths = []
end_nodes = list(set([item for path in T_FBRX_paths for item in path if SchematicRes.part_infos.get(
    item, '').startswith('312423')]))


T_FBRX_paths_temp = []
for end_node in end_nodes:
    T_FBRX_to_COUPLER_RFM_paths += find_FBRX_SW_to_COMPLER_RFM(
        SchematicRes, end_node)
    for T_FBRX_path in T_FBRX_paths:
        if (end_node in T_FBRX_path) and T_FBRX_path[2] != end_node:
            T_FBRX_paths_temp.append(
                get_previous_element(T_FBRX_path, end_node))

T_FBRX_paths = list(
    {tuple(sublist): sublist for sublist in T_FBRX_paths_temp}.values())


T_FBRX_to_COUPLER_RFM_paths = sort_paths_by_special_nodes(
    T_FBRX_to_COUPLER_RFM_paths)
T_FBRX_paths = sort_paths_by_special_nodes(T_FBRX_paths)
T_FBRX_ALL_PATHS = T_FBRX_paths+T_FBRX_to_COUPLER_RFM_paths

if T_FBRX_ALL_PATHS:
    graph, edges, dir_comps = build_and_visualize_path_graph(
        T_FBRX_ALL_PATHS, SchematicRes, output_filename=os.path.join(_pdf_dir, "T_FBRX_ALL_PATHS"))
# 历史：remove_nodes_from_networks(T_FBRX_ALL_PATHS) 会把 FBRX 从主图摘掉，
# ANT reverse/构图看不到 Coupler→TRX 支路；现保留节点。
# endregion


# region RFM和PA/LNA路径分析
T_LNA_PA_RFM_paths = find_LNA_PA_RFM(SchematicRes, RF_chip_ref,PINParser)
grouped_paths = group_paths_by_last_element(T_LNA_PA_RFM_paths)

LNA_PA_nodes = [path[-1]
                for path in T_LNA_PA_RFM_paths if not SchematicRes.part_infos.get(path[-1], '').startswith('312425')]
LNA_PA_nodes = list(set(LNA_PA_nodes))

LNA_PA_paths = []
for end_node in LNA_PA_nodes:
    LNA_PA_paths += find_LNA_PA(SchematicRes, end_node,PINParser)
SchematicRes.remove_nodes_from_networks(LNA_PA_paths)

LNA_PA_paths = sort_paths_by_special_nodes(LNA_PA_paths)
if LNA_PA_paths:
    graph, edges, dir_comps = build_and_visualize_path_graph(
        LNA_PA_paths, SchematicRes, output_filename=os.path.join(_pdf_dir, "LNA_PA_paths"))
SchematicRes.remove_nodes_from_networks(LNA_PA_paths)


T_RFM_ANT_paths = []
for last_element, paths in grouped_paths.items():
    # RFM
    if SchematicRes.part_infos.get(last_element, '').startswith(('312425')):
        T_RFM_ANT_paths += (find_PA_LNA_ANT(SchematicRes,
                            last_element, PINParser))
T_RFM_ANT_paths = sort_paths_by_special_nodes(T_RFM_ANT_paths)


if T_RFM_ANT_paths:
    graph, edges, dir_comps = build_and_visualize_path_graph(
        T_RFM_ANT_paths, SchematicRes, output_filename=os.path.join(_pdf_dir, "T_RFM_ANT_paths"))


# endregion

# region PA/LNA到天线路径分析
T_PA_LNA_ANT_paths = []

for last_element, paths in grouped_paths.items():
    # PA,LNA（非 312425 射频前端模块）
    if not SchematicRes.part_infos.get(last_element, '').startswith(('312425')):
        T_PA_LNA_ANT_paths += (find_PA_LNA_ANT(
            SchematicRes, last_element, PINParser))

T_PA_LNA_ANT_paths += T_RFM_ANT_paths

T_PA_LNA_ANT_paths = sort_paths_by_special_nodes(T_PA_LNA_ANT_paths)
graph = None
if T_PA_LNA_ANT_paths:
    graph, edges, dir_comps = build_and_visualize_path_graph(
        T_PA_LNA_ANT_paths, SchematicRes, output_filename=os.path.join(_pdf_dir, "T_PA_LNA_ANT_paths"))

SchematicRes.remove_nodes_from_networks(T_PA_LNA_ANT_paths)

# endregion

multi_ended, single_ended, isolated = SchematicRes.classify_networks_by_connection_count()
# region 单天线

SAW_DEFINE.clear()

for part_info in SchematicRes. part_infos:
    if SchematicRes.part_infos.get(part_info, "").startswith(('312706', '312702', '312703', '312799')):
        # 312706 SAWFBAR / 312799 其他滤波器(BPF)
        # NC（兼容设计/不贴）的滤波器不参与频段分析、无需 S 参数，统一登记为"未接入"，避免误报缺 S 参数
        if not SchematicRes.part_active.get(part_info, True):
            add_issue('WARNING',
                f"[滤波器未接入] {part_info}\n"
                f"   器件两端被 NC（兼容设计），频段分析将跳过此器件",
                key=f"drx_fail_{part_info}")
            continue
        SNPFile = SNPFileInfo(
            SchematicRes.get_part_type(part_info),
            SchematicRes.part_infos_name.get(part_info, ""),
            ref_des=part_info,
            part_desc=SchematicRes.part_desc.get(part_info, ""),
        )
        if SNPFile.state:
            SAW_DEFINE[part_info] = SNPFile


rf_graph = RfSignalGraph(graph)

# NC/不贴匹配导致 PA→ANT 主路径未含该天线时：先补图，再挂 SNP 频段并传播
from rf.RfGraphModel import (
    attach_and_propagate_filter_bands as _attach_and_propagate_filter_bands,
    prune_reverse_at_series_nc as _prune_series_nc,
    reverse_cone_has_live_rf as _live_rf_cone,
    seed_missing_antenna_stubs as _seed_missing_antenna_stubs,
)
_seeded_ants = _seed_missing_antenna_stubs(rf_graph, SchematicRes_raw)
if _seeded_ants:
    print(
        f"[rf_graph] 补入 NC/stub 天线: {', '.join(_seeded_ants)} "
        f"（signal_infos_raw，含不贴匹配件）"
    )

_gps_handled = {_n for _p in GPS_ALL_PATHS for _n in _p}
_drx_ok, _drx_fail = _attach_and_propagate_filter_bands(
    rf_graph, SAW_DEFINE, SchematicRes_raw, gps_handled=_gps_handled
)
if _drx_ok or _drx_fail:
    _fail_str = f"，未找到锚点: {', '.join(_drx_fail)}" if _drx_fail else ""
    print(f"[DRX] 锚定完成: 成功 {_drx_ok} 个{_fail_str}")
print(f"[rf_graph] 构建完成: {len(rf_graph.nodes)} 个节点")


# endregion


net_infos = []

project_template = create_subproject(file_path)
workspace_path = str(
    Path(project_template["project_dir"]) / project_template["project_name"])

# 功能 3（choice 2）：ADS 工作区建到 GUI 指定的 5_ADS仿真/..._ADS 目录
# （_on_confirm 已创建该目录并写入 setting.ini），未配置时沿用原理图旁的旧路径
_ads_dir = config_result.get("ads_project_directory", "")
if choice == "2" and _ads_dir:
    workspace_path = str(_modernize_output(_ads_dir))


from refnum.graph import (
    collect_connector_feed_ports as _collect_feed_ports,
    iter_antenna_port_nodes as _iter_ant_ports,
)
_feed_ports = _collect_feed_ports(
    getattr(SchematicRes_raw, "signal_infos_raw", None)
    or getattr(SchematicRes_raw, "signal_infos", None)
)
res = []
_rev_full = {}
nc_stub_ants = set()
for name in _iter_ant_ports(rf_graph.nodes, extra_ports=_feed_ports):
    visited, reverse_edges = draw_reverse_paths_new(
        rf_graph, SchematicRes_raw, name, os.path.join(_pdf_dir, f"reverse_paths_{name}"),
        pin_parser=PINParser)
    if reverse_edges:
        _rev_full[name] = (visited, reverse_edges)
        visited, reverse_edges = _prune_series_nc(
            name, visited, reverse_edges, SchematicRes_raw)
        if not _live_rf_cone(visited, SchematicRes_raw, name):
            nc_stub_ants.add(name)
        res.append([name, [visited, reverse_edges]])
for _na in sorted(nc_stub_ants):
    print(
        f"{LogStyle.INFO}ℹ️ {_na} 匹配全是 NC（不贴），不写入 SIwave 网络表"
        f"（仅路径图 PDF / 手册）{LogStyle.ENDC}"
    )
# 识别 GNSS 天线：反向子树里含"端口名带 GPS/GNSS"的滤波器（如 HDDL1L5RSS-B8 的 GPSL1/GPSL5）
# GNSS 属独立导航支路，不属正常收发链路 → 其网络不写入 SIwave 网络表（仅保留路径图 PDF）
import re as _re_gnss
_GNSS_PORT = _re_gnss.compile(r'GPS|GNSS|GLONASS|BEIDOU|GALILEO', _re_gnss.I)
def _is_gnss_filter(snp):
    pi = getattr(snp, 'pin_info', None) or {}
    return any(_GNSS_PORT.search(str(info.get('name', ''))) for info in pi.values())
gnss_ants = set()
for _entry in res:
    _ant_id, _visited = _entry[0], _entry[1][0]
    if any(_is_gnss_filter(SAW_DEFINE[n]) for n in _visited if n in SAW_DEFINE):
        gnss_ants.add(_ant_id)
for _ga in sorted(gnss_ants):
    print(f"{LogStyle.INFO}ℹ️ {_ga} 为 GNSS 天线（路径含 GPS/GNSS 滤波器），其网络不写入 SIwave 网络表（仅出现在路径图 PDF）{LogStyle.ENDC}")

_fbrx_skip = fbrx_siwave_skip_names(
    T_FBRX_ALL_PATHS, SchematicRes_raw, pin_parser=PINParser)
_50r_nets = set(getattr(SchematicRes_raw, "net_class_50r_nets", ()) or ())
for i in range(len(res)):
    [ANT_NUM, [visited, reverse_edges]] = res[i]
    SchematicRes_raw.analyze_electronic_switches(visited, reverse_edges)
    # 网络表/功能2/功能3 统一按「工作习惯分段」（一进多出器件为界，耦合器除外），块名=段id；
    # 段id 与功能2 抽取的 PCB .sNp 文件名一致（ANTx-段id），功能3 据此匹配 PCB 并搭分段树。
    # 串联 NC 剪锥后到不了下一分界的半截段，classify 里丢掉。
    seg, seg_freq, seg_meta = classify_network_nodes_by_segment(
        ANT_NUM, visited, reverse_edges, SchematicRes_raw, rf_graph,
        pin_parser=PINParser)
    _skip_items = list((seg_meta or {}).get("skipped") or [])
    if ANT_NUM not in gnss_ants and ANT_NUM not in nc_stub_ants:
        for sid, nets in seg.items():
            live_nets = [
                n for n in nets if not skip_fbrx_for_siwave(n, _fbrx_skip)
            ]
            if skip_fbrx_for_siwave(sid, _fbrx_skip) and not live_nets:
                _skip_items.append({
                    "name": sid,
                    "nets": list(nets),
                    "reason": "fbrx",
                    "fork": None,
                })
                continue
            if not live_nets:
                continue
            lo, hi = clamp_rf_sweep_mhz(*seg_freq.get(sid, (0, 0)))
            block = safe_component_name(f"{ANT_NUM}-{sid}")
            for node_name in live_nets:
                net_infos.append(
                    f"{siwave_net_name(node_name)},{block},{lo}MHZ,{hi}MHZ"
                )
        _shown = filter_skipped_in_50r(_skip_items, _50r_nets)
        if _shown:
            _lines = [
                f"   {skipped_segment_net_label(it)}：{describe_skipped_segment(it)}"
                for it in _shown
            ]
            _msg = (
                f"[未进网络表] {ANT_NUM} 有 {len(_shown)} 段按规则不写\n"
                + "\n".join(_lines)
            )
            print(f"{LogStyle.INFO}ℹ️ {_msg}{LogStyle.ENDC}")
            add_issue("WARNING", _msg, key=f"skip_seg_{ANT_NUM}")
    SNP_list = [item for item in seg if not skip_fbrx_for_siwave(item, _fbrx_skip)]
    res[i].append(SNP_list)
    res[i].append(seg)
    res[i].append(seg_meta)


# 缺库 S 参数：列出路径上这颗器件接到的网，立刻打进 log。
from app.LogTool import report_missing_snp_impacts as _report_missing_snp
_refdes_nets = {}
for _entry in res:
    _visited = _entry[1][0]
    for _nid in _visited:
        if not _nid or SchematicRes_raw.is_network(_nid):
            continue
        for _net in rf_nets_of_comp(SchematicRes_raw, _nid):
            if _net in _visited:
                _refdes_nets.setdefault(_nid, set()).add(_net)
_report_missing_snp(failed_components, _refdes_nets)


match choice:
    case "1":#只生成网络信息
        _siwave_txt = os.path.join(_siwave_dir, f"{project_name}+Network_Frequency_Band_Information_for_SIwave.txt")
        with open(_siwave_txt, 'w', encoding='utf-8') as f:
            f.write('\n'.join(net_infos))
        print(f"已将网络节点信息保存到: {_siwave_txt}（共 {len(net_infos)} 条网络）")
        from schematic.nc_parts import write_skip_series_nc
        _skip_nc = write_skip_series_nc(_siwave_txt, SchematicRes_raw)
        print(f"串联不贴焊盘名单: {_skip_nc}")

        # ── 完整性校验：以射频图为基准，列出「进了射频图但未写入 SIwave 网络表」的网络 ──
        # txt 实际写入的节点（按天线分组）
        _txt_nodes_by_ant = {}
        for _row in net_infos:
            _p = _row.split(',')
            _txt_nodes_by_ant.setdefault(_p[1].split('-')[0], set()).add(_p[0])
        # 各天线反向遍历(visited)到的网络节点 = 该天线在射频图里应进表的网络
        _visited_by_ant = {_r[0]: _r[1][0] for _r in res}
        _seg_by_ant = {_r[0]: _r[3] for _r in res if len(_r) > 3}

        for _ant in _iter_ant_ports(rf_graph.nodes, extra_ports=_feed_ports):
            if _ant in gnss_ants or _ant in nc_stub_ants:
                continue  # GNSS / 全 NC 口本就不进网络表，跳过完整性检查
            _out = _txt_nodes_by_ant.get(_ant, set())
            _expected = siwave_table_expected_nets(
                _visited_by_ant.get(_ant, set()),
                SchematicRes_temp,
                seg=_seg_by_ant.get(_ant),
                extra_skip=_fbrx_skip,
            )
            _missing = {
                n for n in _expected if siwave_net_name(n) not in _out
            }
            if _missing:
                _whole = "（整根天线无网络写入）" if not _out else ""
                add_issue('WARNING',
                    f"[网络表不完整] {_ant} 有 {len(_missing)} 条网该进网络表，但没有写上{_whole}\n"
                    f"   这些网: {', '.join(sorted(_missing))}\n"
                    f"   按规则不写的段见「未进网络表」。还报了就是漏写，把 log 发给开发人员。",
                    key=f"incomplete_{_ant}")


    case "2":#ADS
        import traceback as _ads_tb
        from ads.ads_runtime import prepare_ads_python
        try:
            prepare_ads_python()
            from ads.ADSLayoutManager import *
        except Exception as _ads_imp_err:
            print(_ads_tb.format_exc())
            print(
                "功能3 无法加载本机 ADS Python 接口。"
                "请确认仿真机已安装 Keysight ADS，并先运行「配置环境」。"
                f"\n{_ads_imp_err}"
            )
            raise SystemExit(1) from _ads_imp_err
        from ads.match_ael import (
            READY_NAME,
            STATUS_NAME,
            TRACE_NAME,
            append_match_trace,
            render_ready_marker_ael,
        )
        from ads.match_pipeline import (
            finalize_rows,
            kick_match_after_boot,
            match_enabled,
            run_match_prepare,
            unrun_rows_from_cells,
            wait_status,
            write_match_ael_file,
            write_match_kick_ael,
        )
        from ads.match_report import write_match_report

        all_sxp_files_component = SchematicDesigner.get_all_sxp_files(
            component_directory)
        all_sxp_files_pcb = SchematicDesigner.get_all_sxp_files(pcb_directory)

        _match_on = match_enabled(config_result)
        _match_cells = []
        with create_and_open_an_empty_workspace(workspace_path) as workspace:
            workspace_path = str(workspace.path)
            designer = SchematicDesigner(project_template, SchematicRes_raw)
            create_library(workspace, project_template["project_name"])
            for i in range(len(res)):
                [ANT_NUM, [visited, reverse_edges], SNP_list, net_work_class] = res[i][:4]
                if ANT_NUM in nc_stub_ants or ANT_NUM in gnss_ants:
                    continue
                seg_meta = res[i][4] if len(res[i]) > 4 else {}
                subproject_name = ANT_NUM
                # 计算 level_matrix（与 case "3" 保持一致）
                level_matrix = get_level_matrix(
                    reverse_edges, SchematicRes_raw, ANT_NUM, _refnum_dir)
                designer.create_subproject_design(
                    subproject_name, level_matrix, net_work_class, SchematicRes_raw, SNP_list, all_sxp_files_pcb, all_sxp_files_component, reverse_edges=reverse_edges, seg_meta=seg_meta)
            if _match_on:
                _match_cells = run_match_prepare(designer, [r[0] for r in res])

        # 生成一键修复宏：load fix_snp.ael 后逐设计 fix_design_with_wires
        #（引脚落位 + 按 snap_point 真实坐标画飘线 + 贴净名 + 静默保存）。
        # 方案B：净名从 _SNP_WIRE_REGISTRY 烤进宏，AEL 不做字符串解析。
        # sidecar 有 _internal 时读写都走内部。打包后优先用打进 exe 的配套宏，
        # 只换 MAIN.exe 时不会继续 load 仿真机上旧的 fix_snp.ael（网标会反）。
        from ads.ads_runtime import install_fix_snp_ael as _install_fix_snp
        _dst_ael = _sidecar("fix_snp.ael", write=True)
        _install_fix_snp(_base_dir, _dst_ael, __file__)
        _ael_dir = os.path.dirname(_dst_ael) if os.path.isfile(_dst_ael) else _base_dir
        if not os.path.isfile(os.path.join(_ael_dir, "fix_snp.ael")):
            _ael_dir = _base_dir
        _ael_base = _ael_dir.replace("\\", "/")
        _ael_lib = project_template["project_name"]
        _registry = get_snp_wire_registry()

        def _ael_str(s):
            return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

        def _ael_namelist(names):
            return ("list(" + ", ".join(_ael_str(n) for n in names) + ")") if names else "list()"

        _ael_lines = [
            "// Auto-generated by function 3. Usage in ADS main window:",
            "//   Tools > AEL Command Line...",
            f'//   load("{_ael_base}/fix_snp_auto.ael");',
            "// Refreshes SnP pins + draws net-labeled wires in ALL designs, then saves.",
            "// One de_info at the end (fix_snp_report); per-schematic success is silent.",
            "// 绝对路径是生成机上的；拷到别的盘后若不存在则 load 同目录 fix_snp.ael。",
            f'decl _fix_snp_abs = "{_ael_base}/fix_snp.ael";',
            "if (ael_file_exists(_fix_snp_abs) == FALSE)",
            "{",
            '    load("fix_snp.ael");',
            "}",
            "else",
            "{",
            "    load(_fix_snp_abs);",
            "}",
            "_fix_reset_stats();",
        ]
        # 先重生段 symbol（ads.exe 真脚位），再画 SnP/层级 symbol 飘线。
        # 反了的话子页线停在 headless 假脚位上，symbol 对不上。
        for _sym_cell in get_segment_symbol_registry():
            _ael_lines.append(f'gen_segment_symbol("{_ael_lib}:{_sym_cell}");')
        # 含 SnP 的分段 cell（ANTx_COM / ANTx_频段）逐个修引脚并画飘线。
        for _cell, _entries in _registry.items():
            _design = f"{_ael_lib}:{_cell}:schematic"
            if _entries:
                _snp_items = ",\n".join(
                    f'    list({_ael_str(_inst)}, {_ael_namelist(_names)})'
                    for _inst, _names in _entries)
                _ael_lines.append(
                    f'fix_design_with_wires("{_design}", list(\n{_snp_items}));')
            else:
                _ael_lines.append(f'fix_design_with_wires("{_design}", list());')
        _ael_lines.append("fix_snp_report();")
        _ael_lines.append("")
        # 内容均为 ASCII（净名/路径），mbcs 与 utf-8 等价；沿用 mbcs。
        for _stale in ("fix_snp.atf", "fix_snp_auto.atf"):
            _p = os.path.join(_ael_dir, _stale)
            if os.path.isfile(_p):
                os.remove(_p)
        with open(os.path.join(_ael_dir, "fix_snp_auto.ael"), "w", encoding="mbcs", errors="replace") as _f:
            _f.write("\n".join(_ael_lines))
        print(f"一键修复宏已生成: {os.path.join(_base_dir, 'fix_snp_auto.ael')}")

        # ── 自动拉起 ADS 打开本次 workspace 并修复 SnP（消除手动粘贴 AEL 步骤）──
        # headless 引擎(keysight.ads.de)无法持久化 SnP 的 Pcell 引脚，故另起一个完整 DE 进程。
        # 用 ads.exe（自管完整 ADS 环境，hpeesofde 裸跑会因缺环境段错误）：ads.exe -m 在启动时
        # 执行 ads_autostart.ael → de_open_workspace 打开本次 workspace + 复用 fix_snp_auto.ael
        # 刷新全部设计的 SnP 并静默保存。用户打开即见修好的 workspace。
        # 拉起前先关掉 headless 引擎里仍打开的 workspace，释放目录锁，避免与 ads.exe 抢占。
        from keysight.ads import de as _de_auto
        if _de_auto.workspace_is_open():
            _de_auto.close_workspace()

        # 编码：de_open_workspace 按 UTF-8 解析路径（与 load() 的 ANSI 不同），workspace 路径
        # 含中文(5_ADS仿真) → 启动宏必须写 UTF-8(无 BOM)；load() 的两个路径均为纯 ASCII，不受影响。
        _autostart_path = _sidecar("ads_autostart.ael", write=True)
        _ws_ael = workspace_path.replace("\\", "/")
        _autostart_lines = [
            "// Auto-generated by function 3 — 启动时自动打开 workspace 并修复全部 SnP。",
            "// Do not sleep or de_analyze here: ads.exe -m runs inside debootcb.",
            f'de_open_workspace("{_ws_ael}");',
            f'load("{_ael_base}/fix_snp_auto.ael");',
        ]
        # load() is ANSI: handshake JSON must sit next to match_50r.ael in the
        # ASCII sidecar dir. A Chinese workspace_path (5_ADS仿真) would never match.
        _match_status = _Path(_ael_dir) / STATUS_NAME
        _match_ready = _Path(_ael_dir) / READY_NAME
        _match_trace = _Path(_ael_dir) / TRACE_NAME
        _match_ael = _Path(_ael_dir) / "match_50r.ael"
        _match_kick = _Path(_ael_dir) / "match_kick.ael"
        if _match_on:
            write_match_ael_file(
                _match_ael, _ael_lib, _match_cells, _match_status, workspace_path)
            write_match_kick_ael(_match_kick, _match_ael, workspace_path)
            _autostart_lines.append(
                render_ready_marker_ael(str(_match_ready), str(_match_trace)))
            _autostart_lines.append(f'load("{_ael_base}/match_50r.ael");')
            _autostart_lines.append('set_user_menu_label("AutoSI");')
            _autostart_lines.append('add_menu("Match50R", "match_50r_run", "User");')
        _autostart_lines.append("")
        with open(_autostart_path, "w", encoding="utf-8", errors="replace") as _f:
            _f.write("\n".join(_autostart_lines))

        _ads_exe = os.path.join(os.environ.get("HPEESOF_DIR", ""), "bin", "ads.exe")
        if os.path.exists(_ads_exe):
            import subprocess
            # 收尾拉起 ADS 的两处坑（均为打包 exe 特有，python 直跑不触发）：
            # ① cwd（致崩根因，已由崩溃 dmp 坐实）：打包后本进程当前目录落在 Nuitka onefile
            #    临时目录，该目录根部有 bundle 的 python312.dll。ADS(hpeesofde) 内嵌 Python 启动时
            #    按 DLL 搜索顺序会从 cwd 加载这个 python312.dll；本进程退出后临时目录被清，ADS 脚下
            #    的 DLL 失效 → hpeesofde 原生崩溃。故显式指定 cwd 为 ADS 自身 bin 目录，杜绝 ADS
            #    继承到临时目录（dmp 里实测加载了 onefile_xxx\python312.dll）。
            # ② TCL_LIBRARY/TK_LIBRARY：tk-inter 注入并指向临时 tcl/tk，同样随退出失效，一并清掉
            #    避免 ADS 指向已删目录。PYTHONPATH/PYTHONHOME 与 python 直跑一致，保留不动。
            _ads_env = dict(os.environ)
            _ads_env["AUTOSI_AEL_DIR"] = _ael_dir
            _aelpath = _ads_env.get("AELPATH", "")
            _ads_env["AELPATH"] = _ael_dir + (os.pathsep + _aelpath if _aelpath else "")
            if "__compiled__" in globals():
                for _k in list(_ads_env):
                    _ku = _k.upper()
                    if _ku in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH", "TIX_LIBRARY") \
                            or _ku.startswith("NUITKA"):
                        _ads_env.pop(_k, None)
            if _match_on:
                if _match_status.is_file():
                    _match_status.unlink()
                if _match_ready.is_file():
                    _match_ready.unlink()
                if _match_trace.is_file():
                    _match_trace.unlink()
            subprocess.Popen([_ads_exe, "-m", _autostart_path.replace("\\", "/")],
                             env=_ads_env, cwd=os.path.dirname(_ads_exe))
            print("已自动拉起 ADS：将打开 workspace 并修复全部 SnP。"
                  "（若个别未自动修好，可在 ADS 主窗口 Tools > AEL Command Line 手动 load fix_snp_auto.ael）")
            if _match_on:
                append_match_trace(_match_trace, "py", "autostart")
                print("匹配步骤写在 _match_50r_trace.txt。"
                      "另开窗口: python scripts/_watch_ads_match.py")
            if _match_on:
                _kicked = kick_match_after_boot(
                    _ads_exe,
                    _match_kick,
                    _match_ready,
                    env=_ads_env,
                    cwd=os.path.dirname(_ads_exe),
                )
                _rows = wait_status(_match_status, timeout_s=3600) if _kicked else None
                if not _rows:
                    print("未跑成")
                    _rows = unrun_rows_from_cells(_match_cells)
                write_match_report(
                    _Path(workspace_path) / f"{project_name}_match_50r.xlsx",
                    finalize_rows(_rows, _match_cells, workspace_path, _ael_lib))
        else:
            print("未找到 ads.exe（HPEESOF_DIR 未设置？），请手动打开 ADS 并 load fix_snp_auto.ael")
            if _match_on:
                print("未跑成")
                write_match_report(
                    _Path(workspace_path) / f"{project_name}_match_50r.xlsx",
                    finalize_rows(
                        unrun_rows_from_cells(_match_cells),
                        _match_cells,
                        workspace_path,
                        _ael_lib,
                    ))


    case "3":#refnum
        for i in range(len(res)):
            [ANT_NUM, [visited, reverse_edges], SNP_list, net_work_class] = res[i][:4]
            level_matrix = get_level_matrix(
                reverse_edges, SchematicRes_raw, ANT_NUM, _refnum_dir)
            expanded_matrix = expand_matrix_rows(
                level_matrix, top_lines=1, bottom_lines=1, fill_value=None)
            res[i] = [ANT_NUM, [visited, reverse_edges],
                    SNP_list, net_work_class, expanded_matrix]

        level_matrixs = []
        for _res_entry in res:
            _ant = _res_entry[0]
            _full_edges = _rev_full.get(_ant, (None, _res_entry[1][1]))[1]
            # 编号矩阵用断开锥（功能4）；手册路径仍用未剪的全图（功能5）
            level_matrixs.append([_ant, _res_entry[-1], _full_edges])
        # 每工程固定只写两个文件（见 docs/功能5总设计.md §1.3）：
        #   A 路径中间产物：7_refnum/中间产物/{工程}_path_preview.xlsx
        #   B 成品手册：    7_refnum/{工程}_refnum.xlsx
        # 先写临时再替换原名；不写 _OK/_NEW。原名替换不掉才带时间戳。
        _proj = _ws_label or project_template["project_name"]
        _path_preview = os.path.join(
            _refnum_mid_dir, f"{_proj}_path_preview.xlsx")
        _refnum_final = os.path.join(
            _refnum_dir, f"{_proj}_refnum.xlsx")
        from ads.enabled_values import import_requested

        _ads_ws = str(config_result.get("refnum_ads_workspace", "") or "").strip()
        _value_source = str(config_result.get("refnum_value_source", "") or "").strip().lower()
        if _value_source not in ("ads", "schematic"):
            _value_source = "ads" if import_requested(config_result.get("refnum_ads_import")) else ""
        _component_values = {}
        if _value_source == "schematic":
            from rf.rlc_seed import schematic_handbook_values

            _component_values = schematic_handbook_values(
                getattr(SchematicRes_raw, "part_value", None))
            print(f"[功能5] 原理图原值 {len(_component_values)} 个")
        elif _value_source != "ads":
            print(f"{LogStyle.INFO}ℹ️ 未选择贴器件值，手册不贴{LogStyle.ENDC}")
        elif not _ads_ws:
            print(f"{LogStyle.INFO}ℹ️ 未指定 ADS 工程，手册不贴器件值{LogStyle.ENDC}")
        else:
            try:
                from ads.enabled_values import read_enabled_component_values
                _component_values, _value_notes = read_enabled_component_values(_ads_ws)
            except Exception as _ads_err:
                _value_notes = []
                _component_values = {}
                add_issue(
                    "WARNING",
                    f"[ADS器件值] 读失败，手册不贴值：{_ads_err}",
                    key="ads_values_read",
                )
                print(f"[功能5] ADS 器件值读失败，手册不贴值：{_ads_err}")
            else:
                print(f"[功能5] ADS 使能器件值 {len(_component_values)} 个 ← {_ads_ws}")
                for _note in _value_notes:
                    add_issue("WARNING", f"[ADS器件值] {_note}", key=f"ads_value_{_note}")
                    print(f"[功能5] {_note}")
        try:
            export_repair_manual(
                level_matrixs=level_matrixs,
                filename=_path_preview,
                handbook_filename=_refnum_final,
                schematic_res=SchematicRes_raw,
                signal_infos=SchematicRes_raw.signal_infos,
                gps_paths=GPS_ALL_PATHS,
                transceiver_paths=T_LNA_PA_RFM_paths,
                pin_parser=PINParser,
                component_values=_component_values,
            )
        except Exception as _export_err:
            import traceback as _tb
            print(f"[功能5] export 失败: {_export_err}")
            print(_tb.format_exc())
            raise

    case "4":  # 功能6：器件 Pin 路径
        from pinpath.DevicePinPathExporter import (
            PinPathEvidenceError,
            export_device_pin_paths,
        )
        _structured = list(T_LNA_PA_RFM_paths) + list(T_PA_LNA_ANT_paths)
        if GPS_ALL_PATHS:
            _structured += list(GPS_ALL_PATHS)
        _segments_by_ant = {
            _entry[0]: _entry[3]
            for _entry in res
            if len(_entry) >= 4
        }

        def _pin_label(_refdes: str, _pin: str) -> str:
            _model = SchematicRes_raw.part_infos_name.get(_refdes, "")
            return PINParser.get_pin_labels(_model).get(_pin, "")

        _pin_path_dir = str(_ws_output(_workspace, OUTPUT_OTHER))
        os.makedirs(_pin_path_dir, exist_ok=True)
        _pin_out = os.path.join(
            _pin_path_dir, f"{project_name}_Device_Pin_Path.txt")
        try:
            _rows, _pin_problems = export_device_pin_paths(
                _pin_out,
                structured_paths=_structured,
                segments_by_ant=_segments_by_ant,
                signal_infos=SchematicRes_raw.signal_infos,
                get_part_type=SchematicRes_raw.get_part_type,
                is_network=SchematicRes_raw.is_network,
                get_pin_label=_pin_label,
            )
            print(f"[功能6] 器件 Pin 路径: {_pin_out}（共 {len(_rows)} 条）")
            for _problem in _pin_problems:
                add_issue(
                    "WARNING",
                    f"[器件Pin路径] {_problem}",
                    key=f"pinpath_{_problem.split(':', 1)[0]}",
                )
                print(f"[功能6] 跳过 {_problem}")
        except PinPathEvidenceError as _pin_err:
            add_issue(
                "ERROR",
                f"[器件Pin路径] {_pin_err}",
                key="pinpath_export",
            )
            print(f"[功能6] 导出失败: {_pin_err}")
            raise

print_issue_summary()
