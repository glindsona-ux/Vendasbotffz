import asyncio, os, sys, tempfile
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "tests"))
try:
    import aiosqlite
except ModuleNotFoundError:
    from _aiosqlite_shim import shim
    sys.modules["aiosqlite"] = shim

os.environ["CHAVE_CRIPTOGRAFIA"] = "teste-chave-fernet-so-pra-validar-123"
os.environ["LICENCA_PEPPER"] = "pepper-teste"

import database as db

async def main():
    fd, caminho = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = caminho
    await db.setup_db()

    GUILD = 111
    # ── Gateways ──
    await db.definir_gateway_config(GUILD, "mercadopago", {"access_token": "APP_USR-segredo-123"})
    creds = await db.obter_gateway_config(GUILD, "mercadopago")
    assert creds == {"access_token": "APP_USR-segredo-123"}, creds
    print("✓ gateway salvo e descriptografado corretamente")

    # a credencial no banco cru NÃO pode estar em texto puro
    conn = await db.get_conn()
    cursor = await conn.execute("SELECT credenciais FROM gateways_config WHERE guild_id=? AND gateway=?", (GUILD, "mercadopago"))
    row = await cursor.fetchone()
    assert "APP_USR-segredo-123" not in row["credenciais"], "credencial vazou em texto puro no banco!"
    print("✓ credencial fica criptografada no banco (não aparece em texto puro)")

    ativos = await db.listar_gateways_configurados(GUILD)
    assert ativos == ["mercadopago"], ativos
    print("✓ listar_gateways_configurados")

    await db.definir_gateway_padrao(GUILD, "mercadopago")
    cfg = await db.obter_config_loja(GUILD)
    assert cfg["gateway_padrao"] == "mercadopago"
    print("✓ gateway padrão definido")

    await db.remover_gateway_config(GUILD, "mercadopago")
    assert await db.obter_gateway_config(GUILD, "mercadopago") is None
    cfg = await db.obter_config_loja(GUILD)
    assert cfg["gateway_padrao"] is None, "gateway_padrao deveria ter sido limpo ao remover o gateway"
    print("✓ remover gateway limpa também o padrão")

    # ── Vitrines ──
    pid = await db.criar_produto(GUILD, "Conta Premium", "desc", 29.9, "manual")
    vid, erro = await db.criar_vitrine(GUILD, "Promo Natal!", "🎄 Promoção de Natal", "descrição", None, 0xFF0000)
    assert erro is None and vid
    print("✓ vitrine criada, slug normalizado:", (await db.obter_vitrine(vid))["slug"])

    vid2, erro2 = await db.criar_vitrine(GUILD, "promo-natal", "Outra", None, None, None)
    assert erro2 is not None, "deveria rejeitar slug duplicado"
    print("✓ slug duplicado rejeitado:", erro2)

    await db.adicionar_produto_vitrine(vid, pid)
    produtos = await db.produtos_da_vitrine(vid)
    assert len(produtos) == 1 and produtos[0]["id"] == pid
    print("✓ produto adicionado à vitrine")

    await db.remover_produto_vitrine(vid, pid)
    assert await db.produtos_da_vitrine(vid) == []
    print("✓ produto removido da vitrine")

    vitrines = await db.listar_vitrines(GUILD)
    assert len(vitrines) == 1
    print("✓ listar_vitrines")

    v = await db.obter_vitrine_por_slug(GUILD, "PROMO-NATAL")
    assert v and v["id"] == vid, "busca por slug deveria ser case-insensitive"
    print("✓ obter_vitrine_por_slug case-insensitive")

    await db.remover_vitrine(vid)
    assert await db.obter_vitrine(vid) is None
    print("✓ remover_vitrine")

    # ── pedido <-> gateway/thread ──
    await db.adicionar_ao_carrinho(GUILD, 999, pid)
    pedido, err = await db.criar_pedido_do_carrinho(GUILD, 999)
    assert pedido and err is None
    await db.definir_gateway_pedido(pedido["id"], "mercadopago", "charge-abc-123")
    await db.definir_thread_pedido(pedido["id"], 555555)
    encontrado = await db.obter_pedido_por_charge("mercadopago", "charge-abc-123")
    assert encontrado["id"] == pedido["id"] and encontrado["canal_thread_id"] == 555555
    print("✓ pedido rastreável por (gateway, charge_id) e liga ao tópico")

    os.remove(caminho)
    print("\n✅ Todas as verificações novas passaram.")

asyncio.run(main())
