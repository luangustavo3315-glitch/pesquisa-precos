from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..models import Candidate
from ..normalizer import normalize_text
from ..price_evidence import detect_sale_unit, parse_prices


class DetailExtractionMixin:
    """Scope and parse one product detail page conservatively."""

    @staticmethod
    def _primary_detail_prefix(body: str, max_chars: int = 5000) -> str:
        """Return the page area that can contain the *main* product identity.

        Recommendation carousels and descriptions routinely contain other product
        names, weights and prices. Identity resolution must happen before those
        blocks. The first monetary price is a useful upper bound because both
        supported stores render the main product title before its price.
        """
        body = str(body or "")
        first_price = body.find("R$")
        limit = first_price if first_price >= 0 else min(len(body), max_chars)
        return body[:max(0, min(limit, max_chars))]

    @classmethod
    def _resolve_detail_title(cls, body: str, listing_title: str, selector_title: str = "") -> str:
        """Resolve the canonical detail-page title without trusting a generic ``h1``.

        Coocerqui currently exposes a site-level ``h1`` ("Supermercados Online –
        Loja Virtual") on product pages. The actual product name is a separate
        line immediately before the main price. We therefore compare short lines
        from the primary product block with the listing/cache title and choose the
        closest one. Lines after the first price are excluded so a recommendation
        cannot masquerade as the requested product after a redirect.
        """
        listing_title = " ".join(str(listing_title or "").split()).strip()
        selector_title = " ".join(str(selector_title or "").split()).strip()
        target = normalize_text(listing_title)
        if not target:
            return selector_title

        candidates: list[str] = []
        if selector_title:
            candidates.append(selector_title)
        prefix = cls._primary_detail_prefix(body)
        for raw_line in prefix.splitlines():
            line = " ".join(raw_line.split()).strip()
            if not line or len(line) < 4 or len(line) > 180 or "R$" in line:
                continue
            if line not in candidates:
                candidates.append(line)

        def score(line: str) -> tuple[int, float, float]:
            norm = normalize_text(line)
            if not norm:
                return (0, 0.0, 0.0)
            seq = SequenceMatcher(None, target, norm).ratio()
            target_tokens = set(target.split())
            line_tokens = set(norm.split())
            overlap = len(target_tokens & line_tokens) / max(1, len(target_tokens))
            exact_raw = int(line == listing_title)
            return (exact_raw, max(seq, overlap), seq)

        best = max(candidates, key=score, default=selector_title)
        best_score = score(best)[1] if best else 0.0
        # Below this point the page does not provide a title close enough to the
        # route/listing identity. Keep the real selector title (if any) so a
        # redirect to a different product is classified as divergent rather than
        # silently inheriting the old listing title.
        if best_score < 0.58 and selector_title:
            return selector_title
        return best or selector_title or listing_title

    @staticmethod
    def _detail_price_window(body: str, title: str, max_chars: int = 1400) -> str:
        """Keep price parsing inside the main product block only.

        The critical invariant is that prices in ``Compare``/recommendation blocks
        or long descriptions never become the price of the page's main product.
        """
        body = str(body or "")
        title = str(title or "").strip()
        stop_re = re.compile(
            r"\n\s*(?:Compare|Quem viu|Produtos relacionados|Avalia(?:ç|c)ões|Descrição|"
            r"Informações do Produto|Quem somos)\b",
            flags=re.I,
        )
        starts: list[int] = []
        if title:
            lower_body = body.casefold()
            lower_title = title.casefold()
            pos = 0
            while True:
                found = lower_body.find(lower_title, pos)
                if found < 0:
                    break
                starts.append(found)
                pos = found + max(1, len(lower_title))
        for start in starts:
            fragment = body[start:start + max_chars]
            fragment = stop_re.split(fragment, maxsplit=1)[0]
            if "R$" in fragment:
                return fragment

        # Conservative fallback: use only the block around the first price and
        # still cut recommendations. Never scan the whole page for the cheapest
        # monetary value.
        price_pos = body.find("R$")
        if price_pos >= 0:
            fragment = body[max(0, price_pos - 350):price_pos + 500]
            return stop_re.split(fragment, maxsplit=1)[0]
        return stop_re.split(body[:max_chars], maxsplit=1)[0]

    async def _structured_detail_price_text(self) -> str:
        fragments: list[str] = []
        for key in ("detail_price_original", "detail_price_current", "detail_price_unit", "detail_promo_badge"):
            selector = self.selector(key)
            if not selector:
                continue
            try:
                value = (await self.page.locator(selector).first.inner_text(timeout=1200)).strip()
                if value:
                    fragments.append(value)
            except Exception as exc:
                self.record_soft_failure(f"selector:{key}", exc)
        if fragments:
            self.record_event("detail_structured_price_hits")
        return " ".join(fragments)

    async def extract_detail_candidate(self, listing_candidate: Candidate) -> tuple[Candidate, int | None]:
        """Open a product detail page and rebuild identity/price from that page.

        Listing prices are discovery hints only. They are never copied into the
        returned candidate, so a stale search card cannot become the final price.
        """
        response = await self.goto(listing_candidate.url)
        status_code = getattr(response, "status", None)
        if isinstance(status_code, int) and status_code >= 400:
            raise RuntimeError(f"página detalhada retornou HTTP {status_code}")

        title_selector = self.selector("product_title", "h1")
        try:
            title_locator = self.page.locator(title_selector).first
            await title_locator.wait_for(state="visible", timeout=10000)
            selector_title = (await title_locator.inner_text(timeout=2000)).strip()
        except Exception as exc:
            self.record_soft_failure("detail_product_title", exc)
            selector_title = ""
        body = await self.body_text()
        title = self._resolve_detail_title(body, listing_candidate.title, selector_title)
        if not title:
            raise RuntimeError("página detalhada sem título de produto confirmável")
        if selector_title and normalize_text(selector_title) != normalize_text(title):
            self.record_event("detail_title_fallback")

        window = self._detail_price_window(body, title)
        structured = await self._structured_detail_price_text()
        parse_source = structured if "R$" in structured else window
        normal, promo, observed, condition = parse_prices(parse_source)
        unavailable = any(
            token in window.lower()
            for token in ("indisponível", "indisponivel", "esgotado", "sem estoque")
        )
        current_url = str(getattr(self.page, "url", "") or listing_candidate.url)
        detail = Candidate(
            title=title,
            url=current_url,
            raw_text=window,
            price_normal=normal,
            price_promo=promo,
            price_observed=observed,
            promo_condition=condition,
            unit_price=detect_sale_unit(parse_source or window),
            available=False if unavailable else True if observed is not None else None,
        )
        if isinstance(listing_candidate.price_observed, (int, float)) and isinstance(observed, (int, float)):
            if abs(float(listing_candidate.price_observed) - float(observed)) > 0.011:
                detail.price_normalization_note = (
                    "preço relido na página detalhada e priorizado sobre a listagem "
                    f"(listagem R$ {float(listing_candidate.price_observed):.2f}; detalhe R$ {float(observed):.2f})"
                )
                self.record_event("detail_listing_price_mismatch")
        self.record_event("detail_pages_verified")
        return detail, status_code
