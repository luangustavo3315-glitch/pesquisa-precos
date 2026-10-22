from __future__ import annotations

import hashlib
import json
import os
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from .models import Product, StoreResult

CHECKPOINT_SCHEMA = 2
CHECKPOINT_FILENAME = "checkpoint_pesquisa_precos.json"
RUN_MANIFEST_SCHEMA = 1
MATCHING_POLICY_VERSION = "detail-page-v6-semantic-domain-family"


def product_history_key(product: Product) -> str:
    return str(product.codigo_interno or product.search_name).strip()


def product_fingerprint(products: list[Product]) -> str:
    identity = [
        {
            "key": product_history_key(product),
            "name": product.search_name,
            "unit": product.unidade_preco,
            "brand": product.marca,
            "required": product.termos_obrigatorios,
            "substitutable": product.termos_substituiveis,
            "excluded": product.termos_excluidos,
            "aliases": product.aliases,
            "aliases_coocerqui": product.aliases_coocerqui,
            "aliases_paguemenos": product.aliases_paguemenos,
        }
        for product in products
    ]
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: str | Path) -> str:
    path = Path(path)
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_hash(source_dir: str | Path) -> str:
    source_dir = Path(source_dir)
    digest = hashlib.sha256()
    for path in sorted(source_dir.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(_sha256_file(path).encode("ascii"))
    collectors = source_dir / "collectors"
    if collectors.exists():
        for path in sorted(collectors.glob("*.py")):
            digest.update(f"collectors/{path.name}".encode("utf-8"))
            digest.update(_sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def build_run_manifest(
    products: list[Product],
    *,
    input_path: str | Path,
    config_path: str | Path,
    code_version: str,
    source_dir: str | Path,
    matching_policy_version: str = MATCHING_POLICY_VERSION,
) -> dict:
    input_path = Path(input_path)
    config_path = Path(config_path)
    rules_path = input_path.with_name(f"{input_path.stem}_regras.json")
    aliases_path = input_path.with_name(f"{input_path.stem}_aliases.json")
    return {
        "schema": RUN_MANIFEST_SCHEMA,
        "code_version": str(code_version),
        "matching_policy_version": str(matching_policy_version),
        "product_fingerprint": product_fingerprint(products),
        "files": {
            "input": _sha256_file(input_path),
            "config": _sha256_file(config_path),
            "rules": _sha256_file(rules_path),
            "aliases": _sha256_file(aliases_path),
            "source_tree": _source_tree_hash(source_dir),
        },
    }


def run_fingerprint(manifest: dict) -> str:
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def checkpoint_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / CHECKPOINT_FILENAME


def write_json_atomic(path: str | Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _serialize_records(records) -> list[dict]:
    return [
        {
            "product_key": product_history_key(product),
            "coocerqui": coocerqui.as_dict(),
            "paguemenos": paguemenos.as_dict(),
        }
        for product, coocerqui, paguemenos in records
    ]


def save_checkpoint(
    path: str | Path,
    products: list[Product],
    records,
    *,
    status: str,
    telemetry: dict | None = None,
    run_fingerprint_value: str = "",
    run_manifest: dict | None = None,
) -> Path:
    if status not in {"in_progress", "complete"}:
        raise ValueError(f"status de checkpoint inválido: {status}")
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "product_fingerprint": product_fingerprint(products),
        "run_fingerprint": run_fingerprint_value,
        "run_manifest": run_manifest or {},
        "total_products": len(products),
        "completed_products": len(records),
        "records": _serialize_records(records),
        "telemetry": telemetry or {},
    }
    return write_json_atomic(path, payload)


def _store_result(payload: dict) -> StoreResult:
    allowed = {field.name for field in fields(StoreResult)}
    values = {key: value for key, value in payload.items() if key in allowed}
    required = {"store_key", "store_name", "queried_at"}
    if not required.issubset(values):
        missing = sorted(required - set(values))
        raise ValueError(f"checkpoint sem campos obrigatórios: {missing}")
    return StoreResult(**values)


def load_checkpoint(
    path: str | Path,
    products: list[Product],
    *,
    expected_run_fingerprint: str = "",
):
    path = Path(path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != CHECKPOINT_SCHEMA:
            return []
        if payload.get("status") != "in_progress":
            return []
        if payload.get("product_fingerprint") != product_fingerprint(products):
            return []
        if expected_run_fingerprint and payload.get("run_fingerprint") != expected_run_fingerprint:
            return []
        rows = payload.get("records")
        if not isinstance(rows, list) or len(rows) > len(products):
            return []

        restored = []
        for index, row in enumerate(rows):
            product = products[index]
            if row.get("product_key") != product_history_key(product):
                return []
            coocerqui = _store_result(row.get("coocerqui") or {})
            paguemenos = _store_result(row.get("paguemenos") or {})
            if "ERRO DE CONSULTA" in {coocerqui.status, paguemenos.status}:
                break
            restored.append((product, coocerqui, paguemenos))
        return restored
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []
