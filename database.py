"""
database.py — FFZ Vendas V2

Infraestrutura de banco (SQLite + aiosqlite) + o sistema de licença
completo, portado do bot FFZ E-Sports original (database.py, ~7400 linhas
lá, aqui só a fatia de licença — o resto desse projeto V2 vai crescer aos
poucos, sem herdar features que não fazem parte da loja).

Mantido FIEL ao original nos pontos que importam:
  - Keys nunca ficam em texto puro no banco — só o HASH (HMAC-SHA256 com
    pepper). Ver _hash_chave / _obter_pepper_licenca.
  - Ativação é ATÔMICA (UPDATE ... WHERE usado_por IS NULL) — sem
    condição de corrida entre dois servidores tentando a mesma key ao
    mesmo tempo.
  - Conexão SEPARADA e somente-leitura (_conn_licenca) dedicada ao check
    de licença, pra ele nunca ficar enfileirado atrás de uma escrita
    pesada na conexão principal.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

import aiosqlite

from constants import (
    MAX_PEDIDOS_PENDENTES_POR_USUARIO,
    VALOR_MINIMO_PEDIDO,
    StatusPedido,
    TipoCupom,
    TipoEntrega,
)
from estoque_utils import separar_validos

DB_PATH = "ffz_vendas.db"

_conn: aiosqlite.Connection | None = None
_conn_lock = asyncio.Lock()

_conn_licenca: aiosqlite.Connection | None = None
_conn_licenca_lock = asyncio.Lock()

# Travas de processo único (o bot roda com UMA conexão SQLite compartilhada,
# então dois comandos ao mesmo tempo se intercalam nos `await`). Cada trava
# protege uma sequência "checar -> gravar" que não pode ser interrompida.
_pedidos_lock = asyncio.Lock()   # criação de pedido (cupom + limite de pendentes)
_estoque_lock = asyncio.Lock()   # upload de estoque (dedupe)

_cache_assinaturas: dict[int, dict | None] = {}
_pepper_licenca_cache: str | None = None


async def get_conn() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        async with _conn_lock:
            if _conn is None:
                conn = await aiosqlite.connect(DB_PATH, timeout=30)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA busy_timeout=30000")
                await conn.execute("PRAGMA cache_size=-16000")
                await conn.execute("PRAGMA temp_store=MEMORY")
                conn.row_factory = aiosqlite.Row
                _conn = conn
    return _conn


async def _get_conn_licenca() -> aiosqlite.Connection:
    """Conexão separada, só-leitura, dedicada ao check de licença — não
    fica enfileirada atrás de escritas pesadas na conexão principal."""
    global _conn_licenca
    if _conn_licenca is None:
        async with _conn_licenca_lock:
            if _conn_licenca is None:
                conn = await aiosqlite.connect(DB_PATH, timeout=30)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA query_only=ON")
                conn.row_factory = aiosqlite.Row
                _conn_licenca = conn
    return _conn_licenca


async def fechar_conn():
    global _conn, _conn_licenca
    if _conn is not None:
        await _conn.close()
        _conn = None
    if _conn_licenca is not None:
        await _conn_licenca.close()
        _conn_licenca = None


async def get_meta(chave: str) -> str | None:
    db = await get_conn()
    async with db.execute("SELECT valor FROM bot_meta WHERE chave = ?", (chave,)) as cursor:
        linha = await cursor.fetchone()
        return linha[0] if linha else None


async def set_meta(chave: str, valor: str):
    db = await get_conn()
    await db.execute(
        "INSERT INTO bot_meta (chave, valor) VALUES (?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (chave, valor),
    )
    await db.commit()


async def setup_db():
    """Cria as tabelas na primeira vez que o bot liga. Schema já nasce no
    formato "moderno" (hash de key desde o início) — sem migração de
    versão antiga em texto puro, porque esse projeto nunca teve isso."""
    db = await get_conn()

    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            chave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS chaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave_hash TEXT NOT NULL UNIQUE,
            chave_mascarada TEXT NOT NULL,
            plano TEXT NOT NULL,
            dias INTEGER NOT NULL,
            criada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            criada_por INTEGER,
            usado_por INTEGER,
            usado_em TIMESTAMP,
            vence TIMESTAMP,
            cancelada INTEGER DEFAULT 0
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS assinaturas (
            guild_id INTEGER PRIMARY KEY,
            plano TEXT,
            ativo INTEGER DEFAULT 1,
            vence TIMESTAMP,
            chave_id INTEGER,
            chave_mascarada TEXT,
            ativado_por INTEGER,
            ativado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            avisado_vencimento INTEGER DEFAULT 0
        )
    """)

    # ─── Loja ───────────────────────────────────────────────────────────
    await db.execute("""
        CREATE TABLE IF NOT EXISTS config_loja (
            guild_id INTEGER PRIMARY KEY,
            chave_pix TEXT,
            tipo_chave TEXT,
            nome_recebedor TEXT,
            cidade TEXT DEFAULT 'Sao Paulo',
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            descricao TEXT,
            preco REAL NOT NULL,
            tipo_entrega TEXT NOT NULL DEFAULT 'manual',
            imagem_url TEXT,
            ativo INTEGER DEFAULT 1,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_produtos_guild ON produtos (guild_id, ativo)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS estoque_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE CASCADE,
            conteudo TEXT NOT NULL,
            entregue INTEGER DEFAULT 0,
            reservado_pedido_id INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            entregue_em TIMESTAMP
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_estoque_disponivel ON estoque_itens "
        "(produto_id, entregue, reservado_pedido_id)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS carrinho_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE CASCADE,
            quantidade INTEGER NOT NULL DEFAULT 1,
            adicionado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, user_id, produto_id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS pedidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            itens_json TEXT NOT NULL,
            valor_total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'aguardando_pagamento',
            payload_pix TEXT,
            entrega_json TEXT,
            canal_ticket_id INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pago_em TIMESTAMP,
            entregue_em TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_guild ON pedidos (guild_id, status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_usuario ON pedidos (guild_id, user_id, status)")

    # Migração leve: bancos criados antes dos cupons não têm essas colunas.
    await _adicionar_coluna_se_faltar(db, "pedidos", "valor_bruto", "REAL")
    await _adicionar_coluna_se_faltar(db, "pedidos", "desconto", "REAL DEFAULT 0")
    await _adicionar_coluna_se_faltar(db, "pedidos", "cupom_codigo", "TEXT")
    await _adicionar_coluna_se_faltar(db, "pedidos", "aprovado_por", "INTEGER")

    # ─── Loja: cupons ───────────────────────────────────────────────────
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            codigo TEXT NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'percentual',
            valor REAL NOT NULL,
            valor_minimo REAL,
            max_usos INTEGER,
            max_por_usuario INTEGER,
            produto_id INTEGER REFERENCES produtos(id) ON DELETE SET NULL,
            expira_em TIMESTAMP,
            ativo INTEGER DEFAULT 1,
            criado_por INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, codigo)
        )
    """)

    # Um cupom "gasta" um uso NA HORA que o pedido é criado (não na
    # entrega), e devolve o uso se o pedido for cancelado/expirar. Assim
    # dá pra abrir vários carrinhos com o mesmo cupom e ele não passa do limite.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cupom_usos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cupom_id INTEGER NOT NULL REFERENCES cupons(id) ON DELETE CASCADE,
            pedido_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            desconto REAL NOT NULL,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_cupom_usos ON cupom_usos (cupom_id, user_id)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS carrinho_cupom (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            codigo TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    await db.commit()


async def _adicionar_coluna_se_faltar(db, tabela: str, coluna: str, definicao: str):
    """ALTER TABLE ... ADD COLUMN só se a coluna ainda não existe. Os nomes
    vêm sempre de literais do próprio código (nunca de input de usuário)."""
    cursor = await db.execute(f"PRAGMA table_info({tabela})")
    existentes = {row[1] for row in await cursor.fetchall()}
    if coluna not in existentes:
        await db.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


# ─── Sistema de licença (keys) ──────────────────────────────────────────────

async def _obter_pepper_licenca() -> str:
    """Segredo usado pra 'temperar' o hash das keys (HMAC). Prioridade:
    env var LICENCA_PEPPER — se não tiver, gera um valor aleatório na
    primeira vez e persiste em bot_meta."""
    global _pepper_licenca_cache
    if _pepper_licenca_cache:
        return _pepper_licenca_cache

    pepper = os.environ.get("LICENCA_PEPPER")
    if not pepper:
        pepper = await get_meta("licenca_pepper")
        if not pepper:
            pepper = secrets.token_hex(32)
            await set_meta("licenca_pepper", pepper)
            print(
                "🔐 LICENCA_PEPPER não configurado na env var — gerei um novo "
                "automaticamente e salvei no banco. Recomendado: mover esse "
                "valor pra uma env var (Render/Discloud) pra ele nunca ficar "
                "no mesmo arquivo que os backups do banco."
            )
    _pepper_licenca_cache = pepper
    return pepper


async def _hash_chave(chave: str) -> str:
    pepper = await _obter_pepper_licenca()
    return hmac.new(pepper.encode("utf-8"), chave.strip().upper().encode("utf-8"), hashlib.sha256).hexdigest()


def _mascarar_chave(chave: str) -> str:
    """FFZ-PREMIUM-X7K2-A9M4-QW3R -> FFZ-PREMIUM-X7K2-****-****"""
    partes = chave.strip().upper().split("-")
    if len(partes) <= 3:
        return chave
    visivel = partes[:3]
    mascarado = ["*" * len(p) for p in partes[3:]]
    return "-".join(visivel + mascarado)


def _gerar_codigo_chave() -> str:
    """secrets, não random — previsibilidade aqui seria uma falha de
    segurança grave (alguém adivinhando keys válidas)."""
    alfabeto = string.ascii_uppercase + string.digits
    blocos = ["".join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(3)]
    return "-".join(blocos)


async def criar_chave(plano: str, dias: int, criada_por: int) -> str:
    """Cria uma key nova e retorna ela em TEXTO PURO — essa é a ÚNICA vez
    que isso acontece. Quem chama isso (ex: /gerarkey) precisa mostrar
    esse retorno pro dono NA HORA, não tem como recuperar depois."""
    db = await get_conn()
    while True:
        chave = f"FFZ-{plano.upper()}-{_gerar_codigo_chave()}"
        hash_ = await _hash_chave(chave)
        cursor = await db.execute("SELECT 1 FROM chaves WHERE chave_hash = ?", (hash_,))
        if not await cursor.fetchone():
            break
    await db.execute(
        "INSERT INTO chaves (chave_hash, chave_mascarada, plano, dias, criada_por) VALUES (?, ?, ?, ?, ?)",
        (hash_, _mascarar_chave(chave), plano.upper(), dias, criada_por),
    )
    await db.commit()
    return chave


async def obter_chave(chave: str):
    db = await get_conn()
    cursor = await db.execute("SELECT * FROM chaves WHERE chave_hash = ?", (await _hash_chave(chave),))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_chaves(limit: int = 50):
    db = await get_conn()
    cursor = await db.execute("SELECT * FROM chaves ORDER BY criada_em DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def ativar_chave(chave: str, guild_id: int, ativado_por: int):
    """Ativação ATÔMICA — o UPDATE só afeta a linha se 'usado_por' ainda
    estiver NULL, evitando duas guilds reivindicando a mesma key numa
    corrida. Retorna (sucesso: bool, motivo: str, dados: dict|None)."""
    chave = chave.strip().upper()
    hash_ = await _hash_chave(chave)
    db = await get_conn()

    cursor = await db.execute("SELECT * FROM chaves WHERE chave_hash = ?", (hash_,))
    row = await cursor.fetchone()
    if not row:
        return False, "nao_encontrada", None
    if row["cancelada"]:
        return False, "cancelada", None
    if row["usado_por"] is not None:
        return False, "ja_usada", None

    dias = row["dias"]
    plano = row["plano"]
    vence = datetime.now() + timedelta(days=dias)
    vence_str = vence.strftime("%Y-%m-%d %H:%M:%S")

    cursor = await db.execute(
        "UPDATE chaves SET usado_por = ?, usado_em = CURRENT_TIMESTAMP, vence = ? "
        "WHERE chave_hash = ? AND usado_por IS NULL",
        (guild_id, vence_str, hash_),
    )
    if cursor.rowcount == 0:
        await db.commit()
        return False, "ja_usada", None

    await db.execute(
        "INSERT INTO assinaturas (guild_id, plano, ativo, vence, chave_id, chave_mascarada, ativado_por, ativado_em, avisado_vencimento) "
        "VALUES (?, ?, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "plano = excluded.plano, ativo = 1, vence = excluded.vence, "
        "chave_id = excluded.chave_id, chave_mascarada = excluded.chave_mascarada, "
        "ativado_por = excluded.ativado_por, ativado_em = CURRENT_TIMESTAMP, "
        "avisado_vencimento = 0",
        (guild_id, plano, vence_str, row["id"], row["chave_mascarada"], ativado_por),
    )
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)
    return True, "ok", {"plano": plano, "dias": dias, "vence": vence_str}


async def cancelar_chave(identificador: str):
    """Aceita o ID numérico (sempre funciona) ou a key completa em texto
    puro (só funciona se quem digitou ainda tiver anotada)."""
    identificador = identificador.strip()
    db = await get_conn()

    if identificador.isdigit():
        cursor = await db.execute("SELECT * FROM chaves WHERE id = ?", (int(identificador),))
    else:
        cursor = await db.execute("SELECT * FROM chaves WHERE chave_hash = ?", (await _hash_chave(identificador),))
    dados = await cursor.fetchone()
    if not dados:
        return False

    if dados["usado_por"]:
        await db.execute("UPDATE assinaturas SET ativo = 0 WHERE guild_id = ?", (dados["usado_por"],))
        _cache_assinaturas.pop(dados["usado_por"], None)
    await db.execute("UPDATE chaves SET cancelada = 1 WHERE id = ?", (dados["id"],))
    await db.commit()
    return True


async def obter_assinatura(guild_id: int):
    if guild_id in _cache_assinaturas:
        return _cache_assinaturas[guild_id]

    db = await get_conn()
    cursor = await db.execute("SELECT * FROM assinaturas WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    resultado = dict(row) if row else None
    _cache_assinaturas[guild_id] = resultado
    return resultado


async def obter_assinatura_fresca(guild_id: int):
    """Igual a obter_assinatura(), mas IGNORA o cache — usada só pelo
    gate de licença, que roda em todo comando protegido. Ler sempre
    fresco garante que a checagem nunca libera passagem baseada em
    estado velho (ex: dois processos do bot de pé ao mesmo tempo)."""
    db = await _get_conn_licenca()
    cursor = await db.execute("SELECT * FROM assinaturas WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    resultado = dict(row) if row else None
    _cache_assinaturas[guild_id] = resultado
    return resultado


async def desativar_assinatura(guild_id: int):
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET ativo = 0 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def revogar_licenca(guild_id: int):
    """Bloqueia manualmente um servidor (ex: chargeback, calote)."""
    await desativar_assinatura(guild_id)


async def reativar_licenca(guild_id: int):
    """Reativa um servidor sem mexer na data de validade já salva."""
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET ativo = 1 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def liberar_licenca_manual(guild_id: int, plano: str, dias: int, liberado_por: int):
    """Libera acesso manualmente SEM consumir key do estoque (teste,
    parceria, cortesia)."""
    db = await get_conn()
    vence = (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO assinaturas (guild_id, plano, ativo, vence, chave_id, chave_mascarada, ativado_por, ativado_em, avisado_vencimento) "
        "VALUES (?, ?, 1, ?, NULL, NULL, ?, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "plano = excluded.plano, ativo = 1, vence = excluded.vence, ativado_por = excluded.ativado_por, "
        "ativado_em = CURRENT_TIMESTAMP, avisado_vencimento = 0",
        (guild_id, plano.upper(), vence, liberado_por),
    )
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)
    return vence


async def estender_licenca(guild_id: int, dias: int):
    """Soma dias a partir do MAIOR valor entre 'agora' e a validade atual
    — evita resetar e o cliente perder dias que ainda tinha."""
    db = await get_conn()
    assinatura = await obter_assinatura(guild_id)
    base = datetime.now()
    if assinatura and assinatura["vence"]:
        try:
            vence_atual = datetime.strptime(assinatura["vence"], "%Y-%m-%d %H:%M:%S")
            if vence_atual > base:
                base = vence_atual
        except ValueError:
            pass
    nova_vence = (base + timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE assinaturas SET vence = ?, ativo = 1, avisado_vencimento = 0 WHERE guild_id = ?",
        (nova_vence, guild_id),
    )
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)
    return nova_vence


async def obter_licencas_vencendo(dias: int = 3):
    """Licenças ativas que vencem dentro de X dias e ainda não avisadas."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT * FROM assinaturas WHERE ativo = 1 AND avisado_vencimento = 0 "
        "AND vence IS NOT NULL AND vence <= datetime('now', 'localtime', ?)",
        (f"+{dias} days",),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def marcar_avisado_vencimento(guild_id: int):
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET avisado_vencimento = 1 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def resetar_aviso_vencimento(guild_id: int):
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET avisado_vencimento = 0 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def listar_servidores_licenciados(limit: int = 50):
    db = await get_conn()
    cursor = await db.execute("SELECT * FROM assinaturas ORDER BY ativado_em DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ─── Loja: configuração de pagamento por servidor ──────────────────────────

async def obter_config_loja(guild_id: int) -> dict | None:
    db = await get_conn()
    cursor = await db.execute("SELECT * FROM config_loja WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def definir_config_loja(guild_id: int, chave_pix: str, tipo_chave: str, nome_recebedor: str, cidade: str = "Sao Paulo"):
    db = await get_conn()
    await db.execute(
        "INSERT INTO config_loja (guild_id, chave_pix, tipo_chave, nome_recebedor, cidade, atualizado_em) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "chave_pix = excluded.chave_pix, tipo_chave = excluded.tipo_chave, "
        "nome_recebedor = excluded.nome_recebedor, cidade = excluded.cidade, "
        "atualizado_em = CURRENT_TIMESTAMP",
        (guild_id, chave_pix, tipo_chave, nome_recebedor, cidade),
    )
    await db.commit()


# ─── Loja: produtos ─────────────────────────────────────────────────────────

async def criar_produto(guild_id: int, nome: str, descricao: str, preco: float, tipo_entrega: str, imagem_url: str = None) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO produtos (guild_id, nome, descricao, preco, tipo_entrega, imagem_url) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, nome, descricao, preco, tipo_entrega, imagem_url),
    )
    await db.commit()
    return cursor.lastrowid


async def editar_produto(produto_id: int, **campos):
    """Atualiza só os campos passados (nome, descricao, preco, tipo_entrega, imagem_url, ativo)."""
    permitidos = {"nome", "descricao", "preco", "tipo_entrega", "imagem_url", "ativo"}
    campos = {k: v for k, v in campos.items() if k in permitidos and v is not None}
    if not campos:
        return
    db = await get_conn()
    set_clause = ", ".join(f"{k} = ?" for k in campos)
    await db.execute(f"UPDATE produtos SET {set_clause} WHERE id = ?", (*campos.values(), produto_id))
    await db.commit()


async def remover_produto(produto_id: int):
    """Soft delete — mantém o histórico de pedidos antigos íntegro (não
    apaga a linha, só tira do catálogo visível)."""
    db = await get_conn()
    await db.execute("UPDATE produtos SET ativo = 0 WHERE id = ?", (produto_id,))
    await db.commit()


async def obter_produto(produto_id: int) -> dict | None:
    db = await get_conn()
    cursor = await db.execute("SELECT * FROM produtos WHERE id = ?", (produto_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_produtos(guild_id: int, apenas_ativos: bool = True) -> list[dict]:
    db = await get_conn()
    query = "SELECT * FROM produtos WHERE guild_id = ?"
    if apenas_ativos:
        query += " AND ativo = 1"
    query += " ORDER BY nome"
    cursor = await db.execute(query, (guild_id,))
    rows = await cursor.fetchall()
    produtos = [dict(r) for r in rows]
    for p in produtos:
        p["estoque_disponivel"] = await contar_estoque_disponivel(p["id"])
    return produtos


# ─── Loja: estoque (só usado por produtos de entrega AUTOMATICA) ───────────

async def adicionar_estoque_itens(produto_id: int, itens: list[str], permitir_duplicados: bool = False) -> dict:
    """Adiciona vários itens de estoque de uma vez (ex: colar 20 chaves,
    uma por linha, ou vir de um arquivo .txt).

    Por padrão NÃO deixa entrar item repetido — nem repetido dentro da
    própria lista, nem igual a um que já existe no estoque desse produto
    (inclusive os já entregues: reenviar a mesma key/conta vendida por
    engano é o erro clássico de quem sobe estoque em massa). Produtos onde
    o conteúdo é intencionalmente igual pra todo mundo (ex: um link fixo)
    usam permitir_duplicados=True.

    Retorna {"adicionados": n, "duplicados": n, "invalidos": n}, onde
    "invalidos" são itens grandes demais pra caber numa mensagem."""
    validos, invalidos = separar_validos([i.strip() for i in itens if i and i.strip()])
    if not validos:
        return {"adicionados": 0, "duplicados": 0, "invalidos": invalidos}

    async with _estoque_lock:
        db = await get_conn()
        novos = validos
        if not permitir_duplicados:
            cursor = await db.execute("SELECT conteudo FROM estoque_itens WHERE produto_id = ?", (produto_id,))
            ja_existentes = {row[0] for row in await cursor.fetchall()}
            novos = []
            vistos = set()
            for item in validos:
                if item in ja_existentes or item in vistos:
                    continue
                vistos.add(item)
                novos.append(item)

        if novos:
            await db.executemany(
                "INSERT INTO estoque_itens (produto_id, conteudo) VALUES (?, ?)",
                [(produto_id, item) for item in novos],
            )
            await db.commit()

    return {
        "adicionados": len(novos),
        "duplicados": len(validos) - len(novos),
        "invalidos": invalidos,
    }


async def limpar_estoque_disponivel(produto_id: int) -> int:
    """Apaga só os itens AINDA NÃO entregues nem reservados. O histórico
    do que já foi vendido nunca é tocado. Retorna quantos foram apagados."""
    db = await get_conn()
    cursor = await db.execute(
        "DELETE FROM estoque_itens WHERE produto_id = ? AND entregue = 0 AND reservado_pedido_id IS NULL",
        (produto_id,),
    )
    await db.commit()
    return cursor.rowcount


async def contar_estoque_disponivel(produto_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM estoque_itens WHERE produto_id = ? AND entregue = 0 AND reservado_pedido_id IS NULL",
        (produto_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def _reservar_e_entregar_estoque(produto_id: int, pedido_id: int, quantidade: int) -> list[str]:
    """Reserva ATOMICAMENTE até `quantidade` itens disponíveis do estoque
    desse produto e já marca como entregues pra esse pedido — usa o
    padrão UPDATE ... WHERE id IN (SELECT ... LIMIT n) pra evitar dois
    pedidos simultâneos levando o mesmo item (mesma lógica de corrida
    resolvida em database.ativar_chave). Retorna o conteúdo dos itens
    conseguidos — pode vir com MENOS itens que `quantidade` se o
    estoque não tiver o suficiente (quem chamar decide o que fazer com
    a diferença faltante, ex: cair pro fluxo manual)."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE estoque_itens SET entregue = 1, reservado_pedido_id = ?, entregue_em = CURRENT_TIMESTAMP "
        "WHERE id IN ("
        "  SELECT id FROM estoque_itens "
        "  WHERE produto_id = ? AND entregue = 0 AND reservado_pedido_id IS NULL "
        "  LIMIT ?"
        ") "
        "RETURNING conteudo",
        (pedido_id, produto_id, quantidade),
    )
    rows = await cursor.fetchall()
    await db.commit()
    return [r[0] for r in rows]


# ─── Loja: carrinho ─────────────────────────────────────────────────────────

async def adicionar_ao_carrinho(guild_id: int, user_id: int, produto_id: int, quantidade: int = 1):
    db = await get_conn()
    await db.execute(
        "INSERT INTO carrinho_itens (guild_id, user_id, produto_id, quantidade) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, produto_id) DO UPDATE SET quantidade = quantidade + excluded.quantidade",
        (guild_id, user_id, produto_id, quantidade),
    )
    await db.commit()


async def definir_quantidade_carrinho(guild_id: int, user_id: int, produto_id: int, quantidade: int):
    db = await get_conn()
    if quantidade <= 0:
        await db.execute(
            "DELETE FROM carrinho_itens WHERE guild_id = ? AND user_id = ? AND produto_id = ?",
            (guild_id, user_id, produto_id),
        )
    else:
        await db.execute(
            "UPDATE carrinho_itens SET quantidade = ? WHERE guild_id = ? AND user_id = ? AND produto_id = ?",
            (quantidade, guild_id, user_id, produto_id),
        )
    await db.commit()


async def remover_do_carrinho(guild_id: int, user_id: int, produto_id: int):
    db = await get_conn()
    await db.execute(
        "DELETE FROM carrinho_itens WHERE guild_id = ? AND user_id = ? AND produto_id = ?",
        (guild_id, user_id, produto_id),
    )
    await db.commit()


async def obter_carrinho(guild_id: int, user_id: int) -> list[dict]:
    """Carrinho já juntado com os dados atuais do produto (nome/preço
    exibidos sempre refletem o catálogo de agora — o preço só \"congela\"
    no pedido, na hora do checkout)."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT c.produto_id, c.quantidade, p.nome, p.preco, p.tipo_entrega, p.ativo "
        "FROM carrinho_itens c JOIN produtos p ON p.id = c.produto_id "
        "WHERE c.guild_id = ? AND c.user_id = ? ORDER BY c.adicionado_em",
        (guild_id, user_id),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def limpar_carrinho(guild_id: int, user_id: int):
    db = await get_conn()
    await db.execute("DELETE FROM carrinho_itens WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    await db.execute("DELETE FROM carrinho_cupom WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    await db.commit()


# ─── Loja: cupons ───────────────────────────────────────────────────────────

_REGEX_CODIGO_CUPOM = re.compile(r"^[A-Z0-9_-]{2,32}$")


def normalizar_codigo_cupom(codigo: str) -> str:
    """Cupom não diferencia maiúscula/minúscula nem espaço: 'Black Friday'
    e 'BLACKFRIDAY' são o mesmo código."""
    return "".join((codigo or "").split()).upper()


async def criar_cupom(
    guild_id: int,
    codigo: str,
    tipo: str,
    valor: float,
    criado_por: int,
    valor_minimo: float | None = None,
    max_usos: int | None = None,
    max_por_usuario: int | None = None,
    produto_id: int | None = None,
    dias_validade: int | None = None,
) -> int:
    """Cria um cupom. Levanta ValueError com mensagem pronta pra mostrar ao
    admin se algum parâmetro for inválido ou o código já existir."""
    codigo = normalizar_codigo_cupom(codigo)
    if not _REGEX_CODIGO_CUPOM.match(codigo):
        raise ValueError("O código precisa ter de 2 a 32 caracteres: só letras, números, `-` ou `_`.")
    if tipo not in {t.value for t in TipoCupom}:
        raise ValueError("Tipo de cupom inválido.")
    valor = round(float(valor), 2)
    if tipo == TipoCupom.PERCENTUAL.value and not (0 < valor < 100):
        raise ValueError("Cupom percentual precisa ser maior que 0% e menor que 100%.")
    if tipo == TipoCupom.FIXO.value and valor <= 0:
        raise ValueError("O desconto fixo precisa ser maior que R$ 0,00.")
    if valor_minimo is not None and valor_minimo <= 0:
        raise ValueError("O valor mínimo precisa ser maior que zero (ou deixe em branco).")
    if max_usos is not None and max_usos < 1:
        raise ValueError("O limite total de usos precisa ser pelo menos 1 (ou deixe em branco).")
    if max_por_usuario is not None and max_por_usuario < 1:
        raise ValueError("O limite por pessoa precisa ser pelo menos 1 (ou deixe em branco).")
    if dias_validade is not None and dias_validade < 1:
        raise ValueError("A validade precisa ser de pelo menos 1 dia (ou deixe em branco).")

    expira_em = None
    if dias_validade is not None:
        expira_em = (datetime.now(timezone.utc) + timedelta(days=dias_validade)).strftime("%Y-%m-%d %H:%M:%S")

    db = await get_conn()
    try:
        cursor = await db.execute(
            "INSERT INTO cupons (guild_id, codigo, tipo, valor, valor_minimo, max_usos, max_por_usuario, "
            "produto_id, expira_em, criado_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, codigo, tipo, valor, valor_minimo, max_usos, max_por_usuario, produto_id, expira_em, criado_por),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"Já existe um cupom `{codigo}` neste servidor.") from None
    await db.commit()
    return cursor.lastrowid


async def obter_cupom(guild_id: int, codigo: str) -> dict | None:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT *, (expira_em IS NOT NULL AND expira_em <= datetime('now')) AS expirado "
        "FROM cupons WHERE guild_id = ? AND codigo = ?",
        (guild_id, normalizar_codigo_cupom(codigo)),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_cupons(guild_id: int) -> list[dict]:
    """Todos os cupons do servidor, já com `usos` (quantos pedidos abertos
    ou concluídos estão usando o cupom agora)."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT c.*, (c.expira_em IS NOT NULL AND c.expira_em <= datetime('now')) AS expirado, "
        "(SELECT COUNT(*) FROM cupom_usos u WHERE u.cupom_id = c.id) AS usos "
        "FROM cupons c WHERE c.guild_id = ? ORDER BY c.ativo DESC, c.criado_em DESC",
        (guild_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def definir_cupom_ativo(guild_id: int, codigo: str, ativo: bool) -> bool:
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE cupons SET ativo = ? WHERE guild_id = ? AND codigo = ?",
        (1 if ativo else 0, guild_id, normalizar_codigo_cupom(codigo)),
    )
    await db.commit()
    return cursor.rowcount == 1


async def calcular_cupom(guild_id: int, user_id: int, codigo: str, itens: list[dict]) -> dict:
    """Valida um cupom contra o carrinho e calcula o desconto.

    Retorna {"ok": True, "cupom": {...}, "desconto": x, "subtotal": s, "total": t}
    ou {"ok": False, "erro": "mensagem pronta pro cliente"}.

    O total NUNCA cai abaixo de VALOR_MINIMO_PEDIDO: Pix sem valor deixa o
    cliente pagar quanto quiser, então cupom não pode zerar o pedido."""
    subtotal = round(sum(i["preco"] * i["quantidade"] for i in itens), 2)
    cupom = await obter_cupom(guild_id, codigo)

    if not cupom or not cupom["ativo"]:
        return {"ok": False, "erro": "Cupom inválido ou indisponível."}
    if cupom["expirado"]:
        return {"ok": False, "erro": "Esse cupom expirou."}

    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(user_id = ?), 0) FROM cupom_usos WHERE cupom_id = ?",
        (user_id, cupom["id"]),
    )
    usos_total, usos_usuario = await cursor.fetchone()
    if cupom["max_usos"] is not None and usos_total >= cupom["max_usos"]:
        return {"ok": False, "erro": "Esse cupom esgotou (limite de usos atingido)."}
    if cupom["max_por_usuario"] is not None and usos_usuario >= cupom["max_por_usuario"]:
        return {"ok": False, "erro": "Você já usou esse cupom o máximo de vezes permitido."}

    if cupom["produto_id"] is not None:
        base_itens = [i for i in itens if i["produto_id"] == cupom["produto_id"]]
        if not base_itens:
            produto = await obter_produto(cupom["produto_id"])
            nome = f"**{produto['nome']}**" if produto else f"o produto #{cupom['produto_id']}"
            return {"ok": False, "erro": f"Esse cupom só vale para {nome}, que não está no seu carrinho."}
        base = round(sum(i["preco"] * i["quantidade"] for i in base_itens), 2)
    else:
        base = subtotal

    if cupom["valor_minimo"] is not None and base < cupom["valor_minimo"]:
        return {"ok": False, "erro": f"Esse cupom exige um mínimo de R$ {cupom['valor_minimo']:.2f} em compras."}

    if cupom["tipo"] == TipoCupom.PERCENTUAL.value:
        desconto = round(base * cupom["valor"] / 100, 2)
    else:
        desconto = min(cupom["valor"], base)

    desconto = min(desconto, round(subtotal - VALOR_MINIMO_PEDIDO, 2))
    if desconto <= 0:
        return {"ok": False, "erro": "Esse cupom não gera desconto nesse carrinho."}

    return {
        "ok": True,
        "cupom": cupom,
        "desconto": desconto,
        "subtotal": subtotal,
        "total": round(subtotal - desconto, 2),
    }


async def definir_cupom_carrinho(guild_id: int, user_id: int, codigo: str):
    db = await get_conn()
    await db.execute(
        "INSERT INTO carrinho_cupom (guild_id, user_id, codigo) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET codigo = excluded.codigo",
        (guild_id, user_id, normalizar_codigo_cupom(codigo)),
    )
    await db.commit()


async def remover_cupom_carrinho(guild_id: int, user_id: int):
    db = await get_conn()
    await db.execute("DELETE FROM carrinho_cupom WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    await db.commit()


async def obter_cupom_carrinho(guild_id: int, user_id: int) -> str | None:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT codigo FROM carrinho_cupom WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def resumo_carrinho(guild_id: int, user_id: int) -> dict:
    """Carrinho + cupom já calculados, numa chamada só — é o que a tela do
    carrinho e o checkout usam, pra os dois sempre mostrarem o mesmo total.

    Se o cupom guardado no carrinho deixou de valer (expirou, esgotou, saiu
    o produto dele do carrinho...), ele é removido e o motivo vai em
    `aviso_cupom`, pra a tela avisar o cliente em vez de sumir calada."""
    itens = await obter_carrinho(guild_id, user_id)
    subtotal = round(sum(i["preco"] * i["quantidade"] for i in itens), 2)
    resumo = {
        "itens": itens, "subtotal": subtotal, "desconto": 0.0, "total": subtotal,
        "cupom": None, "aviso_cupom": None,
    }
    codigo = await obter_cupom_carrinho(guild_id, user_id)
    if codigo and itens:
        calc = await calcular_cupom(guild_id, user_id, codigo, itens)
        if calc["ok"]:
            resumo.update(cupom=calc["cupom"], desconto=calc["desconto"], total=calc["total"])
        else:
            await remover_cupom_carrinho(guild_id, user_id)
            resumo["aviso_cupom"] = calc["erro"]
    return resumo


# ─── Loja: pedidos ──────────────────────────────────────────────────────────

async def criar_pedido_do_carrinho(guild_id: int, user_id: int) -> tuple[dict | None, str | None]:
    """Congela o carrinho atual num pedido (snapshot de nome/preço/cupom) e
    já esvazia o carrinho. Retorna (pedido, None) se deu certo, ou
    (None, "mensagem pronta pro cliente") se algo impede o checkout:
    carrinho vazio, produto que saiu do catálogo, estoque insuficiente,
    cupom que deixou de valer ou excesso de pedidos pendentes.

    Roda dentro de _pedidos_lock: a checagem do cupom (limite de usos) e o
    INSERT do pedido + uso do cupom não podem ser intercalados por outro
    checkout simultâneo — senão dois clientes passam do limite do mesmo cupom."""
    async with _pedidos_lock:
        resumo = await resumo_carrinho(guild_id, user_id)
        itens = resumo["itens"]

        if not itens:
            return None, "Seu carrinho está vazio. Volte em `/loja` e adicione produtos."
        if any(not item["ativo"] for item in itens):
            return None, "Algum item do carrinho saiu do catálogo. Remova-o e tente de novo."
        if resumo["aviso_cupom"]:
            return None, f"Seu cupom não vale mais: {resumo['aviso_cupom']} Revise o carrinho e finalize de novo."
        if await contar_pedidos_pendentes(guild_id, user_id) >= MAX_PEDIDOS_PENDENTES_POR_USUARIO:
            return None, (
                f"Você já tem {MAX_PEDIDOS_PENDENTES_POR_USUARIO} pedidos aguardando pagamento. "
                "Pague um deles (ou espere expirar) antes de abrir outro."
            )

        for item in itens:
            if item["tipo_entrega"] == TipoEntrega.AUTOMATICA.value:
                disponivel = await contar_estoque_disponivel(item["produto_id"])
                if disponivel < item["quantidade"]:
                    return None, (
                        f"Estoque insuficiente de **{item['nome']}** "
                        f"(você pediu {item['quantidade']}, restam {disponivel}). Ajuste a quantidade no carrinho."
                    )

        itens_snapshot = [
            {
                "produto_id": item["produto_id"],
                "nome": item["nome"],
                "preco": item["preco"],
                "quantidade": item["quantidade"],
                "tipo_entrega": item["tipo_entrega"],
            }
            for item in itens
        ]
        cupom = resumo["cupom"]

        db = await get_conn()
        cursor = await db.execute(
            "INSERT INTO pedidos (guild_id, user_id, itens_json, valor_total, valor_bruto, desconto, cupom_codigo, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, user_id, json.dumps(itens_snapshot, ensure_ascii=False),
                resumo["total"], resumo["subtotal"], resumo["desconto"],
                cupom["codigo"] if cupom else None,
                StatusPedido.AGUARDANDO_PAGAMENTO.value,
            ),
        )
        pedido_id = cursor.lastrowid
        if cupom:
            await db.execute(
                "INSERT INTO cupom_usos (cupom_id, pedido_id, user_id, desconto) VALUES (?, ?, ?, ?)",
                (cupom["id"], pedido_id, user_id, resumo["desconto"]),
            )
        await db.commit()
        await limpar_carrinho(guild_id, user_id)

    return await obter_pedido(pedido_id), None


async def contar_pedidos_pendentes(guild_id: int, user_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM pedidos WHERE guild_id = ? AND user_id = ? AND status = ?",
        (guild_id, user_id, StatusPedido.AGUARDANDO_PAGAMENTO.value),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def obter_pedido(pedido_id: int) -> dict | None:
    db = await get_conn()
    cursor = await db.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    pedido = dict(row)
    pedido["itens"] = json.loads(pedido["itens_json"])
    pedido["entrega"] = json.loads(pedido["entrega_json"]) if pedido["entrega_json"] else None
    return pedido


async def salvar_payload_pix(pedido_id: int, payload_pix: str):
    db = await get_conn()
    await db.execute("UPDATE pedidos SET payload_pix = ? WHERE id = ?", (payload_pix, pedido_id))
    await db.commit()


async def marcar_pedido_pago(pedido_id: int, aprovado_por: int | None = None) -> bool:
    """Transição atômica AGUARDANDO -> PAGO. Retorna True só pra QUEM
    conseguiu fazer a transição. Se dois admins rodarem /pedido aprovar
    ao mesmo tempo, só um recebe True — e só esse pode disparar a entrega
    (senão o estoque seria entregue duas vezes)."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE pedidos SET status = ?, pago_em = CURRENT_TIMESTAMP, aprovado_por = ? WHERE id = ? AND status = ?",
        (StatusPedido.PAGO.value, aprovado_por, pedido_id, StatusPedido.AGUARDANDO_PAGAMENTO.value),
    )
    await db.commit()
    return cursor.rowcount == 1


async def processar_entrega_automatica(pedido_id: int) -> dict:
    """Chamado logo depois de marcar_pedido_pago(). Pra cada item do
    pedido com tipo_entrega AUTOMATICA, tenta puxar do estoque na hora.
    Itens MANUAIS, ou automáticos sem estoque suficiente, entram na
    lista `pendente_manual` pro admin resolver num ticket.

    Retorna {"entregue_auto": [...], "pendente_manual": [...]} e já
    salva esse resultado em `entrega_json`. Se não sobrou nada pendente,
    o pedido já sai daqui como ENTREGUE; senão fica PAGO mesmo (entrega
    parcial/manual ainda rolando).
    """
    pedido = await obter_pedido(pedido_id)
    if not pedido:
        return {"entregue_auto": [], "pendente_manual": []}
    # Idempotente: se já processou a entrega desse pedido (ou ele nem está
    # PAGO), devolve o que já foi feito em vez de puxar estoque de novo.
    if pedido["entrega"] is not None:
        return pedido["entrega"]
    if pedido["status"] != StatusPedido.PAGO.value:
        return {"entregue_auto": [], "pendente_manual": []}

    entregue_auto = []
    pendente_manual = []

    for item in pedido["itens"]:
        if item["tipo_entrega"] == TipoEntrega.AUTOMATICA.value:
            conteudos = await _reservar_e_entregar_estoque(item["produto_id"], pedido_id, item["quantidade"])
            if conteudos:
                entregue_auto.append({"nome": item["nome"], "itens": conteudos})
            faltando = item["quantidade"] - len(conteudos)
            if faltando > 0:
                pendente_manual.append({"nome": item["nome"], "quantidade": faltando, "motivo": "sem_estoque"})
        else:
            pendente_manual.append({"nome": item["nome"], "quantidade": item["quantidade"], "motivo": "manual"})

    resultado = {"entregue_auto": entregue_auto, "pendente_manual": pendente_manual}

    db = await get_conn()
    if pendente_manual:
        await db.execute("UPDATE pedidos SET entrega_json = ? WHERE id = ?", (json.dumps(resultado, ensure_ascii=False), pedido_id))
    else:
        await db.execute(
            "UPDATE pedidos SET entrega_json = ?, status = ?, entregue_em = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(resultado, ensure_ascii=False), StatusPedido.ENTREGUE.value, pedido_id),
        )
    await db.commit()
    return resultado


async def marcar_pedido_entregue_manual(pedido_id: int, canal_ticket_id: int = None):
    db = await get_conn()
    if canal_ticket_id is not None:
        await db.execute("UPDATE pedidos SET canal_ticket_id = ? WHERE id = ?", (canal_ticket_id, pedido_id))
    await db.execute(
        "UPDATE pedidos SET status = ?, entregue_em = CURRENT_TIMESTAMP WHERE id = ? AND status = ?",
        (StatusPedido.ENTREGUE.value, pedido_id, StatusPedido.PAGO.value),
    )
    await db.commit()


async def cancelar_pedido(pedido_id: int) -> bool:
    """Cancela um pedido que ainda está AGUARDANDO PAGAMENTO e devolve o uso
    do cupom (se tinha). Retorna False se o pedido não estava mais nesse
    estado — pedido já pago/entregue NUNCA pode ser cancelado por aqui,
    senão o estoque já entregue voltaria pra prateleira e seria vendido
    de novo (item entregue duas vezes)."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE pedidos SET status = ? WHERE id = ? AND status = ?",
        (StatusPedido.CANCELADO.value, pedido_id, StatusPedido.AGUARDANDO_PAGAMENTO.value),
    )
    if cursor.rowcount != 1:
        await db.commit()
        return False
    await db.execute("DELETE FROM cupom_usos WHERE pedido_id = ?", (pedido_id,))
    await db.commit()
    return True


async def expirar_pedidos_antigos(minutos: int) -> int:
    """Marca como EXPIRADO (e devolve o uso do cupom de) pedidos que ficaram
    tempo demais aguardando pagamento — pra rodar num loop periódico."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT id FROM pedidos WHERE status = ? AND criado_em <= datetime('now', ?)",
        (StatusPedido.AGUARDANDO_PAGAMENTO.value, f"-{int(minutos)} minutes"),
    )
    ids = [row[0] for row in await cursor.fetchall()]
    expirados = 0
    for pedido_id in ids:
        # O WHERE status = 'aguardando' garante que um pedido aprovado no
        # meio do caminho (admin foi mais rápido que a task) não seja derrubado.
        upd = await db.execute(
            "UPDATE pedidos SET status = ? WHERE id = ? AND status = ?",
            (StatusPedido.EXPIRADO.value, pedido_id, StatusPedido.AGUARDANDO_PAGAMENTO.value),
        )
        if upd.rowcount == 1:
            await db.execute("DELETE FROM cupom_usos WHERE pedido_id = ?", (pedido_id,))
            expirados += 1
    await db.commit()
    return expirados


async def listar_pedidos_guild(guild_id: int, limit: int = 50) -> list[dict]:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT * FROM pedidos WHERE guild_id = ? ORDER BY criado_em DESC LIMIT ?", (guild_id, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def listar_pedidos_usuario(guild_id: int, user_id: int, limit: int = 15) -> list[dict]:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT * FROM pedidos WHERE guild_id = ? AND user_id = ? ORDER BY criado_em DESC, id DESC LIMIT ?",
        (guild_id, user_id, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ─── Util genérica (usada pelo avatar_manager e por futuros painéis) ──────

def limpar_url_imagem(valor):
    """Valida e limpa uma URL de imagem colada pelo usuário."""
    import re
    caracteres_invisiveis = re.compile(r'[\u200b\u200c\u200d\u2060\ufeff\u00a0]')
    if not valor:
        return None
    limpo = caracteres_invisiveis.sub('', str(valor)).strip()
    if not limpo:
        return None
    if not limpo.lower().startswith(('http://', 'https://')):
        return None
    return limpo
