"""单项目工作区：标记文件、INPUT/OUTPUT 路径、产物命名。

工作区与 MAIN.exe 平级，只由界面创建。输入文件名不跟工作区名对表；
产物带工作区标识。本模块不依赖 GUI。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

MARKER_NAME = "workspace.json"
INPUT_DIR = "INPUT"
LEGACY_INPUT_DIR = "输入"
OUTPUT_DIR = "OUTPUT"
LEGACY_OUTPUT_DIR = "输出"
OUTPUT_NETS = "NETS"
OUTPUT_SPARAM = "SPARAM"
OUTPUT_ADS = "ADS"
OUTPUT_REFNUM = "refnum"
OUTPUT_OTHER = "OTHER"
OUTPUT_LOG = "LOG"
OUTPUT_PREVIEW = "intermediate"
INTERNAL_DIR = "_internal"
DEFAULT_RUNTIME = Path(r"C:\Project\AutoSI")

OUTPUT_SUBDIRS = (
    OUTPUT_NETS,
    OUTPUT_SPARAM,
    OUTPUT_ADS,
    OUTPUT_REFNUM,
    OUTPUT_OTHER,
    OUTPUT_LOG,
)

RESERVED_NAMES = frozenset({
    INTERNAL_DIR,
    "src",
    "docs",
    "scripts",
    "licenses",
    "程序",
    "开发者",
    "原理图辅助",
    "环境安装包",
    "S参数库",
    "2_S参数库",
    "板材库",
    "0_板材库",
})

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*]')
_TO_MODERN = {
    LEGACY_INPUT_DIR: INPUT_DIR,
    LEGACY_OUTPUT_DIR: OUTPUT_DIR,
    "网络表": OUTPUT_NETS,
    "S参数": OUTPUT_SPARAM,
    "其他": OUTPUT_OTHER,
    "日志": OUTPUT_LOG,
    "中间产物": OUTPUT_PREVIEW,
}
_MODERN_TO_LEGACY = {modern: legacy for legacy, modern in _TO_MODERN.items()}


def is_packaged() -> bool:
    return "__compiled__" in globals()


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def runtime_root() -> Path:
    if is_packaged():
        return Path(sys.argv[0]).resolve().parent
    env = os.environ.get("AUTOSI_RUNTIME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_RUNTIME


def ensure_runtime(root: Path | None = None) -> Path:
    path = Path(root or runtime_root()).resolve()
    path.mkdir(parents=True, exist_ok=True)
    (path / INTERNAL_DIR).mkdir(exist_ok=True)
    return path


def internal_dir(runtime: Path) -> Path:
    return Path(runtime) / INTERNAL_DIR


def sidecar_path(runtime: Path, name: str, *, write: bool = False) -> Path:
    """程序旁或 `_internal` 里的同名文件。有内部目录时读写都走内部，避免根目录旧文件盖住。"""
    root = Path(runtime) / name
    hidden = internal_dir(runtime) / name
    if not internal_dir(runtime).is_dir():
        return root
    if write or hidden.is_file():
        return hidden
    return root if root.is_file() else hidden


def marker_path(workspace: Path) -> Path:
    return Path(workspace) / MARKER_NAME


def is_workspace(path: Path) -> bool:
    return marker_path(path).is_file()


def validate_name(name: str) -> str | None:
    text = (name or "").strip()
    if not text:
        return "请输入工作区名称"
    if text in RESERVED_NAMES:
        return f"「{text}」是程序保留名，请换一个"
    if text in {".", ".."} or text.startswith("."):
        return "工作区名称不能以点开头"
    if _ILLEGAL_CHARS.search(text):
        return "名称含有不能用于文件夹的字符"
    if len(text) > 80:
        return "名称过长"
    return None


def input_dir(workspace: Path) -> Path:
    return Path(workspace) / INPUT_DIR


def existing_input_dir(workspace: Path) -> Path:
    modern = input_dir(workspace)
    if _has_payload(modern):
        return modern
    legacy = Path(workspace) / LEGACY_INPUT_DIR
    if _has_payload(legacy):
        return legacy
    return modern


def has_workspace_layout(workspace: Path) -> bool:
    root = Path(workspace)
    return (
        is_workspace(root)
        or (root / INPUT_DIR).is_dir()
        or (root / LEGACY_INPUT_DIR).is_dir()
    )


def output_dir(workspace: Path, *parts: str) -> Path:
    path = Path(workspace) / OUTPUT_DIR
    for part in parts:
        path = path / part
    return path


def _has_payload(path: Path) -> bool:
    if path.is_file():
        return True
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def modernize_output_path(path: Path | str) -> Path:
    """把路径里旧中文目录段换成英文。新写入不得再走进中文目录。"""
    parts = [_TO_MODERN.get(part, part) for part in Path(path).parts]
    return Path(*parts) if parts else Path(path)


def existing_output_dir(workspace: Path, *parts: str) -> Path:
    """读已有产物：英文目录有内容用英文；否则回退到旧中文路径，避免旧工作区被掏空。"""
    modern = output_dir(workspace, *parts)
    if _has_payload(modern):
        return modern
    legacy_parts = tuple(_MODERN_TO_LEGACY.get(part, part) for part in parts)
    variants = (
        (OUTPUT_DIR, parts),
        (OUTPUT_DIR, legacy_parts),
        (LEGACY_OUTPUT_DIR, parts),
        (LEGACY_OUTPUT_DIR, legacy_parts),
    )
    seen = {modern}
    for root, segs in variants:
        path = Path(workspace) / root
        for seg in segs:
            path = path / seg
        if path in seen:
            continue
        seen.add(path)
        if _has_payload(path):
            return path
    return modern


def ensure_layout(workspace: Path) -> Path:
    root = Path(workspace).resolve()
    input_dir(root).mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_SUBDIRS:
        output_dir(root, name).mkdir(parents=True, exist_ok=True)
    output_dir(root, OUTPUT_REFNUM, OUTPUT_PREVIEW).mkdir(parents=True, exist_ok=True)
    return root


def list_workspaces(runtime: Path) -> list[Path]:
    root = Path(runtime)
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and is_workspace(p)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def create_workspace(runtime: Path, name: str) -> Path:
    error = validate_name(name)
    if error:
        raise ValueError(error)
    dest = Path(runtime).resolve() / name.strip()
    if dest.exists():
        raise ValueError(f"已存在同名文件夹：{dest.name}")
    dest.mkdir(parents=True)
    ensure_layout(dest)
    payload = {
        "name": name.strip(),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    marker_path(dest).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dest


def workspace_name(workspace: Path) -> str:
    marker = marker_path(workspace)
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            name = str(data.get("name") or "").strip()
            if name:
                return name
        except (OSError, ValueError):
            pass
    return Path(workspace).name


def product_stem(workspace: Path | str, input_name: str | None, count: int) -> str:
    name = workspace_name(Path(workspace)) if isinstance(workspace, Path) else str(workspace)
    if count <= 1 or not input_name:
        return name
    return f"{name}_{Path(input_name).stem}"


def subproject_name(workspace: Path | str, input_name: str | None, count: int) -> str:
    return f"SubProject_{product_stem(workspace, input_name, count)}"


def newest_names(folder: Path, *, suffix: str | None = None, prefix: str = "") -> list[str]:
    if not folder.is_dir():
        return []
    files = [
        p for p in folder.iterdir()
        if p.is_file()
        and (not prefix or p.name.upper().startswith(prefix.upper()))
        and (suffix is None or p.suffix.lower() == suffix.lower())
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files]
