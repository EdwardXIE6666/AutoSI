"""本机 ADS 运行时：功能3 用目标机已装的 keysight，不吃 exe 里打进去的那份。

打包后 `keysight.ads.ael._ael` 是原生扩展，必须和本机 ADS 的 DLL / HPEESOF_DIR
对上。打进 onefile 的那份来自编译机 ADS 版本，仿真机一对不上就是 ImportError。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_PACKAGES_TAIL = ("tools", "python", "packages")
_ADS_VER = re.compile(r"^(.*)_v(\d+)$", re.I)


def ads_workspace_base_name(name: str) -> str:
    text = str(name or "").strip()
    match = _ADS_VER.fullmatch(text)
    return match.group(1) if match else text


def ads_workspace_occupied(path: Path) -> bool:
    """目录里已有内容（用户可能改过），不能删。空目录不算。"""
    path = Path(path)
    if not path.exists():
        return False
    if path.is_file():
        return True
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def next_ads_workspace_path(requested) -> Path:
    """已有工程不覆盖：xxx_ADS 被占用则写 xxx_ADS_v2、_v3…"""
    req = Path(requested)
    parent = req.parent
    stem = ads_workspace_base_name(req.name) or req.name
    cand = parent / stem
    if not ads_workspace_occupied(cand):
        return cand
    n = 2
    while n <= 9999:
        cand = parent / f"{stem}_v{n}"
        if not ads_workspace_occupied(cand):
            return cand
        n += 1
    raise RuntimeError("ADS 工程版本号过多")


def latest_ads_workspace_path(requested) -> Path:
    """打开产物：同名工程里取最高版本。"""
    req = Path(requested)
    parent = req.parent
    stem = ads_workspace_base_name(req.name) or req.name
    found = []
    base = parent / stem
    if ads_workspace_occupied(base):
        found.append((1, base))
    if parent.is_dir():
        for child in parent.iterdir():
            match = _ADS_VER.fullmatch(child.name)
            if (
                match
                and match.group(1) == stem
                and ads_workspace_occupied(child)
            ):
                found.append((int(match.group(2)), child))
    if not found:
        return base
    return max(found, key=lambda item: item[0])[1]


def create_subproject(project_path):
    """原理图路径 → 工程名/目录。不碰 ADS，功能1/5 也能用。"""
    path = Path(project_path)
    project_name = path.stem
    project_dir = str(path.parent)
    subproject_name = f"SubProject_{project_name}"
    subproject_dir = str(path.parent / subproject_name)
    os.makedirs(str(path.parent / "pic"), exist_ok=True)
    return {
        "project_name": project_name,
        "project_dir": project_dir,
        "subproject_dir": subproject_dir,
        "subproject_name": subproject_name,
    }


def _is_ads_root(path: Path) -> bool:
    return path.is_dir() and (path / "bin" / "ads.exe").is_file()


def find_hpeesof_dir() -> Path | None:
    env = os.environ.get("HPEESOF_DIR", "").strip()
    if env:
        p = Path(env)
        if _is_ads_root(p):
            return p
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for sub in (
            r"SOFTWARE\Keysight\ADS",
            r"SOFTWARE\WOW6432Node\Keysight\ADS",
            r"SOFTWARE\Agilent\ADS",
            r"SOFTWARE\WOW6432Node\Agilent\ADS",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as key:
                    n_sub, _vals, _ = winreg.QueryInfoKey(key)
                    for i in range(n_sub):
                        name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, name) as ver:
                            try:
                                inst, _typ = winreg.QueryValueEx(ver, "InstallDir")
                            except OSError:
                                continue
                            p = Path(str(inst))
                            if _is_ads_root(p):
                                return p
            except OSError:
                continue
    for root in (
        Path(r"C:\Program Files\Keysight"),
        Path(r"C:\Program Files (x86)\Keysight"),
        Path(r"C:\Keysight"),
        Path(r"D:\Program Files\Keysight"),
        Path(r"D:\Keysight"),
        Path(r"E:\Program Files\Keysight"),
        Path(r"E:\Keysight"),
    ):
        if not root.is_dir():
            continue
        hits = sorted(
            (p for p in root.glob("ADS*") if _is_ads_root(p)),
            key=lambda p: p.name,
            reverse=True,
        )
        if hits:
            return hits[0]
    return None


def ensure_ads_home() -> None:
    """vtb.defs 第一行是 INCLUDE $HOME/hpeesof/config/SystemVue/lib.defs。

    Windows 上 HOME 经常没设，ADS 会把它收成 /hpeesof/...，报 3509，后面的元件库也不继续读。
    """
    if os.environ.get("HOME", "").strip():
        return
    profile = os.environ.get("USERPROFILE", "").strip()
    if not profile:
        profile = (
            os.environ.get("HOMEDRIVE", "") + os.environ.get("HOMEPATH", "")
        ).strip()
    if profile:
        os.environ["HOME"] = profile


def prepare_ads_python() -> Path:
    """设定 HPEESOF_DIR / PATH / DLL 目录，并把本机 keysight 放到 sys.path 最前。"""
    ensure_ads_home()
    ads_root = find_hpeesof_dir()
    if ads_root is None:
        raise RuntimeError(
            "未找到 Keysight ADS（HPEESOF_DIR / 安装目录）。"
            "功能3 需要本机安装 ADS。请先运行「配置环境」。"
        )
    os.environ["HPEESOF_DIR"] = str(ads_root)
    if not os.environ.get("SIMARCH"):
        os.environ["SIMARCH"] = "win32_64"
    ads_bin = ads_root / "bin"
    if ads_bin.is_dir():
        os.environ["PATH"] = str(ads_bin) + os.pathsep + os.environ.get("PATH", "")
        add_dll = getattr(os, "add_dll_directory", None)
        if add_dll is not None:
            try:
                add_dll(str(ads_bin))
            except OSError:
                pass
    packages = ads_root.joinpath(*_PACKAGES_TAIL)
    if not packages.is_dir():
        raise RuntimeError(
            f"ADS 安装不完整：找不到 Python 包目录 {packages}"
        )
    loc = str(packages)
    if loc in sys.path:
        sys.path.remove(loc)
    sys.path.insert(0, loc)
    return ads_root


_WIN_FILE_IN_USE = {5, 32, 1224}  # 拒绝访问 / 正被占用 / 内存映射中


def _same_file_bytes(src: Path, dest: Path) -> bool:
    try:
        return (
            dest.is_file()
            and src.stat().st_size == dest.stat().st_size
            and src.read_bytes() == dest.read_bytes()
        )
    except OSError:
        return False


def _copy_replace(src: Path, dest: Path) -> None:
    """覆盖 dest。ADS 正打开/映射该文件时 WinError 1224，不能硬拷。"""
    if src.resolve() == dest.resolve():
        return
    if _same_file_bytes(src, dest):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(str(tmp), str(dest))
        return
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        win = getattr(exc, "winerror", None)
        if win in _WIN_FILE_IN_USE:
            if _same_file_bytes(src, dest):
                return
            print(
                "ADS 正在占用 fix_snp.ael，这次没能更新宏。"
                "请先关掉 ADS 再跑一次功能3。"
            )
            return
        raise


def install_fix_snp_ael(runtime_dir, dest, main_file) -> str | None:
    """把与当前程序配套的 fix_snp.ael 写到 ADS 要 load 的位置。

    打包后只换 MAIN.exe、不换 _internal 宏时，网标会按旧脚号贴，和 symbol port 反掉。
    优先用与 MAIN.py 同目录打进包的那份。
    """
    runtime = Path(runtime_dir)
    main_dir = Path(main_file).resolve().parent
    dest_path = Path(dest)
    candidates = (
        main_dir / "fix_snp.ael",
        runtime / "fix_snp.ael",
        runtime / "_internal" / "fix_snp.ael",
    )
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        return None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    _copy_replace(src, dest_path)
    stale_atf = dest_path.with_suffix(".atf")
    if stale_atf.is_file():
        try:
            stale_atf.unlink()
        except OSError:
            pass
    return str(dest_path)
