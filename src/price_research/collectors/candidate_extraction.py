from __future__ import annotations

from urllib.parse import urljoin

from ..models import Candidate
from ..price_evidence import detect_sale_unit, parse_prices


class CandidateExtractionMixin:
    """Extract listing candidates without mixing identity and price across cards."""

    async def extract_product_cards(
        self,
        card_selector: str,
        title_selector: str,
        href_selector: str | None = None,
    ) -> list[Candidate]:
        """Extrai nome, URL e preço do mesmo cartão, evitando misturar produtos."""
        cards = self.page.locator(card_selector)
        count = min(await cards.count(), 160)
        seen: set[str] = set()
        out: list[Candidate] = []
        for i in range(count):
            card = cards.nth(i)
            try:
                title_el = card.locator(title_selector).first
                title = (await title_el.inner_text(timeout=1500)).strip()
                link_el = card.locator(href_selector).first if href_selector else title_el
                href = await link_el.get_attribute("href")
                if not title or not href:
                    continue
                url = urljoin(self.cfg.get("base_url", ""), href)
                if url in seen:
                    continue
                seen.add(url)
                raw = (await card.inner_text(timeout=2000)).strip()
                structured = await self.structured_price_text(card)
                parse_source = structured if "R$" in structured else raw
                normal, promo, observed, condition = parse_prices(parse_source)
                lower = raw.lower()
                unavailable = any(x in lower for x in ["indisponível", "indisponivel", "esgotado", "sem estoque"])
                out.append(Candidate(
                    title=title,
                    url=url,
                    raw_text=raw,
                    price_normal=normal,
                    price_promo=promo,
                    price_observed=observed,
                    promo_condition=condition,
                    unit_price=detect_sale_unit(raw),
                    available=False if unavailable else True if observed is not None else None,
                ))
            except Exception as exc:
                self.record_soft_failure("candidate_card", exc)
                continue
        self.record_event("candidates_extracted", len(out))
        return out
