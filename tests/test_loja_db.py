"""
Testes da camada de banco da loja (database.py + estoque_utils.py).

Rodar (da raiz do projeto):
    python tests/test_loja_db.py

Não precisa de Discord nem de token: usa um banco temporário. Cobre as
regras que mais dão dor de cabeça em bot de venda: cupom estourando limite,
estoque entregue duas vezes, pedido cancelado depois de entregue, Pix zerado.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

try:
    import aiosqlite  # noqa: F401
except ModuleNotFoundError:  # ambiente sem internet: usa o substituto local
    sys.path.insert(0, os.path.join(RAIZ, "tests"))
    from _aiosqlite_shim import shim
    sys.modules["aiosqlite"] = shim

import database as db  # noqa: E402
import estoque_utils  # noqa: E402

G = 111  # guild de teste
U1, U2, U3 = 1001, 1002, 1003
ADMIN = 9

_passou = 0


def ok(cond, msg):
    global _passou
    if not cond:
        raise AssertionError(f"FALHOU: {msg}")
    _passou += 1
    print(f"  ✓ {msg}")


async def novo_banco():
    await db.fechar_conn()
    fd, caminho = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(caminho)
    db.DB_PATH = caminho
    await db.setup_db()
    return caminho


async def produto_auto(nome="Chave", preco=50.0, estoque=("K1", "K2", "K3", "K4", "K5")):
    pid = await db.criar_produto(G, nome, "desc", preco, "automatica")
    if estoque:
        await db.adicionar_estoque_itens(pid, list(estoque))
    return pid


async def carrinho(user, produto_id, qtd=1):
    await db.adicionar_ao_carrinho(G, user, produto_id, qtd)


# ─────────────────────────────────────────────────────────────────────────


async def teste_migracao():
    print("\n[migração de banco antigo]")
    await db.fechar_conn()
    fd, caminho = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(caminho)
    conn.execute(
        "CREATE TABLE pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
        "user_id INTEGER NOT NULL, itens_json TEXT NOT NULL, valor_total REAL NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'aguardando_pagamento', payload_pix TEXT, entrega_json TEXT, "
        "canal_ticket_id INTEGER, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP, pago_em TIMESTAMP, entregue_em TIMESTAMP)"
    )
    conn.execute("INSERT INTO pedidos (guild_id, user_id, itens_json, valor_total) VALUES (1, 2, '[]', 10)")
    conn.commit()
    conn.close()
    db.DB_PATH = caminho
    await db.setup_db()
    await db.setup_db()  # rodar 2x não pode quebrar
    d = await db.get_conn()
    cur = await d.execute("PRAGMA table_info(pedidos)")
    colunas = {r[1] for r in await cur.fetchall()}
    ok({"valor_bruto", "desconto", "cupom_codigo", "aprovado_por"} <= colunas, "colunas novas criadas em banco antigo")
    cur = await d.execute("SELECT valor_total FROM pedidos")
    ok((await cur.fetchone())[0] == 10, "pedido antigo continua intacto")


async def teste_estoque():
    print("\n[estoque]")
    await novo_banco()
    pid = await db.criar_produto(G, "Conta", None, 20.0, "automatica")

    r = await db.adicionar_estoque_itens(pid, ["A", "B", "A", "  B  ", "", "C"])
    ok(r == {"adicionados": 3, "duplicados": 2, "invalidos": 0}, f"dedupe dentro da lista {r}")
    r = await db.adicionar_estoque_itens(pid, ["C", "D"])
    ok(r["adicionados"] == 1 and r["duplicados"] == 1, "dedupe contra o que já existe")
    r = await db.adicionar_estoque_itens(pid, ["X" * 5000, "E"])
    ok(r["invalidos"] == 1 and r["adicionados"] == 1, "item gigante é rejeitado")
    r = await db.adicionar_estoque_itens(pid, ["LINK", "LINK"], permitir_duplicados=True)
    ok(r["adicionados"] == 2, "permitir_duplicados aceita repetidos")
    ok(await db.contar_estoque_disponivel(pid) == 7, "contagem de estoque disponível = 7")

    # concorrência: dois uploads iguais ao mesmo tempo não duplicam
    pid2 = await db.criar_produto(G, "Outro", None, 5.0, "automatica")
    r1, r2 = await asyncio.gather(
        db.adicionar_estoque_itens(pid2, ["Z1", "Z2"]),
        db.adicionar_estoque_itens(pid2, ["Z1", "Z2"]),
    )
    ok(r1["adicionados"] + r2["adicionados"] == 2, "dois uploads simultâneos iguais não duplicam")

    # limpar só o disponível
    pedido_id = await pedido_pago_auto(pid, U1, 2)
    apagados = await db.limpar_estoque_disponivel(pid)
    ok(apagados == 5, f"limpar apaga só o disponível ({apagados})")
    cur = await (await db.get_conn()).execute("SELECT COUNT(*) FROM estoque_itens WHERE produto_id=? AND entregue=1", (pid,))
    ok((await cur.fetchone())[0] == 2, "itens já entregues ficam intactos")
    ok(pedido_id is not None, "pedido de referência existe")


async def pedido_pago_auto(pid, user, qtd=1):
    await carrinho(user, pid, qtd)
    pedido, erro = await db.criar_pedido_do_carrinho(G, user)
    assert pedido, erro
    assert await db.marcar_pedido_pago(pedido["id"], ADMIN)
    await db.processar_entrega_automatica(pedido["id"])
    return pedido["id"]


async def teste_utils_arquivo():
    print("\n[leitura de arquivo .txt]")
    ok(estoque_utils.itens_do_arquivo("\ufeffa\r\nb\r\n\r\n c \n".encode("utf-8")) == ["a", "b", "c"], "BOM + CRLF + linhas vazias")
    ok(estoque_utils.decodificar_arquivo("ação".encode("latin-1")) == "ação", "fallback latin-1")
    for ruim, nome in [(b"\x00\x01\x02binario", "binário"), (b"  \n\n ", "vazio")]:
        try:
            estoque_utils.itens_do_arquivo(ruim)
            ok(False, f"deveria rejeitar {nome}")
        except estoque_utils.ArquivoEstoqueInvalido:
            ok(True, f"rejeita arquivo {nome}")
    try:
        estoque_utils.itens_do_arquivo(("x\n" * 6000).encode())
        ok(False, "deveria rejeitar excesso de linhas")
    except estoque_utils.ArquivoEstoqueInvalido:
        ok(True, "rejeita arquivo com linhas demais")


async def teste_validacao_cupom():
    print("\n[criação de cupom]")
    await novo_banco()
    invalidos = [
        dict(codigo="X", tipo="percentual", valor=10),            # código curto
        dict(codigo="OK!", tipo="percentual", valor=10),          # caractere inválido
        dict(codigo="AAA", tipo="percentual", valor=100),         # 100% = pedido de graça
        dict(codigo="AAA", tipo="percentual", valor=0),
        dict(codigo="AAA", tipo="fixo", valor=-5),
        dict(codigo="AAA", tipo="xyz", valor=5),
        dict(codigo="AAA", tipo="fixo", valor=5, max_usos=0),
        dict(codigo="AAA", tipo="fixo", valor=5, dias_validade=0),
        dict(codigo="AAA", tipo="fixo", valor=5, valor_minimo=-1),
    ]
    for kw in invalidos:
        try:
            await db.criar_cupom(G, criado_por=ADMIN, **kw)
            ok(False, f"deveria rejeitar {kw}")
        except ValueError:
            ok(True, f"rejeita {kw}")
    await db.criar_cupom(G, "Black Friday", "percentual", 10, ADMIN)
    try:
        await db.criar_cupom(G, "blackfriday", "fixo", 5, ADMIN)
        ok(False, "código duplicado deveria falhar")
    except ValueError:
        ok(True, "código duplicado (case/espaço-insensível) rejeitado")
    await db.criar_cupom(222, "BLACKFRIDAY", "fixo", 5, ADMIN)
    ok(True, "mesmo código em outro servidor é permitido")


async def teste_calculo_cupom():
    print("\n[cálculo de desconto]")
    await novo_banco()
    p1 = await produto_auto("A", 100.0)
    p2 = await produto_auto("B", 40.0)
    await carrinho(U1, p1, 1)
    await carrinho(U1, p2, 1)
    itens = await db.obter_carrinho(G, U1)  # subtotal 140

    await db.criar_cupom(G, "DEZ", "percentual", 10, ADMIN)
    r = await db.calcular_cupom(G, U1, "dez", itens)
    ok(r["ok"] and r["desconto"] == 14.0 and r["total"] == 126.0, "10% de 140 = 14 (código minúsculo funciona)")

    await db.criar_cupom(G, "CINCO", "fixo", 5, ADMIN)
    r = await db.calcular_cupom(G, U1, "CINCO", itens)
    ok(r["ok"] and r["desconto"] == 5.0 and r["total"] == 135.0, "fixo R$5")

    await db.criar_cupom(G, "GIGANTE", "fixo", 9999, ADMIN)
    r = await db.calcular_cupom(G, U1, "GIGANTE", itens)
    ok(r["ok"] and r["total"] == 0.01, f"fixo enorme nunca zera o Pix (total={r.get('total')})")

    await db.criar_cupom(G, "SOA", "percentual", 50, ADMIN, produto_id=p1)
    r = await db.calcular_cupom(G, U1, "SOA", itens)
    ok(r["ok"] and r["desconto"] == 50.0, "cupom de produto desconta só sobre aquele produto (50% de 100)")
    await db.limpar_carrinho(G, U1)
    await carrinho(U1, p2, 1)
    r = await db.calcular_cupom(G, U1, "SOA", await db.obter_carrinho(G, U1))
    ok(not r["ok"] and "só vale" in r["erro"], "cupom de produto recusado sem o produto no carrinho")

    await db.criar_cupom(G, "MIN200", "fixo", 10, ADMIN, valor_minimo=200)
    r = await db.calcular_cupom(G, U1, "MIN200", await db.obter_carrinho(G, U1))
    ok(not r["ok"] and "mínimo" in r["erro"], "valor mínimo respeitado")

    r = await db.calcular_cupom(G, U1, "NAOEXISTE", itens)
    ok(not r["ok"], "cupom inexistente")

    await db.criar_cupom(G, "VELHO", "fixo", 5, ADMIN, dias_validade=1)
    d = await db.get_conn()
    await d.execute("UPDATE cupons SET expira_em = datetime('now', '-1 minutes') WHERE codigo = 'VELHO'")
    await d.commit()
    r = await db.calcular_cupom(G, U1, "VELHO", itens)
    ok(not r["ok"] and "expirou" in r["erro"], "cupom expirado")

    await db.definir_cupom_ativo(G, "DEZ", False)
    r = await db.calcular_cupom(G, U1, "DEZ", itens)
    ok(not r["ok"], "cupom desativado")
    ok(not await db.definir_cupom_ativo(G, "NAOEXISTE", True), "ativar cupom inexistente retorna False")


async def teste_fluxo_cupom_pedido():
    print("\n[cupom no pedido: limites, cancelamento, expiração]")
    await novo_banco()
    pid = await produto_auto("Chave", 50.0)
    await db.criar_cupom(G, "UNICO", "percentual", 20, ADMIN, max_usos=1)

    await carrinho(U1, pid, 1)
    await db.definir_cupom_carrinho(G, U1, "unico")
    resumo = await db.resumo_carrinho(G, U1)
    ok(resumo["total"] == 40.0 and resumo["desconto"] == 10.0, "resumo do carrinho já mostra o desconto")

    pedido, erro = await db.criar_pedido_do_carrinho(G, U1)
    ok(pedido and pedido["valor_total"] == 40.0 and pedido["desconto"] == 10.0 and pedido["cupom_codigo"] == "UNICO",
       "pedido criado com valor descontado e snapshot do cupom")
    ok((await db.obter_cupom_carrinho(G, U1)) is None, "cupom sai do carrinho depois do checkout")

    # outro cliente tenta o mesmo cupom (limite 1): cupom já está "gasto" pelo pedido pendente
    await carrinho(U2, pid, 1)
    await db.definir_cupom_carrinho(G, U2, "UNICO")
    resumo2 = await db.resumo_carrinho(G, U2)
    ok(resumo2["cupom"] is None and "esgotou" in (resumo2["aviso_cupom"] or ""),
       "uso é reservado no checkout (não só na entrega): 2º cliente já vê esgotado")
    ok((await db.obter_cupom_carrinho(G, U2)) is None, "cupom inválido é removido do carrinho")

    # cancelar devolve o uso
    ok(await db.cancelar_pedido(pedido["id"]) is True, "cancelar pedido pendente funciona")
    await db.definir_cupom_carrinho(G, U2, "UNICO")
    ped2, erro = await db.criar_pedido_do_carrinho(G, U2)
    ok(ped2 and ped2["desconto"] == 10.0, "depois de cancelar, o cupom volta a ficar disponível")

    # expirar também devolve
    d = await db.get_conn()
    await d.execute("UPDATE pedidos SET criado_em = datetime('now', '-2 hours') WHERE id = ?", (ped2["id"],))
    await d.commit()
    ok(await db.expirar_pedidos_antigos(30) == 1, "pedido velho expira")
    cur = await d.execute("SELECT COUNT(*) FROM cupom_usos")
    ok((await cur.fetchone())[0] == 0, "expirar devolve o uso do cupom")

    # cupom que vale no carrinho mas morre antes do checkout: NÃO cria pedido com preço diferente do que o cliente viu
    await db.criar_cupom(G, "MORRE", "fixo", 5, ADMIN)
    await carrinho(U3, pid, 1)
    await db.definir_cupom_carrinho(G, U3, "MORRE")
    await db.definir_cupom_ativo(G, "MORRE", False)
    ped3, erro3 = await db.criar_pedido_do_carrinho(G, U3)
    ok(ped3 is None and "não vale mais" in erro3, "cupom que morreu no meio do caminho bloqueia o checkout com aviso")
    ped3, erro3 = await db.criar_pedido_do_carrinho(G, U3)
    ok(ped3 and ped3["desconto"] == 0 and ped3["valor_total"] == 50.0, "2ª tentativa (cupom já removido) segue com preço cheio")


async def teste_corrida_cupom():
    print("\n[corrida: dois checkouts simultâneos no mesmo cupom]")
    await novo_banco()
    pid = await produto_auto("Chave", 50.0)
    await db.criar_cupom(G, "SO1", "fixo", 5, ADMIN, max_usos=1)
    for u in (U1, U2):
        await carrinho(u, pid, 1)
        await db.definir_cupom_carrinho(G, u, "SO1")
    (p1, e1), (p2, e2) = await asyncio.gather(
        db.criar_pedido_do_carrinho(G, U1), db.criar_pedido_do_carrinho(G, U2)
    )
    com_desconto = [p for p in (p1, p2) if p and p["desconto"] > 0]
    ok(len(com_desconto) == 1, "só UM dos dois leva o cupom de 1 uso")


async def teste_pedidos_limites_e_estoque():
    print("\n[limites de pedido e estoque no checkout]")
    await novo_banco()
    pid = await produto_auto("Chave", 10.0, estoque=("A", "B"))

    await carrinho(U1, pid, 3)
    ped, erro = await db.criar_pedido_do_carrinho(G, U1)
    ok(ped is None and "Estoque insuficiente" in erro, "bloqueia checkout com quantidade acima do estoque")
    await db.definir_quantidade_carrinho(G, U1, pid, 1)

    ped, erro = await db.criar_pedido_do_carrinho(G, U1)
    ok(ped is not None, "checkout dentro do estoque")
    ped, erro = await db.criar_pedido_do_carrinho(G, U1)
    ok(ped is None and "vazio" in erro, "carrinho vazio após checkout")

    # limite de pendentes (produto manual pra não depender de estoque)
    manual = await db.criar_produto(G, "Servico", None, 30.0, "manual")
    criados = 1  # já tem 1 pendente
    for _ in range(5):
        await carrinho(U1, manual, 1)
        ped, erro = await db.criar_pedido_do_carrinho(G, U1)
        if ped:
            criados += 1
    ok(criados == 3, f"máximo de 3 pedidos pendentes por cliente (criados={criados})")
    ok(await db.contar_pedidos_pendentes(G, U1) == 3, "contagem de pendentes")
    ok(len(await db.listar_pedidos_usuario(G, U1)) == 3, "listar_pedidos_usuario filtra por cliente")
    ok(len(await db.listar_pedidos_usuario(G, U2)) == 0, "outro cliente não vê esses pedidos")


async def teste_aprovacao_entrega():
    print("\n[aprovação e entrega: sem entrega dupla]")
    await novo_banco()
    pid = await produto_auto("Chave", 10.0, estoque=("K1", "K2", "K3", "K4"))
    await carrinho(U1, pid, 2)
    ped, _ = await db.criar_pedido_do_carrinho(G, U1)

    r1, r2 = await asyncio.gather(db.marcar_pedido_pago(ped["id"], 7), db.marcar_pedido_pago(ped["id"], 8))
    ok(sorted([r1, r2]) == [False, True], "dois admins aprovando juntos: só um consegue")

    e1 = await db.processar_entrega_automatica(ped["id"])
    e2 = await db.processar_entrega_automatica(ped["id"])
    ok(e1 == e2 and len(e1["entregue_auto"][0]["itens"]) == 2, "entrega processada de novo devolve o mesmo resultado")
    ok(await db.contar_estoque_disponivel(pid) == 2, "estoque só baixou 2, não 4")
    final = await db.obter_pedido(ped["id"])
    ok(final["status"] == "entregue" and final["aprovado_por"] in (7, 8), "pedido entregue, com quem aprovou registrado")

    ok(await db.cancelar_pedido(ped["id"]) is False, "NÃO cancela pedido já entregue")
    ok(await db.contar_estoque_disponivel(pid) == 2, "itens entregues não voltam pro estoque")
    ok((await db.obter_pedido(ped["id"]))["status"] == "entregue", "status continua entregue")

    # entrega parcial (estoque acabou entre checkout e aprovação) -> cai pro manual
    pid2 = await produto_auto("Raro", 10.0, estoque=("R1", "R2"))
    await carrinho(U2, pid2, 2)
    ped2, _ = await db.criar_pedido_do_carrinho(G, U2)
    await db.limpar_estoque_disponivel(pid2)
    await db.adicionar_estoque_itens(pid2, ["R1"])
    await db.marcar_pedido_pago(ped2["id"], 7)
    res = await db.processar_entrega_automatica(ped2["id"])
    ok(len(res["entregue_auto"][0]["itens"]) == 1 and res["pendente_manual"][0]["quantidade"] == 1,
       "estoque insuficiente na aprovação: entrega 1 e deixa 1 pendente manual")
    ok((await db.obter_pedido(ped2["id"]))["status"] == "pago", "pedido com pendência continua 'pago'")
    await db.marcar_pedido_entregue_manual(ped2["id"])
    ok((await db.obter_pedido(ped2["id"]))["status"] == "entregue", "entrega manual conclui")

    # entregar_manual não ressuscita pedido cancelado
    await carrinho(U3, pid2, 1)  # sem estoque -> manual
    man = await db.criar_produto(G, "Man", None, 5.0, "manual")
    await carrinho(U3, man, 1)
    await db.limpar_carrinho(G, U3)
    await carrinho(U3, man, 1)
    ped3, _ = await db.criar_pedido_do_carrinho(G, U3)
    await db.cancelar_pedido(ped3["id"])
    await db.marcar_pedido_entregue_manual(ped3["id"])
    ok((await db.obter_pedido(ped3["id"]))["status"] == "cancelado", "pedido cancelado não vira 'entregue' por engano")


async def main():
    testes = [
        teste_migracao, teste_estoque, teste_utils_arquivo, teste_validacao_cupom,
        teste_calculo_cupom, teste_fluxo_cupom_pedido, teste_corrida_cupom,
        teste_pedidos_limites_e_estoque, teste_aprovacao_entrega,
    ]
    for t in testes:
        await t()
    await db.fechar_conn()
    print(f"\n✅ {_passou} verificações passaram.")


if __name__ == "__main__":
    asyncio.run(main())
