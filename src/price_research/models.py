from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

VALID_STATUSES = {
    "EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL", "DIVERGENTE", "NÃO ENCONTRADO",
    "INDISPONÍVEL", "ERRO DE CONSULTA"
}

@dataclass
class Product:
    codigo_interno: str = ""
    descricao_original: str = ""
    nome_normalizado: str = ""
    marca: str = ""
    categoria: str = ""
    termos_obrigatorios: list[str] = field(default_factory=list)
    termos_substituiveis: list[str] = field(default_factory=list)
    termos_excluidos: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    aliases_coocerqui: list[str] = field(default_factory=list)
    aliases_paguemenos: list[str] = field(default_factory=list)
    unidade_preco: str = "un"
    observacoes: str = ""

    @property
    def search_name(self) -> str:
        return self.nome_normalizado or self.descricao_original

    def aliases_for_store(self, store_key: str = "") -> list[str]:
        values = list(self.aliases)
        if store_key == "coocerqui":
            values.extend(self.aliases_coocerqui)
        elif store_key == "paguemenos":
            values.extend(self.aliases_paguemenos)
        out: list[str] = []
        for value in values:
            value = " ".join(str(value or "").split()).strip()
            if value and value not in out:
                out.append(value)
        return out

@dataclass
class Candidate:
    title: str
    url: str
    raw_text: str = ""
    price_normal: Optional[float] = None
    price_promo: Optional[float] = None
    price_observed: Optional[float] = None
    # Valores: kg, un, fraction, kg_labeled, ambiguous ou vazio.
    # Só kg/un podem gerar preço no relatório final.
    unit_price: str = ""
    available: Optional[bool] = None
    promo_condition: str = ""
    # Explica como a base do preço foi validada; nunca autoriza cálculo oculto.
    price_normalization_note: str = ""
    # Nome histórico mantido por compatibilidade. Na v1.7.1 significa que a
    # política de base/unidade já foi aplicada, não que houve conversão.
    weighted_price_normalized: bool = False
    # Evidência estruturada: a lógica nunca depende do texto de notes.
    price_basis: str = ""
    price_validation: str = ""
    raw_price_label: str = ""
    price_evidence_schema: int = 0
    price_evidence_value: Optional[float] = None
    price_evidence_source_url: str = ""

@dataclass
class MatchDecision:
    status: str
    confidence: float
    reason: str
    # Features reais usadas pela regra. Mantidas separadas de ``confidence``
    # para permitir calibração dimensionalmente correta.
    features: dict[str, object] = field(default_factory=dict)
    # Somente thresholds efetivamente avaliados nesta decisão.
    thresholds: dict[str, float] = field(default_factory=dict)
    guard_failures: list[str] = field(default_factory=list)

@dataclass
class StoreResult:
    store_key: str
    store_name: str
    queried_at: str
    status: str = "NÃO ENCONTRADO"
    source_match_status: str = ""
    confidence: float = 0.0
    definitive_price: Optional[float] = None
    price_normal: Optional[float] = None
    price_promo: Optional[float] = None
    price_observed: Optional[float] = None
    unit_price: str = "un"
    store_confirmed: bool = False
    found_name: str = ""
    url: str = ""
    promo_condition: str = ""
    availability: str = ""
    raw_text: str = ""
    method: str = "playwright"
    notes: str = ""
    screenshot: str = ""
    price_basis: str = ""
    price_validation: str = ""
    raw_price_label: str = ""
    price_evidence_schema: int = 0
    price_evidence_value: Optional[float] = None
    price_evidence_source_url: str = ""
    price_evidence_captured_at: str = ""
    source_scope: str = ""
    match_features: dict[str, object] = field(default_factory=dict)
    match_thresholds: dict[str, float] = field(default_factory=dict)
    match_guard_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)
