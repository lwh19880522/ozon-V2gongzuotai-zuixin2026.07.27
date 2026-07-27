from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil


PACKAGE_VERSION = "seed_pool.refined.5000.v1"
SAFE_SOURCE_NAME = "Ozon_5000精细子类目种子池.txt"
FIELD_LABELS = {
    "种子编号（Seed ID）": "source_seed_id",
    "精准中文品名（Chinese Product Name）": "title",
    "产品识别特征（Product Clue）": "product_clue",
    "类目提示（Category Hint）": "category_hint",
    "主俄语查询词（Primary Russian Query）": "primary_query",
    "备用俄语查询词（Alternative Russian Query）": "alternative_query",
    "必须包含词（Required Terms）": "required_terms",
    "排除词（Negative Terms）": "negative_terms",
    "审核状态（Review Status）": "review_status",
}
RECORD_HEADER_RE = re.compile(r"^【第\s+(\d+)\s+条\s+/\s+共\s+(\d+)\s+条】$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a reviewed UTF-8 Ozon seed package without persisting its local source path."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument(
        "--asset-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets" / "seed_pool",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_records(text: str) -> tuple[int, list[dict[str, str]]]:
    expected_total: int | None = None
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"=", "-"}:
            continue
        header = RECORD_HEADER_RE.match(line)
        if header:
            if current is not None:
                records.append(current)
            sequence = int(header.group(1))
            total = int(header.group(2))
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise ValueError(f"Inconsistent record total: {total} != {expected_total}")
            current = {"sequence": str(sequence)}
            continue
        if current is None or "：" not in line:
            continue
        label, value = line.split("：", 1)
        field_name = FIELD_LABELS.get(label.strip())
        if field_name:
            if field_name in current:
                raise ValueError(f"Duplicate field {label!r} in record {current['sequence']}")
            current[field_name] = value.strip()

    if current is not None:
        records.append(current)
    if expected_total is None:
        raise ValueError("No seed record headers were found.")
    return expected_total, records


def validate_records(expected_total: int, records: list[dict[str, str]]) -> None:
    required_fields = {"sequence", *FIELD_LABELS.values()}
    if expected_total != 5000 or len(records) != expected_total:
        raise ValueError(f"Expected 5000 records, found {len(records)} of declared {expected_total}.")

    missing: list[str] = []
    for index, record in enumerate(records, start=1):
        absent = sorted(field for field in required_fields if not record.get(field))
        if absent:
            missing.append(f"{index}: {', '.join(absent)}")
        if int(record["sequence"]) != index:
            raise ValueError(f"Record sequence is not contiguous at {index}: {record['sequence']}")
    if missing:
        raise ValueError("Missing fields: " + "; ".join(missing[:10]))

    unique_checks = {
        "source seed id": [record["source_seed_id"].casefold() for record in records],
        "Chinese title": [record["title"].casefold() for record in records],
        "primary Russian query": [record["primary_query"].casefold() for record in records],
    }
    for label, values in unique_checks.items():
        if len(set(values)) != len(values):
            raise ValueError(f"Duplicate {label} values found.")


def make_seed(record: dict[str, str]) -> dict[str, object]:
    sequence = int(record["sequence"])
    negative_terms = [
        term.strip()
        for term in re.split(r"[；;]", record["negative_terms"])
        if term.strip()
    ]
    notes = {
        "source_seed_id": record["source_seed_id"],
        "source_product_clue": record["product_clue"],
        "required_terms": record["required_terms"],
        "negative_terms": negative_terms,
        "review_status": record["review_status"],
    }
    return {
        "seed_id": f"seed-5000-{sequence:04d}",
        "title_or_keyword": record["title"],
        "source_language": "zh-CN",
        "product_clue": record["product_clue"],
        "category_hint": record["category_hint"],
        "ozon_query_terms_ru": [
            record["primary_query"],
            record["alternative_query"],
        ],
        "auxiliary_query_terms_en": [],
        "ozon_query_language": "ru-RU",
        "query_generation_status": "generated",
        "query_generation_method": "imported_refined_seed_pool",
        "query_generation_confidence": "high",
        "image_reference_when_available": None,
        "notes": json.dumps(notes, ensure_ascii=False, separators=(",", ":")),
    }


def write_package(source: Path, asset_dir: Path) -> None:
    source = source.resolve(strict=True)
    asset_dir.mkdir(parents=True, exist_ok=True)
    source_text = source.read_text(encoding="utf-8-sig")
    expected_total, records = parse_records(source_text)
    validate_records(expected_total, records)

    imported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_sha256 = sha256_file(source)
    seeds = [make_seed(record) for record in records]
    package = {
        "schema_version": 1,
        "package_version": PACKAGE_VERSION,
        "source_file": SAFE_SOURCE_NAME,
        "seed_count": len(seeds),
        "source_sha256": source_sha256,
        "imported_at": imported_at,
        "seeds": seeds,
    }
    review_counts = Counter(record["review_status"] for record in records)
    manifest = {
        "package_version": PACKAGE_VERSION,
        "original_filename": SAFE_SOURCE_NAME,
        "normalized_filename": "seed_pool.initial.json",
        "seed_count": len(seeds),
        "unique_seed_count": len({seed["seed_id"] for seed in seeds}),
        "source_language": "zh-CN",
        "ozon_query_language": "ru-RU",
        "query_policy": "Use reviewed primary Russian query first; alternative query is the bounded fallback.",
        "review_status_counts": dict(sorted(review_counts.items())),
        "source_sha256": source_sha256,
        "imported_at": imported_at,
        "source_provenance": "user_supplied_local_file_path_not_persisted",
        "bundled_asset_path": "assets/seed_pool",
        "runtime_policy": (
            "Replace the runtime active pool when package_version changes while preserving "
            "used and blacklist ledgers."
        ),
    }

    shutil.copyfile(source, asset_dir / SAFE_SOURCE_NAME)
    (asset_dir / "seed_pool.initial.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (asset_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "package_version": PACKAGE_VERSION,
                "seed_count": len(seeds),
                "source_sha256": source_sha256,
                "asset_dir": str(asset_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    args = parse_args()
    write_package(args.source, args.asset_dir)


if __name__ == "__main__":
    main()
