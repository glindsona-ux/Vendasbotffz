"""
gateways/__init__.py — registro central dos gateways de pagamento
automático disponíveis. Pra adicionar um gateway novo: criar o arquivo
`gateways/nome.py` com uma classe que herda de GatewayPagamento (ver
base.py) e registrar aqui embaixo. Nada em cogs/ ou database.py precisa
mudar.
"""

from .base import CampoConfig, GatewayPagamento, ResultadoCobranca
from .livepix import GatewayLivePix
from .mercadopago import GatewayMercadoPago
from .pagbank import GatewayPagBank

GATEWAYS: dict[str, type[GatewayPagamento]] = {
    GatewayMercadoPago.NOME: GatewayMercadoPago,
    GatewayLivePix.NOME: GatewayLivePix,
    GatewayPagBank.NOME: GatewayPagBank,
}


def obter_classe_gateway(nome: str) -> type[GatewayPagamento] | None:
    return GATEWAYS.get(nome)


def instanciar_gateway(nome: str, credenciais: dict) -> GatewayPagamento | None:
    classe = obter_classe_gateway(nome)
    if classe is None:
        return None
    return classe(credenciais)


def listar_gateways() -> list[type[GatewayPagamento]]:
    return list(GATEWAYS.values())
