# FFZ Vendas — atualização: cupons, estoque em massa e correções de segurança

## Novos comandos
| Comando | O que faz |
|---|---|
| `/cupom criar` | Cupom % ou valor fixo, com validade, limite total, limite por pessoa, valor mínimo e/ou só p/ 1 produto |
| `/cupom listar` · `ativar` · `desativar` | Gerencia os cupons e mostra quantos usos cada um teve |
| `/produto estoque_arquivo` | Sobe estoque de um `.txt` (1 item por linha, até 5000 linhas / 1 MB) |
| `/produto limpar_estoque` | Apaga só o estoque ainda não entregue (pede `confirmar: True`) |
| `/produto editar` | Muda nome, preço, descrição, imagem ou liga/desliga o produto |
| `/produto estoque` | Agora ignora itens repetidos (opção `permitir_repetidos` p/ desligar) |

No carrinho do cliente aparece o botão **🎟️ Usar cupom** (e **❌ Tirar cupom**). O Pix já sai com o valor descontado.

## Bugs corrigidos (todos cobertos por teste)
1. **`/pedido cancelar` em pedido já entregue devolvia a key pro estoque** → a mesma key podia ser vendida de novo. Agora só cancela pedido *aguardando pagamento*.
2. **Dois admins aprovando o mesmo pedido juntos entregavam o estoque em dobro.** Agora a aprovação é atômica; quem perde a corrida é avisado.
3. **Entrega repetida**: `processar_entrega_automatica` é idempotente.
4. **Pix de valor zero**: cupom nunca deixa o total abaixo de R$ 0,01 (QR sem valor = cliente paga o que quiser).
5. Cliente podia abrir pedidos pendentes sem limite → máx. 3 por pessoa.
6. Checkout agora bloqueia quantidade acima do estoque (antes o cliente pagava e caía pro manual).
7. `/meuspedidos` deixou de carregar 200 pedidos do servidor inteiro pra filtrar em Python.
8. `/pedido entregarmanual` não ressuscita mais pedido cancelado.

## Como o cupom evita abuso
- O uso é **reservado quando o pedido é criado** (não na entrega) e devolvido se o pedido for cancelado/expirar → não dá pra estourar o limite com vários carrinhos abertos.
- Se o cupom morrer entre o carrinho e o checkout, o pedido **não é criado** com preço diferente do que o cliente viu: ele recebe o aviso.
- Código não diferencia maiúscula/minúscula nem espaço.

## Banco de dados
Migração automática no primeiro start (colunas novas em `pedidos` + tabelas `cupons`, `cupom_usos`, `carrinho_cupom`). Pode subir por cima do banco atual — recomendo copiar o `ffz_vendas.db` antes.

## Testes
`python tests/test_loja_db.py` — 67 verificações, sem token e sem Discord (banco temporário).
