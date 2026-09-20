"""
gateways/mercadopago.py — Pix automático via API do Mercado Pago.

Documentação usada: https://www.mercadopago.com.br/developers/pt/docs/checkout-api/payment-methods/pix
- Criar cobrança: POST /v1/payments (payment_method_id="pix")
- Consultar:      GET  /v1/payments/{id}
- Webhook:        Mercado Pago manda POST {"type":"payment","data":{"id":"..."}}
  pro notification_url — o payload NÃO traz o status, só o ID; por isso
  processar_webhook aqui devolve status "pendente" (o bot.py então chama
  verificar_pagamento(id) pra saber o status real antes de liberar).

Pra ativar: gerar um Access Token de produção (ou teste) em
https://www.mercadopago.com.br/developers/panel/app e cadastrar em
`/configurarloja` → Pagamento → Mercado Pago.
"""

from __future__ import annotations

import logging

import aiohttp

from .base import CampoConfig, GatewayPagamento, ResultadoCobranca

logger = logging.getLogger("ffzvendas.gateway.mercadopago")

API_BASE = "https://api.mercadopago.com"


class GatewayMercadoPago(GatewayPagamento):
    NOME = "mercadopago"
    LABEL = "Mercado Pago"
    EMOJI = "🟦"
    IMPLEMENTADO = True
    CAMPOS_CONFIG = [
        CampoConfig(
            chave="access_token",
            label="Access Token (produção ou teste)",
            placeholder="APP_USR-... ou TEST-...",
            obrigatorio=True,
            secreto=True,
            max_length=200,
        ),
    ]

    def _token(self) -> str | None:
        return self.credenciais.get("access_token")

    async def criar_cobranca(
        self, *, valor: float, descricao: str, pedido_id: int, notification_url: str | None
    ) -> ResultadoCobranca:
        token = self._token()
        if not token:
            return ResultadoCobranca(ok=False, erro="Mercado Pago não está configurado nesse servidor.")

        corpo = {
            "transaction_amount": round(float(valor), 2),
            "description": descricao[:250],
            "payment_method_id": "pix",
            # Mercado Pago exige um e-mail de pagador; como é Pix e a
            # cobrança é anônima do lado do comprador, usamos um e-mail
            # sintético — não afeta o recebimento, só é obrigatório na API.
            "payer": {"email": f"comprador-{pedido_id}@ffzvendas.invalid"},
            "external_reference": str(pedido_id),
        }
        if notification_url:
            corpo["notification_url"] = notification_url

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # evita o MP tratar retries de rede nossa como cobrança duplicada
            "X-Idempotency-Key": f"ffzvendas-pedido-{pedido_id}",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{API_BASE}/v1/payments", json=corpo, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    dados = await resp.json()
                    if resp.status not in (200, 201):
                        mensagem = dados.get("message") or dados.get("error") or f"HTTP {resp.status}"
                        logger.warning(f"Mercado Pago recusou a cobrança do pedido #{pedido_id}: {mensagem}")
                        return ResultadoCobranca(ok=False, erro=f"Mercado Pago recusou: {mensagem}")
        except (aiohttp.ClientError, TimeoutError) as erro:
            logger.error(f"Erro de rede falando com o Mercado Pago (pedido #{pedido_id}): {erro}")
            return ResultadoCobranca(ok=False, erro="Não consegui falar com o Mercado Pago agora. Tente de novo em instantes.")

        try:
            poi = dados["point_of_interaction"]["transaction_data"]
            copia_cola = poi["qr_code"]
            qr_base64 = poi.get("qr_code_base64")
            charge_id = str(dados["id"])
        except (KeyError, TypeError):
            logger.error(f"Resposta do Mercado Pago sem os campos de Pix esperados (pedido #{pedido_id}): {dados}")
            return ResultadoCobranca(ok=False, erro="O Mercado Pago aprovou a cobrança mas não devolveu o QR Pix — tente de novo.")

        return ResultadoCobranca(ok=True, charge_id=charge_id, copia_cola=copia_cola, qr_base64=qr_base64)

    async def verificar_pagamento(self, charge_id: str) -> str:
        token = self._token()
        if not token:
            return "pendente"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{API_BASE}/v1/payments/{charge_id}", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return "pendente"
                    dados = await resp.json()
        except (aiohttp.ClientError, TimeoutError):
            return "pendente"

        return _mapear_status(dados.get("status"))

    @classmethod
    def processar_webhook(cls, payload: dict, headers: dict) -> dict | None:
        # Formato novo: {"type": "payment", "data": {"id": "123"}}
        # Formato antigo (IPN): {"topic": "payment", "resource": ".../123"}
        tipo = payload.get("type") or payload.get("topic")
        if tipo != "payment":
            return None

        charge_id = None
        if isinstance(payload.get("data"), dict):
            charge_id = payload["data"].get("id")
        elif payload.get("resource"):
            charge_id = str(payload["resource"]).rstrip("/").split("/")[-1]

        if not charge_id:
            return None

        # O webhook do MP não traz o status — só avisa "algo mudou nesse
        # pagamento". Quem recebe isso em bot.py deve chamar
        # verificar_pagamento(charge_id) pra saber o status de verdade
        # antes de liberar a entrega.
        return {"charge_id": str(charge_id), "status": "verificar"}


def _mapear_status(status_mp: str | None) -> str:
    if status_mp == "approved":
        return "pago"
    if status_mp in ("cancelled", "rejected"):
        return "cancelado"
    return "pendente"
