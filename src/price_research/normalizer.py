from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

ABBREVIATIONS = {
    r"\bbisc\b": "biscoito",
    r"\bl\s*(\d+)\s*p\s*(\d+)\s*(ml|g)\b": r"leve \1 \3 pague \2 \3",
    r"\bamac[.\s]*roupas\b": "amaciante roupas",
    r"\bap[.\s]*barbear\b": "aparelho de barbear",
    r"\bco\b": "condicionador",
    r"\bdesin(?:f)?\b": "desinfetante",
    r"\bdesod\b": "desodorante",
    r"\bsab\b": "sabonete",
    r"\baerosol\b": "aerossol",
    r"\blimp\b": "limpador",
    r"\bm[.\s]*u\b": "multiuso",
    r"\bfd\b": "folha dupla",
    r"\besponja\s+de\s+aco\b": "la de aco",
    r"\brolos?\b": "unidades",
    r"\bcond\b": "condicionador",
    r"\bcr\s*(?:d|dental)\b": "creme dental",
    r"\bsh\b": "shampoo",
    r"\bdet(?:er)?[.\s]*po\b": "detergente em po",
    r"\bdet(?:er)?[.\s]*liq\b": "detergente liquido",
    r"\binset\s*aer\b": "inseticida aerossol",
    r"\bpapel\s*hig\b": "papel higienico",
    r"\bfat\s*kg\b": "fatiado kg",
    r"\bmax\s*pro\b": "maxima protecao",
    r"\bhortel\b": "hortela",
    r"\bfix\b": "fixo",
    r"\btriple\b": "tripla protecao",
    r"\blv\s+mais\s+pg\s+menos\b": "leve mais pague menos",
    r"\blv\s*(\d+)\s*pg\s*(\d+)\b": r"leve \1 pague \2",
    r"\bl\s*(\d+)\s*p\s*(\d+)\b": r"leve \1 pague \2",
    r"\bc\s*/?\s*(\d+)\b": r"\1 unidades",
    r"\b(\d+)\s*un\b": r"\1 unidades",
    r"\blt\b": "l",
    r"\bgr\b": "g",
}

@dataclass(frozen=True)
class Quantity:
    kind: str
    value: float
    original: str


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


@lru_cache(maxsize=16_384)
def _normalize_text_cached(text: str) -> str:
    text = strip_accents(text).lower()
    text = text.replace("&", " e ")
    # Códigos entre parênteses usados pelo catálogo da Coocerqui.
    text = re.sub(r"\(\s*a\s*\)", " aerossol ", text)
    text = re.sub(r"\(\s*r\s*\)", " roll on ", text)
    text = re.sub(r"\(\s*f\s*\)", " feminino ", text)
    text = re.sub(r"\(\s*m\s*\)", " masculino ", text)
    # Preserva apenas separadores decimais; pontos de abreviações viram espaço.
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", " ", text)
    for pattern, repl in ABBREVIATIONS.items():
        text = re.sub(pattern, repl, text, flags=re.I)
    text = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " x ", text)
    # Uniformiza unidades coladas ao número: 2L -> 2 L, 800G -> 800 G, 30M -> 30 M.
    text = re.sub(r"(?<=\d)(?=(?:kg|ml|g|l|m)\b)", " ", text, flags=re.I)
    text = re.sub(r"[^a-z0-9.%+]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_text(text: str) -> str:
    # A normalização é pura e muito repetida durante matching cruzado do catálogo.
    # Cachear por string evita refazer dezenas de regex para a mesma descrição sem
    # compartilhar objetos mutáveis nem alterar a semântica pública da função.
    return _normalize_text_cached(str(text or ""))


def search_text(text: str) -> str:
    n = normalize_text(text)
    n = re.sub(r"\b(leve|lv)\s+(\d+)\s+(pague|pg)\s+(\d+)\b", r"leve \2 pague \4", n)
    return n


def extract_quantities(text: str) -> list[Quantity]:
    # Não confunda preço com tamanho/contagem: após normalização,
    # ``R$ 9,99 /un`` poderia virar ``r 9.99 un`` e parecer "99 unidades";
    # o mesmo ocorria com ``R$ 24,99 /kg``. Removemos valores monetários
    # antes de procurar peso, volume, comprimento e quantidade.
    raw = str(text or "")
    raw = re.sub(
        r"R\$\s*[0-9]{1,6}(?:[.\s][0-9]{3})*(?:[.,][0-9]{2})?",
        " ",
        raw,
        flags=re.I,
    )
    # /kg e /un descrevem a unidade de VENDA do preço, não peso/contagem do
    # produto. Sem removê-los, "espaguete número 2 ... /kg" podia virar 2 kg.
    raw = re.sub(r"/\s*(?:kg|un|unidade)\b", " ", raw, flags=re.I)
    n = normalize_text(raw)
    out: list[Quantity] = []

    patterns = [
        ("mass_g", r"(?<!\w)(\d+(?:\.\d+)?)\s*(kg|g)\b"),
        ("volume_ml", r"(?<!\w)(\d+(?:\.\d+)?)\s*(l|ml)\b"),
        ("length_m", r"(?<!\w)(\d+(?:\.\d+)?)\s*m\b"),
    ]
    for kind, pattern in patterns:
        for m in re.finditer(pattern, n):
            value = float(m.group(1))
            unit = m.group(2) if m.lastindex and m.lastindex >= 2 else "m"
            if kind == "mass_g" and unit == "kg":
                value *= 1000
            elif kind == "volume_ml" and unit == "l":
                value *= 1000
            out.append(Quantity(kind, value, m.group(0)))

    for m in re.finditer(r"(?<!\w)(\d+)\s*(?:unidades|unidade|un|rolos|rolo)\b", n):
        out.append(Quantity("count", float(m.group(1)), m.group(0)))

    # Apresentações promocionais explícitas também fazem parte da identidade
    # quando estão no nome do produto (ex.: Neve leve 12 pague 11).
    for m in re.finditer(r"\bleve\s+(\d+)\s+pague\s+(\d+)\b", n):
        out.append(Quantity("count", float(m.group(1)), f"leve {m.group(1)}"))
        out.append(Quantity("count", float(m.group(2)), f"pague {m.group(2)}"))
    return out


def quantity_signature(text: str) -> dict[str, set[float]]:
    sig: dict[str, set[float]] = {}
    for q in extract_quantities(text):
        sig.setdefault(q.kind, set()).add(q.value)
    return sig


def comparable_quantity_match(expected: str, candidate: str) -> tuple[bool, str]:
    """Compara quantidades explícitas sem aceitar coincidência parcial.

    A regra antiga aceitava o candidato se *qualquer* quantidade coincidisse.
    Isso permitia, por exemplo, um alvo ``500 ml leve 500 pague 450`` casar
    com ``1000 ml leve 500 pague 450``. Para preço definitivo, todas as
    quantidades explícitas do mesmo tipo precisam coincidir.
    """
    exp = quantity_signature(expected)
    got = quantity_signature(candidate)
    for kind, exp_values in exp.items():
        if kind not in got:
            if kind in {"mass_g", "volume_ml"}:
                other_kind = "volume_ml" if kind == "mass_g" else "mass_g"
                if other_kind in got:
                    return False, (
                        "unidade/quantidade divergente "
                        f"(esperado {kind}, encontrado {other_kind}: {sorted(got[other_kind])})"
                    )
            return False, f"quantidade não confirmada ({kind}: esperado {sorted(exp_values)})"
        if exp_values != got[kind]:
            return False, f"quantidade divergente ({kind}: esperado {sorted(exp_values)}, encontrado {sorted(got[kind])})"
    return True, ""
