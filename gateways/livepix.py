"""
gateways/livepix.py — ENCAIXE PRONTO, ainda não implementado de verdade.

Por quê: eu não tenho uma conta de vendedor + API key do LivePix pra
testar contra a API real, e cravar o formato de requisição/resposta "no
escuro" é como um pagamento real quebra silenciosamente e o dinheiro do
cliente do FFZ Vendas fica preso no limbo. A estrutura toda (registro no
`/configurarloja`, criptografia da chave, fila de webhook) já está
pronta — falta só preencher `criar_cobranca`, `verificar_pagamento` e
`processar_webhook` abaixo com as chamadas HTTP reais assim que houver
a chave de API e a documentação de referência em mãos (geralmente é uma
tarde de trabalho, não mais que isso, com a interface já pronta).

Quando o admin escolhe LivePix em `/configurarloja` mas tenta gerar uma
cobrança automática, o bot mostra um aviso amigável e cai pro Pix manual
— nunca finge que cobrou e nunca deixa o cliente pagando no vácuo.
"""

from __future__ import annotations

from .base import CampoConfig, GatewayPagamento, ResultadoCobranca


class GatewayLivePix(GatewayPagamento):
    NOME = "livepix"
    LABEL = "LivePix"
    EMOJI = "🟪"
    IMPLEMENTADO = False
    CAMPOS_CONFIG = [
        CampoConfig(chave="api_key", label="API Key do LivePix", obrigatorio=True, secreto=True),
    ]

    async def criar_cobranca(self, *, valor, descricao, pedido_id, notification_url) -> ResultadoCobranca:
        return ResultadoCobranca(
            ok=False,
            erro=(
                "O gateway LivePix ainda não está com a integração automática pronta "
                "(falta a chave de API real pra testar contra o servidor deles). "
                "Use Pix manual ou Mercado Pago por enquanto."
            ),
        )

    async def verificar_pagamento(self, charge_id: str) -> str:
        return "pendente"

    @classmethod
    def processar_webhook(cls, payload: dict, headers: dict) -> dict | None:
        return None
