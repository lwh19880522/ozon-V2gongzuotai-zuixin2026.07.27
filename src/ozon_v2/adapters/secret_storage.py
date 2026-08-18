from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from typing import Any, Mapping


SECRET_ENVELOPE_SCHEMA = 1
_DPAPI_ENTROPY = b"ozon-v2-seller-credentials-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def protect_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if os.name == "nt":
        protected = _crypt_protect(raw)
        protection = "windows_dpapi_current_user"
    else:
        protected = raw
        protection = "local_file_permissions"
    return {
        "schema_version": SECRET_ENVELOPE_SCHEMA,
        "protection": protection,
        "ciphertext_b64": base64.b64encode(protected).decode("ascii"),
    }


def unprotect_mapping(envelope: Mapping[str, Any]) -> dict[str, Any]:
    if int(envelope.get("schema_version") or 0) != SECRET_ENVELOPE_SCHEMA:
        raise ValueError("unsupported credentials envelope schema")
    encoded = str(envelope.get("ciphertext_b64") or "")
    try:
        protected = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("credentials envelope is invalid") from exc
    protection = str(envelope.get("protection") or "")
    if protection == "windows_dpapi_current_user":
        if os.name != "nt":
            raise OSError("Windows DPAPI credentials can only be opened by their Windows user")
        raw = _crypt_unprotect(protected)
    elif protection == "local_file_permissions" and os.name != "nt":
        raw = protected
    else:
        raise ValueError("unsupported credentials protection method")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("credentials payload must be an object")
    return payload


def is_secret_envelope(payload: Mapping[str, Any]) -> bool:
    return "ciphertext_b64" in payload and "protection" in payload


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _crypt_protect(raw: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, input_buffer = _blob(raw)
    entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Ozon V2 Seller API credentials",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
        del input_buffer, entropy_buffer


def _crypt_unprotect(protected: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, input_buffer = _blob(protected)
    entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
        del input_buffer, entropy_buffer
