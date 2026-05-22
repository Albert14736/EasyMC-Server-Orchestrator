#!/usr/bin/env python3
"""
One-shot审计：把 MOD_DATABASE.md 里每个 mod 跑一遍 Modrinth API，
用 core.mod_scanner 的分类规则判断它实际是否服务端可用，
对比你手填的 platform 列，找出像 EntityCulling 那种"标着 Universal 但其实是客户端模组"的错条目。

用法:
    python audit_mod_database.py
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.mod_scanner import ModInfo, classify_mod  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MOD_DATABASE.md")
API = "https://api.modrinth.com/v2/project"
UA = "HMSL/0.1 mod-database-audit"


@dataclass
class DbRow:
    name: str
    modrinth_id: str
    platform: str  # 用户手填："Universal" / "Fabric" / "Paper" / ...


def parse_db(path: str) -> List[DbRow]:
    rows: List[DbRow] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            if "|" not in raw or raw.lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 4 or not parts[1] or not parts[2]:
                continue
            # Skip both the column header and the markdown separator row.
            if parts[2] == "Modrinth ID" or parts[2].startswith(":---"):
                continue
            rows.append(DbRow(name=parts[1], modrinth_id=parts[2], platform=parts[3]))
    return rows


def fetch_project(project_id: str, timeout: float = 10.0) -> Optional[ModInfo]:
    try:
        r = requests.get(f"{API}/{project_id}",
                         headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return None
        d = r.json()
        return ModInfo(
            project_id=d.get("id", project_id),
            project_title=d.get("title", project_id),
            client_side=d.get("client_side", "unknown"),
            server_side=d.get("server_side", "unknown"),
        )
    except (requests.RequestException, ValueError):
        return None


def main() -> int:
    rows = parse_db(DB_PATH)
    print(f"\n📖 从 MOD_DATABASE.md 解析出 {len(rows)} 个模组，开始审计...\n")

    client_only, unknown, ok = [], [], []
    width = max(len(r.name) for r in rows) + 2

    for r in rows:
        info = fetch_project(r.modrinth_id)
        status = classify_mod(info)
        side = f"(client={info.client_side}, server={info.server_side})" if info else "(未找到)"
        if status == "client_only":
            icon, bucket = "🚫", client_only
        elif status == "unknown":
            icon, bucket = "❓", unknown
        else:
            icon, bucket = "✅", ok
        bucket.append(r)
        print(f"{icon} {r.name:<{width}} {r.modrinth_id:<12} {r.platform:<10} {side}")

    print(f"\n--- 汇总 ---")
    print(f"  ✅ 服务端可用 : {len(ok)} / {len(rows)}")
    print(f"  🚫 客户端专属 : {len(client_only)} / {len(rows)}  ← 应从数据库剔除")
    print(f"  ❓ 未在 Modrinth 找到 : {len(unknown)} / {len(rows)}")

    if client_only:
        print(f"\n🚨 标着是服务端 mod 但实际仅客户端的条目（建议处理）：")
        for r in client_only:
            print(f"   - {r.name} ({r.modrinth_id})  当前标签: {r.platform}")
    if unknown:
        print(f"\n⚠️  Modrinth 找不到的条目（ID 可能错了，或者只在 CurseForge）：")
        for r in unknown:
            print(f"   - {r.name} ({r.modrinth_id})")

    return 0 if not client_only else 1


if __name__ == "__main__":
    sys.exit(main())
