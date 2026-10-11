"""Rodapé do site: lojas (endereços e mapa), páginas institucionais, novidades por e-mail/WhatsApp,
formas de pagamento. Tudo ajustado pela loja em Configurações > Rodapé do site."""
import hashlib
import re
from urllib.parse import quote, quote_plus
from uuid import UUID

import psycopg
from psycopg import sql
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import config, cripto, db
from ..auth import auditar, ip, requer
from ..pagamento import formulario
from ..web import CsrfInvalido, checar_csrf, mensagem_do_banco, tela

router = APIRouter()

FORMAS = ["Pix", "Dinheiro", "Cartão de débito", "Cartão de crédito", "Link de pagamento"]
BANDEIRAS = ["Visa", "Mastercard", "Elo", "American Express", "Hipercard", "Diners", "Discover", "Aura", "JCB"]
PAGINAS = {
    "quem-somos": "Quem somos",
    "trocas-e-devolucoes": "Trocas, devoluções e aluguel",
    "termos-de-uso": "Termos de uso",
    "politica-de-privacidade": "Política de privacidade",
    "como-funciona": "Como funciona",
    "entrega-e-devolucao": "Entrega e devolução",
    "perguntas-frequentes": "Perguntas frequentes",
    "como-provar": "Como provar",
}
PADRAO = {
    "quem-somos": """A {loja} é especialista em roupas de festa para aluguel e venda: vestidos para madrinhas, formandas,
convidadas, debutantes e noivas, e trajes masculinos.

Cada peça é conferida, ajustada pela nossa costureira e higienizada antes de ir para a cliente. Atendemos com hora
marcada para que a prova seja tranquila e com toda a atenção que o seu momento merece.

Venha provar: agende pelo site ou chame no WhatsApp.""",
    "trocas-e-devolucoes": """ALUGUEL
• A reserva é garantida com o pagamento da entrada; o restante é pago até a retirada.
• A peça é retirada na data combinada e devolvida até a data de devolução, no horário da loja.
• Não lave, não passe e não faça consertos: a limpeza é feita pela loja.
• Atraso, manchas que não saem, rasgos ou perda são cobrados conforme o contrato.

COMPRA
• Peças compradas podem ser trocadas em até 7 dias, sem uso e com etiqueta.
• Compras feitas pelo site ou WhatsApp (fora da loja) podem ser desistidas em até 7 dias após o recebimento
  (art. 49 do Código de Defesa do Consumidor).
• Peças ajustadas sob medida a pedido da cliente não têm troca por tamanho.

Dúvidas? Fale com a gente pelo WhatsApp.""",
    "termos-de-uso": """Ao usar este site você concorda com estes termos.

• As fotos são das peças reais da {loja}; cores podem variar conforme a tela.
• Preços e disponibilidade podem mudar sem aviso. A reserva só vale depois da confirmação da loja e do pagamento da entrada.
• A consulta de datas do site é uma orientação; a confirmação final é feita pela loja.
• É proibido copiar fotos e textos do site sem autorização.
• Pagamentos online são processados pelo Mercado Pago; a loja não recebe nem guarda dados do seu cartão.""",
    "politica-de-privacidade": """A {loja} trata seus dados pessoais conforme a Lei Geral de Proteção de Dados (LGPD, Lei 13.709/2018).

QUAIS DADOS: nome, telefone/WhatsApp, e-mail, CPF e endereço (para o contrato), medidas (para os ajustes) e as
mensagens que você envia pelo site.

PARA QUE: atender seu pedido, agendar provas, fazer o contrato, enviar avisos do seu pedido e, só se você pedir,
novidades da loja.

COMO PROTEGEMOS: os dados ficam cifrados no nosso sistema, com acesso só da equipe que precisa deles. Não vendemos
nem compartilhamos seus dados. Pagamentos online são feitos pelo Mercado Pago: a loja não recebe nem guarda
dados de cartão de crédito ou débito, Pix ou boleto.

CONTA NO SITE: se você criar uma conta, guardamos seu nome, WhatsApp, e-mail e a senha (embaralhada, ninguém
consegue ler). Você pode pedir para apagar a conta em Minha conta.

POR QUANTO TEMPO: enquanto houver relação com a loja e pelo prazo que a lei exige para documentos fiscais e contratos.

SEUS DIREITOS: você pode pedir para ver, corrigir ou apagar seus dados, e parar de receber mensagens a qualquer
momento (responda SAIR no WhatsApp). Fale com a gente pelos contatos abaixo.""",
}

PADRAO.update({
    "como-funciona": """1. ESCOLHA
Veja as peças por ocasião, tamanho e cor, e confira no calendário da peça se ela está livre na data da sua festa.

2. RESERVE
Reserve pelo site (entrada pelo Pix ou cartão) ou na loja. O contrato é assinado na tela, e você recebe tudo pelo WhatsApp.

3. PROVE E AJUSTE
Venha na prova com hora marcada. Nossa costureira faz os ajustes para ficar perfeito em você.

4. RETIRE E BRILHE
Retire a peça pronta, passada e embalada (ou receba em casa). Aproveite a festa!

5. DEVOLVA
Devolva na data combinada, sem lavar: a limpeza é por nossa conta.""",
    "entrega-e-devolucao": """RETIRADA NA LOJA
Sem custo, no horário da loja, na data combinada no pedido.

ENTREGA NA CIDADE E REGIÃO
Entregamos por motoboy ou aplicativo nas cidades atendidas pela {loja}. O valor aparece no carrinho antes de pagar.

ENVIO PARA OUTRAS CIDADES
Peças à venda podem ser enviadas pelos Correios ou transportadora (frete calculado pelo CEP no carrinho).
Aluguel é só com retirada ou entrega local.

DEVOLUÇÃO DO ALUGUEL
Até a data de devolução do contrato, no horário da loja (ou retirada combinada). Não lave nem passe a peça.""",
    "perguntas-frequentes": """POSSO PROVAR ANTES DE ALUGAR?
Sim! Agende sua prova pelo site ou WhatsApp.

A PEÇA É AJUSTADA?
Sim, dentro do que a peça permite, sem custo extra no aluguel (confira as condições na loja).

COMO PAGO?
Pix, cartão de débito ou crédito (em até 12x) e dinheiro na loja. Pelo site, Pix e cartão pelo Mercado Pago.
A loja não guarda dados do seu cartão.

E SE EU PRECISAR CANCELAR?
As regras de cancelamento estão no contrato e em "Trocas, devoluções e aluguel".

PRECISO LAVAR A PEÇA?
Não. A limpeza é por nossa conta.""",
    "como-provar": """1. Agende pelo site em "Agendar prova" ou pelo WhatsApp.
2. Venha com o sapato e a roupa íntima que vai usar na festa: o ajuste fica certinho.
3. A prova leva de 30 a 60 minutos. Traga quem você quiser para ajudar a escolher.
4. Depois dos ajustes, marcamos a 2ª prova para conferir tudo.""",
})


# --------------------------------------------------------------------------- #
# Leitura para o site
# --------------------------------------------------------------------------- #
def link_mapa(u: dict) -> str:
    if u.get("mapa_link"):
        return u["mapa_link"]
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{u['endereco']} {u.get('cidade') or ''}".strip())


def embed_mapa(u: dict) -> str:
    return "https://www.google.com/maps?output=embed&q=" + quote_plus(f"{u['endereco']} {u.get('cidade') or ''}".strip())


def unidades(cur, todas: bool = False) -> list[dict]:
    cur.execute(sql.SQL("""SELECT id::text, nome, endereco, cidade, telefone, whatsapp, horario, mapa_link, principal, ativa, ordem
                      FROM unidades {} ORDER BY ativa DESC, principal DESC, ordem, nome""")
                .format(sql.SQL("" if todas else "WHERE ativa")))
    lista = cur.fetchall()
    for u in lista:
        u["mapa"], u["embed"] = link_mapa(u), embed_mapa(u)
    return lista


def texto_pagina(cur, slug: str, loja: str) -> str:
    cur.execute("SELECT texto FROM paginas WHERE slug = %s ORDER BY definido_em DESC LIMIT 1", (slug,))
    linha = cur.fetchone()
    return linha["texto"] if linha else PADRAO[slug].replace("{loja}", loja)


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
def _tela(request, usuario, ok="", erro="", status=200, v=None):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT nome, sobre_curto, email_contato, formas_site, bandeiras_site, parcelas_no_site, novidades_ativa
                         FROM lojas WHERE id = ctx_loja()""")
        loja = cur.fetchone()
        lista = unidades(cur, todas=True)
        cur.execute("SELECT count(*) FILTER (WHERE saiu_em IS NULL) AS n FROM inscritos")
        inscritos = cur.fetchone()["n"]
        cur.execute("""SELECT DISTINCT ON (slug) slug, to_char(definido_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY') AS quando
                         FROM paginas ORDER BY slug, definido_em DESC""")
        editadas = {r["slug"]: r["quando"] for r in cur.fetchall()}
    return tela(request, "config_rodape.html", usuario, status=status, loja=loja, unidades=lista, v=v or {},
                inscritos=inscritos, editadas=editadas, PAGINAS=PAGINAS, FORMAS=FORMAS, BANDEIRAS=BANDEIRAS,
                ok=ok or request.query_params.get("ok", ""), erro=erro)


def _volta(recado: str, ancora: str = ""):
    return RedirectResponse(f"/config/rodape?ok={quote(recado)}{ancora}", status_code=303)


@router.get("/config/rodape")
def ver(request: Request, usuario: dict = Depends(requer("site.editar"))):
    return _tela(request, usuario)


@router.post("/config/rodape")
async def salvar_rodape(request: Request, usuario: dict = Depends(requer("site.editar"))):
    form = await request.form(max_fields=60)
    checar_csrf(request, str(form.get("csrf") or ""))
    formas = [f for f in form.getlist("formas") if f in FORMAS]
    bandeiras = [b for b in form.getlist("bandeiras") if b in BANDEIRAS]
    email = str(form.get("email_contato") or "").strip().lower()[:120]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _tela(request, usuario, erro="E-mail de contato inválido.", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE lojas SET sobre_curto = %s, email_contato = %s, formas_site = %s, bandeiras_site = %s,
                              parcelas_no_site = %s, novidades_ativa = %s WHERE id = ctx_loja()""",
                    (str(form.get("sobre_curto") or "").strip()[:400] or None, email or None, formas, bandeiras,
                     form.get("parcelas_no_site") == "1", form.get("novidades_ativa") == "1"))
        auditar(cur, request, "rodape_alterado", usuario["id"], "loja")
    return _volta("Rodapé salvo.")


def _ler_unidade(form: dict) -> tuple[dict | None, str | None]:
    d = {k: (form.get(k) or "").strip() for k in ("nome", "endereco", "cidade", "telefone", "whatsapp", "horario", "mapa_link")}
    for k in ("telefone", "whatsapp"):
        d[k] = re.sub(r"\D", "", d[k])
        if d[k].startswith("55") and len(d[k]) in (12, 13):
            d[k] = d[k][2:]
        if d[k] and len(d[k]) not in (10, 11):
            return None, "Telefone e WhatsApp com DDD, ex.: (19) 3272-3606."
    if len(d["nome"]) < 2 or len(d["endereco"]) < 5:
        return None, "Escreva o nome da loja (ex.: Loja Centro) e o endereço completo."
    if d["mapa_link"] and not re.match(r"^https://(www\.)?(google\.[a-z.]+/maps|maps\.google\.[a-z.]+|maps\.app\.goo\.gl|goo\.gl/maps)/",
                                       d["mapa_link"]):
        return None, "O link do mapa precisa ser do Google Maps (Compartilhar > Copiar link)."
    return {k: (v or None) for k, v in d.items()}, None


@router.post("/config/unidades")
def nova_unidade(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_unidade(form)
    if erro:
        return _tela(request, usuario, erro=erro, status=400, v=form)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("SELECT count(*) AS n FROM unidades WHERE ativa AND principal")
            principal = cur.fetchone()["n"] == 0
            cur.execute("""INSERT INTO unidades (loja_id, nome, endereco, cidade, telefone, whatsapp, horario, mapa_link, principal)
                           VALUES (ctx_loja(), %(nome)s, %(endereco)s, %(cidade)s, %(telefone)s, %(whatsapp)s, %(horario)s,
                                   %(mapa_link)s, %(principal)s)""", {**d, "principal": principal})
            auditar(cur, request, "unidade_criada", usuario["id"], "loja", None, {"nome": d["nome"]})
    except psycopg.Error as e:
        return _tela(request, usuario, erro=mensagem_do_banco(e), status=400, v=form)
    return _volta("Loja/endereço cadastrado. Já aparece no rodapé do site.", "#unidades")


@router.post("/config/unidades/{uid}")
def mexer_unidade(request: Request, uid: UUID, usuario: dict = Depends(requer("site.editar")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    acao = form.get("acao")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        if acao == "salvar":
            d, erro = _ler_unidade(form)
            if erro:
                return _tela(request, usuario, erro=erro, status=400)
            cur.execute("""UPDATE unidades SET nome=%(nome)s, endereco=%(endereco)s, cidade=%(cidade)s, telefone=%(telefone)s,
                                  whatsapp=%(whatsapp)s, horario=%(horario)s, mapa_link=%(mapa_link)s WHERE id = %(id)s""",
                        {**d, "id": str(uid)})
        elif acao == "principal":
            cur.execute("UPDATE unidades SET principal = false WHERE principal")
            cur.execute("UPDATE unidades SET principal = true, ativa = true WHERE id = %s", (str(uid),))
        elif acao in ("desativar", "ativar"):
            cur.execute("UPDATE unidades SET ativa = %s, principal = principal AND %s WHERE id = %s",
                        (acao == "ativar", acao == "ativar", str(uid)))
        else:
            return _volta("Nada mudou.")
        auditar(cur, request, f"unidade_{acao}", usuario["id"], "unidade", str(uid))
    return _volta("Endereços atualizados.", "#unidades")


@router.get("/config/paginas/{slug}")
def editar_pagina(request: Request, slug: str, usuario: dict = Depends(requer("site.editar"))):
    if slug not in PAGINAS:
        return tela(request, "erro.html", usuario, status=404, titulo="Página não encontrada")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
        texto = texto_pagina(cur, slug, cur.fetchone()["nome"])
    return tela(request, "config_pagina.html", usuario, slug=slug, titulo_pagina=PAGINAS[slug], texto=texto,
                ok=request.query_params.get("ok", ""))


@router.post("/config/paginas/{slug}")
def salvar_pagina(request: Request, slug: str, usuario: dict = Depends(requer("site.editar")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    if slug not in PAGINAS:
        return tela(request, "erro.html", usuario, status=404, titulo="Página não encontrada")
    texto = (form.get("texto") or "").strip().replace("\r\n", "\n")[:20000]
    if form.get("padrao") == "1":
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
            texto = PADRAO[slug].replace("{loja}", cur.fetchone()["nome"])
    if len(texto) < 20:
        return tela(request, "config_pagina.html", usuario, slug=slug, titulo_pagina=PAGINAS[slug], texto=texto,
                    erro="Escreva o texto da página (pelo menos algumas frases).", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("INSERT INTO paginas (loja_id, slug, texto, definido_por) VALUES (ctx_loja(), %s, %s, %s)",
                    (slug, texto, usuario["id"]))
        auditar(cur, request, "pagina_alterada", usuario["id"], "pagina", None, {"pagina": slug})
    return RedirectResponse(f"/config/paginas/{slug}?ok={quote('Página salva. Já está no site.')}", status_code=303)


# --------------------------------------------------------------------------- #
# Público
# --------------------------------------------------------------------------- #
@router.get("/site/pagina/{slug}")
def pagina(request: Request, slug: str):
    from .site import _loja, _pagina
    with db.transacao() as cur:
        if slug not in PAGINAS:
            return _pagina(request, "site/nao_encontrada.html", cur)
        texto = texto_pagina(cur, slug, _loja(cur)["nome"])
        return _pagina(request, "site/pagina.html", cur, titulo_pagina=PAGINAS[slug], texto=texto)


@router.post("/site/novidades")
def novidades(request: Request, form: dict = Depends(formulario)):
    try:
        checar_csrf(request, form.get("csrf"))
    except CsrfInvalido:
        return JSONResponse({"erro": "Recarregue a página e tente de novo."}, status_code=400)
    if form.get("site"):                                        # campo-isca de robô
        return JSONResponse({"ok": True, "texto": "Pronto! Você vai receber nossas novidades."})
    nome = (form.get("nome") or "").strip()[:80]
    email = (form.get("email") or "").strip().lower()[:120]
    fone = cripto.so_digitos(form.get("telefone"))[:13]
    if len(nome) < 2:
        return JSONResponse({"erro": "Escreva seu nome."}, status_code=400)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return JSONResponse({"erro": "Confira o e-mail."}, status_code=400)
    if fone and not cripto.indice_telefone(fone):
        return JSONResponse({"erro": "WhatsApp com DDD, ex.: (19) 99999-9999."}, status_code=400)
    if not email and not fone:
        return JSONResponse({"erro": "Deixe seu e-mail ou WhatsApp."}, status_code=400)
    if form.get("lgpd") != "1":
        return JSONResponse({"erro": "Marque que aceita receber as novidades."}, status_code=400)
    origem = hashlib.sha256(f"{ip(request) or '-'}|{config.SESSION_SECRET}".encode()).hexdigest()[:32]
    with db.transacao() as cur:
        cur.execute("SELECT novidades_ativa FROM lojas WHERE id = ctx_loja()")
        if not cur.fetchone()["novidades_ativa"]:
            return JSONResponse({"erro": "Cadastro de novidades fechado no momento."}, status_code=400)
        cur.execute("SELECT count(*) AS n FROM inscritos WHERE origem_hash = %s AND consentimento_em > now() - interval '1 hour'",
                    (origem,))
        if cur.fetchone()["n"] >= 5:
            return JSONResponse({"erro": "Muitos cadastros agora. Tente mais tarde."}, status_code=429)
        idx_mail = cripto.indice(email, "email") if email else None
        idx_fone = cripto.indice_telefone(fone) if fone else None
        cur.execute("""SELECT id FROM inscritos WHERE (email_idx = %s OR telefone_idx = %s)""", (idx_mail or "-", idx_fone or "-"))
        existente = cur.fetchone()
        if existente:
            cur.execute("UPDATE inscritos SET saiu_em = NULL, consentimento_em = now() WHERE id = %s", (existente["id"],))
        else:
            cur.execute("""INSERT INTO inscritos (loja_id, nome_c, telefone_c, telefone_idx, email_c, email_idx, origem_hash)
                           VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s)""",
                        (cripto.cifrar(nome, "inscritos.nome"), cripto.cifrar(fone or None, "inscritos.telefone"), idx_fone,
                         cripto.cifrar(email or None, "inscritos.email"), idx_mail, origem))
    return JSONResponse({"ok": True, "texto": "Pronto! Você vai receber nossas novidades. Para sair, é só pedir."})
