from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..models import Candidate, StoreResult
from ..normalizer import normalize_text

if TYPE_CHECKING:
    from .coocerqui import CoocerquiCollector


class StoreContextManager:
    """Owns branch selection, validation and fail-closed price authorization."""

    def __init__(self, collector: CoocerquiCollector) -> None:
        self.collector = collector

    async def selected_store_text(self) -> str:
        collector = self.collector
        selector = collector.selector(
            "selected_store_button",
            'button[aria-label="Entrega ou Retirada"]',
        )
        try:
            control = collector.page.locator(selector).first
            await control.wait_for(state="visible", timeout=5000)
            return (await control.inner_text(timeout=2000)).strip()
        except Exception as exc:
            collector.record_soft_failure("selected_store_text", exc)
            return ""

    def target_store_is_selected(self, selected_text: str) -> bool:
        collector = self.collector
        normalized = normalize_text(selected_text)
        expected = [
            normalize_text(str(term))
            for term in collector.cfg.get("expected_address_terms", [])
            if str(term).strip()
        ]
        if expected:
            return all(term in normalized for term in expected)
        fallback = normalize_text(
            collector.selector("store_option_text", "Coocerqui - San Juan")
        )
        return bool(fallback) and fallback in normalized

    async def ensure_target_store(self) -> bool:
        collector = self.collector
        try:
            await collector.goto(collector.cfg["base_url"])
            await collector.page.wait_for_timeout(2500)
            selected_text = await self.selected_store_text()
            if not self.target_store_is_selected(selected_text):
                selection_changed = False
                try:
                    delivery_button = collector.selector(
                        "delivery_button_name",
                        "Entrega ou Retirada",
                    )
                    store_name = collector.selector(
                        "store_option_text",
                        "Coocerqui - San Juan",
                    )
                    await collector.page.get_by_role(
                        "button",
                        name=delivery_button,
                    ).click(timeout=6000)
                    await collector.page.wait_for_timeout(500)
                    tile_selector = collector.selector(
                        "store_tile",
                        "vip-card-endereco-entrega",
                    )
                    tile = collector.page.locator(tile_selector).filter(
                        has_text=store_name
                    ).first
                    if await tile.count():
                        await tile.click(timeout=6000)
                        selection_changed = True
                    else:
                        radio = collector.page.get_by_role(
                            "radio",
                            name=re.compile(re.escape(store_name)),
                        ).first
                        if await radio.count():
                            await radio.click(timeout=6000)
                            selection_changed = True
                    if not selection_changed:
                        raise RuntimeError(
                            f"opção de loja não encontrada: {store_name}"
                        )
                    await collector.page.wait_for_timeout(1200)
                    # A navegação dura estabiliza o catálogo na filial escolhida.
                    await collector.goto(collector.cfg["base_url"])
                    await collector.page.wait_for_timeout(1200)
                    collector.record_event("store_context_reloaded")
                except Exception as exc:
                    collector.record_soft_failure(
                        "verify_store_interaction",
                        exc,
                    )
                selected_text = await self.selected_store_text()

            collector.store_confirmed = self.target_store_is_selected(
                selected_text
            )
            collector.store_verification_note = (
                "Coocerqui San Juan confirmada na sessão"
                if collector.store_confirmed
                else (
                    "não foi possível confirmar a unidade San Juan no seletor ativo"
                    + (
                        f" (exibido: {selected_text})"
                        if selected_text
                        else ""
                    )
                )
            )
            return collector.store_confirmed
        except Exception as exc:
            collector.record_soft_failure("verify_store", exc)
            collector.store_verification_note = (
                "falha ao verificar unidade Coocerqui: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    async def revalidate(self) -> bool:
        collector = self.collector
        selected_text = await self.selected_store_text()
        if self.target_store_is_selected(selected_text):
            return True
        collector.store_confirmed = False
        collector.record_event("store_context_lost")
        collector.store_verification_note = (
            "contexto da unidade San Juan foi perdido durante a navegação"
            + (f" (exibido: {selected_text})" if selected_text else "")
        )
        return False

    def reject_price_without_target_store(
        self,
        result: StoreResult,
    ) -> StoreResult:
        collector = self.collector
        strict = bool(
            collector.global_cfg.get("strict_store_confirmation", True)
        )
        allow_unconfirmed = bool(
            collector.cfg.get("allow_unconfirmed_online", False)
        )
        if (
            not strict
            or allow_unconfirmed
            or collector.store_confirmed
        ):
            return result

        result.store_confirmed = False
        result.source_scope = "online_unconfirmed_branch"
        result.definitive_price = None
        result.price_validation = "rejected"
        if (
            result.source_match_status in {"EXATO", "SUBSTITUTO ACEITO"}
            and result.status != "INDISPONÍVEL"
        ):
            result.status = "PROVÁVEL"
        note = (
            collector.store_verification_note
            or "unidade San Juan não confirmada"
        )
        if note not in result.notes:
            result.notes = (
                result.notes + "; " + note + "; preço bloqueado"
            ).strip("; ")
        return result


class SearchRecoveryPolicy:
    """Owns the one-shot recovery for transient blank Coocerqui searches."""

    def __init__(self, collector: CoocerquiCollector) -> None:
        self.collector = collector

    @staticmethod
    def is_explicit_empty(page_text: str) -> bool:
        normalized = normalize_text(page_text)
        markers = (
            "nao conseguimos encontrar nenhum produto",
            "nenhum produto encontrado",
            "nenhum resultado encontrado",
            "0 produtos",
        )
        return any(marker in normalized for marker in markers)

    def should_reload(self, page_text: str) -> bool:
        collector = self.collector
        if self.is_explicit_empty(page_text):
            collector.record_event("explicit_empty_searches")
            return False
        if not collector.global_cfg.get(
            "coocerqui_blank_search_reload_enabled",
            True,
        ):
            collector.record_event("blank_search_reload_disabled")
            return False
        return True

    async def recover(
        self,
        *,
        url: str,
        page_text: str,
        card_selector: str,
        title_selector: str,
        href_selector: str,
    ) -> list[Candidate]:
        collector = self.collector
        if not self.should_reload(page_text):
            return []

        collector.record_event("blank_search_reloads")
        await collector.goto(url)
        recovery_timeout = max(
            1000,
            int(
                collector.global_cfg.get(
                    "coocerqui_blank_search_recovery_timeout_ms",
                    10000,
                )
            ),
        )
        try:
            await collector.page.locator(card_selector).first.wait_for(
                state="visible",
                timeout=recovery_timeout,
            )
        except Exception as exc:
            collector.record_soft_failure(
                "blank_search_recovery_wait",
                exc,
            )
        await collector.page.wait_for_timeout(600)
        candidates = await collector.extract_product_cards(
            card_selector=card_selector,
            title_selector=title_selector,
            href_selector=href_selector,
        )
        if candidates:
            collector.record_event("blank_search_recovered")
            return candidates

        refreshed_text = await collector.body_text()
        if self.is_explicit_empty(refreshed_text):
            collector.record_event("explicit_empty_searches")
        else:
            collector.record_soft_failure("blank_search_recovery_empty")
        return []
