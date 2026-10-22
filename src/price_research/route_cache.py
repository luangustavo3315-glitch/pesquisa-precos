from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Product

ROUTE_CACHE_SCHEMA = 1
ROUTE_CACHE_FILENAME = "product_route_cache.json"
_VALID_SOURCE_STATUSES = {"EXATO", "SUBSTITUTO ACEITO"}


def product_key(product: Product) -> str:
    return str(product.codigo_interno or product.search_name).strip()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ProductRouteCache:
    """Persistent cache of validated product routes.

    Only navigation/identity metadata is stored. Prices are deliberately never
    cached: every run must reopen the product page and read the current price.
    """

    def __init__(self, path: str | Path, *, bootstrap_output_dir: str | Path | None = None):
        self.path = Path(path)
        self.payload: dict[str, Any] = {"schema": ROUTE_CACHE_SCHEMA, "routes": {}}
        self._load()
        if bootstrap_output_dir is not None:
            self.bootstrap_from_latest_complete_run(Path(bootstrap_output_dir))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("schema") != ROUTE_CACHE_SCHEMA or not isinstance(payload.get("routes"), dict):
            return
        self.payload = payload

    def save(self) -> None:
        _write_json_atomic(self.path, self.payload)

    def get(self, product: Product, store_key: str) -> dict[str, Any] | None:
        store = self.payload.get("routes", {}).get(store_key, {})
        route = store.get(product_key(product))
        return dict(route) if isinstance(route, dict) else None

    def record_success(
        self,
        product: Product,
        store_key: str,
        *,
        url: str,
        title: str,
        source_match_status: str,
        confidence: float,
        verified_at: str,
    ) -> None:
        if not url or source_match_status not in _VALID_SOURCE_STATUSES:
            return
        routes = self.payload.setdefault("routes", {}).setdefault(store_key, {})
        key = product_key(product)
        previous = routes.get(key) if isinstance(routes.get(key), dict) else {}
        # A validated exact route is more valuable than an accepted substitute.
        # A later fallback must never erase the known exact URL merely because
        # the exact page had a transient price/read issue in that run. True
        # identity changes are handled by ``invalidate`` before a replacement is
        # recorded.
        if previous.get("source_match_status") == "EXATO" and source_match_status == "SUBSTITUTO ACEITO":
            previous["last_fallback_url"] = str(url)
            previous["last_fallback_title"] = str(title or "")
            previous["last_fallback_verified_at"] = str(verified_at or "")
            routes[key] = previous
            self.save()
            return
        routes[key] = {
            "url": str(url),
            "title": str(title or ""),
            "source_match_status": str(source_match_status),
            "confidence": round(float(confidence or 0.0), 3),
            "last_verified_at": str(verified_at or ""),
            "successes": int(previous.get("successes", 0)) + 1,
            "failures": 0,
            "bootstrapped": bool(previous.get("bootstrapped", False)),
        }
        self.save()

    def record_failure(self, product: Product, store_key: str, *, reason: str = "") -> int:
        routes = self.payload.setdefault("routes", {}).setdefault(store_key, {})
        key = product_key(product)
        route = routes.get(key)
        if not isinstance(route, dict):
            return 0
        route["failures"] = int(route.get("failures", 0)) + 1
        route["last_failure_reason"] = str(reason or "")[:500]
        route["last_failure_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()
        return int(route["failures"])

    def invalidate(self, product: Product, store_key: str, *, reason: str = "") -> None:
        routes = self.payload.setdefault("routes", {}).setdefault(store_key, {})
        key = product_key(product)
        route = routes.pop(key, None)
        if route is not None:
            invalid = self.payload.setdefault("invalidated", [])
            invalid.append({
                "store_key": store_key,
                "product_key": key,
                "url": route.get("url", "") if isinstance(route, dict) else "",
                "reason": str(reason or "")[:500],
                "invalidated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            if len(invalid) > 200:
                del invalid[:-200]
            self.save()

    def bootstrap_from_latest_complete_run(self, output_dir: Path) -> int:
        """Seed routes from the latest completed JSON, but never trust them blindly.

        Seeded URLs are revalidated on the detailed product page before any
        price can be accepted, so old matching mistakes cannot silently persist.
        """
        if any(self.payload.get("routes", {}).get(store) for store in ("coocerqui", "paguemenos")):
            return 0
        if not output_dir.exists():
            return 0
        candidates = [
            path for path in output_dir.glob("pesquisa_precos_*.json")
            if not path.name.endswith("_telemetry.json") and "benchmark" not in path.name.lower()
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run = payload.get("run") or {}
            if run.get("status") != "complete":
                continue
            records = payload.get("records")
            if not isinstance(records, list):
                continue
            seeded = 0
            for record in records:
                product = record.get("product") or {}
                key = str(product.get("codigo_interno") or product.get("nome_normalizado") or product.get("descricao_original") or "").strip()
                if not key:
                    continue
                for store_key in ("coocerqui", "paguemenos"):
                    result = record.get(store_key) or {}
                    status = str(result.get("source_match_status") or "")
                    url = str(result.get("url") or "")
                    if status not in _VALID_SOURCE_STATUSES or not url:
                        continue
                    store = self.payload.setdefault("routes", {}).setdefault(store_key, {})
                    store[key] = {
                        "url": url,
                        "title": str(result.get("found_name") or ""),
                        "source_match_status": status,
                        "confidence": float(result.get("confidence") or 0.0),
                        "last_verified_at": str(result.get("queried_at") or ""),
                        "successes": 0,
                        "failures": 0,
                        "bootstrapped": True,
                    }
                    seeded += 1
            if seeded:
                self.payload["bootstrapped_from"] = path.name
                self.save()
                return seeded
        return 0
