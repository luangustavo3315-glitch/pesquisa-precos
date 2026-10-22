from __future__ import annotations

# Compatibility facade: public imports stay stable while decision stages live in
# focused modules that can be tested and calibrated independently.
from .guards import brand_present as _brand_present, detect_product_type
from .matcher_primitives import alias_match as _alias_match
from .matcher_primitives import decision as _decision
from .matcher_primitives import similarity
from .matcher_rules import classify

__all__ = [
    "classify",
    "similarity",
    "detect_product_type",
    "_brand_present",
    "_alias_match",
    "_decision",
]
