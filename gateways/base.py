"""
gateways/base.py — a interface comum que todo gateway de pagamento (Pix
manual, Mercado Pago, LivePix, PagBank...) implementa. É o "encaixe" que
o resto do bot (loja.py, pedidos.py) usa sem precisar saber qual gateway
está por trás — assim plugar um gateway novo é criar 1 arquivo aqui
dentro, sem mexer no resto do sistema.

Um CampoConfig descreve um campo que o admin preenche no modal de
`/configurarloja` pra ativar aquele gateway (ex: access_token do Mercado
Pago). `secreto=True` faz o campo virar um TextInput de estilo parágrafo
mas o VALOR sempre é criptografado no banco (crypto_utils) — o "secreto"
aqui é só sobre não deixar o valor visível depois de salvo nas telas do
bot, a criptografia em si é sempre aplicada a toda credencial.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CampoConfig:
    chave: str  # nome do campo no dict de credenciais (ex: "access_token")
    label: str  # label mostrado no modal
    placeholder: str = ""
    obrigatorio: bool = True
    secreto: bool = True  # se True, não é reexibido depois de salvo
    max_length: int = 200


@dataclass
class ResultadoCobranca:
    ok: bool
    erro: str | None = None
    charge_id: str | None = None
    copia_cola: str | None = None  # Pix copia-e-cola, se o gateway gerar
    qr_base64: str | None = None  # imagem do QR em base64 (data: opcional)
    checkout_url: str | None = None  # link de checkout, se o gateway usar isso em vez de QR


class GatewayPagamento:
    """Classe-base. Cada gateway concreto sobrescreve NOME, LABEL,
    CAMPOS_CONFIG e os três métodos assíncronos abaixo."""

    NOME: str = "base"
    LABEL: str = "Gateway base"
    EMOJI: str = "💳"
    # True = já está pronto pra uso real; False = "encaixe" ainda sem a
    # chamada de API implementada (ver livepix.py / pagbank.py).
    IMPLEMENTADO: bool = False
    CAMPOS_CONFIG: list[CampoConfig] = field(default_factory=list)

    def __init__(self, credenciais: dict):
        self.credenciais = credenciais or {}

    async def criar_cobranca(
        self, *, valor: float, descricao: str, pedido_id: int, notification_url: str | None
    ) -> ResultadoCobranca:
        """Cria a cobrança no gateway e devolve como o cliente paga
        (QR/copia-e-cola ou link de checkout)."""
        raise NotImplementedError

    async def verificar_pagamento(self, charge_id: str) -> str:
        """Consulta ativamente o status no gateway. Retorna
        'pendente' | 'pago' | 'cancelado'. Usado como fallback quando o
        webhook não chega (rede instável, etc.) — não é o caminho
        principal de confirmação."""
        raise NotImplementedError

    @classmethod
    def processar_webhook(cls, payload: dict, headers: dict) -> dict | None:
        """Interpreta o corpo (já parseado como dict/JSON) que o gateway
        mandou pro nosso endpoint de webhook. Retorna
        {'charge_id': str, 'status': 'pago'|'cancelado'|'pendente'} ou
        None se o payload não for reconhecido / não for um evento de
        pagamento (o endpoint responde 200 do mesmo jeito nesses casos,
        gateway não deve ficar re-tentando pra sempre)."""
        raise NotImplementedError
