"""Botões da tela inicial: cada pessoa vê só os do próprio cargo, e a Dona/Dev podem esconder botões por cargo.

Esconder é só visual (deixa a tela mais simples). Quem manda no acesso continua sendo a permissão do cargo:
um botão escondido que a pessoa ainda pode usar abre pelo endereço; para tirar o acesso, mude o cargo em Equipe.
"""
# (permissões que liberam o botão, rótulo, ícone, endereço, estilo)
BOTOES = [
    (("caixa.entrada",),                       "Recebi dinheiro", "dinheiro_entra", "/caixa/receber", "entrada"),
    (("caixa.saida",),                         "Paguei algo",     "dinheiro_sai", "/caixa/pagar",   "saida"),
    (("pedidos.criar",),                       "Novo pedido",     "mais", "/pedidos/novo",  "destaque"),
    (("catalogo.ver",),                        "Procurar roupa",  "lupa", "/pecas",         "neutro"),
    (("catalogo.editar",),                     "Cadastrar roupa", "lapis", "/pecas/nova",    "neutro"),
    (("pedidos.ver",),                         "Pedidos",         "lista", "/pedidos",       "neutro"),
    (("pedidos.ver",),                         "Reimprimir",      "impressora", "/reimprimir",    "neutro"),
    (("agenda.ver",),                          "Agenda",          "calendario", "/agenda",        "neutro"),
    (("site.solicitacoes",),                   "Pedidos do site", "envelope", "/solicitacoes",  "neutro"),
    (("clientes.ver",),                        "Clientes",        "pessoas", "/clientes",      "neutro"),
    (("ajustes.ver",),                         "Fila de costura", "tesoura", "/costura",       "neutro"),
    (("limpeza.ver",),                         "Limpeza",         "limpeza", "/limpeza",       "neutro"),
    (("caixa.fechar", "relatorios.completo"),  "Caixa do dia",    "caixa", "/caixa",         "neutro"),
    (("caixa.entrada", "crediario.liberar"),   "Crediário da loja", "carne", "/crediario",   "neutro"),
    (("fornecedores.editar", "caixa.saida"),   "Fornecedores",    "etiqueta", "/fornecedores",  "neutro"),
    (("servicos.editar",),                     "Serviços avulsos", "tesoura", "/servicos",  "neutro"),
    (("relatorios.completo",),                 "Relatórios",      "grafico", "/relatorios",    "neutro"),
    (("usuarios.operacionais", "rh.funcionarios"), "Equipe",      "equipe", "/equipe",        "neutro"),
    (("config.ver",),                          "Configurações",   "engrenagem", "/config",        "neutro"),
    (("site.editar", "config.pagamento"),      "Site e pagamento online", "globo", "/config/site", "neutro"),
    (("site.editar",),                         "Endereços da loja", "mapa", "/config/rodape#unidades", "neutro"),
    (("site.editar",),                         "Banners e destaques", "vitrine", "/config/banners", "neutro"),
    (("avaliacoes.moderar",),                  "Avaliações",      "estrela", "/config/avaliacoes", "neutro"),
    (("entrega.config",),                      "Sacola e entrega", "caminhao", "/config/entrega", "neutro"),
    (("vitrine.regras",),                      "Regras da vitrine", "cabide", "/config/vitrine", "neutro"),
    (("site.editar",),                         "Redes sociais",   "coracao", "/config/redes",  "neutro"),
    (("whatsapp.config",),                     "WhatsApp e e-mail", "chat", "/config/whatsapp", "neutro"),
    (("presenca.ver",),                        "Online agora",    "online", "/online",    "neutro"),
    (("sistema.ver_tudo",),                    "Painel técnico",  "painel", "/sistema",       "neutro"),
    (("botoes.escolher",),                     "Botões de cada cargo", "botoes", "/config/botoes", "neutro"),
    ((),                                       "Minha senha",     "chave", "/minha-senha",   "neutro"),
]

# Explicação que aparece ao parar o mouse (ou focar) em cada botão do menu e da tela inicial
DICAS = {
    "/caixa/receber": "Lançar dinheiro que entrou: pagamento de pedido, parcela do crediário ou outro recebimento. Procure a cliente ou o pedido no topo da tela.",
    "/caixa/pagar": "Lançar uma despesa da loja: fornecedor, tipo de gasto, nº da nota e foto do comprovante.",
    "/pedidos/novo": "Começar um aluguel ou uma venda: escolha a peça, a cliente, as datas e a entrada.",
    "/pedidos": "Todos os pedidos: em andamento, para entregar hoje, atrasados, vindos do site. Clique num pedido para ver e avançar as etapas.",
    "/agenda": "Provas, retiradas e devoluções marcadas, dia a dia.",
    "/solicitacoes": "Pedidos de prova feitos pelo site esperando resposta da loja.",
    "/clientes": "Fichas das clientes: contato, medidas, pedidos e quanto falta pagar.",
    "/pecas": "Procurar uma roupa pelo nome, código, tamanho ou cor e ver se está livre.",
    "/pecas/nova": "Cadastrar uma roupa nova com preços, fotos e se aparece no site.",
    "/costura": "Peças que estão com a costureira para ajuste.",
    "/limpeza": "Peças devolvidas esperando lavanderia e as que já voltaram.",
    "/caixa": "Tudo o que entrou e saiu no dia, dinheiro na gaveta e desfazer lançamentos.",
    "/crediario": "Carnês da loja: parcelas em aberto, vencidas, receber parcela e imprimir o carnê com Pix.",
    "/fornecedores": "Cadastro de quem vende para a loja (costureira, lavanderia, tecidos) e o que já foi pago a cada um.",
    "/reimprimir": "Imprimir de novo comprovante, contrato, checklist, etiqueta, carnê, recibos e ficha de medidas.",
    "/servicos": "Serviços cobrados à parte (limpeza, conserto, taxas): o motivo de todo recebimento sem pedido.",
    "/relatorios": "Resumo do mês: faturamento, aluguéis, vendas e formas de pagamento.",
    "/equipe": "Funcionárias, cargos, acessos, aparelhos conectados e avisos no WhatsApp.",
    "/online": "Quem está com o sistema aberto agora e quais clientes estão no site.",
    "/config": "Regras de pagamento: entrada, parcelas, juros, cancelamento, maquininhas e contrato.",
    "/config/site": "Liga o site, ocasiões do catálogo e o pagamento online (Mercado Pago).",
    "/config/rodape": "Endereços das lojas com mapa, páginas do rodapé e formas de pagamento mostradas no site.",
    "/config/banners": "Banners de promoção do topo do site e os 4 destaques antes do rodapé.",
    "/config/avaliacoes": "Aprovar ou responder avaliações das clientes e a nota do Google.",
    "/config/entrega": "Liga a sacola do site, entrada ou total, cidades de entrega e frete Melhor Envio.",
    "/config/vitrine": "Quando a peça sai do estoque e do calendário do site, e o horário do aviso de atraso.",
    "/config/redes": "Links do Instagram, Facebook, TikTok e outras redes que aparecem no site.",
    "/config/whatsapp": "Conexão do WhatsApp da loja, e-mail e horário de funcionamento.",
    "/sistema": "Painel técnico: saúde do sistema, fila de mensagens e segurança.",
    "/config/botoes": "Escolher quais botões cada cargo vê na tela inicial e no menu.",
    "/minha-senha": "Trocar a sua senha de acesso.",
}


def dica(url: str) -> str:
    return DICAS.get(base(url), "")


# Seções da tela inicial (a ordem aqui é a ordem na tela)
SECOES = [
    ("Atendimento", ("/caixa/receber", "/caixa/pagar", "/pedidos/novo", "/pedidos", "/reimprimir", "/agenda", "/clientes",
                     "/solicitacoes")),
    ("Roupas e produção", ("/pecas", "/pecas/nova", "/costura", "/limpeza")),
    ("Dinheiro", ("/caixa", "/crediario", "/fornecedores", "/servicos", "/relatorios")),
    ("Site e loja online", ("/config/site", "/config/banners", "/config/avaliacoes", "/config/entrega", "/config/vitrine",
                            "/config/rodape", "/config/redes")),
    ("Administração", ("/equipe", "/online", "/config", "/config/whatsapp", "/config/botoes", "/sistema", "/minha-senha")),
]


def por_secao(botoes: list[tuple]) -> list[tuple[str, list]]:
    grupos = []
    for titulo, enderecos in SECOES:
        itens = [b for e in enderecos for b in botoes if base(b[3]) == e]
        if itens:
            grupos.append((titulo, itens))
    usados = {id(b) for _, itens in grupos for b in itens}
    resto = [b for b in botoes if id(b) not in usados]
    if resto:
        grupos.append(("Outros", resto))
    return grupos


# Nunca somem (senão a Dona se tranca para fora ou a pessoa não troca a senha)
FIXOS = {"/config/botoes", "/minha-senha"}
# Cargos da diretoria técnica que escolhem os botões: não aparecem na tabela
QUEM_ESCOLHE = ("DONO", "DESENVOLVEDOR", "MASTER")


def base(endereco: str) -> str:
    return endereco.split("#", 1)[0]


def enderecos() -> list[str]:
    return [base(b[3]) for b in BOTOES if base(b[3]) not in FIXOS]


def visiveis(cur, usuario: dict) -> list[tuple]:
    perms = usuario["permissoes"]
    lista = [b for b in BOTOES if not b[0] or any(p in perms for p in b[0])]
    cur.execute("""SELECT pa.codigo FROM usuario_papeis up JOIN papeis pa ON pa.id = up.papel_id
                    WHERE up.usuario_id = %s AND (up.valido_ate IS NULL OR up.valido_ate > now())""", (usuario["id"],))
    papeis = {r["codigo"] for r in cur.fetchall()}
    if not papeis or papeis & set(QUEM_ESCOLHE):
        return lista
    cur.execute("SELECT papel, endereco FROM botoes_ocultos WHERE papel = ANY(%s)", (list(papeis),))
    ocultos: dict[str, set] = {}
    for r in cur.fetchall():
        ocultos.setdefault(r["endereco"], set()).add(r["papel"])
    # Some só se TODOS os cargos da pessoa esconderem aquele botão
    return [b for b in lista if base(b[3]) in FIXOS or ocultos.get(base(b[3]), set()) != papeis]
