"""
constants.py — FFZ Vendas V2

Constantes compartilhadas do sistema de loja: tipos de entrega e paleta
de cores. Mantém o mesmo estilo visual "card branco" já usado no
sistema de licença (licenca.py) — sem accent_color colorido, só tons de
cinza/branco.
"""

from enum import Enum


class TipoEntrega(str, Enum):
    """Como um produto é entregue pro cliente depois do pagamento
    confirmado.

    AUTOMATICA — o produto tem um estoque de itens (ex: chaves, contas,
    códigos) cadastrados previamente. Assim que o pagamento é aprovado,
    o bot puxa um item disponível do estoque e entrega na hora, sem
    intervenção humana. Se o estoque estiver vazio, cai automaticamente
    pro fluxo MANUAL (nunca vende algo que não tem pra entregar).

    MANUAL — não tem estoque pré-cadastrado (ex: serviço, produto sob
    encomenda). Depois do pagamento aprovado, abre um ticket/thread pra
    um admin entregar na mão.
    """
    AUTOMATICA = "automatica"
    MANUAL = "manual"


TIPO_ENTREGA_LABEL = {
    TipoEntrega.AUTOMATICA: "Automática",
    TipoEntrega.MANUAL: "Manual",
}

TIPO_ENTREGA_EMOJI = {
    TipoEntrega.AUTOMATICA: "⚡",
    TipoEntrega.MANUAL: "🎫",
}


class StatusPedido(str, Enum):
    """Ciclo de vida de um pedido, do carrinho até a entrega."""
    AGUARDANDO_PAGAMENTO = "aguardando_pagamento"
    PAGO = "pago"
    ENTREGUE = "entregue"
    CANCELADO = "cancelado"
    EXPIRADO = "expirado"


STATUS_PEDIDO_LABEL = {
    StatusPedido.AGUARDANDO_PAGAMENTO: "Aguardando pagamento",
    StatusPedido.PAGO: "Pago — aguardando entrega",
    StatusPedido.ENTREGUE: "Entregue",
    StatusPedido.CANCELADO: "Cancelado",
    StatusPedido.EXPIRADO: "Expirado",
}

STATUS_PEDIDO_EMOJI = {
    StatusPedido.AGUARDANDO_PAGAMENTO: "🕓",
    StatusPedido.PAGO: "💳",
    StatusPedido.ENTREGUE: "✅",
    StatusPedido.CANCELADO: "🚫",
    StatusPedido.EXPIRADO: "⌛",
}

# ─── Paleta (mesmo espírito neutro de licenca.py: CORES_PLANO) ─────────────

COR_LOJA = 0xE0E0E0
COR_SUCESSO = 0xFFFFFF
COR_AVISO = 0xD9D9D9
COR_ERRO = 0xB0B0B0

# Prazo em minutos que um pedido "aguardando pagamento" fica de pé antes
# de expirar sozinho e devolver o item pro estoque (evita reservar
# estoque pra sempre em carrinho abandonado).
MINUTOS_EXPIRAR_PEDIDO = 30

# Quantos produtos aparecem por página no catálogo do painel da loja.
PRODUTOS_POR_PAGINA = 5

# Quantos pedidos "aguardando pagamento" o mesmo cliente pode ter abertos
# ao mesmo tempo (evita spam de Pix/pedido fantasma enchendo a fila do admin).
MAX_PEDIDOS_PENDENTES_POR_USUARIO = 3

# Menor total que um pedido pode ter depois de cupom. NUNCA pode ser zero:
# um Pix Copia e Cola sem o campo de valor deixa o cliente digitar qualquer
# quantia (ver pix_utils.gerar_payload_pix), ou seja, pedido de graça.
VALOR_MINIMO_PEDIDO = 0.01

# Limites do upload de estoque em massa (/produto estoque_arquivo).
MAX_BYTES_ARQUIVO_ESTOQUE = 1_000_000
MAX_LINHAS_ESTOQUE = 5000
MAX_TAMANHO_ITEM_ESTOQUE = 1800  # cabe numa mensagem do Discord (limite 2000) com folga


class TipoCupom(str, Enum):
    """PERCENTUAL — valor é a % de desconto (ex: 10 = 10%).
    FIXO — valor é um desconto em reais (ex: 5 = R$ 5,00 de desconto)."""
    PERCENTUAL = "percentual"
    FIXO = "fixo"


TIPO_CUPOM_LABEL = {
    TipoCupom.PERCENTUAL: "Percentual",
    TipoCupom.FIXO: "Valor fixo",
}
