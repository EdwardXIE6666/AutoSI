"""AutoSI GUI 的文件扫描、配置和命令构造模型。

本模块不依赖任何 GUI 框架。PySide6 正式界面通过这里访问工作目录，
旧 CustomTkinter 界面继续作为独立回退 Adapter 保留。
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.workspace import (
    INPUT_DIR,
    OUTPUT_ADS,
    OUTPUT_NETS,
    OUTPUT_OTHER,
    OUTPUT_REFNUM,
    OUTPUT_SPARAM,
    ensure_layout,
    ensure_runtime,
    existing_input_dir,
    existing_output_dir,
    has_workspace_layout,
    input_dir,
    output_dir,
    runtime_root,
    sidecar_path,
    source_root,
    subproject_name,
)
from siwave_extractor.stackup_xml import (
    copper_layer_count,
    load_stackup_xml,
    new_blank_stackup,
    stackup_siwave_problems,
)

OTHER_PACKAGE = "其它封装类型"
OTHER_MATERIAL = "其它板材"
LAYER_PRESETS = ("6层", "8层", "10层", "12层", "14层", "16层")
HDI_PRESETS = ("1阶", "2阶", "3阶", "4阶", "任意阶")
_STACKUP_DIR_ILLEGAL = re.compile(r'[<>:"/\\|?*]')


FUNCTIONS = {
    "1": ("网络频段信息", "从原理图生成 SIwave 网络频段表"),
    "2": ("初始化 SIWAVE", "导入 ODB++、叠层并去使能 R/L/C"),
    "3": ("SIwave 抽取", "按网络表抽取 PCB S 参数"),
    "4": ("ADS 仿真", "生成 ADS 工程并加载 S 参数"),
    "5": ("refnum 生成", "输出维修链路与编号匹配 Excel"),
    "6": ("器件 Pin 路径", "导出 Transceiver 到 ANT 的器件 Pin 连接"),
}

GUI_FUNCTIONS = ("1", "2", "3", "4", "5", "6")


def runtime_base_dir() -> Path:
    return ensure_runtime(runtime_root())


def scan_schematics(base_dir: Path) -> list[str]:
    folder = existing_input_dir(base_dir)
    if not folder.is_dir():
        return []
    from app.file_kinds import is_schematic_text
    values = [
        p for p in folder.iterdir()
        if p.is_file() and is_schematic_text(p)
    ]
    values.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in values]


def scan_odb(base_dir: Path) -> list[str]:
    folder = existing_input_dir(base_dir)
    if not folder.is_dir():
        return []
    from app.file_kinds import is_odb_file
    values = [p for p in folder.iterdir() if p.is_file() and is_odb_file(p)]
    values.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in values]


def scan_siw(base_dir: Path) -> list[str]:
    folder = existing_input_dir(base_dir)
    if not folder.is_dir():
        return []
    from app.file_kinds import is_siw_file
    values = [p for p in folder.iterdir() if p.is_file() and is_siw_file(p)]
    values.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in values]


def scan_netlists(base_dir: Path) -> list[str]:
    folder = existing_output_dir(base_dir, OUTPUT_NETS)
    if not folder.is_dir():
        return []
    from app.file_kinds import is_netlist_text
    values = [p for p in folder.iterdir() if p.is_file() and is_netlist_text(p)]
    return [p.name for p in sorted(values, key=lambda p: p.stat().st_mtime, reverse=True)]


def scan_subprojects(base_dir: Path) -> list[str]:
    folder = existing_output_dir(base_dir, OUTPUT_SPARAM)
    if not folder.is_dir():
        return []
    values = [p for p in folder.iterdir() if p.is_dir() and p.name.startswith("SubProject_")]
    values.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in values]


SNP_LIB_NAMES = ("S参数库", "Sparameter", "2_S参数库")
SNP_LIB_SIDECAR = "snp_library_path.txt"


def find_library(base_dir: Path, names: Iterable[str]) -> Path | None:
    found = list_libraries(base_dir, names)
    return found[0] if found else None


def list_libraries(base_dir: Path, names: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in (Path("D:/"), Path(base_dir).resolve().parent, Path(base_dir).resolve()):
        for name in names:
            candidate = root / name
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve()
            key = str(resolved).casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(resolved)
    return found


def list_snp_libraries(base_dir: Path) -> list[Path]:
    return list_libraries(base_dir, SNP_LIB_NAMES)


def load_saved_snp_library(runtime_dir: Path) -> Path | None:
    path = sidecar_path(runtime_dir, SNP_LIB_SIDECAR)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().strip('"')
    except OSError:
        return None
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_dir():
        return candidate.resolve()
    return None


def save_snp_library(runtime_dir: Path, lib_dir: Path) -> None:
    dest = sidecar_path(runtime_dir, SNP_LIB_SIDECAR, write=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(str(Path(lib_dir).resolve()), encoding="utf-8")


def find_snp_library(base_dir: Path) -> Path | None:
    saved = load_saved_snp_library(base_dir)
    if saved is not None:
        return saved
    found = list_snp_libraries(base_dir)
    return found[0] if found else None


def find_board_library(base_dir: Path) -> Path | None:
    return find_library(base_dir, ("板材库", "0_板材库"))


def scan_board_tree(base_dir: Path) -> dict[str, dict[str, dict[str, list[str]]]]:
    library = find_board_library(base_dir)
    return scan_stackup_tree(library) if library else {}


def scan_stackup_tree(root: Path | None) -> dict[str, dict[str, dict[str, list[str]]]]:
    tree: dict[str, dict[str, dict[str, list[str]]]] = {}
    if root is None or not Path(root).is_dir():
        return tree
    for package in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(".")):
        for material in sorted(p for p in package.iterdir() if p.is_dir()):
            layers = sorted(
                (p for p in material.iterdir() if p.is_dir()),
                key=lambda p: int("".join(c for c in p.name if c.isdigit()) or "999"),
            )
            for layer in layers:
                hdis = sorted(
                    p.name for p in layer.iterdir()
                    if p.is_dir() and any(x.suffix.lower() == ".xml" for x in p.iterdir())
                )
                if hdis:
                    tree.setdefault(package.name, {}).setdefault(material.name, {})[layer.name] = hdis
    return tree


def merge_stackup_trees(*trees: dict) -> dict[str, dict[str, dict[str, list[str]]]]:
    merged: dict[str, dict[str, dict[str, list[str]]]] = {}
    for tree in trees:
        for package, materials in (tree or {}).items():
            for material, layers in materials.items():
                for layer, hdis in layers.items():
                    bucket = (
                        merged.setdefault(package, {})
                        .setdefault(material, {})
                        .setdefault(layer, [])
                    )
                    for hdi in hdis:
                        if hdi not in bucket:
                            bucket.append(hdi)
    return merged


def _safe_stackup_part(value: str) -> str:
    text = _STACKUP_DIR_ILLEGAL.sub("_", str(value or "").strip()) or "未命名"
    return text.strip(" .") or "未命名"


def custom_stackup_root(workspace: Path) -> Path:
    if has_workspace_layout(workspace):
        return output_dir(workspace, OUTPUT_OTHER, "stackup")
    return existing_input_dir(workspace) / "stackup"


def custom_stackup_file(workspace: Path, package: str, material: str, layers: str, hdi: str) -> Path:
    folder = (
        custom_stackup_root(workspace)
        / _safe_stackup_part(package)
        / _safe_stackup_part(material)
        / _safe_stackup_part(layers)
        / _safe_stackup_part(hdi)
    )
    stem = "_".join(
        _safe_stackup_part(part) for part in (package, material, layers, hdi)
    )
    return folder / f"{stem}.xml"


def find_stackup_xml(
    base_dir: Path,
    package: str,
    material: str,
    layers: str,
    hdi: str,
    workspace: Path | None = None,
) -> Path | None:
    if workspace is not None:
        custom = custom_stackup_file(workspace, package, material, layers, hdi)
        if custom.is_file():
            return custom
        custom_dir = custom.parent
        if custom_dir.is_dir():
            found = sorted(custom_dir.glob("*.xml"))
            if found:
                return found[0]
    library = find_board_library(base_dir)
    if not library:
        return None
    folder = library / package / material / layers / hdi
    if not folder.is_dir():
        return None
    return next(iter(sorted(folder.glob("*.xml"))), None)


def stackup_package_choices(tree: dict) -> list[str]:
    names = sorted(tree)
    if OTHER_PACKAGE not in names:
        names.append(OTHER_PACKAGE)
    return names


def stackup_material_choices(tree: dict, package: str) -> list[str]:
    names = sorted((tree.get(package) or {}))
    if OTHER_MATERIAL not in names:
        names.append(OTHER_MATERIAL)
    return names


def _stackup_count_key(name: str) -> tuple:
    """层数、阶数按数字从小到大。没有数字的（如任意阶）排在后面。"""
    digits = "".join(c for c in str(name) if c.isdigit())
    if digits:
        return (0, int(digits), str(name))
    return (1, 0, str(name))


def stackup_layer_choices(tree: dict, package: str, material: str) -> list[str]:
    names = list((tree.get(package) or {}).get(material) or {})
    for preset in LAYER_PRESETS:
        if preset not in names:
            names.append(preset)
    names.sort(key=_stackup_count_key)
    return names


def stackup_hdi_choices(tree: dict, package: str, material: str, layers: str) -> list[str]:
    names = list(((tree.get(package) or {}).get(material) or {}).get(layers) or [])
    for preset in HDI_PRESETS:
        if preset not in names:
            names.append(preset)
    names.sort(key=_stackup_count_key)
    return names


def project_key(name: str) -> str:
    stem = Path(name).stem
    upper = stem.upper()
    for marker in ("+NETWORK_", "_SCH", "_PCB", "_REFNUM", "_SIWAVE"):
        index = upper.find(marker)
        if index >= 0:
            stem = stem[:index]
            break
    if stem.startswith("SubProject_"):
        stem = stem[len("SubProject_"):]
    return stem.rstrip("_- ")


def ads_dir_from_subproject(name: str) -> str:
    key = name[len("SubProject_"):] if name.startswith("SubProject_") else name
    return f"{key}_ADS"


def write_settings(base_dir: Path, values: dict[str, str]) -> None:
    path = sidecar_path(base_dir, "setting.ini", write=True)
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(path, encoding="utf-8")
    sections = {
        "Schematic_ASCII_File": "PROJECT_DATA",
        "Schematic_LGA_Ref": "PROJECT_DATA",
        "Workspace_Directory": "PROJECT_DATA",
        "Component_S_Parameter_Library": "SNP_LIBRARIES",
        "PCB_S_Parameters": "SNP_LIBRARIES",
        "ADS_Project_Directory": "SIMULATION_DATA",
        "Antenna_Match_50R": "SIMULATION_DATA",
        "Refnum_ADS_Workspace": "SIMULATION_DATA",
        "Refnum_ADS_Import": "SIMULATION_DATA",
        "Refnum_Value_Source": "SIMULATION_DATA",
    }
    for key, value in values.items():
        section = sections[key]
        if not config.has_section(section):
            config.add_section(section)
        for existing in list(config.options(section)):
            if existing.casefold() == key.casefold():
                config.remove_option(section, existing)
        config.set(section, key, value)
    with path.open("w", encoding="utf-8") as handle:
        config.write(handle)


def read_parallel_workers(base_dir: Path) -> int:
    try:
        data = json.loads(sidecar_path(base_dir, "config.json").read_text(encoding="utf-8"))
        return max(1, min(4, int(data.get("parallel_workers", 1))))
    except Exception:
        return 1


def write_parallel_workers(base_dir: Path, workers: int) -> Path:
    path = sidecar_path(base_dir, "config.json", write=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["parallel_workers"] = max(1, min(4, int(workers)))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@dataclass
class GuiSelection:
    schematic: str = ""
    odb: str = ""
    siw: str = ""
    netlist: str = ""
    subproject: str = ""
    output_dir: str = ""
    workers: int = 1
    deactivate_rlc: bool = True
    import_stackup: bool = True
    match_50r: bool = False
    refnum_ads_workspace: str = ""
    refnum_ads_import: bool = True
    refnum_value_source: str = "ads"
    package: str = ""
    material: str = ""
    layers: str = ""
    hdi: str = ""


@dataclass
class GuiModel:
    base_dir: Path
    runtime_dir: Path | None = None
    selection: GuiSelection = field(default_factory=GuiSelection)
    schematics: list[str] = field(default_factory=list)
    odb_files: list[str] = field(default_factory=list)
    siw_files: list[str] = field(default_factory=list)
    netlists: list[str] = field(default_factory=list)
    subprojects: list[str] = field(default_factory=list)
    board_tree: dict = field(default_factory=dict)
    snp_library: Path | None = None
    snp_candidates: list[Path] = field(default_factory=list)
    stackup_doc: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir).resolve()
        active = has_workspace_layout(self.base_dir)
        if active:
            ensure_layout(self.base_dir)
        if self.runtime_dir is None:
            self.runtime_dir = self.base_dir.parent if active else self.base_dir
        self.runtime_dir = Path(self.runtime_dir).resolve()
        if active:
            self.selection.output_dir = str(output_dir(self.base_dir, OUTPUT_SPARAM))
        self.selection.workers = read_parallel_workers(self.runtime_dir)
        self.refresh()

    def bind_workspace(self, workspace: Path) -> None:
        self.base_dir = Path(workspace).resolve()
        ensure_layout(self.base_dir)
        self.runtime_dir = self.base_dir.parent
        self.selection.output_dir = str(output_dir(self.base_dir, OUTPUT_SPARAM))
        self.refresh()

    def refresh(self) -> None:
        current = self.selection
        self.schematics = scan_schematics(self.base_dir)
        self.odb_files = scan_odb(self.base_dir)
        self.siw_files = scan_siw(self.base_dir)
        self.netlists = scan_netlists(self.base_dir)
        self.subprojects = scan_subprojects(self.base_dir)
        self.board_tree = merge_stackup_trees(
            scan_board_tree(self.runtime_dir),
            scan_stackup_tree(custom_stackup_root(self.base_dir)),
        )
        self._load_snp_libraries()
        self._load_refnum_ads_workspace()
        self._load_refnum_ads_import()
        odb_source = self.odb_source()
        if not (
            odb_source
            and odb_source.is_file()
            and odb_source.suffix.lower() == ".tgz"
            and Path(current.odb).is_absolute()
        ):
            if current.odb not in self.odb_files:
                current.odb = self.odb_files[0] if self.odb_files else ""
        for attr, values in (
            ("schematic", self.schematics),
            ("siw", self.siw_files), ("netlist", self.netlists),
            ("subproject", self.subprojects),
        ):
            value = getattr(current, attr)
            if value not in values:
                setattr(current, attr, values[0] if values else "")
        packages = sorted(self.board_tree)
        if current.package not in packages:
            current.package = packages[0] if packages else ""
        self.normalize_stackup()

    def _load_snp_libraries(self) -> None:
        found = list_snp_libraries(self.runtime_dir)
        saved = load_saved_snp_library(self.runtime_dir)
        candidates: list[Path] = []
        seen: set[str] = set()
        for path in ([saved] if saved else []) + found:
            if path is None or not path.is_dir():
                continue
            key = str(path.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(path.resolve())
        self.snp_candidates = candidates
        self.snp_library = saved if saved else (candidates[0] if candidates else None)

    def _load_refnum_ads_workspace(self) -> None:
        if self.selection.refnum_ads_workspace:
            return
        path = sidecar_path(self.runtime_dir, "setting.ini")
        if not path.is_file():
            return
        config = configparser.ConfigParser()
        config.read(path, encoding="utf-8")
        saved = ""
        if config.has_section("SIMULATION_DATA"):
            saved = config.get("SIMULATION_DATA", "Refnum_ADS_Workspace", fallback="").strip()
        if saved:
            self.selection.refnum_ads_workspace = saved

    def _load_refnum_ads_import(self) -> None:
        if getattr(self, "_refnum_ads_import_loaded", False):
            return
        self._refnum_ads_import_loaded = True
        path = sidecar_path(self.runtime_dir, "setting.ini")
        if not path.is_file():
            return
        config = configparser.ConfigParser()
        config.read(path, encoding="utf-8")
        if not config.has_section("SIMULATION_DATA"):
            return
        source = config.get(
            "SIMULATION_DATA", "Refnum_Value_Source", fallback="",
        ).strip().lower()
        if source not in ("ads", "schematic"):
            source = "ads"
        self.selection.refnum_value_source = source
        self.selection.refnum_ads_import = source == "ads"

    def discover_ads_workspaces(self) -> list[Path]:
        """OUTPUT/ADS 下带 lib.defs 的工程，新的在前。"""
        root = existing_output_dir(self.base_dir, OUTPUT_ADS)
        if not root.is_dir():
            return []
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            if "lib.defs" in filenames:
                found.append(Path(dirpath))
                dirnames.clear()
        found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return found

    def set_snp_library(self, path: str | Path) -> str | None:
        candidate = Path(str(path).strip().strip('"'))
        if not candidate.is_dir():
            return "请选择有效的 S 参数库文件夹"
        resolved = candidate.resolve()
        save_snp_library(self.runtime_dir, resolved)
        key = str(resolved).casefold()
        self.snp_candidates = [
            item for item in self.snp_candidates
            if str(item).casefold() != key
        ]
        self.snp_candidates.insert(0, resolved)
        self.snp_library = resolved
        return None

    def odb_choices(self) -> list[str]:
        choices = list(self.odb_files)
        if self.selection.odb and Path(self.selection.odb).is_absolute():
            choices.insert(0, self.selection.odb)
        return choices

    def odb_source(self) -> Path | None:
        if not self.selection.odb:
            return None
        selected = Path(self.selection.odb).expanduser()
        if selected.is_absolute():
            return selected.resolve()
        return existing_input_dir(self.base_dir) / selected

    def set_odb_source(self, value: str) -> str | None:
        selected = Path(value).expanduser()
        source = selected.resolve() if selected.is_absolute() else existing_input_dir(self.base_dir) / selected
        if source.suffix.lower() != ".tgz":
            return "请选择 .tgz 格式的 ODB++ 文件。"
        if not source.is_file():
            return f"ODB 文件不存在：{source}"
        from app.file_kinds import mismatch_message
        error = mismatch_message("odb", source)
        if error:
            return error
        self.selection.odb = str(source) if selected.is_absolute() else selected.name
        return None

    # 输入文件导入：外部文件复制进工程标准目录后刷新并选中
    INPUT_IMPORT_KINDS = {
        "schematic": {
            "folder": INPUT_DIR,
            "selection": "schematic",
            "title": "添加原理图",
            "filter": "PADS 原理图 (*.txt);;所有文件 (*)",
            "button": "添加…",
            "suffixes": {".txt"},
            "name_ok": lambda name: Path(name).suffix.lower() == ".txt",
            "name_error": "原理图须为 .txt 文件。",
        },
        "odb": {
            "folder": INPUT_DIR,
            "selection": "odb",
            "suffixes": {".tgz"},
            "title": "添加 ODB++",
            "filter": "ODB++ 文件 (*.tgz);;所有文件 (*)",
            "button": "添加…",
            "name_ok": lambda name: Path(name).suffix.lower() == ".tgz",
            "name_error": "ODB++ 须为 .tgz 文件。",
        },
        "siw": {
            "folder": INPUT_DIR,
            "selection": "siw",
            "suffixes": {".siw"},
            "title": "添加 SIwave 工程",
            "filter": "SIwave 工程 (*.siw);;所有文件 (*)",
            "button": "添加…",
            "name_ok": lambda name: Path(name).suffix.lower() == ".siw",
            "name_error": "请选择 .siw 文件。",
        },
        "netlist": {
            "folder": OUTPUT_NETS,
            "selection": "netlist",
            "suffixes": {".txt"},
            "title": "添加网络表",
            "filter": "网络频段表 (*.txt);;所有文件 (*)",
            "button": "添加…",
            "name_ok": lambda name: Path(name).suffix.lower() == ".txt",
            "name_error": "请选择 .txt 文件。",
        },
    }

    def import_input_file(
        self,
        kind: str,
        source: str | Path,
        *,
        overwrite: bool = False,
    ) -> tuple[str | None, str | None]:
        """复制外部输入文件到工程标准目录并选中。

        返回 (选中文件名, 错误信息)。错误为 ``EXISTS:路径`` 时表示目标已存在且未覆盖。
        """
        spec = self.INPUT_IMPORT_KINDS.get(kind)
        if not spec:
            return None, f"未知输入类型：{kind}"
        src = Path(source).expanduser().resolve()
        if not src.is_file():
            return None, f"文件不存在：{src}"
        from app.file_kinds import FORMAT_ERROR_PREFIX, mismatch_message
        suffixes = spec["suffixes"]
        if suffixes is not None and src.suffix.lower() not in suffixes:
            return None, f"{FORMAT_ERROR_PREFIX}{spec['name_error']}"
        kind_error = mismatch_message(kind, src)
        if kind_error:
            return None, f"{FORMAT_ERROR_PREFIX}{kind_error}"
        if not spec["name_ok"](src.name):
            return None, spec["name_error"]

        dest_dir = (
            output_dir(self.base_dir, OUTPUT_NETS)
            if kind == "netlist"
            else input_dir(self.base_dir)
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        try:
            same_file = dest.exists() and dest.resolve() == src.resolve()
        except OSError:
            same_file = False
        if same_file:
            selected = src.name
        else:
            if dest.exists() and not overwrite:
                return None, f"EXISTS:{dest}"
            try:
                shutil.copy2(src, dest)
            except OSError as exc:
                return None, f"复制失败：{exc}"
            selected = dest.name

        self.refresh()
        list_attr = {
            "schematic": "schematics",
            "siw": "siw_files",
            "netlist": "netlists",
            "odb": "odb_files",
        }[kind]
        values = getattr(self, list_attr)
        if selected not in values:
            return None, (
                f"已复制到 {spec['folder']}，但未被扫描识别："
                f"{selected}（请检查命名规则）"
            )
        setattr(self.selection, spec["selection"], selected)
        return selected, None

    def normalize_stackup(self) -> None:
        s = self.selection
        packages = stackup_package_choices(self.board_tree)
        if s.package and s.package not in packages:
            packages = [s.package] + packages
        if not s.package:
            s.package = next((p for p in packages if p != OTHER_PACKAGE), packages[0] if packages else OTHER_PACKAGE)
        materials = stackup_material_choices(self.board_tree, s.package)
        if s.material and s.material not in materials:
            materials = [s.material] + materials
        if not s.material:
            s.material = next((m for m in materials if m != OTHER_MATERIAL), materials[0] if materials else OTHER_MATERIAL)
        layers = stackup_layer_choices(self.board_tree, s.package, s.material)
        if not s.layers:
            s.layers = layers[0] if layers else "8层"
        hdis = stackup_hdi_choices(self.board_tree, s.package, s.material, s.layers)
        if not s.hdi:
            s.hdi = hdis[0] if hdis else "任意阶"

    def stackup_xml(self) -> Path | None:
        s = self.selection
        return find_stackup_xml(
            self.runtime_dir, s.package, s.material, s.layers, s.hdi, self.base_dir
        )

    def reload_stackup_doc(self) -> None:
        """有库/已保存 XML 则加载；没有则按层数生成空结构给用户填。"""
        xml = self.stackup_xml()
        if xml is not None and xml.is_file():
            self.stackup_doc = load_stackup_xml(xml)
            return
        n = copper_layer_count(self.selection.layers) or 8
        self.stackup_doc = new_blank_stackup(n, self._custom_stackup_path())

    def _custom_stackup_path(self) -> Path:
        s = self.selection
        return custom_stackup_file(self.base_dir, s.package, s.material, s.layers, s.hdi)

    def save_custom_stackup(self) -> Path:
        doc = self.stackup_doc
        if doc is None:
            raise ValueError("没有可保存的叠层")
        dest = self._custom_stackup_path()
        doc.write(dest)
        doc.path = dest
        doc.dirty = False
        self.board_tree = merge_stackup_trees(
            scan_board_tree(self.runtime_dir),
            scan_stackup_tree(custom_stackup_root(self.base_dir)),
        )
        return dest

    def stackup_xml_for_import(self) -> Path | None:
        """导入用 XML：库文件、已保存自定义，或把当前表写成工作区副本。"""
        doc = self.stackup_doc
        xml = self.stackup_xml()
        if doc is None:
            return xml
        if xml and xml.is_file() and not getattr(doc, "dirty", False):
            return xml
        dest = xml if xml else self._custom_stackup_path()
        if xml and xml.is_file():
            try:
                xml.resolve().relative_to(custom_stackup_root(self.base_dir).resolve())
            except ValueError:
                dest = self._custom_stackup_path()
        return doc.write(dest)

    def mismatch(self, function: str) -> str:
        return ""

    def _sparam_dir(self) -> Path:
        s = self.selection
        if s.subproject:
            return existing_output_dir(self.base_dir, OUTPUT_SPARAM, s.subproject)
        name = subproject_name(self.base_dir, s.siw, len(self.siw_files))
        return output_dir(self.base_dir, OUTPUT_SPARAM, name)

    def _validate_50r(self) -> tuple[bool, str]:
        from schematic.net_class_50r import MISSING_50R_MESSAGE, extract_50r_nets_from_file

        path = existing_input_dir(self.base_dir) / self.selection.schematic
        if not path.is_file():
            return False, "无法读取原理图，无法检查 50R 网络"
        try:
            nets = extract_50r_nets_from_file(path)
        except OSError:
            return False, "无法读取原理图，无法检查 50R 网络"
        if not nets:
            return False, MISSING_50R_MESSAGE
        return True, ""

    def validate(self, function: str) -> tuple[bool, str]:
        s = self.selection
        if function in ("1", "5", "6"):
            if not s.schematic:
                return False, "输入 中没有可用的 FG 原理图"
            return self._validate_50r()
        if function == "2":
            if not s.deactivate_rlc and not s.import_stackup:
                return False, "请至少选择“去使能 R/L/C”或“导入叠层信息”其中一项。"
            if not s.siw:
                return False, "未选择 .siw 工程文件，无法初始化 SIWAVE。"
            return True, ""
        if function == "3":
            ok = bool(s.siw and s.netlist)
            return ok, "" if ok else "请选择 .siw 工程和网络频段信息文件"
        if function == "4":
            if not s.schematic:
                return False, "请选择原理图文件"
            if not s.subproject:
                return False, "OUTPUT/SPARAM 中没有可用子项目"
            return self._validate_50r()
        return False, "未知功能"

    def collect_settings(self, function: str) -> dict[str, str]:
        s = self.selection
        values: dict[str, str] = {
            "Workspace_Directory": str(self.base_dir),
        }
        if function in ("1", "4", "5", "6") and s.schematic:
            values["Schematic_ASCII_File"] = str(existing_input_dir(self.base_dir) / s.schematic)
        if self.snp_library:
            values["Component_S_Parameter_Library"] = str(self.snp_library)
        if function == "4" and s.subproject:
            values["PCB_S_Parameters"] = str(self._sparam_dir())
            ads_name = ads_dir_from_subproject(s.subproject)
            ads_path = output_dir(self.base_dir, OUTPUT_ADS, ads_name)
            ads_path.mkdir(parents=True, exist_ok=True)
            values["ADS_Project_Directory"] = str(ads_path)
            values["Antenna_Match_50R"] = "1" if s.match_50r else "0"
        if function == "5":
            source = s.refnum_value_source if s.refnum_value_source in ("ads", "schematic") else "ads"
            values["Refnum_Value_Source"] = source
            values["Refnum_ADS_Workspace"] = s.refnum_ads_workspace.strip()
            values["Refnum_ADS_Import"] = "1" if source == "ads" else "0"
        return values

    def _main_command(self, choice: str) -> list[str]:
        if "__compiled__" in globals():
            return [str(Path(sys.argv[0]).resolve()), "--worker", "--choice", choice]
        return [sys.executable, "-u", str(source_root() / "src" / "MAIN.py"), "--worker", "--choice", choice]

    def siwave_init_command(self) -> tuple[list[str] | None, str | None]:
        s = self.selection
        if not s.deactivate_rlc and not s.import_stackup:
            return None, "请至少选择“去使能 R/L/C”或“导入叠层信息”其中一项。"
        if not s.siw:
            return None, "未选择 .siw 工程文件，无法初始化 SIWAVE。"
        command = ["python", "-u", "-m", "siwave_extractor", "--project", str(existing_input_dir(self.base_dir) / s.siw), "--stackup-only"]
        if s.deactivate_rlc:
            command.append("--deactivate-rlc")
        if s.import_stackup:
            xml = self.stackup_xml_for_import()
            if not xml or not Path(xml).exists():
                return None, "没有可用叠层。请选择封装/层数并填写，或先保存自定义叠层。"
            try:
                problems = stackup_siwave_problems(load_stackup_xml(xml))
            except Exception as exc:
                return None, f"叠层 XML 读失败：{exc}"
            if problems:
                return None, "叠层 XML 不能导入 SIwave（会崩溃）：\n" + "\n".join(problems)
            command += ["--stackup", str(xml)]
        return command, None

    def command_for(self, function: str) -> tuple[list[str] | None, str | None]:
        s = self.selection
        if function == "1":
            return self._main_command("1"), None
        if function == "2":
            return self.siwave_init_command()
        if function == "3":
            sparam = output_dir(self.base_dir, OUTPUT_SPARAM)
            sparam.mkdir(parents=True, exist_ok=True)
            s.output_dir = str(sparam)
            command = ["python", "-u", "-m", "siwave_extractor",
                       "--project", str(existing_input_dir(self.base_dir) / s.siw),
                       "--nets", str(existing_output_dir(self.base_dir, OUTPUT_NETS) / s.netlist),
                       "--out", str(sparam),
                       "--config", str(sidecar_path(self.runtime_dir, "config.json", write=True))]
            return command, None
        if function == "4":
            return self._main_command("2"), None
        if function == "5":
            return self._main_command("3"), None
        if function == "6":
            return self._main_command("4"), None
        return None, "未知功能"

    def odb_import_command(self) -> tuple[list[str] | None, str | None, str]:
        if not self.selection.odb:
            return None, "请先选择或添加一个 ODB++ 文件。", ""
        source = self.odb_source()
        if not source or source.suffix.lower() != ".tgz":
            return None, "请选择 .tgz 格式的 ODB++ 文件。", ""
        if not source.is_file():
            return None, f"ODB 文件不存在：{source}", ""
        output = source.with_suffix(".siw").name
        command = ["python", "-u", "-m", "siwave_extractor", "--import-odb", str(source),
                   "--out-siw", str(input_dir(self.base_dir) / output)]
        return command, None, output

    def output_target(self, function: str) -> tuple[str, Path]:
        s = self.selection
        if function == "1":
            return "网络表", existing_output_dir(self.base_dir, OUTPUT_NETS)
        if function == "2":
            return "SIwave 工程", input_dir(self.base_dir)
        if function == "3":
            stem = Path(s.siw).stem if s.siw else ""
            name = f"SubProject_{stem}" if stem else ""
            dest = output_dir(self.base_dir, OUTPUT_SPARAM, name) if name else output_dir(self.base_dir, OUTPUT_SPARAM)
            return "PCB S 参数", dest
        if function == "4":
            from ads.ads_runtime import latest_ads_workspace_path
            base = output_dir(self.base_dir, OUTPUT_ADS, ads_dir_from_subproject(s.subproject))
            return "ADS 工程", latest_ads_workspace_path(base)
        if function == "6":
            return "器件 Pin 路径", existing_output_dir(self.base_dir, OUTPUT_OTHER)
        return "refnum Excel", existing_output_dir(self.base_dir, OUTPUT_REFNUM)


def subprocess_environment(runtime_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    candidates = (
        source_root() / "src",
        Path(runtime_dir) / "_internal" / "src",
        Path(runtime_dir) / "src",
    )
    src = next(
        (str(path) for path in candidates if path.is_dir()),
        str(source_root() / "src"),
    )
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env
