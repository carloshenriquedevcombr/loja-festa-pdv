# Sistema Loja de Festa

Gestão para loja de aluguel e venda de roupas de festa: catálogo com código automático, pedidos
com provas e retirada, e livro caixa que não perde lançamento. Pensado para ser usado por pessoas
sem familiaridade com tecnologia, num tablet da loja.

**Estado:** Sprint 8 (veja abaixo o que mudou em cada sprint). Desde a Sprint 3, todos os botões da tela inicial funcionam, com visual em vidro fosco
no mesmo estilo do site (branco, preto e dourado), com contraste AAA e botões grandes:

| Tela | O que faz |
| --- | --- |
| Recebi dinheiro / Paguei algo | Valor, forma, maquininha e parcelas; tela de conferência; clique duplo não duplica |
| Caixa do dia | Entradas, saídas, dinheiro que deve estar na gaveta; "Desfazer" gera estorno com motivo |
| Cadastrar / Procurar roupa | Código automático (VES-0001…), busca sem acento e com erro de digitação, leitor de código de barras |
| Etiqueta | Código de barras Code128 para imprimir |
| Novo pedido | Aluguel, venda na hora e venda com ajuste; entrada mínima; bloqueio de datas |
| Pedido | Saldo, receber pagamento, etapas (provas, ajuste, retirada, devolução, limpeza), cancelar |
| Fila de costura / Limpeza | Cada cargo avança só as etapas dele |
| Configurações | Maquininhas (com cor e apelido) e regras de entrada, parcelas, juros e limites |
| Relatórios / Painel técnico | Resultado do mês por dia e por forma; auditoria para o Dev |
| Clientes | Cadastro com telefone, CPF, e-mail e autorização LGPD; histórico de pedidos; medidas da costureira |
| Agenda | Da contratação à devolução: 1ª prova, 2ª prova, retirada, devolução; lembrete no WhatsApp; remarcar; "faltou" |
| Equipe | Cadastrar funcionário com cargos (fixo ou freelance com prazo), trocar cargos, desativar, redefinir senha |
| Minha senha | Qualquer pessoa troca a própria senha |
| Site da loja (vitrine) | `/site`: menu por ocasião (Casamento › Madrinha, Festa › Debutante…), Masculino, Infantil, filtros de tamanho/cor/comprimento, "livres na data da festa", página da peça com galeria, pedir prova e WhatsApp |
| Ficha de medidas | Feminina (19 pontos) e masculina (11), com desenho de frente e costas numerado; o número acende ao tocar no campo; histórico com diferença da medição anterior; tamanho sugerido de terno; ficha para imprimir com colunas da 1ª e 2ª prova |
| Fotos das peças | Até 12 por peça, direto da câmera do celular; o sistema gira, reduz e apaga a localização da foto |
| Pedidos do site | Pedidos de prova chegam no sistema; a atendente chama no WhatsApp, marca o horário e vira cliente + Agenda |
| Link de pagamento | Mercado Pago (Pix e cartão): a Dona cola o token uma vez; pedido do WhatsApp fica guardado 48 h esperando o pagamento e vira Reservado sozinho |
| Imprimir / PDF | Comprovante do pedido, recibo, extrato do caixa por período e extrato da cliente: folha A4, térmica 80 mm ou 58 mm, e PDF |

## Testar no seu PC (Windows)

Precisa só do [Docker Desktop](https://www.docker.com/products/docker-desktop/) aberto.
Extraia o zip, entre na pasta e, no PowerShell:

```powershell
.\local.ps1            # sobe tudo, cria a loja na 1ª vez e abre http://localhost:8000
.\local.ps1 celular    # abre também no celular (mesmo Wi-Fi): mostra o endereço http://192.168.x.x:8000
.\local.ps1 usuario    # cria um funcionário para ver a tela de outro cargo
.\local.ps1 testes     # 280 testes (banco + app) + backup e restauração
.\local.ps1 parar      # desliga (dados guardados)   |   .\local.ps1 zerar  # apaga tudo
```

Se o Windows disser que a execução de scripts está desabilitada ou que o arquivo "não está assinado
digitalmente", use `.\local.cmd` no lugar de `.\local.ps1` (ex.: `.\local.cmd celular`), ou rode uma vez
`Get-ChildItem -Recurse | Unblock-File` dentro da pasta (tira a marca de "baixado da internet"). O ambiente local usa `docker-compose.local.yml`
e `.env.local`, separados da produção.

### Sem Docker (Python + PostgreSQL instalados no Windows)

1. Instale o **Python 3.12** ([python.org](https://www.python.org/downloads/)) marcando *Add python.exe to PATH*.
2. Instale o **PostgreSQL 16** ([EDB](https://www.enterprisedb.com/downloads/postgres-postgresql-downloads)): porta 5432,
   anote a senha do usuário `postgres`. O Stack Builder no fim pode ser pulado.
3. Na pasta do projeto, dê duplo clique em **`sem-docker.cmd`** (ou `.\sem-docker.ps1` no PowerShell).

Na 1ª vez ele cria o `.venv` e instala as bibliotecas, pede a senha do `postgres` (só para criar o banco `loja` e os
usuários `loja_owner`/`app_user`; não fica guardada), aplica as migrações, cria a loja e a conta da Dona e abre
`http://localhost:8000`. Nas outras vezes só liga. Deixe a janela aberta; **Ctrl+C** desliga.

```powershell
.\sem-docker.ps1 usuario   # cria um funcionário
.\sem-docker.ps1 parar     # desliga a fila de avisos, se ficou aberta
.\sem-docker.ps1 zerar     # apaga o banco "loja" deste PC
```

Senhas internas e chaves ficam em `.env.semdocker`; fotos em `dados-local\fotos`. Porta ocupada: troque `PORTA_LOCAL`
no `.env.semdocker`. PostgreSQL em outra porta: troque `PG_PORTA`. Os testes automáticos continuam pelo Docker
(`.\local.ps1 testes`).

## Site e pagamento online

* O site fica em `/site` (quem abre o endereço da loja sem estar logado cai nele). A equipe entra por
  "Área da equipe", no rodapé.
* Em **Site e pagamento online** a Dona/Gerente cuida do WhatsApp, Instagram, horário, ocasiões do
  menu, tipos de peça e liga o Mercado Pago colando o *Access Token* (passo a passo na própria tela).
  O token fica cifrado no banco; nem o desenvolvedor consegue lê-lo pela tela.
* Na VPS, preencha `SITE_URL` no `.env` (ex.: `https://www.minhaloja.com.br`) para o Mercado Pago avisar
  os pagamentos sozinho. No PC local não há aviso automático: use o botão **Conferir se pagou** no pedido.
* Aviso do Mercado Pago nunca é aceito "de olhos fechados": o sistema sempre pergunta ao Mercado Pago,
  com o token da loja, se o pagamento existe, de quanto foi e de qual link é.

## WhatsApp, avisos, contrato e segurança (Sprint 4)

* **WhatsApp da loja** (WPPConnect da VPS), conectado com **código de 8 letras** digitado no celular,
  sem QR code. Tudo sai por uma **fila** (tabela `mensagens`, cifrada) enviada pelo container `avisos`
  (`python -m app.trabalhador`): a tela nunca espera o WhatsApp e nada se perde se ele cair.
* **Avisos automáticos**: pedido novo (cliente e equipe), prova pedida no site, prova marcada, pagamento
  pelo link, etapas (ajuste → costureira, devolução → limpeza, pronto/retirou/devolveu → cliente),
  remarcação, cancelamento, lembrete da véspera, agenda e fila de costura às 8h, resumo do caixa no fim do dia.
  Cada funcionário escolhe o que recebe em **Equipe**. Cliente que responde **SAIR** para de receber.
* **Comandos** no WhatsApp da loja (só números da equipe): `#caixa #ontem #hoje #pedidos #site #ajuda`.
* **Chat do site**: a cliente conversa numa janelinha do site; a equipe recebe e responde pelo WhatsApp
  (`#CÓDIGO resposta` ou respondendo a mensagem).
* **Contrato assinado na tela** (tablet/celular): código de verificação `XXXX-XXXX-XXXX` e hash; sai em
  toda impressão A4, no PDF, no e-mail (Gmail da loja) e na página da cliente `/site/meu-pedido/...`.
  Qualquer um confere em `/site/contrato`. O texto padrão está em `app/app/contrato.py` — **peça para um
  advogado revisar**.
* **Criptografia**: CPF, telefone, e-mail, endereço, observações, medidas, contratos, chat e fila com
  AES-256-GCM (`DADOS_CHAVE`). Só o nome fica em texto (para a busca). Busca por telefone/CPF usa índice cego.
* **Entrada**: senha de 8 a 32 com maiúscula, minúscula, número e símbolo (senhas antigas são obrigadas
  a trocar); código no WhatsApp para Dona/Dev/Financeiro e para aparelho novo; aviso de aparelho/IP
  novo e de entrada fora do horário; IP bloqueado após 12 erros; sessão sai sozinha após 30 min parada;
  botão "desconectar de todos os aparelhos"; limites contra robôs; Cloudflare (docs/VPS.md).
* **Redes sociais**: botão próprio na tela inicial; os links aparecem no rodapé do site.

## Marca, rodapé, impressos e parcelas

* **Logo e marca** (Site e pagamento online → Logo e marca): logo em PNG/JPG/WEBP (SVG recusado); o sistema tira o
  fundo branco, gera a versão preto e branco (impressora térmica) e os ícones (favicon, celular). A logo sai no site,
  no login, nos comprovantes (A4, cupom e PDF), na ficha de medidas, no checklist e nas etiquetas.
* **Rodapé do site** (… → Rodapé): várias lojas/endereços com "Ver no mapa" (Google Maps só carrega ao tocar) e
  "Como chegar"; redes sociais; Quem somos, Trocas e devoluções, Termos e Privacidade (textos prontos e editáveis,
  com histórico — revisar com advogado); "Receba nossas novidades" (cifrado, com consentimento); formas de pagamento.
* **Checklist do vestido** (no pedido): ficha 10 × 15 cm (ou A4) que fica no cabide da reserva até a entrega/devolução,
  com código de barras (o leitor em "Procurar roupa" abre o pedido) e QR code (abre o pedido no celular); os passos
  já feitos saem marcados com data e quem fez.
* **Etiquetas das peças**: para pendurar (50 × 90 mm), adesiva (60 × 40 mm) ou folha A4 com 21 (63,5 × 38,1 mm);
  uma peça ou várias de uma vez ("Procurar roupa" → Imprimir etiquetas), com SKU, código de barras, QR, tamanho e preço.
* **Texto do contrato** (Configurações → Contrato): editor com os campos, prévia e histórico; contrato assinado nunca muda.
* **Modelo de parcelas**: a simulação (entrada, vezes, sem juros, parcela mínima) aparece em Configurações e na página
  da peça no site, usando as mesmas regras do balcão.

## Entrada e cancelamento

* Em **Configurações → Regras de pagamento**, a entrada para reservar e o que a loja fica no cancelamento são
  definidos separadamente para **aluguel** e **venda com ajuste**, cada um em **% do total** ou **valor fixo (R$)**.
  Entrada fixa nunca passa do total; a multa nunca passa do que a cliente já pagou.
* Antes de cancelar, o pedido mostra a conta (quanto a loja fica e quanto devolve). "Cancelar sem multa" (culpa da
  loja) só Dona/Gerente, e fica na auditoria. Pedido feito fora da loja (link/WhatsApp/site) cancelado em até 7 dias
  devolve tudo (CDC art. 49), se a opção estiver ligada.
* A devolução é registrada no pedido (Pix ou dinheiro) e sai do caixa; o banco não deixa devolver mais que a regra.
* O contrato cita a regra de cancelamento do pedido (`{multa_cancelamento}`). Regras novas valem para pedidos novos.

## Regras da vitrine e devolução

* **Regras da vitrine** (botão na tela inicial da Dona/Gerente): a loja escolhe se a peça sai do estoque
  **do pagamento até a devolução** (padrão) ou **só nas datas do aluguel**; se peça alugada continua no site
  como "ocupada" ou some; se venda com ajuste some; antecedência da prova pelo site; horário do aviso de atraso.
  Vale para os pedidos novos; tudo fica na auditoria.
* No modo padrão a peça sai do estoque e do calendário do site no dia do pagamento/contrato e só volta quando
  a atendente aperta **Cliente devolveu** (botão que só aparece depois de **Cliente retirou**), mais os dias de limpeza.
* Passou da devolução: aviso na tela inicial, filtro **Devolução atrasada** em Pedidos, WhatsApp diário para a
  cliente e lista para a equipe, comando `#atrasos`.

## Caixa, fornecedores e crediário (Sprint 6)

* **Recebi dinheiro** tem uma busca no topo (nome, telefone, CPF, código da peça ou do comprovante): mostra os pedidos
  com valor a receber e a próxima parcela do crediário da cliente. Formas: dinheiro, Pix, débito, crédito, **boleto** e
  **carnê de financeira** (em quantas vezes).
* **Paguei algo**: dinheiro, Pix, boleto, carnê e **cartão de débito/crédito da loja** (sem maquininha, até 12x);
  **fornecedor**, tipo de despesa, nº da nota/boleto/recibo e **foto da nota** (tirada pelo celular; fica numa pasta
  privada, sem dados do celular, e só abre para quem mexe no caixa). **Fornecedores** tem botão próprio
  (cadastro com CPF/CNPJ conferido, Pix, categoria e tudo que já foi pago a ele).
* **Crediário da loja (carnê próprio)**: dentro do pedido, em *Receber*, a Dona/Gerente/Financeiro escolhe
  *Crediário da loja*, em até 12x, com o 1º vencimento em até 90 dias. A cliente precisa ter CPF. O pedido fica quitado
  e as parcelas ficam em **Crediário da loja**. O **carnê** sai para imprimir (3 por folha A4) com código de barras e
  **QR Code Pix copia e cola com o valor da parcela** (cadastre a chave Pix da loja na tela do Crediário).
  A parcela é recebida pelo valor exato; desfazer no Caixa do dia reabre a parcela; cancelar exige motivo e o que
  faltava volta para o pedido. WhatsApp avisa a cliente 2 dias antes, no dia e com 1/5/10/20/30 dias de atraso, e a
  equipe marcada em "pagamentos" recebe a lista das vencidas.
  **Juros e multa por atraso no crediário não são cobrados automaticamente** — combine com o contador/advogado
  antes de cobrar, e revise o contrato.
* **Botões de cada cargo** (Dona e Dev): tabela para esconder botões da tela inicial por cargo. Só esconde (deixa a
  tela mais simples); quem manda no acesso é o cargo em Equipe.
* **Endereços da loja**: botão na tela inicial que abre direto *Lojas e endereços* (rodapé do site, mapa).
* **QR da via da cliente**: leva para *Verificar pedido* no site já com o código, a situação e o histórico; funcionária
  que escaneia cai direto no pedido. Serve para pedidos antigos também (número de 8 letras do comprovante).

## Site: banners, avaliações, conta da cliente e sacola (Sprint 6)

* **Banners e destaques** (botão na tela inicial): promoções que giram no topo do site (foto ou cor, datas de início
  e fim, link só para páginas do site) e os 4 destaques com ícone antes do rodapé (Como funciona, Entrega e devolução,
  Perguntas frequentes, Como provar — os textos se editam em *Rodapé do site → Páginas*).
* **Avaliações**: depois da devolução (aluguel) ou da entrega (venda) a cliente recebe no WhatsApp um link assinado
  para dar estrelas e comentar. **Nada aparece no site sem a Dona/Gerente aprovar**; a loja pode responder. A nota do
  Google aparece junto (digitada ou buscada todo dia pela API — veja docs/VPS.md).
* **Conta da cliente** (*Entrar* no topo do site): código de 6 números no WhatsApp **ou** e-mail e senha. Telefone que
  já tem ficha na loja só vira conta depois do código (ninguém toma a ficha de outra pessoa). *Minha conta* mostra os
  pedidos, a situação, o link para verificar o contrato e as compras do site; troca de senha e pedido para apagar os
  dados (LGPD, a equipe é avisada).
* **Sacola** (ligar em *Sacola e entrega*): a cliente escolhe as datas do aluguel ou compra, escolhe **retirar na
  loja**, **entrega na cidade** (motoboy/aplicativo, nas cidades e valores que a Dona cadastra) ou **envio pelo
  Melhor Envio** (só compra), e paga **entrada ou tudo** no Mercado Pago (Pix ou cartão). A peça do aluguel fica
  segura enquanto o pagamento está aberto; pagou → o pedido entra sozinho em *Pedidos → Do site*, já no caixa, com o
  endereço de entrega para a equipe. Valor diferente ou recusa do caixa → fica "para conferir" e a equipe é avisada.
* **Dados de pagamento nunca são guardados**: cartão, Pix e boleto são digitados só na página do Mercado Pago; a loja
  guarda apenas o valor, a forma e o número do pagamento.
* **Tela inicial do sistema** em seções (Atendimento, Roupas e produção, Dinheiro, Site e loja online, Administração)
  com ícones desenhados nas cores da loja. O layout completo do modelo que você mandou vem quando chegar a imagem.
* Revise com o advogado: política de privacidade (conta e LGPD), trocas/devoluções e cancelamento de compras online
  (direito de arrependimento de 7 dias) e o texto do contrato para aluguéis feitos pelo site.

## Layout novo e "quem está online" (Sprint 7)

* **Menu lateral escuro** com ícones e seções (Atendimento, Roupas e produção, Dinheiro, Site e loja online,
  Administração); a tela aberta fica marcada em dourado. No celular o menu abre pelo botão ☰. Respeita o cargo e os
  botões escondidos pela Dona.
* **Barra do topo**: título, data, **busca rápida** (nome, telefone, CPF, código da peça ou do comprovante/QR — o
  código abre o pedido direto), quantos estão online, **tema claro/escuro** (guardado no aparelho) e Sair.
* **Painel** (tela inicial): números do dia em cartões coloridos (entrou, saiu, pedidos, entregar/receber hoje,
  atrasos, compras do site, parcelas vencidas, avaliações), botões grandes (Novo pedido, Recebi, Paguei), gráfico das
  entradas dos últimos 14 dias (com tabela), formas de pagamento do mês, agenda de hoje com calendário do mês,
  retiradas e devoluções próximas, e **Online agora**. Cada bloco só aparece para quem tem o cargo.
* **Online agora** (Dona, Gerente, Dev): quem da equipe está com o sistema aberto e em que tela, quais clientes com
  conta estão no site (e onde: sacola, peça, conta…) e quantos visitantes sem conta. Página própria que se atualiza
  a cada 30 s e comando **#online** no WhatsApp. Online = página aberta nos últimos 3 minutos; guarda só a área
  (ex.: "Pedidos"), nunca números ou dados. O sinal de presença não conta como uso: a saída automática por tempo
  parado continua valendo.

## Ajustes da Sprint 7b

* **CSS sempre atualizado**: CSS e JS vão com a versão no endereço (`app.css?v=…`). Quando o arquivo muda, o navegador
  busca o novo sozinho — acabou o "layout quebrado" por arquivo velho guardado no navegador.
* **Dicas ao parar o mouse** (ou ao focar com Tab): cada botão do menu e da tela inicial, os números do painel, a busca,
  o tema, o Sair e os campos dos formulários (valor, forma, NSU, parcelas, datas, CPF…) explicam o que fazem.
* **Símbolos das redes e dos cartões** no rodapé do site: em *Rodapé do site → Símbolos das redes e cartões* a loja envia
  o símbolo oficial de cada um (Instagram, WhatsApp, Visa, Pix…), baixado do kit de marca da empresa. Sem arquivo,
  aparece o nome. O sistema não desenha logotipos de terceiros.
* **Minha conta**: o cadastro fica ao lado do "Entrar", no mesmo modelo.

## Sprint 8

* **Pedido começa pela cliente**: "1. Cliente" no topo do novo pedido — procurar pelo nome, telefone ou CPF e escolher,
  ou escrever nome, sobrenome e telefone da cliente nova. Sem cliente, nem venda simples é guardada.
* **Carnê para imprimir**: A4 (3 por folha) ou **térmica 80 mm**; cada parcela com o seu **código** (13 números com dígito
  de conferência) em **código de barras** com os números embaixo **ou QR code**. O código na busca do topo (ou no leitor)
  abre a parcela para receber. Botões de imprimir no crediário e no pedido; no pedido, "📒 Parcelar no carnê da loja".
* **Recebi dinheiro com motivo**: sem pedido, só com a cliente + um **serviço cadastrado** (limpeza, conserto, taxa…)
  em **Serviços avulsos**. O banco recusa entrada avulsa sem serviço.
* **Link de pagamento pelo Pix da loja** (Inter, Itaú, qualquer banco) além do Mercado Pago: página com QR e copia-e-cola;
  a loja toca em "Pix caiu no banco: confirmar" e entra no caixa.
* **Mercado Pago ligado → sacola ligada**: os botões *Alugar* e *Comprar* aparecem nas peças com preço no site.
* **Comprar online com ajuste na loja**: a Dona define o valor em *Sacola e entrega*; a cliente escolhe sem ajuste ou com
  ajuste (+ valor) e a data em que precisa da peça; continua escolhendo retirada ou entrega. Vira pedido de venda com ajuste.
* **Melhor Envio pela tela**: token colado em *Sacola e entrega*, passo a passo e "Testar conexão".
* **Usuário Master**: acesso a tudo, sem restrição (só a Dona ou outro Master concede).
* **Reimprimir**: comprovante (A4/80/58/PDF), contrato, checklist, etiqueta, carnê, recibos e ficha de medidas num lugar só.
* **Cadastrar roupa** com vários tamanhos (XPP a EGG, 34 a 56, "outro") e a **ocasião principal** da peça.
* **Site**: rodapé no modelo (logo, sobre, redes, novidades · loja com mapa, institucional, ajuda, contato); início na ordem
  ocasião → como funciona → novidades; "Escolha pela ocasião" sem repetir a mesma peça; selo "Aluguel".
* **Menu lateral encolhe** no computador (só ícones) — botão no topo do menu.

## Segurança

Cada ataque (SQL Injection, força bruta, F12, XSS, CSRF, IDOR, roubo de sessão…) e como o sistema se
defende está em [docs/SEGURANCA.md](docs/SEGURANCA.md). Todos são tentados pelos testes automáticos.

## Impressora térmica

Na tela de imprimir, escolha **Térmica 80 mm** (ou 58 mm) e toque em **Imprimir**. Na janela do
navegador, escolha a impressora de cupom e deixe as margens em "Nenhuma". O botão **Baixar PDF**
gera o mesmo documento em PDF (a bobina sai numa tira só, do tamanho certo). Endereço, telefone,
CNPJ e o recado do fim do cupom ficam em **Configurações → Dados da loja nos comprovantes**.

## Estrutura

| Pasta | Conteúdo |
| --- | --- |
| `migrations/` | Esquema do banco em SQL, aplicado em ordem por `scripts/migrate.sh` |
| `db/` | Imagem do PostgreSQL 16 com locale pt-BR e as migrações embutidas |
| `app/` | App FastAPI: login, sessão, permissões, `/health`, CLI de administração |
| `backup/` | Backup diário, cópia externa com restic e teste de restauração |
| `tests/` | 50 testes das travas do banco + 230 testes do app de ponta a ponta |
| `docs/VPS.md` | Passo a passo de implantação na VPS |

## Onde ficam as regras

As regras que protegem dinheiro e estoque estão **no banco**, não só no código. Mesmo com um bug
no app, o PostgreSQL recusa: lançamento editado ou apagado, vestido reservado duas vezes nas mesmas
datas, reserva sem entrada mínima, retirada com saldo aberto, desconto sem aprovação, parcelamento
fora do crédito, dados de uma loja vistos por outra. O app conecta com um papel (`app_user`) que não
tem permissão de apagar nada.

## Rodar os testes do banco

Precisa de um PostgreSQL 16 com as extensões `pg_trgm`, `unaccent` e `btree_gist`:

```bash
docker build -t loja-db -f db/Dockerfile .
docker run -d --name pg -p 5432:5432 -e POSTGRES_PASSWORD=dev -e APP_DB_PASSWORD=dev loja-db
PGHOST=localhost PGUSER=postgres PGPASSWORD=dev python3 tests/test_banco.py
```

O teste cria e apaga um banco `loja_teste`. Ele se recusa a rodar em um banco com outro nome.

## Nova migração

Crie `migrations/0003_descricao.sql` (sem `BEGIN`/`COMMIT`: o script já abre a transação).
Tabelas novas recebem `SELECT` para o app automaticamente; `INSERT`/`UPDATE` e a política de RLS
por `loja_id` precisam ser declarados na própria migração.

## Decisões da Sprint 0

- **Nginx Proxy Manager** da VPS no lugar de um Nginx próprio.
- **Migrações em SQL puro com psql**, no lugar do Alembic: o esquema depende de triggers, RLS e
  restrições de exclusão, que ficam mais claros e testáveis em SQL.
- **psycopg sem ORM** por enquanto: poucas consultas, todas explícitas. Um ORM pode entrar quando
  as telas crescerem.
- **Banco numa rede Docker interna**, sem porta publicada: os outros serviços da VPS não o alcançam.
