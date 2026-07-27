from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.context import build_default_context
from ozon_v2.domain.models import utc_now_iso
from ozon_v2.services.credential_service import CredentialService


def main() -> int:
    parser = argparse.ArgumentParser(description="Ozon V2 credential assistant popup.")
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args()

    repo = FsRepo(build_default_context(runtime_root=Path(args.runtime_root)))
    repo.initialize_runtime()
    _write_window_state(repo, "open")

    root = tk.Tk()
    root.title("Ozon V2 凭证助手 (Credential Assistant)")
    root.geometry("560x300")
    root.minsize(520, 280)
    root.resizable(False, False)

    frame = tk.Frame(root, padx=24, pady=20)
    frame.pack(fill="both", expand=True)

    title = tk.Label(frame, text="Ozon V2 凭证助手 (Credential Assistant)", font=("Microsoft YaHei UI", 14, "bold"))
    title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

    note = tk.Label(
        frame,
        text="用于先拉取店铺已有商品并去重。API Key 只保存到本机运行数据目录，不写入日志。",
        font=("Microsoft YaHei UI", 9),
        wraplength=500,
        justify="left",
    )
    note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

    tk.Label(frame, text="店铺 ID (Store ID / Client-Id)", font=("Microsoft YaHei UI", 10)).grid(row=2, column=0, sticky="w", pady=6)
    client_id_var = tk.StringVar()
    client_id_entry = tk.Entry(frame, textvariable=client_id_var, width=42)
    client_id_entry.grid(row=2, column=1, sticky="ew", pady=6)

    tk.Label(frame, text="API 密钥 (API Key)", font=("Microsoft YaHei UI", 10)).grid(row=3, column=0, sticky="w", pady=6)
    api_key_var = tk.StringVar()
    api_key_entry = tk.Entry(frame, textvariable=api_key_var, width=42, show="*")
    api_key_entry.grid(row=3, column=1, sticky="ew", pady=6)

    path_label = tk.Label(
        frame,
        text=f"保存位置 (Save Path): {repo.credentials_path}",
        font=("Microsoft YaHei UI", 8),
        wraplength=500,
        justify="left",
        fg="#555555",
    )
    path_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 12))

    buttons = tk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))

    def save() -> None:
        result = CredentialService(repo).save_credentials(client_id_var.get(), api_key_var.get())
        if not result.ok:
            messagebox.showerror(
                "信息不完整 (Missing Information)",
                "\n".join(result.errors) or result.message,
                parent=root,
            )
            return
        _write_window_state(repo, "saved")
        messagebox.showinfo(
            "已保存 (Saved)",
            "店铺凭证已保存到运行数据目录。\nAPI Key 不会写入 Obsidian 或项目代码。",
            parent=root,
        )
        root.destroy()

    def close_later() -> None:
        _write_window_state(repo, "closed_later")
        root.destroy()

    save_button = tk.Button(buttons, text="保存 (Save)", command=save, width=16)
    save_button.pack(side="right", padx=(8, 0))
    later_button = tk.Button(buttons, text="稍后 (Later)", command=close_later, width=14)
    later_button.pack(side="right")

    frame.columnconfigure(1, weight=1)
    root.protocol("WM_DELETE_WINDOW", close_later)
    client_id_entry.focus_set()
    root.mainloop()
    return 0


def _write_window_state(repo: FsRepo, state: str) -> None:
    payload = {
        "state": state,
        "pid": os.getpid(),
        "opened_at": _existing_opened_at(repo) or utc_now_iso(),
        "updated_at": utc_now_iso(),
        "credentials_path": str(repo.credentials_path),
        "template_path": str(repo.credentials_template_path),
    }
    repo.credential_assistant_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _existing_opened_at(repo: FsRepo) -> str | None:
    path = repo.credential_assistant_state_path
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    opened_at = payload.get("opened_at")
    return opened_at if isinstance(opened_at, str) else None


if __name__ == "__main__":
    raise SystemExit(main())
