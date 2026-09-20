"""
owner.py — decorator @is_owner() pra comandos que só VOCÊ (dono do SaaS
FFZ Vendas) pode usar, tipo /gerarkey, /revogarlicenca etc.

Importante: licenca.comando_isento_de_licenca() detecta automaticamente
qualquer comando decorado com @is_owner() (procura pelo nome da closure
"is_owner.<locals>.predicate") — então todo comando de dono já fica isento
do bloqueio de licença sem precisar listar nome por nome.
"""

import os

import discord
from discord import app_commands


def _owner_id() -> int | None:
    valor = os.getenv("OWNER_ID")
    return int(valor) if valor and valor.isdigit() else None


def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        owner_id = _owner_id()
        if owner_id is None:
            return False
        return interaction.user.id == owner_id
    return app_commands.check(predicate)
