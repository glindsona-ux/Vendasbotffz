"""
crypto_utils.py — criptografia simétrica (Fernet/AES) pras credenciais dos
gateways de pagamento (access tokens, API keys) ficarem no banco de forma
que só o próprio bot (com a CHAVE_CRIPTOGRAFIA do .env) consegue ler.

Sem isso, um dump do banco (ffz_vendas.db) exporia o access_token de
produção do Mercado Pago de todo mundo em texto puro — e quem pega esse
token consegue criar cobranças e ver o extrato de vendas da vítima.

A chave em si (CHAVE_CRIPTOGRAFIA) NUNCA fica no banco, só no .env — se
ela mudar ou sumir, as credenciais salvas ficam ilegíveis (por isso o
comando de configurar gateway sempre deixa fácil recadastrar).
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_fernet_instance: Fernet | None = None


def _obter_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    chave_raw = os.getenv("CHAVE_CRIPTOGRAFIA")
    if not chave_raw:
        # Fallback pra não derrubar o bot se esqueceram de gerar a chave —
        # mas isso é claramente sinalizado nos logs, porque é sério: dados
        # criptografados com essa chave-fallback são recuperáveis por
        # qualquer um que leia o código-fonte.
        import logging

        logging.getLogger("ffzvendas").warning(
            "⚠️ CHAVE_CRIPTOGRAFIA não definida no .env — usando uma chave "
            "derivada e fixa (INSEGURO em produção). Gere uma com "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "e coloque em CHAVE_CRIPTOGRAFIA no .env."
        )
        chave_raw = "ffz-vendas-chave-insegura-troque-isso-no-env"

    # Deriva 32 bytes válidos pro Fernet a partir de qualquer string que a
    # pessoa tenha colocado no .env (aceita tanto uma chave Fernet real
    # quanto uma senha qualquer).
    digest = hashlib.sha256(chave_raw.encode("utf-8")).digest()
    chave_fernet = base64.urlsafe_b64encode(digest)
    _fernet_instance = Fernet(chave_fernet)
    return _fernet_instance


def criptografar(texto: str) -> str:
    """Criptografa uma string (ex: access_token) pra guardar no banco."""
    if texto is None:
        return None
    return _obter_fernet().encrypt(texto.encode("utf-8")).decode("utf-8")


def descriptografar(texto_criptografado: str) -> str | None:
    """Descriptografa. Retorna None se a chave mudou ou o dado é inválido
    (evita o bot crashar por causa de uma credencial ilegível — o gateway
    correspondente é tratado como \"não configurado\" nesse caso)."""
    if not texto_criptografado:
        return None
    try:
        return _obter_fernet().decrypt(texto_criptografado.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
