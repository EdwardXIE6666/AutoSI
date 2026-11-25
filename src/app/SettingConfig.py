import os
import sys
import re
import json
import queue
import codecs
import threading
import subprocess
import configparser
import time

# Keysight ADS corrupts the Tcl/Tk library search path via registry.
# Force-point to Python's own tcl/tk before tkinter loads.
_tcl_dir = os.path.join(os.path.dirname(sys.executable), 'tcl')
if os.path.isdir(_tcl_dir) and 'TCL_LIBRARY' not in os.environ:
    os.environ['TCL_LIBRARY'] = os.path.join(_tcl_dir, 'tcl8.6')
    os.environ['TK_LIBRARY'] = os.path.join(_tcl_dir, 'tk8.6')

import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
from app.LogTool import *


# ─── 私有辅助函数 ────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ANSI_SPLIT_RE = re.compile(r'\x1b\[([0-9;]*)m')
_ANSI_TAG_MAP = {
    '1;31': 'ansi_red',
    '1;32': 'ansi_green',
    '1;33': 'ansi_yellow',
    '1;34': 'ansi_blue',
    '1;35': 'ansi_magenta',
    '1;36': 'ansi_cyan',
    # LogStyle 真彩色码（1;38;2;R;G;B），与 LogTool.LogStyle 一一对应
    '1;38;2;255;85;85':   'ansi_red',      # ERROR
    '1;38;2;80;250;123':  'ansi_green',    # SUCCESS
    '1;38;2;97;175;239':  'ansi_blue',     # INFO
    '1;38;2;255;184;108': 'ansi_yellow',   # WARNING
    '1;38;2;189;147;249': 'ansi_magenta',  # HIGHLIGHT
    '1;38;2;139;233;253': 'ansi_cyan',     # NOTICE
}


def _scan_netlist_files(base_dir):
    """返回 2_输出_SIwave网络表/ 下所有 .txt 文件名列表（按修改时间倒序，最新在前）"""
    net_dir = os.path.join(base_dir, "2_输出_SIwave网络表")
    if not os.path.isdir(net_dir):
        return []
    txts = [f for f in os.listdir(net_dir) if f.lower().endswith(".txt")]
    return sorted(txts, key=lambda f: os.path.getmtime(os.path.join(net_dir, f)),
                  reverse=True)


def _scan_siw_files(base_dir):
    """扫描 3_Siwave/ 子文件夹，返回 .siw 文件名列表（已排序）"""
    siw_dir = os.path.join(base_dir, "3_Siwave")
    if not os.path.isdir(siw_dir):
        return []
    return sorted(
        f for f in os.listdir(siw_dir)
        if f.lower().endswith(".siw") and os.path.isfile(os.path.join(siw_dir, f))
    )


def _scan_odb_files(base_dir):
    """扫描 0_ODB/ 子文件夹，返回 ODB++ 压缩包文件名列表（仅 .tgz）。"""
    odb_dir = os.path.join(base_dir, "0_ODB")
    if not os.path.isdir(odb_dir):
        return []
    return sorted(
        f for f in os.listdir(odb_dir)
        if f.lower().endswith(".tgz") and os.path.isfile(os.path.join(odb_dir, f))
    )


def _scan_sch_files(base_dir):
    """扫描 SCH/ 子文件夹，返回 (文件夹路径或None, [原理图文件名列表])"""
    sch_dir = os.path.join(base_dir, "1_SCH_原理图")
    if not os.path.isdir(sch_dir):
        return None, []
    files = sorted([
        f for f in os.listdir(sch_dir)
        if os.path.isfile(os.path.join(sch_dir, f)) and f.lower().endswith(".txt")
    ])
    return sch_dir, files


def _find_snp_library(base_dir):
    """优先查找外置 S 参数库，再回退到程序目录。"""
    project_dir = os.path.dirname(os.path.abspath(base_dir))
    candidates = [
        ("D:\\", ["S参数库", "Sparameter", "2_S参数库"]),
        (project_dir, ["S参数库", "Sparameter", "2_S参数库"]),
        (base_dir, ["S参数库", "Sparameter", "2_S参数库"]),
    ]
    for root, names in candidates:
        for name in names:
            path = os.path.join(root, name)
            if os.path.isdir(path):
                return path
    return None


def _find_board_library(base_dir):
    """优先查找外置板材库，再回退到程序目录。"""
    project_dir = os.path.dirname(os.path.abspath(base_dir))
    candidates = [
        ("D:\\", ["板材库", "0_板材库"]),
        (project_dir, ["板材库", "0_板材库"]),
        (base_dir, ["板材库", "0_板材库"]),
    ]
    for root, names in candidates:
        for name in names:
            path = os.path.join(root, name)
            if os.path.isdir(path):
                return path
    return None


def _scan_board_library(base_dir):
    """扫描板材库，返回 {封装: {板材: {层数: [HDI阶数]}}} 结构。"""
    lib_dir = _find_board_library(base_dir)
    tree = {}
    if not lib_dir:
        return tree
    for pkg in sorted(os.listdir(lib_dir)):
        pd = os.path.join(lib_dir, pkg)
        if not os.path.isdir(pd) or pkg.startswith('.'):
            continue
        for mat in sorted(os.listdir(pd)):
            md = os.path.join(pd, mat)
            if not os.path.isdir(md):
                continue
            layers = sorted(
                (l for l in os.listdir(md) if os.path.isdir(os.path.join(md, l))),
                key=lambda x: int(''.join(c for c in x if c.isdigit()) or '999')
            )
            for lay in layers:
                ld = os.path.join(md, lay)
                hdis = sorted(
                    h for h in os.listdir(ld)
                    if os.path.isdir(os.path.join(ld, h))
                    and any(f.endswith('.xml') for f in os.listdir(os.path.join(ld, h)))
                )
                if hdis:
                    tree.setdefault(pkg, {}).setdefault(mat, {})[lay] = hdis
    return tree


def _find_stackup_xml(base_dir, pkg, mat, layers, hdi):
    """返回板材库中对应叠层的 XML 文件绝对路径，找不到返回 None。"""
    lib_dir = _find_board_library(base_dir)
    if not lib_dir:
        return None
    d = os.path.join(lib_dir, pkg, mat, layers, hdi)
    if not os.path.isdir(d):
        return None
    xmls = [f for f in os.listdir(d) if f.endswith('.xml')]
    return os.path.join(d, xmls[0]) if xmls else None


def _scan_subproject_folders(base_dir):
    """返回 base_dir/SubProject/ 下所有 SubProject_ 开头的文件夹名列表（已排序）"""
    sub_root = os.path.join(base_dir, "4_PCB_S参数")
    if not os.path.isdir(sub_root):
        return []
    try:
        return sorted([
            f for f in os.listdir(sub_root)
            if f.startswith("SubProject_") and os.path.isdir(os.path.join(sub_root, f))
        ])
    except OSError:
        return []


def _extract_project_name(filename):
    """
    从 SCH 文件名提取项目名。
    'FG200A-NA-31-00_SCH_V1.0.2.txt' -> 'FG200A-NA-31-00'
    无 _SCH 则返回去掉扩展名的完整文件名。
    """
    stem = os.path.splitext(filename)[0]
    idx = stem.upper().find("_SCH")
    return stem[:idx] if idx != -1 else stem


def _project_key(filename: str) -> str:
    """从任意文件名/文件夹名提取项目根名（FG... 前缀），用于跨文件项目一致性校验。"""
    stem = os.path.splitext(filename)[0]
    if stem.startswith("SubProject_"):
        stem = stem[len("SubProject_"):]
    stem_u = stem.upper()
    for sep in ("_SCH", "_PCB"):
        idx = stem_u.find(sep)
        if idx != -1:
            return stem[:idx]
    idx = stem.find("+")
    if idx != -1:
        return stem[:idx]
    return stem


def _ads_dir_from_subproject(subproject_name):
    """
    从 SubProject 文件夹名推导 ADS 目录名。
    'SubProject_FG200A-NA-31-00_PCB_20260316' -> 'FG200A-NA-31-00_PCB_20260316_ADS'
    """
    prefix = "SubProject_"
    base = subproject_name[len(prefix):] if subproject_name.startswith(prefix) else subproject_name
    return base + "_ADS"


# ─── GUI 类 ──────────────────────────────────────────────────────────────

class _ProjectSelectorApp:
    # ── Apple 风格配色（白/灰/近黑 + 单一 Action Blue）──────────────
    # 参考 design-md/apple/DESIGN.md：UI chrome 后退，交互色统一 #0066cc。
    _ACCENT       = "#0066cc"   # Action Blue（所有主操作 / 选中态）
    _ACCENT_HOVER = "#0071e3"   # Focus Blue
    _ACCENT_LIGHT = "#2997ff"   # 深色面上的链接蓝
    _ACCENT_SOFT  = "#f5f5f7"   # Apple parchment / chip 底
    _ACCENT_TEXT  = "#0066cc"   # 浅底上的 Action Blue
    _ON_ACCENT    = "#ffffff"
    _BG           = "#f5f5f7"   # 页面底
    _SURFACE      = "#ffffff"   # 主面
    _SURFACE2     = "#fafafc"   # pearl 控件底
    _SURFACE_RAISED = "#ffffff"
    _BORDER       = "#e0e0e0"   # Apple hairline
    _INK          = "#1d1d1f"
    _MUTED        = "#7a7a7a"
    _OK           = "#1d7f43"
    _WARN         = "#9a6500"
    _ERROR        = "#c02b25"

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.result   = None
        self.choice   = None

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self._root = ctk.CTk()
        self._root.title("Fibocom AutoSI")
        self._root.geometry("1120x920")
        self._root.resizable(True, True)
        self._root.minsize(900, 700)
        self._root.configure(fg_color=self._BG)

        # 窗口图标（Fibocom 4色 F）
        _ico = os.path.join(base_dir, "fibocom.ico")
        if not os.path.isfile(_ico):
            _ico = os.path.join(base_dir, "_internal", "fibocom.ico")
        if os.path.isfile(_ico):
            try:
                self._root.iconbitmap(_ico)
            except Exception:
                pass

        # 扫描文件夹（启动时一次）
        self._sch_dir, self._sch_files = _scan_sch_files(base_dir)
        self._snp_path                 = _find_snp_library(base_dir)
        self._sub_folders              = _scan_subproject_folders(base_dir)
        self._siw_files                = _scan_siw_files(base_dir)
        # 初始化第一步（可选）：ODB++ 导入。0_ODB 不存在则建空目录，方便用户放置 ODB++
        os.makedirs(os.path.join(base_dir, "0_ODB"), exist_ok=True)
        self._odb_files                = _scan_odb_files(base_dir)

        # 状态变量
        self._func_var = tk.StringVar(value="1")
        self._sch_var  = tk.StringVar(value=self._sch_files[0] if self._sch_files else "")
        self._sub_var  = tk.StringVar(value=self._sub_folders[0] if self._sub_folders else "")

        # SIwave 抽取配置（功能 3）：从文件夹扫描后列表选择，默认选第一个/最新一个
        self._netlist_files  = _scan_netlist_files(base_dir)
        self._siw_var        = tk.StringVar(value=self._siw_files[0] if self._siw_files else "")
        self._nets_var       = tk.StringVar(value=self._netlist_files[0] if self._netlist_files else "")
        # ODB++ 导入选择（初始化第一步）：默认不选，需用户显式从 0_ODB/ 选定后再导入
        self._odb_var        = tk.StringVar(value=self._odb_files[0] if self._odb_files else "")
        self._siwave_out_var = tk.StringVar(value=os.path.join(base_dir, "4_PCB_S参数"))
        # 并行抽取进程数：默认读取 config.json 现有值（持久化），1=单进程
        self._par_workers_var = tk.StringVar(value=self._read_cfg_workers())

        self._q = None  # 子进程输出队列（运行时创建）

        # SIwave 初始化状态（叠层导入 + R/L/C 去激活）
        self._stk_tree = _scan_board_library(base_dir)
        self._stk_en_var  = tk.BooleanVar(value=False)
        self._init_deactivate_var = tk.BooleanVar(value=True)
        self._init_stackup_var = tk.BooleanVar(value=True)
        self._match_50r_var = tk.BooleanVar(value=False)
        self._stk_pkg_var = tk.StringVar()
        self._stk_mat_var = tk.StringVar()
        self._stk_lay_var = tk.StringVar()
        self._stk_hdi_var = tk.StringVar()
        self._stk_import_done = False  # 叠层是否已导入（本次会话内）
        self._icon_cache = {}

        self._build_ui()
        self._refresh_sections()
        self._build_welcome_page()
        self._file_list_snapshot = self._scan_file_list_snapshot()
        self._root.after(2000, self._poll_file_list_changes)

    def _icon(self, name, color=None, size=20):
        color = color or self._INK
        key = (name, color, size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        from PIL import Image, ImageDraw
        target_size = size
        size = 24
        scale = 3
        w = h = size * scale
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        c = color
        sw = max(2, int(size * 0.11)) * scale
        p = lambda v: int(v * scale)

        if name == "play_arrow":
            d.polygon([(p(7), p(5)), (p(7), p(size - 5)), (p(size - 5), p(size / 2))], fill=c)
        elif name == "layers":
            d.polygon([(p(size / 2), p(3)), (p(size - 3), p(7)), (p(size / 2), p(11)), (p(3), p(7))],
                      outline=c, width=sw)
            d.line([(p(4), p(11)), (p(size / 2), p(15)), (p(size - 4), p(11))], fill=c, width=sw)
            d.line([(p(4), p(15)), (p(size / 2), p(19)), (p(size - 4), p(15))], fill=c, width=sw)
        elif name == "memory":
            d.rounded_rectangle([p(5), p(5), p(size - 5), p(size - 5)], radius=p(2), outline=c, width=sw)
            for x in (3, size - 3):
                for y in (7, 11, 15):
                    d.line([(p(x), p(y)), (p(5 if x < size / 2 else size - 5), p(y))], fill=c, width=sw)
        elif name == "account_tree":
            boxes = [(3, 4, 8, 9), (13, 3, 19, 9), (13, 14, 19, 20)]
            for box in boxes:
                d.rounded_rectangle([p(v) for v in box], radius=p(1.5), outline=c, width=sw)
            d.line([(p(8), p(6.5)), (p(11), p(6.5)), (p(11), p(17)), (p(13), p(17))], fill=c, width=sw)
            d.line([(p(11), p(6.5)), (p(13), p(6.5))], fill=c, width=sw)
        elif name == "analytics":
            d.line([(p(4), p(18)), (p(20), p(18))], fill=c, width=sw)
            for x, top in ((6, 11), (11, 7), (16, 4)):
                d.rounded_rectangle([p(x), p(top), p(x + 3), p(18)], radius=p(1), fill=c)
        elif name == "table_chart":
            d.rounded_rectangle([p(4), p(5), p(20), p(19)], radius=p(2), outline=c, width=sw)
            d.line([(p(4), p(10)), (p(20), p(10))], fill=c, width=sw)
            d.line([(p(9), p(5)), (p(9), p(19))], fill=c, width=sw)
        elif name == "folder_open":
            d.line([(p(3), p(8)), (p(8), p(8)), (p(10), p(11)), (p(21), p(11))], fill=c, width=sw)
            d.rounded_rectangle([p(3), p(8), p(21), p(19)], radius=p(2), outline=c, width=sw)
            d.line([(p(6), p(18)), (p(10), p(12)), (p(22), p(12)), (p(18), p(19))], fill=c, width=sw)
        elif name == "image":
            d.rounded_rectangle([p(4), p(5), p(20), p(19)], radius=p(2), outline=c, width=sw)
            d.ellipse([p(14), p(8), p(17), p(11)], fill=c)
            d.line([(p(6), p(17)), (p(11), p(12)), (p(14), p(15)), (p(16), p(13)), (p(20), p(17))],
                   fill=c, width=sw)
        elif name == "check_circle":
            d.ellipse([p(3), p(3), p(size - 3), p(size - 3)], outline=c, width=sw)
            d.line([(p(7), p(12)), (p(11), p(16)), (p(18), p(8))], fill=c, width=sw)
        elif name == "error":
            d.ellipse([p(3), p(3), p(size - 3), p(size - 3)], outline=c, width=sw)
            d.line([(p(12), p(6)), (p(12), p(14))], fill=c, width=sw)
            d.ellipse([p(11), p(17), p(13), p(19)], fill=c)
        elif name == "arrow_back":
            d.line([(p(19), p(12)), (p(6), p(12))], fill=c, width=sw)
            d.line([(p(11), p(7)), (p(6), p(12)), (p(11), p(17))], fill=c, width=sw)
        else:
            d.ellipse([p(4), p(4), p(size - 4), p(size - 4)], outline=c, width=sw)

        self._icon_cache[key] = ctk.CTkImage(im, size=(target_size, target_size))
        return self._icon_cache[key]

    def _configure_confirm_action(self, text, icon_name="play_arrow"):
        if hasattr(self, "_confirm_btn"):
            self._confirm_btn.configure(
                text=text,
                image=self._icon(icon_name, self._ON_ACCENT, 22),
                compound="left"
            )

    def _configure_stk_action(self, text, icon_name="layers", state=None):
        if not hasattr(self, "_stk_import_btn"):
            return
        kwargs = {
            "text": text,
            "image": self._icon(icon_name, self._ON_ACCENT, 18),
            "compound": "left",
        }
        if state is not None:
            kwargs["state"] = state
        self._configure_stk_import_btn(**kwargs)

    # ── UI 构建 ──────────────────────────────────────────────────────────

    def _build_ui(self):
        # 置顶状态
        self._pin_var = tk.BooleanVar(value=False)
        self._build_app_bar()

        self._build_tip_bar()

        self._main_area = ctk.CTkFrame(self._root, fg_color="transparent")
        self._main_area.pack(fill="both", expand=True, padx=30, pady=(4, 8))

        # 功能选择：左侧导航
        self._func_card = ctk.CTkFrame(
            self._main_area, fg_color=self._SURFACE_RAISED, corner_radius=18,
            border_width=1, border_color=self._BORDER)
        self._func_card.pack(side="left", fill="y", padx=(0, 16), pady=(0, 0))
        self._func_card.configure(width=200)
        self._func_card.pack_propagate(False)
        self._attach_shadow(self._func_card)

        ctk.CTkLabel(
            self._func_card, text="AutoSI 流程",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=16, weight="bold"),
            text_color=self._INK
        ).pack(anchor="w", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            self._func_card, text="选择要执行的步骤",
            font=ctk.CTkFont(size=11),
            text_color=self._MUTED
        ).pack(anchor="w", padx=16, pady=(0, 14))

        btn_row = ctk.CTkFrame(self._func_card, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 14))

        func_labels = {
            "1": ("01", "网络频段信息", "account_tree"),
            "2": ("02", "初始化 SIWAVE", "layers"),
            "3": ("03", "SIwave 抽取", "memory"),
            "4": ("04", "ADS 仿真", "analytics"),
            "5": ("05", "refnum 生成", "table_chart"),
            "6": ("06", "器件 Pin 路径", "account_tree"),
        }
        self._func_btns = {}
        for key, (step, label, icon_name) in func_labels.items():
            active = key == "1"
            item = ctk.CTkFrame(
                btn_row, fg_color=self._SURFACE if active else "transparent",
                corner_radius=18, border_width=1,
                border_color=self._ACCENT if active else self._BORDER)
            item.pack(fill="x", pady=(0, 8))
            stripe = ctk.CTkFrame(
                item, width=3, height=38,
                fg_color=self._ACCENT if active else "transparent",
                corner_radius=2)
            stripe.pack(side="left", fill="y", padx=(0, 8), pady=6)
            btn = ctk.CTkButton(
                item, text=f"{step}  {label}", height=46, corner_radius=18,
                image=self._icon(icon_name, self._INK if active else self._MUTED, 20),
                compound="left",
                anchor="w",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
                fg_color="transparent",
                text_color=self._INK if active else self._MUTED,
                hover_color="#f5f5f7",
                border_width=0,
                command=lambda k=key: self._on_func_change(k)
            )
            btn.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=4)
            self._func_btns[key] = {"item": item, "stripe": stripe, "button": btn, "icon": icon_name}

        # 可滚动内容区
        self._scroll = ctk.CTkScrollableFrame(
            self._main_area, fg_color=self._BG, corner_radius=0,
            border_width=0)
        self._scroll.pack(side="left", fill="both", expand=True)

        # 项目不一致警告条（初始隐藏，_validate 根据需要插入 scroll 上方）
        self._warn_frame = ctk.CTkFrame(self._root, fg_color=self._ERROR, corner_radius=8)
        self._warn_label = ctk.CTkLabel(
            self._warn_frame, text="",
            text_color="#ffffff",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
            wraplength=580, justify="left", anchor="w",
        )
        self._warn_label.pack(fill="x", padx=14, pady=8)

        # 各配置区块（创建但先不 pack，由 _refresh_sections 控制）
        self._sch_frame = self._make_card(self._scroll, "原理图文件（1_SCH_原理图/）")
        self._sch_list_frame = ctk.CTkFrame(self._sch_frame, fg_color="transparent")
        self._sch_list_frame.pack(fill="x", padx=16, pady=(0, 12))
        self._rebuild_sch_list()

        self._snp_frame = self._make_card(self._scroll, "S 参数库")
        snp_msg, snp_color = (
            (f"✓  已找到   {self._snp_path}/", self._OK)
            if self._snp_path else
            ("⚠  未找到 D盘或程序目录下的 S 参数库，部分功能可能失败", self._WARN)
        )
        ctk.CTkLabel(self._snp_frame, text=snp_msg, text_color=snp_color,
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=13)).pack(anchor="w", padx=16, pady=(0, 12))

        self._sub_frame = self._make_card(self._scroll, "PCB 参数（4_PCB_S参数/）")
        self._sub_list_frame = ctk.CTkFrame(self._sub_frame, fg_color="transparent")
        self._sub_list_frame.pack(fill="x", padx=16, pady=(0, 12))

        self._ads_frame = self._make_card(self._scroll, "ADS 项目目录")
        self._ads_label = ctk.CTkLabel(
            self._ads_frame, text="—", text_color=self._MUTED,
            font=ctk.CTkFont(size=12)
        )
        self._ads_label.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkCheckBox(
            self._ads_frame,
            text="天线口自动匹配仿真",
            variable=self._match_50r_var,
            command=self._on_match_50r_toggle,
            fg_color=self._ACCENT,
            hover_color=self._ACCENT_HOVER,
            border_color=self._BORDER,
            text_color=self._INK,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold")
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self._match_50r_hint = ctk.CTkLabel(
            self._ads_frame,
            text="将按现有拓扑、从标准电感电容表里选值调 L/C，使天线口尽量靠近 50Ω，带内 VSWR < 1.3 为验收。",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
            text_color=self._MUTED,
            wraplength=760,
            justify="left",
        )

        # SIwave 抽取配置卡片（功能 3）
        self._siwave_frame = self._make_card(self._scroll, "SIwave S 参数抽取")

        ctk.CTkLabel(self._siwave_frame, text=".siw 工程文件（3_Siwave/）",
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=13), text_color=self._MUTED
                     ).pack(anchor="w", padx=16)
        self._siw_list_frame = ctk.CTkFrame(self._siwave_frame, fg_color="transparent")
        self._siw_list_frame.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(self._siwave_frame, text="网络频段信息 .txt（2_输出_SIwave网络表/，功能 1 的输出）",
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=13), text_color=self._MUTED
                     ).pack(anchor="w", padx=16)
        self._nets_list_frame = ctk.CTkFrame(self._siwave_frame, fg_color="transparent")
        self._nets_list_frame.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(self._siwave_frame, text="结果输出目录（默认 4_PCB_S参数，即功能 4 的输入）",
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=13), text_color=self._MUTED
                     ).pack(anchor="w", padx=16)
        ctk.CTkEntry(
            self._siwave_frame, textvariable=self._siwave_out_var, width=520,
            fg_color=self._SURFACE2, border_color=self._BORDER,
            text_color=self._INK
        ).pack(anchor="w", padx=16, pady=(0, 12))

        ctk.CTkLabel(self._siwave_frame,
                     text="并行抽取进程数（默认 1=单进程；内存带宽充足的工作站可设 2~4 提速）",
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=13), text_color=self._MUTED
                     ).pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            self._siwave_frame, variable=self._par_workers_var,
            values=["1", "2", "3", "4"], width=90,
            fg_color=self._SURFACE2, button_color=self._ACCENT,
            button_hover_color=self._ACCENT_HOVER, text_color=self._INK,
            dropdown_fg_color=self._SURFACE, dropdown_text_color=self._INK
        ).pack(anchor="w", padx=16, pady=(0, 12))

        self._rebuild_siw_list()
        self._rebuild_nets_list()

        # SIwave 初始化卡片（功能 2）
        self._stk_frame = self._make_card(self._scroll, "初始化 SIWAVE")
        self._build_stackup_card_body()

        self._rebuild_sub_list()

        # 确认按钮
        self._confirm_btn = ctk.CTkButton(
            self._root, text="确认并运行",
            image=self._icon("play_arrow", self._ON_ACCENT, 22),
            compound="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold"),
            fg_color=self._ACCENT, text_color=self._ON_ACCENT,
            text_color_disabled=self._MUTED,
            hover_color=self._ACCENT_HOVER, height=48, corner_radius=24,
            border_width=0,
            command=self._on_confirm
        )
        self._confirm_btn.pack(fill="x", padx=30, pady=(0, 22))

        self._refresh_header_state()

    def _welcome_reveal_label(self, parent, final_color, **kwargs):
        label = ctk.CTkLabel(parent, text_color=self._SURFACE, **kwargs)
        self._welcome_reveal_widgets.append((label, final_color))
        return label

    @staticmethod
    def _blend_hex_color(start, end, ratio):
        start_rgb = tuple(int(start[i:i + 2], 16) for i in (1, 3, 5))
        end_rgb = tuple(int(end[i:i + 2], 16) for i in (1, 3, 5))
        rgb = tuple(round(a + (b - a) * ratio) for a, b in zip(start_rgb, end_rgb))
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    def _build_welcome_page(self):
        self._welcome_reveal_widgets = []
        self._welcome_frame = ctk.CTkFrame(
            self._root, fg_color=self._BG, corner_radius=0, border_width=0)
        self._welcome_frame.place(x=0, y=0, relwidth=1, relheight=1)
        self._welcome_frame.lift()

        header = ctk.CTkFrame(
            self._welcome_frame, fg_color=self._SURFACE, corner_radius=0,
            border_width=0)
        header.pack(fill="x")
        self._make_fibocom_logo(header).pack(side="left", padx=(42, 30), pady=28)
        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.pack(side="left", fill="x", expand=True, pady=24)
        self._welcome_title_label = self._welcome_reveal_label(
            title_group, text="欢迎使用 AutoSI",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=32, weight="bold"),
            final_color=self._INK)
        self._welcome_title_label.pack(anchor="w")
        self._welcome_reveal_label(
            title_group, text="射频 SI 自动化工具 · 从原理图分析到 SIwave、ADS 与维修链路输出",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=16),
            final_color=self._MUTED).pack(anchor="w", pady=(6, 0))
        right_header = ctk.CTkFrame(header, fg_color="transparent")
        right_header.pack(side="right", padx=42)
        self._welcome_motion_label = ctk.CTkLabel(
            right_header, text="●  ○  ○", text_color=self._ACCENT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=18, weight="bold"))
        self._welcome_motion_label.pack(pady=(0, 8))
        ctk.CTkLabel(
            right_header, text="仅限内部使用", height=30, corner_radius=15,
            fg_color=self._ACCENT_SOFT, text_color=self._ACCENT_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12, weight="bold")
        ).pack()

        body = ctk.CTkFrame(
            self._welcome_frame, fg_color=self._BG, corner_radius=0,
            border_width=0)
        body.pack(fill="both", expand=True, padx=42, pady=28)
        body.grid_columnconfigure((0, 1), weight=1, uniform="welcome_card")
        body.grid_rowconfigure(0, weight=1)

        self._welcome_intro_card = ctk.CTkFrame(
            body, fg_color=self._SURFACE, corner_radius=26,
            border_width=2, border_color=self._BORDER)
        self._welcome_intro_card.grid(
            row=0, column=0, sticky="nsew", padx=(0, 14))
        self._welcome_reveal_label(
            self._welcome_intro_card, text="AutoSI 能做什么",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=26, weight="bold"),
            final_color=self._INK).pack(anchor="w", padx=30, pady=(34, 12))
        self._welcome_reveal_label(
            self._welcome_intro_card,
            text="把射频工作流中的重复步骤自动化。",
            wraplength=520, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=17),
            final_color=self._MUTED).pack(anchor="w", padx=30, pady=(0, 24))
        self._welcome_intro_items = ctk.CTkFrame(
            self._welcome_intro_card, fg_color="transparent")
        intro_items = self._welcome_intro_items
        intro_items.pack(fill="both", expand=True, padx=30, pady=(0, 24))
        intro_items.grid_columnconfigure(0, weight=1)
        for row_index in range(4):
            intro_items.grid_rowconfigure(row_index, weight=1, uniform="intro_item")
        for row_index, title in enumerate((
                "原理图 TXT 分析",
                "SIwave 自动化抽取",
                "ADS 自动化仿真",
                "refnum 维修链路输出")):
            bubble = ctk.CTkFrame(
                intro_items, fg_color=self._SURFACE2, corner_radius=20,
                border_width=1, border_color=self._BORDER)
            bubble.grid(row=row_index, column=0, sticky="nsew", pady=6)
            ctk.CTkLabel(
                bubble, text="✔", width=36, height=36, corner_radius=18,
                fg_color=self._ACCENT_SOFT, text_color=self._ACCENT_TEXT,
                font=ctk.CTkFont(size=16, weight="bold")
            ).pack(side="left", padx=(16, 12), pady=12)
            self._welcome_reveal_label(
                bubble, text=title, anchor="w",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=19, weight="bold"),
                final_color=self._INK).pack(side="left", fill="x", expand=True,
                                            padx=(0, 16), pady=12)

        self._welcome_prep_card = ctk.CTkFrame(
            body, fg_color=self._SURFACE, corner_radius=26,
            border_width=2, border_color=self._BORDER)
        self._welcome_prep_card.grid(
            row=0, column=1, sticky="nsew", padx=(14, 0))
        self._welcome_reveal_label(
            self._welcome_prep_card, text="开始前需要准备什么",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=26, weight="bold"),
            final_color=self._INK).pack(anchor="w", padx=30, pady=(34, 12))
        self._welcome_reveal_label(
            self._welcome_prep_card,
            text="把对应资料放入工作目录，再进入 AutoSI 按步骤执行。",
            wraplength=520, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=17),
            final_color=self._MUTED).pack(anchor="w", padx=30, pady=(0, 24))
        self._welcome_prep_items = ctk.CTkFrame(
            self._welcome_prep_card, fg_color="transparent")
        prep_items = self._welcome_prep_items
        prep_items.pack(fill="both", expand=True, padx=30, pady=(0, 24))
        prep_items.grid_columnconfigure(0, weight=1)
        for row_index in range(4):
            prep_items.grid_rowconfigure(row_index, weight=1, uniform="prep_item")
        for row_index, (num, title, desc) in enumerate((
                ("1", "库与软件环境", "官方 S 参数库、板材库、ADS 和 SIwave"),
                ("2", "原理图", "将 PADS 原理图 txt 放入 1_SCH_原理图"),
                ("3", "ODB++ 或 SIwave 工程", "ODB++ 放入 0_ODB，或将 .siw 放入 3_Siwave"),
                ("4", "文件占用检查", "关闭即将被覆盖的工程或 Excel 结果"))):
            row = ctk.CTkFrame(
                prep_items, fg_color=self._SURFACE2, corner_radius=20,
                border_width=1, border_color=self._BORDER)
            row.grid(row=row_index, column=0, sticky="nsew", pady=6)
            ctk.CTkLabel(
                row, text=num, width=36, height=36, corner_radius=18,
                fg_color=self._INK, text_color=self._ON_ACCENT,
                font=ctk.CTkFont(size=14, weight="bold")
            ).pack(side="left", padx=(16, 12), pady=12)
            text_group = ctk.CTkFrame(row, fg_color="transparent")
            text_group.pack(side="left", fill="x", expand=True, padx=(0, 16), pady=10)
            self._welcome_reveal_label(
                text_group, text=title, anchor="w",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=18, weight="bold"),
                final_color=self._INK).pack(fill="x")
            self._welcome_reveal_label(
                text_group, text=desc, anchor="w", wraplength=470, justify="left",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=14),
                final_color=self._MUTED).pack(fill="x", pady=(3, 0))

        footer = ctk.CTkFrame(
            self._welcome_frame, fg_color=self._SURFACE, corner_radius=0,
            border_width=0)
        footer.pack(fill="x")
        ctk.CTkLabel(
            footer,
            text="首次使用请先阅读《自动化仿真操作指导》",
            text_color=self._MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14),
        ).pack(side="left", padx=42, pady=18)
        ctk.CTkButton(
            footer, text="进入 AutoSI  →", width=300, height=54,
            corner_radius=27, fg_color=self._ACCENT, hover_color=self._ACCENT_HOVER,
            text_color=self._ON_ACCENT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=16, weight="bold"),
            command=self._enter_autosi
        ).pack(side="right", padx=42, pady=18)
        ctk.CTkButton(
            footer, text="查看操作指导", width=180, height=54,
            corner_radius=27, fg_color=self._SURFACE2, hover_color=self._BG,
            border_width=1, border_color=self._BORDER, text_color=self._INK,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold"),
            command=self._open_ops_guide
        ).pack(side="right", padx=(0, 12), pady=18)
        self._root.after(180, self._animate_welcome_reveal)
        self._root.after(250, self._animate_welcome)

    def _animate_welcome_reveal(self, index=0, phase=1):
        welcome = getattr(self, "_welcome_frame", None)
        widgets = getattr(self, "_welcome_reveal_widgets", ())
        if welcome is None or index >= len(widgets):
            return
        try:
            if not welcome.winfo_exists():
                return
            widget, final_color = widgets[index]
            widget.configure(text_color=self._blend_hex_color(
                self._SURFACE, final_color, phase / 4))
            if phase < 4:
                self._root.after(
                    45, lambda: self._animate_welcome_reveal(index, phase + 1))
            else:
                self._root.after(
                    70, lambda: self._animate_welcome_reveal(index + 1, 1))
        except tk.TclError:
            pass

    def _animate_welcome(self, step=0):
        welcome = getattr(self, "_welcome_frame", None)
        if welcome is None:
            return
        try:
            if not welcome.winfo_exists():
                return
            motion = ("●  ○  ○", "○  ●  ○", "○  ○  ●")
            self._welcome_motion_label.configure(text=motion[step % len(motion)])
            self._root.after(480, lambda: self._animate_welcome(step + 1))
        except tk.TclError:
            pass

    def _ops_guide_path(self):
        return os.path.join(self.base_dir, "自动化仿真操作指导.docx")

    def _open_ops_guide(self):
        """打开程序目录下的《自动化仿真操作指导.docx》。"""
        path = self._ops_guide_path()
        if not os.path.isfile(path):
            messagebox.showwarning(
                "操作指导",
                f"未找到操作指导文件：\n{path}\n\n"
                "请确认该文档与 MAIN.exe 在同一目录（发布包会随包分发）。",
            )
            return
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(
                ["explorer", "/select,", path],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def _enter_autosi(self):
        welcome = getattr(self, "_welcome_frame", None)
        if welcome is None:
            return
        try:
            if welcome.winfo_exists():
                welcome.destroy()
        except tk.TclError:
            pass
        self._welcome_frame = None
        if hasattr(self, "_home_btn"):
            try:
                self._home_btn.configure(state="normal")
            except tk.TclError:
                pass
        self._root.after_idle(lambda: self._root.focus_force())

    def _return_to_welcome(self):
        """工作区返回欢迎页；运行中提示确认；返回前尽量切回配置页。"""
        if getattr(self, "_welcome_frame", None) is not None:
            try:
                if self._welcome_frame.winfo_exists():
                    return
            except tk.TclError:
                pass
        prog = getattr(self, "_prog_frame", None)
        on_progress = False
        if prog is not None:
            try:
                on_progress = bool(prog.winfo_exists())
            except tk.TclError:
                on_progress = False
        # 进度页且「返回配置」仍禁用 ≈ 任务未结束
        running = False
        if on_progress:
            back_btn = getattr(self, "_back_btn", None)
            if back_btn is not None:
                try:
                    running = str(back_btn.cget("state")) == "disabled"
                except tk.TclError:
                    running = True
            else:
                running = True
        if running:
            if not messagebox.askyesno(
                "返回欢迎页",
                "当前有任务正在运行或尚未结束。\n"
                "返回欢迎页不会强制杀后台进程，但界面将离开运行页。确定吗？",
            ):
                return
        if on_progress:
            try:
                self._back_to_config()
            except Exception:
                pass
        self._build_welcome_page()
        if hasattr(self, "_home_btn"):
            try:
                self._home_btn.configure(state="disabled")
            except tk.TclError:
                pass

    def _build_app_bar(self):
        self._app_bar = ctk.CTkFrame(
            self._root, fg_color=self._SURFACE, corner_radius=0,
            border_width=0, border_color=self._BORDER
        )
        self._app_bar.pack(fill="x", padx=0, pady=(0, 10))
        self._attach_shadow(self._app_bar)

        left = ctk.CTkFrame(self._app_bar, fg_color="transparent")
        left.pack(side="left", padx=24, pady=10)
        self._make_fibocom_logo(left).pack(side="left")
        self._home_btn = ctk.CTkButton(
            left, text="欢迎页", width=72, height=28, corner_radius=14,
            fg_color=self._SURFACE2, hover_color=self._BG,
            border_width=1, border_color=self._BORDER, text_color=self._INK,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12, weight="bold"),
            command=self._return_to_welcome, state="disabled",
        )
        self._home_btn.pack(side="left", padx=(16, 0))
        try:
            self._root.bind_all("<Control-h>", lambda _e: self._return_to_welcome())
            self._root.bind_all("<Control-H>", lambda _e: self._return_to_welcome())
        except tk.TclError:
            pass

        center = ctk.CTkFrame(self._app_bar, fg_color="transparent")
        center.pack(side="left", fill="x", expand=True, padx=(24, 18), pady=10)
        self._ownership_label = ctk.CTkLabel(
            center,
            text="版权所有 © 深圳市广和通无线股份有限公司 MBB｜仅限内部使用，未经授权严禁外传",
            height=34,
            corner_radius=18,
            fg_color=self._ACCENT_SOFT,
            text_color=self._INK,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
            anchor="center"
        )
        self._ownership_label.pack(fill="x", expand=True, pady=(8, 6))

        right = ctk.CTkFrame(self._app_bar, fg_color="transparent")
        right.pack(side="right", padx=24, pady=10)
        self._pin_switch = ctk.CTkSwitch(
            right, text="置顶", variable=self._pin_var,
            font=ctk.CTkFont(size=11), text_color=self._MUTED,
            fg_color="#d2d2d7", progress_color=self._ACCENT,
            button_color=self._SURFACE, button_hover_color=self._SURFACE,
            command=lambda: self._root.attributes('-topmost', self._pin_var.get())
        )
        self._pin_switch.pack(side="right", padx=(10, 0))
        self._snp_chip = ctk.CTkLabel(
            right, text="", corner_radius=13, height=26, padx=12,
            font=ctk.CTkFont(size=11, weight="bold"), anchor="center"
        )
        self._snp_chip.pack(side="right", padx=(8, 0))
        self._func_chip = ctk.CTkLabel(
            right, text="", corner_radius=13, width=62, height=26, padx=12,
            fg_color=self._ACCENT_SOFT, text_color=self._INK,
            font=ctk.CTkFont(size=11, weight="bold"), anchor="center"
        )
        self._func_chip.pack(side="right")

    def _make_card(self, parent, title):
        frame = ctk.CTkFrame(parent, fg_color=self._SURFACE_RAISED, corner_radius=18,
                             border_width=1, border_color=self._BORDER)
        self._attach_shadow(frame)
        head = ctk.CTkFrame(
            frame, fg_color=self._SURFACE2, corner_radius=14,
            border_width=0)
        head.pack(fill="x", padx=14, pady=(14, 12))
        ctk.CTkFrame(head, width=3, height=18, fg_color=self._ACCENT,
                     corner_radius=2).pack(side="left", padx=(10, 10), pady=9)
        ctk.CTkLabel(head, text=title,
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"),
                     text_color=self._INK).pack(side="left", pady=9)
        return frame

    def _refresh_header_state(self):
        if hasattr(self, "_func_chip"):
            self._func_chip.configure(text=f"功能 {self._func_var.get()}")
        if hasattr(self, "_snp_chip"):
            if self._snp_path:
                self._snp_chip.configure(
                    text="S 参数库已就绪",
                    fg_color="#f5f5f7",
                    text_color=self._OK
                )
            else:
                self._snp_chip.configure(
                    text="S 参数库缺失",
                    fg_color="#f5f5f7",
                    text_color=self._ERROR
                )

    def _attach_shadow(self, widget, shadow_color=None, dx=0, dy=0):
        # CustomTkinter does not provide real elevation; overlay shadows built with
        # place() can cover packed siblings after resize/scroll refresh.
        return None

    def _ops_guide_tip_text(self):
        return (
            "操作指导：欢迎页可点「查看操作指导」，"
            "或打开程序目录下的《自动化仿真操作指导》"
        )

    def _build_tip_bar(self):
        self._tip_messages = [
            self._license_tip_text(),
            self._ops_guide_tip_text(),
            "S参数库：正在校验官方完整性..."
        ]
        self._tip_frame = ctk.CTkFrame(
            self._root, fg_color=self._SURFACE, corner_radius=0,
            border_width=1, border_color=self._BORDER)
        self._tip_frame.pack(fill="x", padx=48, pady=(0, 8))
        self._attach_shadow(self._tip_frame)
        ctk.CTkLabel(
            self._tip_frame, text="提示",
            width=36, text_color=self._ACCENT_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=11, weight="bold")
        ).pack(side="left", padx=(10, 4), pady=3)
        self._tip_canvas = tk.Canvas(
            self._tip_frame, height=18, bg=self._SURFACE, highlightthickness=0,
            bd=0
        )
        self._tip_canvas.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=3)
        self._tip_text_id = None
        self._tip_speed_px = 1
        self._tip_canvas.bind("<Configure>", lambda _e: self._reset_tip_canvas_text())
        self._animate_tip_bar()
        self._start_snp_verify_for_tip()

    def _license_tip_text(self):
        try:
            from app._build_license import BUILD_EXPIRES_AT
            return f"授权：MAIN.exe 有效期至 {BUILD_EXPIRES_AT}"
        except Exception:
            return "授权：未读取到 MAIN.exe 授权时间，请联系管理员确认打包结果"

    def _set_tip_messages(self, messages):
        self._tip_messages = [m for m in messages if m]
        if hasattr(self, "_tip_canvas"):
            self._reset_tip_canvas_text()

    def _tip_text(self):
        return "    |    ".join(self._tip_messages) or "提示：暂无状态信息"

    def _reset_tip_canvas_text(self):
        if not hasattr(self, "_tip_canvas"):
            return
        text = self._tip_text()
        self._tip_canvas.delete("tip_text")
        self._tip_canvas.update_idletasks()
        width = max(self._tip_canvas.winfo_width(), 1)
        self._tip_text_id = self._tip_canvas.create_text(
            width, 9, text=text, anchor="w", fill=self._INK,
            font=("Microsoft YaHei UI", 10), tags="tip_text"
        )

    def _animate_tip_bar(self):
        if not hasattr(self, "_tip_canvas"):
            return
        try:
            if self._tip_text_id is None:
                self._reset_tip_canvas_text()
            else:
                self._tip_canvas.move(self._tip_text_id, -self._tip_speed_px, 0)
                bbox = self._tip_canvas.bbox(self._tip_text_id)
                if bbox and bbox[2] < 0:
                    self._tip_canvas.coords(
                        self._tip_text_id, self._tip_canvas.winfo_width(), 9)
            self._root.after(16, self._animate_tip_bar)
        except Exception:
            pass

    def _start_snp_verify_for_tip(self):
        def worker():
            message = self._build_snp_tip_message()
            self._root.after(0, lambda: self._set_tip_messages([
                self._license_tip_text(),
                self._ops_guide_tip_text(),
                message,
            ]))
        threading.Thread(target=worker, daemon=True).start()

    def _build_snp_tip_message(self):
        if not self._snp_path:
            return "S参数库：未找到。解决：请从部门平台下载官方 S 参数库，并放到 D:\\S参数库、C:\\Project\\S参数库，或程序目录的 S参数库 文件夹。"
        try:
            from app.SnpLibrarySeal import verify_library, CACHE_NAME
            ok, problems = verify_library(
                self._snp_path, os.path.join(self.base_dir, CACHE_NAME))
        except Exception as exc:
            return f"S参数库：校验失败（{exc}）。解决：请检查库文件是否完整，必要时联系管理员恢复官方库。"
        if ok:
            return "S参数库：官方完整性校验通过，可放心用于仿真。"
        detail = "；".join(problems[:2])
        if len(problems) > 2:
            detail += f"；等共 {len(problems)} 项"
        return (
            f"S参数库：非官方或已被篡改（{detail}）。"
            "解决：不要继续运行仿真，请删除当前库后从部门平台重新下载官方库；若仍失败，联系管理员重新封库/恢复官方库。"
        )

    def _rebuild_sch_list(self):
        for w in self._sch_list_frame.winfo_children():
            w.destroy()
        if self._sch_dir is None:
            ctk.CTkLabel(
                self._sch_list_frame,
                text="✗  未找到 1_SCH_原理图 文件夹\n   请在程序同级目录下创建 1_SCH_原理图 文件夹并放入原理图文件",
                text_color=self._ERROR, font=ctk.CTkFont(size=12), justify="left"
            ).pack(anchor="w")
        elif not self._sch_files:
            ctk.CTkLabel(
                self._sch_list_frame,
                text="✗  1_SCH_原理图 文件夹内未找到原理图 txt",
                text_color=self._ERROR, font=ctk.CTkFont(size=12)
            ).pack(anchor="w")
        else:
            for f in self._sch_files:
                ctk.CTkRadioButton(
                    self._sch_list_frame, text=f,
                    variable=self._sch_var, value=f,
                    font=ctk.CTkFont(family="Consolas", size=13),
                    text_color=self._INK, fg_color=self._ACCENT,
                    border_color=self._BORDER, hover_color=self._ACCENT_HOVER,
                    command=self._on_selection_change
                ).pack(anchor="w", pady=2)

    def _rebuild_sub_list(self):
        for w in self._sub_list_frame.winfo_children():
            w.destroy()
        if not self._sub_folders:
            ctk.CTkLabel(
                self._sub_list_frame,
                text="⚠  未找到 4_PCB_S参数/SubProject_* 文件夹，可跳过",
                text_color=self._WARN, font=ctk.CTkFont(size=12)
            ).pack(anchor="w")
        else:
            for folder in self._sub_folders:
                ctk.CTkRadioButton(
                    self._sub_list_frame, text=folder,
                    variable=self._sub_var, value=folder,
                    font=ctk.CTkFont(family="Consolas", size=13),
                    text_color=self._INK, fg_color=self._ACCENT,
                    border_color=self._BORDER, hover_color=self._ACCENT_HOVER,
                    command=lambda: (self._update_ads_label(), self._on_selection_change())
                ).pack(anchor="w", pady=2)
            self._update_ads_label()

    def _rebuild_siw_list(self):
        for w in self._siw_list_frame.winfo_children():
            w.destroy()
        self._siw_files = _scan_siw_files(self.base_dir)
        if not self._siw_files:
            ctk.CTkLabel(
                self._siw_list_frame,
                text="✗  3_Siwave 文件夹内未找到 .siw 文件",
                text_color=self._ERROR, font=ctk.CTkFont(size=12)
            ).pack(anchor="w")
        else:
            if self._siw_var.get() not in self._siw_files:
                self._siw_var.set(self._siw_files[0])
            for f in self._siw_files:
                ctk.CTkRadioButton(
                    self._siw_list_frame, text=f,
                    variable=self._siw_var, value=f,
                    font=ctk.CTkFont(family="Consolas", size=13),
                    text_color=self._INK, fg_color=self._ACCENT,
                    border_color=self._BORDER, hover_color=self._ACCENT_HOVER,
                    command=self._on_selection_change
                ).pack(anchor="w", pady=2)

    def _rebuild_nets_list(self):
        for w in self._nets_list_frame.winfo_children():
            w.destroy()
        self._netlist_files = _scan_netlist_files(self.base_dir)
        if not self._netlist_files:
            ctk.CTkLabel(
                self._nets_list_frame,
                text="✗  2_输出_SIwave网络表 内未找到 .txt 文件，请先运行功能 1",
                text_color=self._ERROR, font=ctk.CTkFont(size=12)
            ).pack(anchor="w")
        else:
            if self._nets_var.get() not in self._netlist_files:
                self._nets_var.set(self._netlist_files[0])
            for f in self._netlist_files:
                ctk.CTkRadioButton(
                    self._nets_list_frame, text=f,
                    variable=self._nets_var, value=f,
                    font=ctk.CTkFont(family="Consolas", size=13),
                    text_color=self._INK, fg_color=self._ACCENT,
                    border_color=self._BORDER, hover_color=self._ACCENT_HOVER,
                    command=self._on_selection_change
                ).pack(anchor="w", pady=2)

    # ── 状态刷新 ─────────────────────────────────────────────────────────

    def _scan_file_list_snapshot(self):
        sch_dir, sch_files = _scan_sch_files(self.base_dir)
        return {
            "sch": (sch_dir, tuple(sch_files)),
            "sub": tuple(_scan_subproject_folders(self.base_dir)),
            "siw": tuple(_scan_siw_files(self.base_dir)),
            "nets": tuple(_scan_netlist_files(self.base_dir)),
            "odb": tuple(_scan_odb_files(self.base_dir)),
        }

    @staticmethod
    def _keep_valid_selection(variable, values):
        values = list(values)
        if variable.get() not in values:
            variable.set(values[0] if values else "")

    def _poll_file_list_changes(self):
        try:
            if not self._root.winfo_exists():
                return
            current = self._scan_file_list_snapshot()
            previous = getattr(self, "_file_list_snapshot", {})

            if current["sch"] != previous.get("sch"):
                self._sch_dir, sch_files = current["sch"]
                self._sch_files = list(sch_files)
                self._keep_valid_selection(self._sch_var, self._sch_files)
                self._rebuild_sch_list()

            if current["sub"] != previous.get("sub"):
                self._sub_folders = list(current["sub"])
                self._keep_valid_selection(self._sub_var, self._sub_folders)
                self._rebuild_sub_list()

            # ODB 导入过程中 .siw 可能尚未写完，完成前不提前暴露给用户。
            if (current["siw"] != previous.get("siw")
                    and not getattr(self, "_odb_import_running", False)):
                self._siw_files = list(current["siw"])
                self._keep_valid_selection(self._siw_var, self._siw_files)
                if (getattr(self, "_odb_generated_siw", "")
                        and self._odb_generated_siw not in self._siw_files):
                    self._odb_generated_siw = ""
                self._rebuild_siw_list()
                self._rebuild_stk_siw_list()
                self._update_stk_label()

            if current["nets"] != previous.get("nets"):
                self._netlist_files = list(current["nets"])
                self._keep_valid_selection(self._nets_var, self._netlist_files)
                self._rebuild_nets_list()

            if current["odb"] != previous.get("odb"):
                self._odb_files = list(current["odb"])
                self._keep_valid_selection(self._odb_var, self._odb_files)
                self._rebuild_odb_list()

            # 导入中保留旧 siw 快照，导入结束后下一轮仍能触发刷新。
            if getattr(self, "_odb_import_running", False) and "siw" in previous:
                current["siw"] = previous["siw"]
            self._file_list_snapshot = current
            self._validate()
        except (OSError, tk.TclError):
            pass
        finally:
            try:
                if self._root.winfo_exists():
                    self._root.after(2000, self._poll_file_list_changes)
            except tk.TclError:
                pass

    def _on_selection_change(self):
        self._validate()

    def _on_func_change(self, key):
        self._func_var.set(key)
        for k, nav in self._func_btns.items():
            active = k == key
            nav["item"].configure(
                fg_color=self._ACCENT_SOFT if active else self._SURFACE,
                border_width=1,
                border_color=self._ACCENT if active else self._BORDER
            )
            nav["stripe"].configure(fg_color=self._ACCENT if active else "transparent")
            nav["button"].configure(
                text_color=self._INK if active else self._MUTED,
                image=self._icon(nav["icon"], self._INK if active else self._MUTED, 20)
            )
        if key == "2":
            self._configure_confirm_action("初始化 SIWAVE", "layers")
        elif key == "3":
            self._configure_confirm_action("抽取 S 参数", "memory")
        else:
            self._configure_confirm_action("确认并运行", "play_arrow")
        self._refresh_header_state()
        self._refresh_sections()
        if key == "2":
            self._root.after_idle(self._scroll_to_top)

    def _scroll_to_top(self):
        try:
            self._scroll._parent_canvas.yview_moveto(0)
        except (AttributeError, tk.TclError):
            pass

    def _refresh_sections(self):
        """Unpack all sections then repack in correct order based on selected function."""
        for frame in (self._sch_frame, self._snp_frame,
                      self._sub_frame, self._ads_frame, self._siwave_frame,
                      self._stk_frame):
            frame.pack_forget()

        func = self._func_var.get()
        if func in ("1", "5", "6"):
            self._sch_frame.pack(fill="x", padx=12, pady=(12, 12))
            self._snp_frame.pack(fill="x", padx=12, pady=(0, 12))

        if func == "2":
            self._stk_frame.pack(fill="x", padx=12, pady=(12, 12))

        if func == "3":
            self._siwave_frame.pack(fill="x", padx=12, pady=(12, 12))

        if func == "4":
            # ADS 依赖原理图 + PCB S 参数，两项均在本页选择（不跨功能读 setting.ini）
            self._sch_frame.pack(fill="x", padx=12, pady=(12, 12))
            self._snp_frame.pack(fill="x", padx=12, pady=(0, 12))
            self._sub_frame.pack(fill="x", padx=12, pady=(0, 12))
            self._ads_frame.pack(fill="x", padx=12, pady=(0, 12))
            self._update_ads_label()

        self._validate()

    def _update_ads_label(self):
        sub = self._sub_var.get()
        if sub:
            self._ads_label.configure(
                text=f"将自动创建：5_ADS仿真/{_ads_dir_from_subproject(sub)}/",
                text_color=self._INK
            )
        else:
            self._ads_label.configure(text="—", text_color=self._MUTED)

    def _on_match_50r_toggle(self):
        if self._match_50r_var.get():
            self._match_50r_hint.pack(anchor="w", padx=16, pady=(0, 12))
        else:
            self._match_50r_hint.pack_forget()

    @staticmethod
    def _norm_key(s: str) -> str:
        return s.upper().replace("-", "_")

    def _check_project_mismatch(self, func: str) -> str:
        """仅检查当前功能页内已选配置是否同属一项目，不跨功能读 setting.ini。"""
        if func == "3":
            siw, nets = self._siw_var.get(), self._nets_var.get()
            if siw and nets:
                k_siw, k_nets = _project_key(siw), _project_key(nets)
                if k_siw and k_nets and self._norm_key(k_siw) != self._norm_key(k_nets):
                    return f"项目不一致：.siw 工程「{k_siw}」与网络表「{k_nets}」不属于同一项目"
        elif func == "4":
            sub = self._sub_var.get()
            sch = self._sch_var.get()
            if sub and sch:
                k_sub, k_sch = _project_key(sub), _project_key(sch)
                if k_sub and k_sch and self._norm_key(k_sub) != self._norm_key(k_sch):
                    return (
                        f"项目不一致：PCB 参数文件夹「{k_sub}」"
                        f"与原理图「{k_sch}」不属于同一项目"
                    )
        return ""

    def _validate(self):
        func = self._func_var.get()
        mismatch = self._check_project_mismatch(func)

        # 先更新按钮状态（必须在任何 layout 操作之前，防止 pack 异常导致按钮未被禁用）
        if func in ("1", "5", "6"):
            ok = (
                not mismatch
                and self._sch_dir is not None
                and bool(self._sch_files)
            )
        elif func == "2":
            _cmd, error = self._build_siwave_init_cmd()
            ok = error is None
        elif func == "3":
            ok = not mismatch and bool(self._siw_var.get()) and bool(self._nets_var.get())
        elif func == "4":
            ok = (
                not mismatch
                and bool(self._sch_var.get())
                and bool(self._sub_var.get())
            )
        else:
            ok = not mismatch
        self._confirm_btn.configure(
            state="normal" if ok else "disabled",
            fg_color=self._ACCENT if ok else self._SURFACE2
        )

        # 再更新警告条（layout 操作，失败不影响按钮禁用）
        if mismatch:
            self._warn_label.configure(text=f"⚠  {mismatch}")
            self._warn_frame.pack(fill="x", padx=30, pady=(0, 6), before=self._main_area)
        else:
            self._warn_frame.pack_forget()

    # ── 确认 ─────────────────────────────────────────────────────────────

    def _on_confirm(self):
        func = self._func_var.get()
        result = self._collect_run_result(func)
        self._begin_run(func, result)

    def _collect_run_result(self, func):
        result = {}
        # 功能4（ADS）在本页自选原理图并写入，不依赖其它功能残留的 setting.ini
        if func in ("1", "4", "5", "6"):
            sch_file = self._sch_var.get()
            if sch_file:
                result["Schematic_ASCII_File"] = os.path.join(self.base_dir, "1_SCH_原理图", sch_file)
        if self._snp_path:
            result["Component_S_Parameter_Library"] = self._snp_path
        if func == "4":
            sub = self._sub_var.get()
            if sub:
                result["PCB_S_Parameters"] = os.path.join(self.base_dir, "4_PCB_S参数", sub)
                ads_name = _ads_dir_from_subproject(sub)
                ads_path = os.path.join(self.base_dir, "5_ADS仿真", ads_name)
                os.makedirs(ads_path, exist_ok=True)
                result["ADS_Project_Directory"] = ads_path
                result["Antenna_Match_50R"] = "1" if self._match_50r_var.get() else "0"
        return result

    def _begin_run(self, func, result):
        if result:
            _write_settings(os.path.join(self.base_dir, "setting.ini"), result)
        self.result = result
        self.choice = func
        self._run_started_at = time.time()
        self._show_progress_view()
        self._start_run(func)

    # ── 进度页 ───────────────────────────────────────────────────────────

    def _show_progress_view(self):
        self._main_area.pack_forget()
        self._confirm_btn.pack_forget()

        self._prog_frame = ctk.CTkFrame(self._root, fg_color=self._BG)
        self._prog_frame.pack(fill="both", expand=True, padx=30, pady=(0, 22))
        self._attach_shadow(self._prog_frame)

        self._status_label = ctk.CTkLabel(
            self._prog_frame, text="准备中…",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold"),
            text_color=self._ACCENT)
        self._status_label.pack(anchor="w", pady=(0, 6))

        self._build_run_step_bar()
        self._run_failed_phase = None
        self._set_run_step_state("prepare")

        self._prog_bar = ctk.CTkProgressBar(
            self._prog_frame, mode="indeterminate", height=4,
            progress_color=self._ACCENT, fg_color=self._SURFACE2)
        self._prog_bar.pack(fill="x", pady=(0, 8))
        self._prog_bar.start()

        self._log_box = ctk.CTkTextbox(
            self._prog_frame, fg_color=self._SURFACE, text_color=self._INK,
            border_width=1, border_color=self._BORDER, corner_radius=18,
            font=ctk.CTkFont(family="Consolas", size=12))
        self._log_box.pack(fill="both", expand=True)
        self._log_box.configure(state="disabled")
        _tb = self._log_box._textbox
        _tb.tag_config("ansi_red",     foreground="#c0392b")
        _tb.tag_config("ansi_green",   foreground="#1a7a1a")
        _tb.tag_config("ansi_yellow",  foreground="#b36000")
        _tb.tag_config("ansi_blue",    foreground="#1a4a9a")
        _tb.tag_config("ansi_magenta", foreground="#7a1a7a")
        _tb.tag_config("ansi_cyan",    foreground="#007a8a")

        self._result_frame = ctk.CTkFrame(
            self._prog_frame, fg_color=self._SURFACE, corner_radius=18,
            border_width=1, border_color=self._BORDER)
        self._attach_shadow(self._result_frame)

        self._back_btn = ctk.CTkButton(
            self._prog_frame, text="返回配置", state="disabled", height=40,
            image=self._icon("arrow_back", self._INK, 18), compound="left",
            fg_color=self._SURFACE, hover_color=self._ACCENT_SOFT,
            text_color=self._INK, text_color_disabled=self._MUTED,
            border_width=1, border_color=self._BORDER, corner_radius=20,
            command=self._back_to_config)
        self._back_btn.pack(fill="x", pady=(10, 0))

    def _build_run_step_bar(self):
        self._run_step_frame = ctk.CTkFrame(self._prog_frame, fg_color="transparent")
        self._run_step_frame.pack(fill="x", pady=(0, 10))
        self._run_step_defs = [
            ("prepare", "准备输入"),
            ("launch", "启动任务"),
            ("running", "执行中"),
            ("finish", "生成结果"),
        ]
        self._run_step_widgets = {}
        for idx, (key, label) in enumerate(self._run_step_defs):
            item = ctk.CTkFrame(
                self._run_step_frame, fg_color=self._SURFACE, corner_radius=18,
                border_width=1, border_color=self._BORDER)
            item.pack(side="left", fill="x", expand=True)
            dot = ctk.CTkLabel(
                item, text=str(idx + 1), width=24, height=24,
                fg_color=self._SURFACE2, corner_radius=12,
                text_color=self._MUTED,
                font=ctk.CTkFont(size=12, weight="bold"))
            dot.pack(side="left", padx=(10, 8), pady=8)
            text = ctk.CTkLabel(
                item, text=label, text_color=self._MUTED,
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=12, weight="bold"))
            text.pack(side="left", padx=(0, 10), pady=8)
            self._run_step_widgets[key] = {"frame": item, "dot": dot, "text": text}
            if idx < len(self._run_step_defs) - 1:
                ctk.CTkLabel(
                    self._run_step_frame, text="→", width=22,
                    text_color=self._MUTED,
                    font=ctk.CTkFont(size=14, weight="bold")
                ).pack(side="left", padx=4)

    def _set_run_step_state(self, active_key, failed=False):
        order = [k for k, _ in self._run_step_defs]
        active_idx = order.index(active_key) if active_key in order else 0
        for idx, key in enumerate(order):
            widgets = self._run_step_widgets[key]
            if failed and idx == active_idx:
                frame_color, dot_color, text_color = "#fff1f0", self._ERROR, self._ERROR
                dot_text = "!"
            elif idx < active_idx:
                frame_color, dot_color, text_color = "#eef8f1", self._OK, self._OK
                dot_text = "✓"
            elif idx == active_idx:
                frame_color, dot_color, text_color = self._SURFACE, self._ACCENT, self._ACCENT
                dot_text = str(idx + 1)
            else:
                frame_color, dot_color, text_color = self._SURFACE, self._SURFACE2, self._MUTED
                dot_text = str(idx + 1)
            widgets["frame"].configure(fg_color=frame_color)
            widgets["dot"].configure(
                text=dot_text, fg_color=dot_color,
                text_color=(self._ON_ACCENT if idx <= active_idx or (failed and idx == active_idx) else self._MUTED))
            widgets["text"].configure(text_color=text_color)

    def _append_log(self, text):
        tb = self._log_box._textbox
        self._log_box.configure(state="normal")
        text = text.replace("\r\n", "\n")
        # 按 ANSI 码分段，奇数索引为颜色码，偶数索引为文本
        parts = _ANSI_SPLIT_RE.split(text)
        current_tag = None
        for i, part in enumerate(parts):
            if i % 2 == 1:
                current_tag = _ANSI_TAG_MAP.get(part)  # None → 重置为默认色
            else:
                # 处理 \r（子进程进度条原地刷新）
                segs = part.split("\r")
                for j, seg in enumerate(segs):
                    if j > 0:
                        tb.delete("end-1c linestart", "end-1c")
                    if seg:
                        if current_tag:
                            tb.insert("end", seg, (current_tag,))
                        else:
                            tb.insert("end", seg)
        tb.see("end")
        self._log_box.configure(state="disabled")

    # ── SIwave 初始化 UI ─────────────────────────────────────────────────────

    def _stk_pkgs(self):
        return sorted(self._stk_tree.keys())

    def _stk_mats(self, pkg):
        return sorted(self._stk_tree.get(pkg, {}).keys())

    def _stk_layers(self, pkg, mat):
        if not mat:
            return []
        return sorted(
            self._stk_tree.get(pkg, {}).get(mat, {}).keys(),
            key=lambda x: int(''.join(c for c in x if c.isdigit()) or '999')
        )

    def _stk_hdis(self, pkg, mat, lay):
        return self._stk_tree.get(pkg, {}).get(mat, {}).get(lay, [])

    def _resolve_stk_xml(self):
        pkg = self._stk_pkg_var.get()
        mat = self._stk_mat_var.get()
        lay = self._stk_lay_var.get()
        hdi = self._stk_hdi_var.get()
        if not (pkg and mat and lay and hdi):
            return None
        return _find_stackup_xml(self.base_dir, pkg, mat, lay, hdi)

    def _build_stackup_card_body(self):
        # —— 初始化第一步（可选）：ODB++ 导入，把 0_ODB/ 里的 ODB 转成 .siw 工程 ——
        self._odb_section = ctk.CTkFrame(
            self._stk_frame, fg_color=self._ACCENT_SOFT, corner_radius=18,
            border_width=1, border_color=self._ACCENT)
        self._odb_section.pack(fill="x", padx=16, pady=(0, 18))
        odb_header = ctk.CTkFrame(self._odb_section, fg_color="transparent")
        odb_header.pack(fill="x", padx=16, pady=(14, 2))
        self._odb_title_label = ctk.CTkLabel(
            odb_header,
            text="从 ODB++ 新建 SIwave 工程",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"),
            text_color=self._ACCENT_TEXT
        )
        self._odb_title_label.pack(side="left")
        self._odb_phase_label = ctk.CTkLabel(
            odb_header, text="可选 · 未开始", height=24, corner_radius=12,
            fg_color=self._SURFACE, text_color=self._MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=11, weight="bold"))
        self._odb_phase_label.pack(side="right")
        ctk.CTkLabel(
            self._odb_section,
            text="独立步骤 · 从 0_ODB/ 选择 ODB++ 压缩包，导入后生成新的 .siw 工程",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12), text_color=self._MUTED
        ).pack(anchor="w", padx=16, pady=(0, 8))
        self._odb_list_frame = ctk.CTkFrame(self._odb_section, fg_color="transparent")
        self._odb_list_frame.pack(fill="x", padx=16, pady=(0, 8))
        self._rebuild_odb_list()

        self._odb_status_label = ctk.CTkLabel(
            self._odb_section, text="",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
            text_color=self._MUTED, anchor="w")
        self._odb_status_label.pack(fill="x", padx=16, pady=(0, 6))

        self._odb_import_btn = ctk.CTkButton(
            self._odb_section, text="导入 ODB 生成工程",
            image=self._icon("layers", self._ON_ACCENT, 18), compound="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
            fg_color=self._ACCENT, text_color=self._ON_ACCENT,
            hover_color=self._ACCENT_HOVER, height=38, corner_radius=19,
            border_width=0, command=self._on_odb_import
        )
        self._odb_import_btn.pack(anchor="w", padx=16, pady=(0, 14))

        stk_header = ctk.CTkFrame(self._stk_frame, fg_color="transparent")
        stk_header.pack(fill="x", padx=16, pady=(0, 2))
        ctk.CTkLabel(
            stk_header, text="初始化 SIwave 工程",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"),
            text_color=self._INK
        ).pack(side="left")
        self._stk_phase_label = ctk.CTkLabel(
            stk_header, text="等待工程", height=24, corner_radius=12,
            fg_color=self._SURFACE2, text_color=self._MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=11, weight="bold"))
        self._stk_phase_label.pack(side="right")
        ctk.CTkLabel(
            self._stk_frame,
            text="选择已有或刚生成的 .siw 工程，再执行下方一项或两项初始化操作",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12), text_color=self._MUTED
        ).pack(anchor="w", padx=16, pady=(0, 10))
        ctk.CTkLabel(self._stk_frame, text=".siw 工程文件（3_Siwave/）",
                     font=ctk.CTkFont(family="Microsoft YaHei UI", size=13), text_color=self._MUTED
                     ).pack(anchor="w", padx=16)
        self._stk_siw_list_frame = ctk.CTkFrame(self._stk_frame, fg_color="transparent")
        self._stk_siw_list_frame.pack(fill="x", padx=16, pady=(0, 10))
        self._rebuild_stk_siw_list()

        self._deact_section = ctk.CTkFrame(
            self._stk_frame, fg_color=self._SURFACE, corner_radius=18,
            border_width=1, border_color=self._BORDER)
        self._deact_section.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkCheckBox(
            self._deact_section,
            text="去使能 R/L/C",
            variable=self._init_deactivate_var,
            command=self._on_init_option_change,
            fg_color=self._ACCENT,
            hover_color=self._ACCENT_HOVER,
            border_color=self._BORDER,
            text_color=self._INK,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold")
        ).pack(anchor="w", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            self._deact_section,
            text="将 SIwave Home > Circuit Element Parameters 中的 Capacitors、Inductors、Resistors 设为 inactive。",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
            text_color=self._MUTED,
            wraplength=760,
            justify="left"
        ).pack(anchor="w", padx=12, pady=(0, 10))

        self._stackup_section = ctk.CTkFrame(
            self._stk_frame, fg_color=self._SURFACE, corner_radius=18,
            border_width=1, border_color=self._BORDER)
        self._stackup_section.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkCheckBox(
            self._stackup_section,
            text="导入叠层信息",
            variable=self._init_stackup_var,
            command=self._on_init_option_change,
            fg_color=self._ACCENT,
            hover_color=self._ACCENT_HOVER,
            border_color=self._BORDER,
            text_color=self._INK,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold")
        ).pack(anchor="w", padx=12, pady=(10, 8))

        if not self._stk_tree:
            ctk.CTkLabel(
                self._stackup_section,
                text="⚠  未找到 D:\\板材库 / C:\\Project\\板材库 / 程序目录 板材库，导入叠层信息不可用；仍可只执行去使能 R/L/C。",
                text_color=self._WARN, font=ctk.CTkFont(size=12)
            ).pack(anchor="w", padx=12, pady=(0, 12))
            self._init_stackup_var.set(False)
        else:
            self._build_stackup_import_options(self._stackup_section)

        self._stk_status_label = ctk.CTkLabel(
            self._stk_frame, text="",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
            text_color=self._MUTED, anchor="w")
        self._stk_status_label.pack(fill="x", padx=16, pady=(0, 6))

        self._stk_import_btn = ctk.CTkButton(
            self._stk_frame, text="初始化 SIWAVE",
            image=self._icon("layers", self._ON_ACCENT, 18), compound="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
            fg_color=self._ACCENT, text_color=self._ON_ACCENT,
            hover_color=self._ACCENT_HOVER, height=38, corner_radius=19,
            border_width=0, command=self._on_stk_import
        )
        self._stk_import_btn.pack(anchor="w", padx=16, pady=(0, 12))
        self._set_odb_visual_state("idle")
        self._update_odb_label()
        self._update_stk_label()

    def _build_stackup_import_options(self, parent):
        # 四级下拉选择行
        sel_row = ctk.CTkFrame(parent, fg_color="transparent")
        sel_row.pack(fill="x", padx=12, pady=(0, 8))

        pkgs = self._stk_pkgs()
        if pkgs:
            self._stk_pkg_var.set(pkgs[0])
        mats = self._stk_mats(self._stk_pkg_var.get())
        if mats:
            self._stk_mat_var.set(mats[0])
        lays = self._stk_layers(self._stk_pkg_var.get(), self._stk_mat_var.get())
        if lays:
            self._stk_lay_var.set(lays[0])
        hdis = self._stk_hdis(self._stk_pkg_var.get(), self._stk_mat_var.get(), self._stk_lay_var.get())
        if hdis:
            self._stk_hdi_var.set(hdis[0])

        _opt_kw = dict(fg_color=self._SURFACE2, button_color=self._ACCENT,
                       button_hover_color=self._ACCENT_HOVER,
                       text_color=self._INK, dropdown_fg_color=self._SURFACE,
                       dropdown_text_color=self._INK)

        ctk.CTkLabel(sel_row, text="封装类型:", font=ctk.CTkFont(size=13),
                     text_color=self._MUTED).pack(side="left")
        self._stk_pkg_menu = ctk.CTkOptionMenu(
            sel_row, variable=self._stk_pkg_var, values=pkgs or ["—"],
            width=110, command=self._on_stk_pkg_change, **_opt_kw)
        self._stk_pkg_menu.pack(side="left", padx=(4, 20))

        ctk.CTkLabel(sel_row, text="板材:", font=ctk.CTkFont(size=13),
                     text_color=self._MUTED).pack(side="left")
        self._stk_mat_menu = ctk.CTkOptionMenu(
            sel_row, variable=self._stk_mat_var, values=mats or ["—"],
            width=100, command=self._on_stk_mat_change, **_opt_kw)
        self._stk_mat_menu.pack(side="left", padx=(4, 20))

        ctk.CTkLabel(sel_row, text="层数:", font=ctk.CTkFont(size=13),
                     text_color=self._MUTED).pack(side="left")
        self._stk_lay_menu = ctk.CTkOptionMenu(
            sel_row, variable=self._stk_lay_var, values=lays or ["—"],
            width=90, command=self._on_stk_lay_change, **_opt_kw)
        self._stk_lay_menu.pack(side="left", padx=(4, 20))

        ctk.CTkLabel(sel_row, text="HDI 阶数:", font=ctk.CTkFont(size=13),
                     text_color=self._MUTED).pack(side="left")
        self._stk_hdi_menu = ctk.CTkOptionMenu(
            sel_row, variable=self._stk_hdi_var, values=hdis or ["—"],
            width=100, command=lambda _: self._update_stk_label(), **_opt_kw)
        self._stk_hdi_menu.pack(side="left", padx=(4, 0))

        # XML 路径提示
        self._stk_xml_label = ctk.CTkLabel(
            parent, text="",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color=self._MUTED, anchor="w")
        self._stk_xml_label.pack(fill="x", padx=12, pady=(0, 6))

        self._stk_preview_note = ctk.CTkLabel(
            parent, text="",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
            text_color=self._MUTED, anchor="w")
        self._stk_preview_note.pack(fill="x", padx=12, pady=(0, 6))
        self._stk_preview_open_btn = ctk.CTkButton(
            parent, text="打开原图", width=104, height=32, corner_radius=16,
            image=self._icon("image", self._INK, 16), compound="left",
            fg_color=self._SURFACE, hover_color=self._ACCENT_SOFT,
            text_color=self._INK, border_width=1, border_color=self._BORDER,
            command=self._open_stackup_image)
        self._stk_preview_open_btn.pack(anchor="w", padx=12, pady=(0, 6))
        self._stk_preview_label = ctk.CTkLabel(
            parent, text="", fg_color="#ffffff",
            corner_radius=18, height=640)
        self._stk_preview_label.pack(fill="x", padx=12, pady=(0, 8))
        self._stk_preview_img = None
        self._stk_preview_img_path = None

    def _rebuild_stk_siw_list(self):
        if not hasattr(self, "_stk_siw_list_frame"):
            return
        for w in self._stk_siw_list_frame.winfo_children():
            w.destroy()
        self._siw_files = _scan_siw_files(self.base_dir)
        if not self._siw_files:
            ctk.CTkLabel(
                self._stk_siw_list_frame,
                text="✗  3_Siwave 文件夹内未找到 .siw 文件",
                text_color=self._ERROR, font=ctk.CTkFont(size=12)
            ).pack(anchor="w")
            return
        if self._siw_var.get() not in self._siw_files:
            self._siw_var.set(self._siw_files[0])
        generated_siw = getattr(self, "_odb_generated_siw", "")
        for f in self._siw_files:
            is_generated = bool(generated_siw and f == generated_siw)
            ctk.CTkRadioButton(
                self._stk_siw_list_frame,
                text=f"{f}   ✓ ODB 新生成" if is_generated else f,
                variable=self._siw_var, value=f,
                font=ctk.CTkFont(family="Consolas", size=13,
                                 weight="bold" if is_generated else "normal"),
                text_color=self._OK if is_generated else self._INK,
                fg_color=self._OK if is_generated else self._ACCENT,
                border_color=self._OK if is_generated else self._BORDER,
                hover_color=self._ACCENT_HOVER,
                command=lambda: (self._update_stk_label(), self._on_selection_change())
            ).pack(anchor="w", pady=2)

    def _on_stk_pkg_change(self, _):
        pkg = self._stk_pkg_var.get()
        mats = self._stk_mats(pkg)
        self._stk_mat_menu.configure(values=mats if mats else ["—"])
        mat = mats[0] if mats else ""
        if mat:
            self._stk_mat_var.set(mat)
        self._on_stk_mat_change(None)

    def _on_stk_mat_change(self, _):
        pkg = self._stk_pkg_var.get()
        mat = self._stk_mat_var.get()
        lays = self._stk_layers(pkg, mat)
        self._stk_lay_menu.configure(values=lays if lays else ["—"])
        if lays:
            self._stk_lay_var.set(lays[0])
            hdis = self._stk_hdis(pkg, mat, lays[0])
            self._stk_hdi_menu.configure(values=hdis if hdis else ["—"])
            if hdis:
                self._stk_hdi_var.set(hdis[0])
        self._update_stk_label()

    def _on_stk_lay_change(self, _):
        pkg = self._stk_pkg_var.get()
        mat = self._stk_mat_var.get()
        lay = self._stk_lay_var.get()
        hdis = self._stk_hdis(pkg, mat, lay)
        self._stk_hdi_menu.configure(values=hdis if hdis else ["—"])
        if hdis:
            self._stk_hdi_var.set(hdis[0])
        self._update_stk_label()

    def _update_stk_label(self):
        stackup_enabled = bool(self._init_stackup_var.get())
        deact_enabled = bool(self._init_deactivate_var.get())
        xml = self._resolve_stk_xml() if hasattr(self, '_stk_xml_label') else None
        xml_ok = bool(xml and os.path.exists(xml))

        if hasattr(self, '_stk_xml_label'):
            if not stackup_enabled:
                self._stk_xml_label.configure(text="未选择导入叠层信息", text_color=self._MUTED)
            elif xml_ok:
                rel = os.path.relpath(xml, self.base_dir)
                self._stk_xml_label.configure(text=f"✓  {rel}", text_color=self._OK)
            else:
                self._stk_xml_label.configure(text="✗  未找到对应叠层文件", text_color=self._ERROR)

        can_run = bool(self._siw_var.get()) and (deact_enabled or stackup_enabled) and (
            not stackup_enabled or xml_ok)
        if self._stk_import_done and can_run:
            self._configure_stk_action("SIWAVE 已初始化", "check_circle", state="disabled")
        else:
            self._configure_stk_action(
                "初始化 SIWAVE", "layers", state=("normal" if can_run else "disabled"))
        self._update_stk_phase_state()
        self._update_stackup_preview()

    def _update_stk_phase_state(self):
        if not hasattr(self, "_stk_phase_label"):
            return
        if getattr(self, "_stk_import_running", False):
            text, fg, color = "正在初始化…", self._ACCENT_SOFT, self._ACCENT_TEXT
        elif self._stk_import_done:
            text, fg, color = "✓ 已完成", "#eef8f1", self._OK
        elif self._siw_var.get():
            text, fg, color = "可以初始化", self._ACCENT_SOFT, self._ACCENT_TEXT
        else:
            text, fg, color = "等待工程", self._SURFACE2, self._MUTED
        self._stk_phase_label.configure(text=text, fg_color=fg, text_color=color)

    def _on_init_option_change(self):
        self._stk_import_done = False
        self._update_stk_label()
        self._validate()

    def _build_siwave_init_cmd(self):
        deact_enabled = bool(self._init_deactivate_var.get())
        stackup_enabled = bool(self._init_stackup_var.get())
        if not deact_enabled and not stackup_enabled:
            return None, "请至少选择“去使能 R/L/C”或“导入叠层信息”其中一项。"
        siw_file = self._siw_var.get()
        if not siw_file:
            return None, "未选择 .siw 工程文件，无法初始化 SIWAVE。"
        cmd = ["python", "-u", "-m", "siwave_extractor",
               "--project", os.path.join(self.base_dir, "3_Siwave", siw_file),
               "--stackup-only"]
        if deact_enabled:
            cmd.append("--deactivate-rlc")
        if stackup_enabled:
            xml = self._resolve_stk_xml()
            if not xml or not os.path.exists(xml):
                return None, "未找到对应叠层文件，请重新选择封装/板材/层数/HDI。"
            from siwave_extractor.stackup_xml import (
                load_stackup_xml, stackup_siwave_problems,
            )
            problems = stackup_siwave_problems(load_stackup_xml(xml))
            if problems:
                return None, "叠层 XML 不能导入 SIwave（会崩溃）：\n" + "\n".join(problems)
            cmd += ["--stackup", xml]
        return cmd, None

    def _selected_stackup_dir(self):
        xml = self._resolve_stk_xml()
        return os.path.dirname(xml) if xml else None

    def _stackup_preview_candidates(self):
        d = self._selected_stackup_dir()
        if not d or not os.path.isdir(d):
            return []
        exts = (".png", ".jpg", ".jpeg", ".webp")
        xml = self._resolve_stk_xml()
        if not xml:
            return []
        base = os.path.splitext(os.path.basename(xml))[0]
        matches = []
        for ext in exts:
            p = os.path.join(d, base + ext)
            if os.path.isfile(p):
                matches.append(p)
        return matches

    def _fallback_stackup_contact_sheet(self):
        lib_dir = _find_board_library(self.base_dir)
        if not lib_dir:
            return None
        p = os.path.join(lib_dir, "_待匹配_叠层截图", "叠层截图索引.jpg")
        return p if os.path.isfile(p) else None

    def _update_stackup_preview(self):
        if not hasattr(self, "_stk_preview_label"):
            return
        imgs = self._stackup_preview_candidates()
        img_path = imgs[0] if imgs else self._fallback_stackup_contact_sheet()
        if not img_path:
            self._stk_preview_label.configure(image=None, text="暂无叠层截图")
            self._stk_preview_open_btn.configure(state="disabled")
            self._stk_preview_note.configure(
                text="未找到与当前叠层 XML 同名的截图；请按 XML 文件名给截图命名并放入同一文件夹。",
                text_color=self._WARN)
            self._stk_preview_img = None
            self._stk_preview_img_path = None
            return
        try:
            from PIL import Image
            im = Image.open(img_path)
            orig_w, orig_h = im.size
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            im.thumbnail((920, 640), resampling)
            self._stk_preview_img = ctk.CTkImage(im.copy(), size=im.size)
            self._stk_preview_img_path = img_path
            self._stk_preview_open_btn.configure(state="normal")
            self._stk_preview_label.configure(image=self._stk_preview_img, text="")
            if imgs:
                self._stk_preview_note.configure(
                    text=f"叠层截图：{os.path.basename(img_path)}  原始尺寸 {orig_w}x{orig_h}"
                         + (f"（共 {len(imgs)} 张）" if len(imgs) > 1 else ""),
                    text_color=self._OK)
            else:
                self._stk_preview_note.configure(
                    text="当前叠层暂无专属截图，暂显示待匹配截图索引；请把对应截图放入当前叠层文件夹。",
                    text_color=self._WARN)
        except Exception as exc:
            self._stk_preview_label.configure(image=None, text="叠层截图加载失败")
            self._stk_preview_open_btn.configure(state="disabled")
            self._stk_preview_note.configure(text=f"叠层截图加载失败：{exc}", text_color=self._ERROR)
            self._stk_preview_img = None
            self._stk_preview_img_path = None

    def _open_stackup_image(self):
        path = getattr(self, "_stk_preview_img_path", None)
        if not path or not os.path.isfile(path):
            return
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(["explorer", "/select,", path], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _on_stk_en_toggle(self):
        """复选框 toggle 时重置导入状态，并更新确认按钮文字"""
        self._stk_import_done = False
        self._update_stk_label()
        if self._func_var.get() == "2" and self._stk_en_var.get():
            self._configure_confirm_action("抽取 S 参数", "memory")
        else:
            self._configure_confirm_action("确认并运行", "play_arrow")

    def _on_stk_import(self):
        """执行 SIwave 初始化（导入叠层 + R/L/C inactive），不触发抽取"""
        self._configure_stk_action("初始化中...", "layers", state="disabled")
        self._set_stk_status("正在初始化 SIWAVE…", self._ACCENT_TEXT)
        self._root.update_idletasks()
        cmd, error = self._build_siwave_init_cmd()
        if error:
            self._set_stk_status(error, self._ERROR)
            self._stk_import_running = False
            self._configure_stk_action("初始化 SIWAVE", "layers", state="normal")
            return
        self._q = queue.Queue()
        _src_dir = os.path.join(self.base_dir, "src")
        _old_pp = os.environ.get("PYTHONPATH", "")
        env = {**os.environ,
               "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": _src_dir + (os.pathsep + _old_pp if _old_pp else "")}
        self._stk_import_running = True
        self._stk_import_started_at = time.time()
        self._update_stk_phase_state()
        self._run_stk_import(env, cmd)

    def _set_stk_status(self, text, color=None):
        if not hasattr(self, "_stk_status_label"):
            return
        try:
            if not self._stk_status_label.winfo_exists():
                return
            self._stk_status_label.configure(
                text=text, text_color=(color or self._MUTED))
        except tk.TclError:
            pass

    def _configure_stk_import_btn(self, **kwargs):
        if not hasattr(self, "_stk_import_btn"):
            return
        try:
            if self._stk_import_btn.winfo_exists():
                self._stk_import_btn.configure(**kwargs)
        except tk.TclError:
            pass

    def _run_stk_import(self, env, cmd):
        def target():
            self._q.put(("status", "正在初始化 SIWAVE…"))
            self._q.put(("line", "\n========== 初始化 SIWAVE ==========\n"))
            try:
                proc = subprocess.Popen(
                    cmd, cwd=self.base_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                output = []
                while True:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                    if line:
                        decoded = line.decode("utf-8", errors="replace")
                        output.append(decoded)
                        self._q.put(("line", decoded))
                rc = proc.wait()
                ok = (rc == 0)
            except OSError as e:
                self._q.put(("line", f"启动失败: {e}\n"))
                ok = False
            self._q.put(("stk_done", ok))
        t = threading.Thread(target=target, daemon=True)
        t.start()
        self._root.after(1000, self._tick_stk_import_status)
        self._root.after(30, self._poll_stk_import_queue)

    def _tick_stk_import_status(self):
        if not getattr(self, "_stk_import_running", False):
            return
        elapsed = int(time.time() - getattr(self, "_stk_import_started_at", time.time()))
        dots = "." * (elapsed % 3 + 1)
        self._configure_stk_action(f"初始化中{dots}", "layers", state="disabled")
        self._set_stk_status(f"正在初始化 SIWAVE… 已运行 {elapsed}s，请等待 SIwave 完成处理。", self._ACCENT_TEXT)
        self._root.after(1000, self._tick_stk_import_status)

    def _poll_stk_import_queue(self):
        try:
            while True:
                msg = self._q.get_nowait()
                kind, data = msg if isinstance(msg, tuple) else ("line", msg)
                if kind == "stk_done":
                    self._stk_import_running = False
                    ok = data
                    if ok:
                        self._stk_import_done = True
                        self._configure_stk_action("SIWAVE 已初始化", "check_circle", state="disabled")
                        self._set_stk_status("✓  SIWAVE 初始化完成。", self._OK)
                    else:
                        self._configure_stk_action("初始化 SIWAVE（重试）", "layers", state="normal")
                        self._set_stk_status("✗  SIWAVE 初始化失败，请查看控制台输出或重试。", self._ERROR)
                    self._update_stk_phase_state()
                    return
                elif kind == "status":
                    self._set_stk_status(data, self._ACCENT_TEXT)
                elif kind == "line":
                    last = str(data).strip().splitlines()
                    if last:
                        self._set_stk_status(f"SIwave：{last[-1]}", self._MUTED)
        except queue.Empty:
            pass
        self._root.after(30, self._poll_stk_import_queue)

    # ── ODB 导入（初始化第一步）────────────────────────────────────────────

    def _rebuild_odb_list(self):
        if not hasattr(self, "_odb_list_frame"):
            return
        for w in self._odb_list_frame.winfo_children():
            w.destroy()
        self._odb_files = _scan_odb_files(self.base_dir)
        if not self._odb_files:
            ctk.CTkLabel(
                self._odb_list_frame,
                text="⚠  0_ODB 文件夹内未找到 ODB++(.tgz) 文件。把 ODB 放入 0_ODB/ 后点下方按钮导入生成工程，或直接在下方选择已有 .siw。",
                text_color=self._WARN,
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
                justify="left", wraplength=760
            ).pack(anchor="w")
            self._odb_var.set("")
        else:
            if self._odb_var.get() not in self._odb_files:
                self._odb_var.set(self._odb_files[0])
            for f in self._odb_files:
                ctk.CTkRadioButton(
                    self._odb_list_frame, text=f,
                    variable=self._odb_var, value=f,
                    font=ctk.CTkFont(family="Consolas", size=13),
                    text_color=self._INK, fg_color=self._ACCENT,
                    border_color=self._BORDER, hover_color=self._ACCENT_HOVER,
                    command=self._update_odb_label
                ).pack(anchor="w", pady=2)
        self._update_odb_label()

    def _update_odb_label(self):
        if not hasattr(self, "_odb_import_btn"):
            return
        if getattr(self, "_odb_import_running", False):
            return
        has = bool(self._odb_var.get())
        self._configure_odb_action(
            "导入 ODB 生成工程", "layers",
            state="normal" if has else "disabled")

    def _set_odb_visual_state(self, state):
        if not hasattr(self, "_odb_section"):
            return
        states = {
            "idle": (self._ACCENT_SOFT, self._ACCENT, self._ACCENT_TEXT,
                     "可选 · 未开始", self._SURFACE, self._MUTED),
            "running": (self._ACCENT_SOFT, self._ACCENT_LIGHT, self._ACCENT_TEXT,
                        "正在导入…", self._SURFACE, self._ACCENT_TEXT),
            "success": ("#eef8f1", self._OK, self._OK,
                        "✓ 已生成工程", self._SURFACE, self._OK),
            "error": ("#fff4f2", self._ERROR, self._ERROR,
                      "导入失败", self._SURFACE, self._ERROR),
        }
        bg, border, title_color, phase, phase_bg, phase_color = states[state]
        self._odb_section.configure(fg_color=bg, border_color=border)
        self._odb_title_label.configure(text_color=title_color)
        self._odb_phase_label.configure(
            text=phase, fg_color=phase_bg, text_color=phase_color)

    def _animate_odb_activity(self):
        if not getattr(self, "_odb_import_running", False):
            return
        step = getattr(self, "_odb_anim_step", 0)
        dots = "." * (step % 3 + 1)
        pulse_colors = (self._ACCENT, self._ACCENT_LIGHT, "#75baff", self._ACCENT_LIGHT)
        self._configure_odb_action(f"导入中{dots}", "layers", state="disabled")
        try:
            if self._odb_section.winfo_exists():
                self._odb_section.configure(border_color=pulse_colors[step % len(pulse_colors)])
        except tk.TclError:
            return
        self._odb_anim_step = step + 1
        self._root.after(350, self._animate_odb_activity)

    def _configure_odb_action(self, text, icon_name="layers", state=None):
        if not hasattr(self, "_odb_import_btn"):
            return
        kwargs = {
            "text": text,
            "image": self._icon(icon_name, self._ON_ACCENT, 18),
            "compound": "left",
        }
        if state is not None:
            kwargs["state"] = state
        self._configure_odb_btn(**kwargs)

    def _configure_odb_btn(self, **kwargs):
        if not hasattr(self, "_odb_import_btn"):
            return
        try:
            if self._odb_import_btn.winfo_exists():
                self._odb_import_btn.configure(**kwargs)
        except tk.TclError:
            pass

    def _set_odb_status(self, text, color=None):
        if not hasattr(self, "_odb_status_label"):
            return
        try:
            if not self._odb_status_label.winfo_exists():
                return
            self._odb_status_label.configure(
                text=text, text_color=(color or self._MUTED))
        except tk.TclError:
            pass

    def _on_odb_import(self):
        """初始化第一步：把选定的 ODB++(.tgz) 导入 SIwave，生成新的 .siw 工程。"""
        odb_file = self._odb_var.get()
        if not odb_file:
            self._set_odb_status("请先选择 0_ODB/ 下的 ODB++(.tgz) 文件。", self._ERROR)
            return
        odb_path = os.path.join(self.base_dir, "0_ODB", odb_file)
        if not os.path.isfile(odb_path):
            self._set_odb_status(f"ODB 文件不存在：{odb_path}", self._ERROR)
            return
        out_name = os.path.splitext(odb_file)[0] + ".siw"
        out_siw = os.path.join(self.base_dir, "3_Siwave", out_name)
        cmd = ["python", "-u", "-m", "siwave_extractor",
               "--import-odb", odb_path, "--out-siw", out_siw]
        self._set_odb_status("正在导入 ODB…", self._ACCENT_TEXT)
        self._root.update_idletasks()
        self._odb_q = queue.Queue()
        _src_dir = os.path.join(self.base_dir, "src")
        _old_pp = os.environ.get("PYTHONPATH", "")
        env = {**os.environ,
               "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": _src_dir + (os.pathsep + _old_pp if _old_pp else "")}
        self._odb_import_running = True
        self._odb_import_started_at = time.time()
        self._odb_out_name = out_name
        self._odb_anim_step = 0
        self._set_odb_visual_state("running")
        self._animate_odb_activity()
        self._run_odb_import(env, cmd)

    def _run_odb_import(self, env, cmd):
        def target():
            self._odb_q.put(("status", "正在导入 ODB…"))
            self._odb_q.put(("line", "\n========== 导入 ODB ==========\n"))
            try:
                proc = subprocess.Popen(
                    cmd, cwd=self.base_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                while True:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                    if line:
                        self._odb_q.put(("line", line.decode("utf-8", errors="replace")))
                ok = (proc.wait() == 0)
            except OSError as e:
                self._odb_q.put(("line", f"启动失败: {e}\n"))
                ok = False
            self._odb_q.put(("odb_done", ok))
        threading.Thread(target=target, daemon=True).start()
        self._root.after(1000, self._tick_odb_import_status)
        self._root.after(30, self._poll_odb_import_queue)

    def _tick_odb_import_status(self):
        if not getattr(self, "_odb_import_running", False):
            return
        elapsed = int(time.time() - getattr(self, "_odb_import_started_at", time.time()))
        self._set_odb_status(f"正在导入 ODB… 已运行 {elapsed}s，请等待 SIwave 完成导入。", self._ACCENT_TEXT)
        self._root.after(1000, self._tick_odb_import_status)

    def _poll_odb_import_queue(self):
        try:
            while True:
                msg = self._odb_q.get_nowait()
                kind, data = msg if isinstance(msg, tuple) else ("line", msg)
                if kind == "odb_done":
                    self._odb_import_running = False
                    ok = data
                    if ok:
                        # 刷新 .siw 列表，自动选中刚由 ODB 生成的工程，便于继续叠层/去使能
                        new_name = getattr(self, "_odb_out_name", "")
                        self._odb_generated_siw = new_name
                        if new_name:
                            self._siw_var.set(new_name)
                        self._rebuild_stk_siw_list()
                        self._rebuild_siw_list()
                        self._update_stk_label()
                        self._configure_odb_action("重新导入 ODB", "layers", state="normal")
                        self._set_odb_visual_state("success")
                        self._set_odb_status(
                            f"✓  ODB 已导入并生成工程 {new_name}（已自动选中，可继续下方叠层/去使能初始化）",
                            self._OK)
                    else:
                        self._configure_odb_action("导入 ODB（重试）", "layers", state="normal")
                        self._set_odb_visual_state("error")
                        self._set_odb_status("✗  ODB 导入失败，请查看控制台输出或重试。", self._ERROR)
                    return
                elif kind == "status":
                    self._set_odb_status(data, self._ACCENT_TEXT)
                elif kind == "line":
                    last = str(data).strip().splitlines()
                    if last:
                        self._set_odb_status(f"SIwave：{last[-1]}", self._MUTED)
        except queue.Empty:
            pass
        self._root.after(30, self._poll_odb_import_queue)

    def _back_to_config(self):
        self._prog_frame.destroy()
        # 重新扫描文件列表（功能 1 运行后会生成新的网络表 txt，功能 3 会生成 SubProject）
        self._rebuild_stk_siw_list()
        self._rebuild_siw_list()
        self._rebuild_nets_list()
        self._rebuild_sub_list()
        self._rebuild_odb_list()
        self._main_area.pack(fill="both", expand=True, padx=30, pady=(0, 8))
        self._confirm_btn.pack(fill="x", padx=30, pady=(0, 22))
        # 运行成功后自动切到下一个功能（1→2→3→4→5）
        nxt = {"1": "2", "2": "3", "3": "4", "4": "5"}.get(self._func_var.get())
        if getattr(self, "_run_ok", False) and nxt:
            self._on_func_change(nxt)

    # ── 并行设置（config.json）────────────────────────────────────────────

    def _cfg_path(self):
        return os.path.join(self.base_dir, "config.json")

    def _read_cfg_workers(self):
        try:
            with open(self._cfg_path(), encoding="utf-8") as f:
                return str(max(1, int(json.load(f).get("parallel_workers", 1))))
        except Exception:
            return "1"

    def _write_cfg_workers(self, n):
        # 把并行进程数写回 config.json（保留其它键），抽取器运行时读取该文件
        path = self._cfg_path()
        data = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["parallel_workers"] = int(n)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 子进程执行 ───────────────────────────────────────────────────────

    def _start_run(self, func):
        def main_cmd(c):
            if "__compiled__" in globals():
                return [os.path.abspath(sys.argv[0]), "--worker", "--choice", c]
            return [sys.executable, "-u", os.path.join(self.base_dir, "src", "MAIN.PY"),
                    "--worker", "--choice", c]

        # GUI 功能号 → 执行步骤（MAIN 流水线内部 choice 仍为 1/2/3）
        if func == "1":
            steps = [("功能 1  网络频段信息", main_cmd("1"))]
        elif func == "2":
            cmd, error = self._build_siwave_init_cmd()
            if error:
                self._set_stk_status(error, self._ERROR)
                return
            steps = [("功能 2  初始化 SIWAVE", cmd)]
        elif func == "3":
            cmd = ["python", "-u", "-m", "siwave_extractor",
                   "--project", os.path.join(self.base_dir, "3_Siwave", self._siw_var.get()),
                   "--nets", os.path.join(self.base_dir, "2_输出_SIwave网络表", self._nets_var.get())]
            out_dir = self._siwave_out_var.get().strip()
            if out_dir:
                cmd += ["--out", out_dir]
            # 把并行进程数写回 config.json，供抽取器读取
            try:
                self._write_cfg_workers(self._par_workers_var.get())
            except Exception:
                pass
            steps = [("功能 3  SIwave S 参数抽取", cmd)]
        elif func == "4":
            steps = [("功能 4  ADS 仿真", main_cmd("2"))]
        elif func == "6":
            steps = [("功能 6  器件 Pin 路径", main_cmd("4"))]
        else:
            steps = [("功能 5  refnum 生成", main_cmd("3"))]

        self._q = queue.Queue()
        threading.Thread(target=self._run_steps, args=(steps,), daemon=True).start()
        self._root.after(30, self._poll_queue)

    def _run_steps(self, steps):
        # PYTHONPATH 指向 src/，使 "python -m siwave_extractor" 能找到 src/siwave_extractor 包
        # PYTHONUNBUFFERED 关闭子进程输出缓冲，日志可实时回流
        _src_dir = os.path.join(self.base_dir, "src")
        _old_pp = os.environ.get("PYTHONPATH", "")
        env = {**os.environ,
               "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": _src_dir + (os.pathsep + _old_pp if _old_pp else "")}
        for label, cmd in steps:
            self._q.put(("phase", "launch"))
            self._q.put(("status", f"正在执行：{label}"))
            self._q.put(("line", f"\n========== {label} ==========\n"))
            try:
                proc = subprocess.Popen(
                    cmd, cwd=self.base_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                self._q.put(("line", f"启动失败: {e}\n"))
                self._q.put(("phase_failed", "launch"))
                self._q.put(("done", False))
                return
            self._q.put(("phase", "running"))
            # 按字节块读取（不等整行），\r 进度刷新也能实时显示
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    self._q.put(("line", text))
            proc.wait()
            if proc.returncode != 0:
                self._q.put(("line", f"\n❌ {label} 失败（退出码 {proc.returncode}），流程中止\n"))
                self._q.put(("phase_failed", "running"))
                self._q.put(("done", False))
                return
            self._q.put(("line", f"✅ {label} 完成\n"))
        self._q.put(("phase", "finish"))
        self._q.put(("done", True))

    def _poll_queue(self):
        done = None
        lines = []  # 本轮所有文本合并后一次性插入，避免高频 insert 压垮主线程
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "line":
                    lines.append(payload)
                elif kind == "status":
                    self._status_label.configure(text=payload)
                elif kind == "phase":
                    self._set_run_step_state(payload)
                elif kind == "phase_failed":
                    self._run_failed_phase = payload
                    self._set_run_step_state(payload, failed=True)
                elif kind == "done":
                    done = payload
        except queue.Empty:
            pass
        if lines:
            self._append_log("".join(lines))
        if done is None:
            self._root.after(30, self._poll_queue)
        else:
            self._run_ok = done
            self._set_run_step_state(
                "finish" if done else (self._run_failed_phase or "running"),
                failed=not done)
            self._prog_bar.stop()
            self._prog_bar.configure(
                mode="determinate",
                progress_color=self._OK if done else self._ERROR)
            self._prog_bar.set(1.0)
            self._status_label.configure(
                text="全部完成 ✅" if done else "执行失败 ❌",
                text_color=self._OK if done else self._ERROR)
            self._back_btn.configure(
                state="normal",
                fg_color=(self._ACCENT if done else self._SURFACE),
                hover_color=(self._ACCENT_HOVER if done else self._ACCENT_SOFT),
                text_color=(self._ON_ACCENT if done else self._INK),
                image=self._icon("check_circle" if done else "arrow_back",
                                 self._ON_ACCENT if done else self._INK, 18),
                compound="left",
                text="返回配置",
                border_color=(self._ACCENT if done else self._BORDER)
            )
            # 功能 4 成功后：把 ADS 一键宏命令复制到剪贴板并给出高亮指引
            if done and self._func_var.get() == "4":
                self._show_ads_guide()
            self._show_run_result(done)

    def _output_targets_for_func(self, func):
        if func == "1":
            return [("网络表", os.path.join(self.base_dir, "2_输出_SIwave网络表"))]
        if func == "2":
            return [("SIwave 工程", os.path.join(self.base_dir, "3_Siwave"))]
        if func == "3":
            out_dir = self._siwave_out_var.get().strip()
            if not os.path.isabs(out_dir):
                out_dir = os.path.join(self.base_dir, out_dir)
            return [("PCB S 参数", out_dir)]
        if func == "4":
            sub = self._sub_var.get()
            if sub:
                from ads.ads_runtime import latest_ads_workspace_path
                ads_name = _ads_dir_from_subproject(sub)
                base = os.path.join(self.base_dir, "5_ADS仿真", ads_name)
                return [("ADS 工程", str(latest_ads_workspace_path(base)))]
            return [("ADS 工程", os.path.join(self.base_dir, "5_ADS仿真"))]
        if func == "6":
            return [("器件 Pin 路径", os.path.join(self.base_dir, "OUTPUT", "OTHER"))]
        return [("refnum Excel", os.path.join(self.base_dir, "7_refnum"))]

    def _recent_outputs(self, folder, limit=5):
        if not os.path.isdir(folder):
            return []
        started_at = getattr(self, "_run_started_at", 0) - 2
        items = []
        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime >= started_at:
                    items.append((mtime, name, os.path.isdir(path)))
        except OSError:
            return []
        items.sort(reverse=True)
        return items[:limit]

    def _open_folder(self, folder):
        if not os.path.isdir(folder):
            return
        try:
            os.startfile(folder)
        except Exception:
            subprocess.Popen(["explorer", folder], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _show_run_result(self, done):
        for w in self._result_frame.winfo_children():
            w.destroy()
        self._result_frame.pack(fill="x", pady=(10, 0), before=self._back_btn)

        top = ctk.CTkFrame(self._result_frame, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(
            top, text="运行结果",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
            text_color=self._INK
        ).pack(side="left")
        ctk.CTkLabel(
            top,
            text="已完成" if done else "运行未完成",
            corner_radius=12, height=24, padx=10,
            fg_color=(self._ACCENT_SOFT if done else self._ACCENT_SOFT),
            text_color=(self._OK if done else self._ERROR),
            font=ctk.CTkFont(size=11, weight="bold")
        ).pack(side="right")

        if not done:
            ctk.CTkLabel(
                self._result_frame,
                text="请先查看上方日志中的失败原因，修正输入或环境后重试。",
                font=ctk.CTkFont(size=12), text_color=self._MUTED,
                anchor="w", justify="left"
            ).pack(anchor="w", padx=12, pady=(0, 10))
            return

        for label, folder in self._output_targets_for_func(self._func_var.get()):
            row = ctk.CTkFrame(
                self._result_frame, fg_color=self._SURFACE,
                corner_radius=18, border_width=1, border_color=self._BORDER
            )
            row.pack(fill="x", padx=12, pady=(4, 8))
            exists = os.path.isdir(folder)
            rel = os.path.relpath(folder, self.base_dir) if exists else folder
            recent = self._recent_outputs(folder)
            if recent:
                names = []
                for _, name, is_dir in recent:
                    names.append(f"{name}/" if is_dir else name)
                detail = "；".join(names)
            else:
                detail = "未发现本次新增或更新的文件"
            ctk.CTkLabel(
                row, text=f"{label}：{rel}",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=12, weight="bold"),
                text_color=self._INK if exists else self._WARN,
                anchor="w"
            ).pack(side="left", fill="x", expand=True, padx=(10, 0), pady=10)
            ctk.CTkButton(
                row, text="打开目录", width=104, height=32, corner_radius=16,
                image=self._icon("folder_open", self._INK, 16), compound="left",
                state="normal" if exists else "disabled",
                fg_color=self._SURFACE, hover_color=self._ACCENT_SOFT,
                text_color=self._INK, border_width=1, border_color=self._BORDER,
                command=lambda p=folder: self._open_folder(p)
            ).pack(side="right", padx=(0, 10), pady=10)
            ctk.CTkLabel(
                self._result_frame, text=detail,
                font=ctk.CTkFont(family="Consolas", size=11),
                text_color=self._MUTED, anchor="w", justify="left",
                wraplength=680
            ).pack(anchor="w", padx=12, pady=(0, 4))

    # ── ADS 一键宏指引（功能 4 完成后）───────────────────────────────────

    def _show_ads_guide(self):
        """日志区插入高亮指引卡片（琥珀底 + 命令行深蓝白字），并闪烁提醒"""
        _cmd = 'load("{}/fix_snp_auto.ael");'.format(
            self.base_dir.replace("\\", "/"))
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(_cmd)
            _clip = "（已自动复制到剪贴板，直接粘贴即可）"
        except Exception:
            _clip = ""

        tb = self._log_box._textbox  # CTkTextbox 内部的原生 tkinter.Text
        tb.tag_config("guide", background="#fff3d6", foreground="#7a4d00",
                      lmargin1=10, lmargin2=10, spacing1=2, spacing3=2,
                      font=("Microsoft YaHei UI", 11, "bold"))
        tb.tag_config("guide_cmd", background="#46688f", foreground="#ffffff",
                      lmargin1=24, spacing1=6, spacing3=6,
                      font=("Consolas", 12, "bold"))
        tb.tag_raise("guide_cmd")

        self._log_box.configure(state="normal")
        tb.insert("end", "\n")
        tb.insert("end",
                  "  ⚠ ADS 工程已生成，还差最后一步加载 S 参数：  \n"
                  "  1. 打开 ADS，打开 5_ADS仿真 下生成的工作区  \n"
                  "  2. ADS 主窗口菜单：Tools > AEL Command Line...  \n"
                  "  3. 粘贴下面这行命令并回车" + _clip + "  \n", "guide")
        tb.insert("end", f"  {_cmd}  \n", ("guide", "guide_cmd"))
        tb.insert("end",
                  "  完成后所有 SnP 控件自动加载 S 参数并保存，  \n"
                  "  ADS 消息窗口会显示 refreshed SnP 总数。  \n", "guide")
        tb.insert("end", "\n")
        tb.see("end")
        self._log_box.configure(state="disabled")

        self._status_label.configure(
            text="全部完成 ✅  请按下方高亮指引完成最后一步（命令已复制）")
        self._flash_guide()

    def _make_fibocom_logo(self, parent):
        """Fibocom LOGO 贴图"""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        _icon_path = os.path.join(self.base_dir, "OIP.webp")
        if not os.path.isfile(_icon_path):
            _icon_path = os.path.join(self.base_dir, "_internal", "OIP.webp")
        if os.path.isfile(_icon_path):
            from PIL import Image
            _img = Image.open(_icon_path)
            _ctk_img = ctk.CTkImage(_img, size=(240, 34))
            _lbl = ctk.CTkLabel(frame, image=_ctk_img, text="")
            _lbl.image = _ctk_img
            _lbl.pack(side="left")
        return frame

    def _corner_icon(self):
        """左上角贴 OIP.webp 小图标"""
        _icon_path = os.path.join(self.base_dir, "OIP.webp")
        if not os.path.isfile(_icon_path):
            _icon_path = os.path.join(self.base_dir, "_internal", "OIP.webp")
        if os.path.isfile(_icon_path):
            from PIL import Image
            _img = Image.open(_icon_path)
            # 保持比例，宽度约40px
            w, h = _img.size
            _scale = 40 / w
            _ctk_img = ctk.CTkImage(_img, size=(40, int(h * _scale)))
            _lbl = ctk.CTkLabel(self._root, image=_ctk_img, text="")
            _lbl.image = _ctk_img
            _lbl.place(x=12, y=12)

    def _animate_logo(self, i=0):
        """逐字母亮起 Fibocom logo（每隔 75ms 一个字母）"""
        if i >= len(self._logo_labels):
            return
        lbl, color = self._logo_labels[i]
        lbl.configure(text_color=color)
        self._root.after(75, lambda: self._animate_logo(i + 1))

    def _pulse_back_btn(self, n=0):
        """成功后返回按钮绿色脉冲闪烁，吸引用户点击"""
        _bright, _normal = "#4caf50", self._OK
        try:
            self._back_btn.configure(fg_color=_bright if n % 2 == 0 else _normal)
        except Exception:
            return
        if n < 7:
            self._root.after(280, lambda: self._pulse_back_btn(n + 1))
        else:
            self._back_btn.configure(fg_color=_normal)

    def _flash_guide(self, n=0):
        """指引卡片背景与状态栏文字交替闪烁，提醒用户注意"""
        _bg = ["#ffd27a", "#fff3d6"]
        _fg = [self._WARN, self._OK]
        try:
            self._log_box._textbox.tag_config("guide", background=_bg[n % 2])
            self._status_label.configure(text_color=_fg[n % 2])
        except Exception:
            return
        if n < 9:
            self._root.after(350, lambda: self._flash_guide(n + 1))

    # ── 动画 ──────────────────────────────────────────────────────────────

    def _fade_in(self, step=0):
        alpha = min(1.0, step * 0.06)
        self._root.attributes('-alpha', alpha)
        if alpha < 1.0:
            self._root.after(16, lambda: self._fade_in(step + 1))

    def _pulse_title(self, palette, idx=0):
        target = getattr(self, "_app_title_label", None)
        if target is None:
            target = getattr(self, "_title_label", None)
        if target is not None:
            target.configure(text_color=palette[idx])
        if idx + 1 < len(palette):
            self._root.after(55, lambda: self._pulse_title(palette, idx + 1))

    def run(self):
        self._root.attributes('-alpha', 0.0)
        self._root.after(30, self._fade_in)
        self._root.after(400, lambda: self._pulse_title(
            ["#e0e6ec", "#c5d0da", "#a8b8c6", "#8e9fae", "#6e7a89"]
        ))
        self._root.mainloop()
        return self.result, self.choice


# ─── 公开函数 ────────────────────────────────────────────────────────────

def _write_settings(settings_path, result):
    """将 GUI 配置写回 setting.ini（worker 进程从该文件读取）"""
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(settings_path, encoding="utf-8")

    section_map = {
        "Schematic_ASCII_File":          "PROJECT_DATA",
        "Schematic_LGA_Ref":             "PROJECT_DATA",
        "Component_S_Parameter_Library": "SNP_LIBRARIES",
        "PCB_S_Parameters":              "SNP_LIBRARIES",
        "ADS_Project_Directory":         "SIMULATION_DATA",
        "Antenna_Match_50R":             "SIMULATION_DATA",
    }
    for key, val in result.items():
        section = section_map[key]
        if not config.has_section(section):
            config.add_section(section)
        for existing in list(config.options(section)):
            if existing.casefold() == key.casefold():
                config.remove_option(section, existing)
        config.set(section, key, val)

    with open(settings_path, "w", encoding="utf-8") as f:
        config.write(f)


def run_app():
    """启动持久化 GUI（MAIN 无 --worker 参数时的入口）"""
    # Nuitka onefile 模式下 __file__ 指向临时解压目录，需用 sys.argv[0] 定位 exe 所在目录
    if "__compiled__" in globals():
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    else:
        # src/app/ 再上两级是项目根目录
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    app = _ProjectSelectorApp(base_dir)
    app.run()
