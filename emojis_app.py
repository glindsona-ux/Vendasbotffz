"""
emojis_app.py — versão inicial (V2 ainda não tem upload automático de
emojis de aplicação). Mantém a MESMA interface do bot original
(obter(nome, padrao)) pra que licenca.py e outros módulos portados
funcionem sem alteração — hoje sempre cai no `padrao` (emoji de teclado),
até a gente portar o sistema de auto-upload também.
"""

_cache: dict[str, str] = {}


def obter(nome: str, padrao: str) -> str:
    emoji = _cache.get(nome)
    return str(emoji) if emoji else padrao


def total() -> int:
    return len(_cache)
