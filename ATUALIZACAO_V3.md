# FFZ Vendas — atualização: gateways automáticos, `/configurarloja` e vitrines públicas

Essa atualização entrega as três frentes que ficaram pendentes: arquitetura
de pagamento automático (com Mercado Pago já funcionando de verdade),
painel de admin único, e vitrine pública com tópico privado de checkout.
**Nada do que já existia foi removido** — `/loja`, `/carrinho`,
`/configurarpagamento`, `/produto ...` continuam funcionando exatamente
como antes.

## 1. Antes de subir: variáveis de ambiente novas

Adicione no `.env` (veja `.env.example` atualizado):

```
CHAVE_CRIPTOGRAFIA=<gere com o comando abaixo>
BASE_URL=https://seu-bot.onrender.com
```

Gerar a `CHAVE_CRIPTOGRAFIA`:
```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Sem ela o bot ainda sobe (usa uma chave-fallback com aviso no log), mas
**troque antes de configurar qualquer gateway de verdade** — é essa chave
que protege o access_token do Mercado Pago no banco.

`BASE_URL` é a URL pública do próprio bot (a do Render/Discloud). Sem ela,
os gateways automáticos ainda criam a cobrança, mas o webhook não tem
endereço de volta — os pedidos ficam dependendo de `/pedido aprovar` manual
mesmo.

Instale a dependência nova:
```
pip install -r requirements.txt
```
(adicionou só `cryptography`, pra criptografar as credenciais dos gateways.)

## 2. Gateway de pagamento — arquitetura em plugin

Pasta `gateways/` nova: `base.py` define a interface comum
(`criar_cobranca`, `verificar_pagamento`, `processar_webhook`) que qualquer
gateway implementa.

| Gateway | Status |
|---|---|
| **Mercado Pago** | ✅ 100% implementado — Pix real via API, com QR Code, copia-e-cola e confirmação automática por webhook |
| **LivePix** | 🔧 encaixe pronto — estrutura toda no lugar, só falta eu ter a chave de API real de vocês pra terminar a chamada de verdade (evita "inventar" um formato de resposta nunca testado) |
| **PagBank** | 🔧 encaixe pronto — mesma situação do LivePix |

O Pix **manual** (o que já existia) continua igual, sem mudanças — é o
fallback que sempre funciona, mesmo sem nenhum gateway configurado.

Assim que vocês tiverem token real de LivePix/PagBank, plugar é rápido:
preencher os 3 métodos em `gateways/livepix.py` / `gateways/pagbank.py`
seguindo o molde do `mercadopago.py` — nada mais no resto do bot precisa
mudar.

### Webhook

`bot.py` agora expõe `POST /webhook/<gateway>` (ex: `/webhook/mercadopago`).
O Mercado Pago já é configurado automaticamente pra chamar essa URL
(`notification_url`) toda vez que o servidor cria uma cobrança — não
precisa cadastrar nada manualmente no painel deles.

## 3. `/configurarloja` — painel único

Reúne num só comando:
- **Pix manual** — mesmo formulário do `/configurarpagamento` antigo
- **Gateways automáticos** — botão por gateway (Mercado Pago, LivePix,
  PagBank), cada um abre um formulário só com os campos daquele gateway;
  dá pra escolher qual é o **padrão** (o que aparece como botão "pagar
  automático" no checkout)
- Atalhos de referência pra `/produto ...` e `/vitrine ...`

`/configurarpagamento` continua existindo e faz a mesma coisa que o botão
"Pix manual" do painel novo — é só outro caminho pra mesma configuração.

## 4. Vitrines públicas + tópico privado

```
/vitrine criar nome titulo descricao banner_url cor_hex
/vitrine produto_add nome produto_id
/vitrine produto_remover nome produto_id
/vitrine listar
/vitrine remover nome
/vitrine postar nome        ← publica no canal atual
```

O post público mostra banner, título, descrição e cada produto com
thumbnail/preço/emoji de entrega, terminando num botão **🛒 Comprar**.

Quando alguém clica em Comprar:
1. O bot abre um **tópico privado** (só o cliente + a equipe veem)
2. Dentro, aparece um Select com os produtos daquela vitrine pra montar o
   pedido, com total atualizado a cada escolha
3. Botões de finalizar: **Pix manual** (sempre disponível) e, se o
   servidor tiver um gateway automático configurado como padrão, também
   **Pagar automático** — que já confirma sozinho via webhook

O `/loja` antigo (catálogo ephemeral direto) continua existindo em
paralelo — é útil pra quem não quer publicar vitrine nenhuma.

## 5. Banco de dados

Migração automática no primeiro start (tabelas novas `gateways_config`,
`vitrines`, `vitrine_produtos` + colunas novas em `pedidos` e
`config_loja`). Sobe por cima do banco atual sem apagar nada — mas, como
sempre, recomendo copiar o `ffz_vendas.db` antes de atualizar em produção.

## 6. Testes

```
python tests/test_loja_db.py              # 67 verificações antigas — todas passando
python tests/test_gateways_vitrines.py    # 13 verificações novas (gateway + vitrine) — todas passando
```
