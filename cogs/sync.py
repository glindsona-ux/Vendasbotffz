"""
cogs/sync.py — comando manual para forçar a sincronização dos slash
commands, sem precisar reiniciar o bot inteiro.

Por que isso existe:
- bot.tree.sync() sem guild = sync GLOBAL. Comandos globais podem levar
  até ~1h pra aparecer pra todo mundo (cache do próprio Discord, não é
  bug do bot).
- bot.tree.sync(guild=X) = sync só naquele servidor, mas aparece
  IMEDIATAMENTE nele.
- Depender só do sync automático do on_ready é lento pra testar e ainda
  roda de novo a cada reconexão à toa. Esse comando resolve sob demanda.

Uso (comando de texto, prefixo "!", só o dono do bot pode usar):
  !sync            -> sync global (padrão, propaga em ~1h)
  !sync aqui       -> sincroniza instantaneamente só no servidor atual
  !sync todos      -> sincroniza instantaneamente em TODOS os servidores
                       onde o bot está (evita esperar a propagação global)
"""

import asyncio
import logging

import discord
from discord.ext import commands

from owner import _owner_id

logger = logging.getLogger("ffzvendas")


class Sync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        owner_id = _owner_id()
        return owner_id is not None and ctx.author.id == owner_id

    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context, escopo: str = "global"):
        escopo = escopo.lower().strip()

        if escopo == "aqui":
            if ctx.guild is None:
                return await ctx.send("❌ Use `!sync aqui` dentro de um servidor.")
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
            return await ctx.send(
                f"✅ {len(synced)} comando(s) sincronizado(s) instantaneamente neste servidor."
            )

        if escopo in ("todos", "todos_servidores", "all"):
            total_guilds = len(self.bot.guilds)
            msg = await ctx.send(f"🔄 Sincronizando em {total_guilds} servidor(es)...")
            ok, falhas = 0, 0
            for guild in self.bot.guilds:
                try:
                    self.bot.tree.copy_global_to(guild=guild)
                    await self.bot.tree.sync(guild=guild)
                    ok += 1
                except discord.HTTPException:
                    falhas += 1
                    logger.error(f"❌ Falha ao sincronizar comandos no servidor {guild.id}")
                await asyncio.sleep(1)  # evita rate limit da API do Discord
            aviso_falhas = f" ⚠️ {falhas} falha(s)." if falhas else ""
            return await msg.edit(
                content=f"✅ Sincronizado instantaneamente em {ok}/{total_guilds} servidor(es).{aviso_falhas}"
            )

        # padrão: sync global
        synced = await self.bot.tree.sync()
        await ctx.send(
            f"✅ {len(synced)} comando(s) sincronizado(s) globalmente.\n"
            f"⏳ Pode levar até ~1h pra aparecer em todos os servidores (cache do Discord)."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Sync(bot))
