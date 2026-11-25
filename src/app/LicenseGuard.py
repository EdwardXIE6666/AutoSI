"""AutoSI MAIN.exe 限时授权校验。"""
import base64
import json
import os
from datetime import date, datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app._build_license import BUILD_EXPIRES_AT, BUILD_ID, BUILD_ISSUED_AT
from app._snp_pubkey import PUBLIC_KEY_B64

LICENSE_FILE_NAME = "autosi_license.json"
PRODUCT_NAME = "AutoSI"


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _today():
    return datetime.now(timezone.utc).date()


def _verify_signature(payload, sig_b64):
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
        pub.verify(base64.b64decode(sig_b64), _canonical(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _load_signed_license(base_dir):
    path = os.path.join(base_dir, LICENSE_FILE_NAME)
    if not os.path.isfile(path):
        path = os.path.join(base_dir, "_internal", LICENSE_FILE_NAME)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None, "授权文件无法读取，文件可能已损坏，请联系管理员重新获取"

    sig = data.get("sig", "")
    payload = {k: v for k, v in data.items() if k != "sig"}
    if not _verify_signature(payload, sig):
        return None, "授权文件签名无效，可能被修改，请联系管理员重新获取"
    return payload, None


def _validate_license_payload(payload, today):
    if payload.get("product") != PRODUCT_NAME:
        return None, "授权文件不适用于 AutoSI"

    expires_at = _parse_date(payload.get("expires_at"))
    if expires_at is None:
        return None, "授权文件到期日期格式无效，请联系管理员重新获取"
    if today > expires_at:
        return None, f"授权文件已于 {expires_at.isoformat()} 到期，请联系管理员更新授权"

    min_build_date = payload.get("min_build_date")
    if min_build_date:
        min_build = _parse_date(min_build_date)
        build_date = _parse_date(BUILD_ISSUED_AT)
        if min_build is None or build_date is None:
            return None, "授权文件构建日期限制无效，请联系管理员重新获取"
        if build_date < min_build:
            return None, "当前 MAIN.exe 版本过旧，请联系管理员获取新版程序"

    license_build_id = payload.get("build_id")
    if license_build_id and license_build_id != BUILD_ID:
        return None, "授权文件不适用于当前 MAIN.exe，请联系管理员重新获取"

    return expires_at, None


def verify_runtime_license(base_dir):
    """返回 (ok, message)。ok=False 时 message 可直接展示给用户。"""
    today = _today()
    build_expires = _parse_date(BUILD_EXPIRES_AT)
    if build_expires is None:
        return False, "程序内置授权信息无效，请联系管理员重新打包"

    if today <= build_expires:
        return True, f"内置授权有效期至 {build_expires.isoformat()}"

    payload, error = _load_signed_license(base_dir)
    if error:
        return False, error
    if payload is None:
        return False, (
            f"MAIN.exe 已于 {build_expires.isoformat()} 到期。\n"
            f"请联系管理员获取新的 {LICENSE_FILE_NAME} 授权文件，并放到程序目录后重新运行。"
        )

    expires_at, error = _validate_license_payload(payload, today)
    if error:
        return False, error
    return True, f"授权文件有效期至 {expires_at.isoformat()}"
