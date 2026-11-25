"""S参数库完整性封装/校验（Ed25519 非对称签名）。

设计目标（方案 A）：
  - 库保持明文文件，兼容现有 os.walk 读取逻辑
  - 开发者用私钥封库（build_manifest_file），生成签名清单 .snp_manifest.json
  - 用户侧用编译进 exe 的公钥校验（verify_library），检出改动/新增/删除 → 拒绝运行
  - 本地校验缓存：首次全量哈希，之后凭 大小+修改时间 命中缓存，仅重哈希变动文件

安全模型：
  - 清单签名：Ed25519。私钥只在开发者手上，公钥编进程序。用户即便逆向 exe 拿到公钥
    也无法伪造合法签名，无法把改过的库冒充官方库。
  - 本地缓存签名：HMAC（密钥嵌入程序）。缓存只是本地加速器，用于挡意外损坏/朴素伪造；
    其密钥可被逆向提取，属已知取舍——不影响"清单不可伪造"这一核心保证。
"""
import os
import json
import hmac
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.exceptions import InvalidSignature

from app._snp_pubkey import PUBLIC_KEY_B64

# 本地缓存 HMAC 密钥（仅保护本地加速缓存，非核心信任锚）
_CACHE_KEY = b"AutoSI-SNP-LocalCache-Key-2026-v1"

MANIFEST_NAME = ".snp_manifest.json"
CACHE_NAME = ".snp_verify_cache.json"

_HASH_WORKERS = 8


class LibraryFileUnreadable(OSError):
    """库文件读不了：封库必须停，不能静默跳过。"""

    def __init__(self, errors):
        self.errors = list(errors)
        lines = ["无法读取库文件，封库中止："]
        for rel, err in self.errors:
            lines.append(f"  - {rel}")
            if err:
                lines.append(f"    {err}")
        lines.append("请先确认这些文件能打开（解密或解除占用）后再封。")
        super().__init__("\n".join(lines))


def _extended_win_path(path):
    ap = os.path.abspath(path)
    if ap.startswith("\\\\?\\"):
        return ap
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def _hash_stream(path):
    m = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            m.update(chunk)
    return m.hexdigest()


def _hash_via_win32(path):
    """CRT open 报 Invalid argument 时，用 CreateFile 再读一遍。"""
    if os.name != "nt":
        raise OSError(22, "Invalid argument", path)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    share_all = 0x00000007
    open_existing = 3
    sequential = 0x08000000
    invalid = wintypes.HANDLE(-1).value
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(
        _extended_win_path(path), generic_read, share_all,
        None, open_existing, sequential, None,
    )
    if handle in (None, invalid):
        err = ctypes.get_last_error()
        raise OSError(err or 22, f"CreateFile failed ({err})", path)
    fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
    m = hashlib.sha256()
    with os.fdopen(fd, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            m.update(chunk)
    return m.hexdigest()


def _sha256(path):
    try:
        return _hash_stream(path)
    except OSError as err:
        if getattr(err, "errno", None) != 22:
            raise
    try:
        return _hash_stream(_extended_win_path(path) if os.name == "nt" else path)
    except OSError as err:
        if getattr(err, "errno", None) != 22:
            raise
    return _hash_via_win32(path)


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _cache_sign(entries):
    return hmac.new(_CACHE_KEY, _canonical(entries), hashlib.sha256).hexdigest()


def _is_exe_rel(rel: str) -> bool:
    base = os.path.basename(str(rel or "").replace("\\", "/"))
    return os.path.splitext(base)[1].lower() == ".exe"


def _iter_lib_files(lib_dir):
    """库内受检文件 [(相对路径posix, 绝对路径)]。清单/缓存/.exe 不进封库检测。"""
    out = []
    for root, _, files in os.walk(lib_dir):
        for f in files:
            if f in (MANIFEST_NAME, CACHE_NAME):
                continue
            if _is_exe_rel(f):
                continue
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, lib_dir).replace(os.sep, "/")
            out.append((rel, ap))
    return out


def list_skipped_exes(lib_dir):
    """库里的 .exe 相对路径，不进清单、不参与校验。"""
    out = []
    for root, _, files in os.walk(lib_dir):
        for f in files:
            if not _is_exe_rel(f):
                continue
            ap = os.path.join(root, f)
            out.append(os.path.relpath(ap, lib_dir).replace(os.sep, "/"))
    return out


def _hash_one(item):
    rel, ap = item
    try:
        return rel, _sha256(ap), None
    except OSError as err:
        return rel, None, err


def compute_manifest(lib_dir):
    """全量哈希受检库文件（不含 .exe），返回 {相对路径: sha256}。"""
    files = _iter_lib_files(lib_dir)
    result = {}
    errors = []
    with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as ex:
        for rel, h, err in ex.map(_hash_one, files):
            if err is not None:
                errors.append((rel, err))
            else:
                result[rel] = h
    if errors:
        raise LibraryFileUnreadable(errors)
    return result


def build_manifest_file(lib_dir, private_key_path):
    """开发者封库：计算清单 → 私钥 Ed25519 签名 → 写入 lib_dir/.snp_manifest.json。

    返回文件数。private_key_path 指向开发者私钥 PEM。
    """
    with open(private_key_path, "rb") as f:
        priv = serialization_load_private(f.read())

    files = compute_manifest(lib_dir)
    sig = priv.sign(_canonical(files))
    manifest = {
        "version": 2,
        "files": files,
        "sig": base64.b64encode(sig).decode("ascii"),
    }
    out = os.path.join(lib_dir, MANIFEST_NAME)
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=0)
    return len(files)


def serialization_load_private(pem_bytes):
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519 类型")
    return key


def _verify_manifest_sig(files, sig_b64):
    """用编译进程序的公钥验证清单签名。"""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
        pub.verify(base64.b64decode(sig_b64), _canonical(files))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _load_cache(cache_path):
    """读取并验签本地缓存。签名无效或读取失败 → 返回空（视为无缓存）。"""
    try:
        with open(cache_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if data.get("sig") == _cache_sign(data.get("entries", {})):
            return data.get("entries", {})
    except (OSError, ValueError):
        pass
    return {}


def _save_cache(cache_path, entries):
    try:
        with open(cache_path, "w", encoding="utf-8") as fp:
            json.dump({"entries": entries, "sig": _cache_sign(entries)},
                      fp, ensure_ascii=False)
    except OSError:
        pass  # 缓存写不进去只影响下次速度，不影响校验正确性


def verify_library(lib_dir, cache_path):
    """校验 S 参数库完整性。

    返回 (ok: bool, problems: list[str])。
    缓存命中（大小+修改时间未变）则信任缓存哈希，否则重新哈希。
    """
    manifest_path = os.path.join(lib_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return False, ["S参数库未封装（缺少完整性清单），请联系开发者获取官方库"]

    try:
        with open(manifest_path, "r", encoding="utf-8") as fp:
            manifest = json.load(fp)
    except (OSError, ValueError):
        return False, ["S参数库完整性清单无法读取，文件可能已损坏，请联系开发者"]

    expected = manifest.get("files", {})
    if not _verify_manifest_sig(expected, manifest.get("sig", "")):
        return False, ["S参数库完整性清单签名无效：封库私钥和程序公钥不是一对，或清单已被改过。请用当前程序对应的私钥重新封库"]

    # 签名仍对整份清单验；比对磁盘时不检 .exe（含旧清单里残留的 exe 条目）。
    expected_check = {rel: digest for rel, digest in expected.items() if not _is_exe_rel(rel)}
    actual = _iter_lib_files(lib_dir)
    actual_map = {rel: ap for rel, ap in actual}

    problems = []
    missing = sorted(set(expected_check) - set(actual_map))
    extra = sorted(set(actual_map) - set(expected_check))
    for rel in missing:
        problems.append(f"文件缺失: {rel}")
    for rel in extra:
        problems.append(f"多出未授权文件: {rel}")

    cache = _load_cache(cache_path)
    new_cache = {}
    to_hash = []  # (rel, ap, sig)

    for rel in expected_check:
        ap = actual_map.get(rel)
        if ap is None:
            continue  # 已计入 missing
        try:
            st = os.stat(ap)
        except OSError:
            problems.append(f"文件无法访问: {rel}")
            continue
        sig = [st.st_size, st.st_mtime_ns]
        c = cache.get(rel)
        if c and c[0] == sig[0] and c[1] == sig[1]:
            new_cache[rel] = c  # 命中缓存，信任哈希
        else:
            to_hash.append((rel, ap, sig))

    if to_hash:
        with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as ex:
            hashed = ex.map(lambda t: _hash_one((t[0], t[1])), to_hash)
            for (rel, _ap, sig), (_rel, h, err) in zip(to_hash, hashed):
                if err is not None:
                    problems.append(f"文件无法访问: {rel}")
                    continue
                new_cache[rel] = [sig[0], sig[1], h]

    for rel in expected_check:
        c = new_cache.get(rel)
        if c is not None and c[2] != expected_check[rel]:
            problems.append(f"文件内容被修改: {rel}")

    if problems:
        return False, problems

    _save_cache(cache_path, new_cache)
    return True, []
